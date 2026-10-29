"""Read-only WeCom snapshotter.

The live WXWork files are never opened for writing. Each refresh copies a DB
and its WAL into a temporary directory, decrypts the copy, applies the latest
valid committed WAL prefix, checks SQLite integrity, and atomically replaces
the local snapshot.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import struct
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENDOR = ROOT / "third_party" / "wecom-decrypt"
sys.path.insert(0, str(VENDOR))
from wxwork_crypto import (  # noqa: E402
    decrypt_wxsqlite3_aes128_page,
    decrypt_wxwork_database,
    is_plain_sqlite_page,
)

PAGE = 4096
WAL_HEADER = 32
FRAME_HEADER = 24
WAL_MAGIC_LE = 0x377F0682
WAL_MAGIC_BE = 0x377F0683
WAL_VERSION = 3007000
DEFAULT_NAMES = ("message.db", "session.db", "message_lookup.db", "user.db")
LOG = logging.getLogger(__name__)


class SnapshotError(RuntimeError):
    """Base error for a snapshot that cannot be produced safely."""


class SourceChangedError(SnapshotError):
    """The source DB or WAL changed while its copy was being made."""


class WalFormatError(SnapshotError):
    """The WAL header is malformed or uses an unsupported format."""


class UnsupportedPageSize(SnapshotError):
    """The decryptor only supports 4096-byte database pages."""


@dataclass(frozen=True)
class WalHeader:
    salt1: int
    salt2: int
    checksum1: int
    checksum2: int
    byteorder: str


@dataclass
class WalApplyResult:
    applied: int
    skipped: int
    committed_frames: int = 0
    commit_dbsize: int = 0
    warnings: list[str] = field(default_factory=list)


def load_settings(config: Path) -> tuple[Path, Path, dict]:
    cfg = json.loads(config.read_text(encoding="utf-8"))
    base = config.resolve().parent

    def resolve(value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else base / path

    db_dir = resolve(cfg["wxwork_db_dir"])
    out_dir = resolve(cfg["wxwork_decrypted_dir"])
    keys_path = resolve(cfg["wxwork_keys_file"])
    keys = json.loads(keys_path.read_text(encoding="utf-8"))
    return db_dir, out_dir, keys


def key_for(keys: dict, rel: str) -> bytes | None:
    entry = keys.get(rel) or keys.get(rel.replace("\\", "/"))
    if not entry:
        return None
    return bytes.fromhex(entry["enc_key"])


def _file_fingerprint(path: Path) -> tuple[int, int, int, int, int, int] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return (
        stat.st_dev,
        stat.st_ino,
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
        stat.st_mode,
    )


def copy_stable_pair(src: Path, copied_db: Path, copied_wal: Path) -> dict:
    """Copy a live DB/WAL pair without ever opening either source for writing.

    Both files are fingerprinted before the first copy and after the last copy.
    This also detects a WAL appearing or disappearing during the operation.
    """
    src = Path(src)
    copied_db = Path(copied_db)
    copied_wal = Path(copied_wal)
    wal_src = Path(str(src) + "-wal")
    before_db = _file_fingerprint(src)
    if before_db is None or not src.is_file():
        raise FileNotFoundError(src)
    before_wal = _file_fingerprint(wal_src)
    copied_wal.unlink(missing_ok=True)
    try:
        shutil.copy2(src, copied_db)
        if before_wal is not None:
            shutil.copy2(wal_src, copied_wal)
    except FileNotFoundError as exc:
        raise SourceChangedError(f"source DB or WAL changed during copy: {exc}") from exc

    after_db = _file_fingerprint(src)
    after_wal = _file_fingerprint(wal_src)
    changed = []
    if before_db != after_db:
        changed.append("DB")
    if before_wal != after_wal:
        changed.append("WAL")
    if changed:
        raise SourceChangedError(
            f"source {' and '.join(changed)} changed during copy; snapshot retry required"
        )
    return {
        "db_bytes": copied_db.stat().st_size,
        "wal_copied": before_wal is not None,
        "wal_bytes": copied_wal.stat().st_size if copied_wal.exists() else 0,
    }


def validate_database_page_size(header: bytes) -> int:
    """Read the SQLite page-size field, also preserved by wxSQLite3 AES."""
    if len(header) < 18:
        raise SnapshotError("database header is shorter than 18 bytes")
    page_size = struct.unpack(">H", header[16:18])[0]
    if page_size == 1:
        page_size = 65536
    if page_size != PAGE:
        raise UnsupportedPageSize(
            f"unsupported database page size {page_size}; expected {PAGE}"
        )
    return page_size


def _wal_checksum(
    data: bytes, state: tuple[int, int] = (0, 0), byteorder: str = "<"
) -> tuple[int, int]:
    """SQLite's cumulative WAL checksum over complete pairs of uint32 values."""
    if len(data) % 8:
        raise ValueError("WAL checksum input length must be a multiple of 8")
    s1, s2 = state
    for x, y in struct.iter_unpack(f"{byteorder}II", data):
        s1 = (s1 + x + s2) & 0xFFFFFFFF
        s2 = (s2 + y + s1) & 0xFFFFFFFF
    return s1, s2


def validate_wal_header(wal_path: Path) -> WalHeader | None:
    """Validate WAL magic, version, page size, and header checksum."""
    wal_path = Path(wal_path)
    if not wal_path.exists() or wal_path.stat().st_size == 0:
        return None
    with wal_path.open("rb") as wal:
        header = wal.read(WAL_HEADER)
    if len(header) != WAL_HEADER:
        raise WalFormatError(
            f"incomplete WAL header: {len(header)} of {WAL_HEADER} bytes"
        )
    magic, version, page_size = struct.unpack(">III", header[:12])
    if magic == WAL_MAGIC_LE:
        byteorder = "<"
    elif magic == WAL_MAGIC_BE:
        byteorder = ">"
    else:
        raise WalFormatError(f"invalid WAL magic 0x{magic:08x}")
    if page_size != PAGE:
        raise UnsupportedPageSize(
            f"unsupported WAL page size {page_size}; expected {PAGE}"
        )
    if version != WAL_VERSION:
        raise WalFormatError(
            f"unsupported WAL format version {version}; expected {WAL_VERSION}"
        )
    salt1, salt2, expected1, expected2 = struct.unpack(">IIII", header[16:32])
    actual1, actual2 = _wal_checksum(header[:24], byteorder=byteorder)
    if (actual1, actual2) != (expected1, expected2):
        raise WalFormatError(
            "WAL header checksum mismatch: "
            f"expected {(expected1, expected2)}, calculated {(actual1, actual2)}"
        )
    return WalHeader(salt1, salt2, expected1, expected2, byteorder)


def apply_committed_wal(
    wal_path: Path,
    plain_path: Path,
    key: bytes,
    header: WalHeader | None = None,
) -> WalApplyResult:
    """Apply the valid frame prefix through its latest commit, then truncate.

    A salt/checksum mismatch invalidates that frame and every later frame. Valid
    uncommitted tail frames are never applied. The checksum covers encrypted
    page bytes, exactly as stored in the WAL, before page decryption.
    """
    wal_path = Path(wal_path)
    plain_path = Path(plain_path)
    if header is None:
        header = validate_wal_header(wal_path)
    if header is None:
        return WalApplyResult(0, 0)

    frame_size = FRAME_HEADER + PAGE
    payload_size = wal_path.stat().st_size - WAL_HEADER
    total_frames, partial_bytes = divmod(payload_size, frame_size)
    warnings: list[str] = []
    checksum = (header.checksum1, header.checksum2)
    valid_frames = 0
    last_commit = 0
    commit_dbsize = 0

    with wal_path.open("rb") as wal:
        wal.seek(WAL_HEADER)
        for index in range(1, total_frames + 1):
            frame_header = wal.read(FRAME_HEADER)
            page = wal.read(PAGE)
            pgno, dbsize, salt1, salt2 = struct.unpack(">IIII", frame_header[:16])
            if pgno == 0 or pgno == 0xFFFFFFFF:
                warnings.append(f"WAL frame {index} has invalid page number {pgno}; tail ignored")
                break
            if (salt1, salt2) != (header.salt1, header.salt2):
                warnings.append(
                    f"WAL frame {index} has stale salt {(salt1, salt2)}; tail ignored"
                )
                break
            checksum = _wal_checksum(frame_header[:8], checksum, header.byteorder)
            checksum = _wal_checksum(page, checksum, header.byteorder)
            expected = struct.unpack(">II", frame_header[16:24])
            if checksum != expected:
                warnings.append(
                    f"WAL frame {index} checksum mismatch: expected {expected}, "
                    f"calculated {checksum}; tail ignored"
                )
                break
            valid_frames = index
            if dbsize:
                last_commit = index
                commit_dbsize = dbsize

    if partial_bytes:
        warnings.append(f"ignored incomplete WAL tail of {partial_bytes} bytes")
    if valid_frames > last_commit:
        warnings.append(
            f"ignored {valid_frames - last_commit} uncommitted WAL tail frame(s)"
        )
    if not last_commit:
        if total_frames or partial_bytes:
            warnings.append("WAL has no valid committed transaction; base DB retained")
        return WalApplyResult(0, total_frames, warnings=warnings)

    applied = 0
    with wal_path.open("rb") as wal, plain_path.open("r+b") as out:
        wal.seek(WAL_HEADER)
        for _index in range(last_commit):
            frame_header = wal.read(FRAME_HEADER)
            page = wal.read(PAGE)
            pgno = struct.unpack(">I", frame_header[:4])[0]
            decoded = decrypt_wxsqlite3_aes128_page(key, page, pgno)
            out.seek((pgno - 1) * PAGE)
            out.write(decoded)
            applied += 1
        out.truncate(commit_dbsize * PAGE)
    warnings.append(
        f"applied WAL through frame {last_commit}; database size set to "
        f"{commit_dbsize} page(s)"
    )
    return WalApplyResult(
        applied=applied,
        skipped=total_frames - applied,
        committed_frames=last_commit,
        commit_dbsize=commit_dbsize,
        warnings=warnings,
    )


def apply_wal(wal_path: Path, plain_path: Path, key: bytes) -> tuple[int, int]:
    """Compatibility tuple API; real WAL files always use committed semantics."""
    wal_path = Path(wal_path)
    magic = b""
    if wal_path.exists():
        with wal_path.open("rb") as wal:
            magic = wal.read(4)
    if magic in (struct.pack(">I", WAL_MAGIC_LE), struct.pack(">I", WAL_MAGIC_BE)):
        result = apply_committed_wal(wal_path, plain_path, key)
        return result.applied, result.skipped
    return _apply_legacy_wal(wal_path, plain_path, key)


def _apply_legacy_wal(wal_path: Path, plain_path: Path, key: bytes) -> tuple[int, int]:
    """Patch legacy header-less synthetic streams used by old diagnostics."""
    if not wal_path.exists() or wal_path.stat().st_size <= WAL_HEADER:
        return 0, 0
    patched = skipped = 0
    with wal_path.open("rb") as wf, plain_path.open("r+b") as out:
        header = wf.read(WAL_HEADER)
        if len(header) != WAL_HEADER:
            return 0, 0
        salt1, salt2 = struct.unpack(">II", header[16:24])
        while True:
            fh = wf.read(FRAME_HEADER)
            if len(fh) < FRAME_HEADER:
                break
            page = wf.read(PAGE)
            if len(page) < PAGE:
                break
            pgno = struct.unpack(">I", fh[:4])[0]
            frame_salts = struct.unpack(">II", fh[8:16])
            if not pgno or pgno > 1000000 or frame_salts != (salt1, salt2):
                skipped += 1
                continue
            decoded = decrypt_wxsqlite3_aes128_page(key, page, pgno)
            out.seek((pgno - 1) * PAGE)
            out.write(decoded)
            patched += 1
    return patched, skipped


def open_readonly(path: Path) -> sqlite3.Connection:
    """Open a standalone snapshot without creating or consulting WAL sidecars."""
    uri = Path(path).resolve().as_uri() + "?mode=ro&immutable=1"
    return sqlite3.connect(uri, uri=True)


def check_db(path: Path) -> None:
    con = open_readonly(path)
    try:
        rows = con.execute("PRAGMA integrity_check").fetchall()
        if not rows or any(row[0] != "ok" for row in rows):
            result = "; ".join(str(row[0]) for row in rows) or "no result"
            raise SnapshotError(f"integrity_check={result}")
    finally:
        con.close()


def checkpoint_copied_db(path: Path) -> dict:
    """Checkpoint a disposable plain SQLite copy, never the live source."""
    con = sqlite3.connect(str(path))
    try:
        con.execute("PRAGMA busy_timeout=5000")
        row = con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if row is None:
            raise SnapshotError("wal_checkpoint returned no result")
        busy, log_frames, checkpointed = (int(value) for value in row)
        if busy:
            raise SnapshotError(
                f"wal_checkpoint busy: log={log_frames}, checkpointed={checkpointed}"
            )
        return {
            "busy": busy,
            "log_frames": max(0, log_frames),
            "checkpointed_frames": max(0, checkpointed),
        }
    finally:
        con.close()


def replace_snapshot_file(plain: Path, destination: Path) -> None:
    """Atomically replace a snapshot and remove stale SQLite sidecars."""
    plain = Path(plain)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, staging_name = tempfile.mkstemp(prefix=".snapshot_", dir=destination.parent)
    os.close(fd)
    staging = Path(staging_name)
    replaced = False
    try:
        shutil.copyfile(plain, staging)
        os.replace(staging, destination)
        replaced = True
    finally:
        staging.unlink(missing_ok=True)
        if replaced:
            for suffix in ("-wal", "-shm"):
                Path(str(destination) + suffix).unlink(missing_ok=True)


def snapshot_one(
    rel: str, db_dir: Path, out_dir: Path, keys: dict, attempts: int = 3
) -> dict:
    src = db_dir / rel
    if not src.is_file():
        raise FileNotFoundError(src)
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    key = key_for(keys, rel)
    last_error: Exception | None = None
    retry_warnings: list[str] = []
    for attempt in range(1, attempts + 1):
        try:
            with tempfile.TemporaryDirectory(prefix="wecom_snapshot_") as td:
                work = Path(td)
                copied_db = work / "source.db"
                copied_wal = work / "source.db-wal"
                copy_stable_pair(src, copied_db, copied_wal)
                with copied_db.open("rb") as fh:
                    head = fh.read(PAGE)
                validate_database_page_size(head)
                wal_header = validate_wal_header(copied_wal)

                plain = work / "plain.db"
                warnings: list[str] = []
                commit_dbsize = 0
                if is_plain_sqlite_page(head):
                    shutil.copy2(copied_db, plain)
                    if copied_wal.exists():
                        shutil.copy2(copied_wal, Path(str(plain) + "-wal"))
                    checkpoint = checkpoint_copied_db(plain)
                    patched = checkpoint["checkpointed_frames"]
                    skipped = 0
                else:
                    if key is None:
                        raise SnapshotError("no verified key for encrypted DB")
                    decrypt_wxwork_database(str(copied_db), str(plain), key)
                    wal_result = apply_committed_wal(
                        copied_wal, plain, key, header=wal_header
                    )
                    patched = wal_result.applied
                    skipped = wal_result.skipped
                    commit_dbsize = wal_result.commit_dbsize
                    warnings.extend(wal_result.warnings)
                check_db(plain)
                destination = out_dir / rel
                replace_snapshot_file(plain, destination)
                all_warnings = retry_warnings + warnings
                for warning in all_warnings:
                    LOG.warning("%s: %s", rel, warning)
                return {
                    "name": rel,
                    "bytes": destination.stat().st_size,
                    "wal_frames_applied": patched,
                    "wal_frames_skipped": skipped,
                    "wal_commit_dbsize": commit_dbsize,
                    "attempts": attempt,
                    "warnings": all_warnings,
                    "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
                }
        except UnsupportedPageSize:
            raise
        except Exception as exc:  # retry a live snapshot if the source moved
            last_error = exc
            if attempt < attempts:
                delay = 0.15 * attempt
                warning = (
                    f"attempt {attempt}/{attempts} failed: {exc}; "
                    f"retrying in {delay:.2f}s"
                )
                retry_warnings.append(warning)
                LOG.warning("%s: %s", rel, warning)
                time.sleep(delay)
    raise SnapshotError(
        f"{rel}: snapshot failed after {attempts} attempt(s); "
        f"last error: {last_error}"
    ) from last_error


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, default=Path(os.environ.get("WECOM_CONFIG", str(VENDOR / "config.json"))))
    ap.add_argument("--all", action="store_true", help="snapshot every DB with a verified key")
    ap.add_argument("--names", nargs="+", default=list(DEFAULT_NAMES))
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    db_dir, out_dir, keys = load_settings(args.config)
    names = sorted(k for k in keys if not k.startswith("_")) if args.all else args.names
    results = []
    for rel in names:
        result = snapshot_one(rel, db_dir, out_dir, keys)
        results.append(result)
        print(f"OK {rel} bytes={result['bytes']} wal_applied={result['wal_frames_applied']} wal_skipped={result['wal_frames_skipped']}")
    state = out_dir / "snapshot_state.json"
    state.write_text(json.dumps({"updated_at": int(time.time()), "files": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"SNAPSHOT_STATE={state}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
