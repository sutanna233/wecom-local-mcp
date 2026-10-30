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
CACHE_VERSION = 1
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


def copy_stable_pair(
    src: Path, copied_db: Path, copied_wal: Path, copy_db: bool = True
) -> dict:
    """Copy a live DB/WAL pair without ever opening either source for writing.

    Both files are fingerprinted before the first copy and after the last copy.
    This also detects a WAL appearing or disappearing during the operation.
    When a decrypted baseline is reusable, ``copy_db=False`` copies only the
    WAL while still verifying that the main DB stayed unchanged.
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
    if not copy_db:
        copied_db.unlink(missing_ok=True)
    try:
        if copy_db:
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
        "db_bytes": copied_db.stat().st_size if copy_db else before_db[2],
        "db_copied": copy_db,
        "db_fingerprint": before_db,
        "wal_copied": before_wal is not None,
        "wal_bytes": copied_wal.stat().st_size if copied_wal.exists() else 0,
        "wal_fingerprint": before_wal,
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


def _cache_path(destination: Path) -> Path:
    return Path(str(destination) + ".snapshot-cache.json")


def _fingerprints_equal(left, right) -> bool:
    if left is None or right is None:
        return left is None and right is None
    return tuple(left) == tuple(right)


def _source_fingerprints(src: Path):
    db_fingerprint = _file_fingerprint(src)
    if db_fingerprint is None:
        raise FileNotFoundError(src)
    return db_fingerprint, _file_fingerprint(Path(str(src) + "-wal"))


def _key_sha256(key: bytes | None) -> str | None:
    return hashlib.sha256(key).hexdigest() if key is not None else None


def _load_cache(path: Path) -> dict | None:
    try:
        cache = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(cache, dict) or cache.get("version") != CACHE_VERSION:
        return None
    if (
        not isinstance(cache.get("source"), str)
        or not isinstance(cache.get("sha256"), str)
        or not isinstance(cache.get("encrypted"), bool)
        or not isinstance(cache.get("db_fingerprint"), list)
        or not isinstance(cache.get("destination_fingerprint"), list)
        or (
            cache.get("wal_fingerprint") is not None
            and not isinstance(cache.get("wal_fingerprint"), list)
        )
    ):
        return None
    try:
        if int(cache.get("wal_committed_bytes") or 0) < 0:
            return None
        if int(cache.get("wal_commit_dbsize") or 0) < 0:
            return None
    except (TypeError, ValueError):
        return None
    return cache


def _cache_identity_matches(cache: dict, src: Path, key: bytes | None) -> bool:
    return (
        cache.get("source") == str(src.resolve())
        and cache.get("key_sha256") == _key_sha256(key)
    )


def _destination_matches_cache(cache: dict, destination: Path) -> bool:
    return destination.is_file() and _fingerprints_equal(
        cache.get("destination_fingerprint"), _file_fingerprint(destination)
    )


def _cache_unchanged(
    cache: dict | None,
    src: Path,
    destination: Path,
    key: bytes | None,
    db_fingerprint,
    wal_fingerprint,
) -> bool:
    return bool(
        cache is not None
        and _cache_identity_matches(cache, src, key)
        and _fingerprints_equal(cache.get("db_fingerprint"), db_fingerprint)
        and _fingerprints_equal(cache.get("wal_fingerprint"), wal_fingerprint)
        and _destination_matches_cache(cache, destination)
    )


def _cache_reusable(
    cache: dict | None,
    src: Path,
    destination: Path,
    key: bytes | None,
    db_fingerprint,
) -> bool:
    return bool(
        cache is not None
        and cache.get("encrypted") is True
        and _cache_identity_matches(cache, src, key)
        and _fingerprints_equal(cache.get("db_fingerprint"), db_fingerprint)
        and _destination_matches_cache(cache, destination)
    )


def _sha256_file(path: Path, length: int | None = None) -> str:
    digest = hashlib.sha256()
    remaining = length
    with Path(path).open("rb") as source:
        while remaining is None or remaining > 0:
            chunk_size = 1024 * 1024 if remaining is None else min(1024 * 1024, remaining)
            chunk = source.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
            if remaining is not None:
                remaining -= len(chunk)
    if remaining:
        raise SnapshotError(f"file ended before requested {length} bytes: {path}")
    return digest.hexdigest()


def _wal_prefix_metadata(wal_path: Path, committed_frames: int) -> tuple[int, str | None]:
    if committed_frames <= 0:
        return 0, None
    prefix_bytes = WAL_HEADER + committed_frames * (FRAME_HEADER + PAGE)
    return prefix_bytes, _sha256_file(wal_path, prefix_bytes)


def _baseline_wal_compatible(cache: dict, copied_wal: Path) -> bool:
    """The destination must represent a committed prefix of the current WAL."""
    previous_bytes = int(cache.get("wal_committed_bytes") or 0)
    if previous_bytes == 0:
        # No WAL frames were present in the cached destination, so it is the
        # decrypted main-DB baseline and can accept any valid WAL generation.
        return True
    expected_hash = cache.get("wal_committed_sha256")
    if (
        not expected_hash
        or not copied_wal.is_file()
        or copied_wal.stat().st_size < previous_bytes
    ):
        return False
    return _sha256_file(copied_wal, previous_bytes) == expected_hash


def _write_cache(path: Path, cache: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, staging_name = tempfile.mkstemp(prefix=".snapshot_cache_", dir=path.parent)
    staging = Path(staging_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(cache, output, ensure_ascii=False, indent=2)
        os.replace(staging, path)
    finally:
        staging.unlink(missing_ok=True)


def _remove_destination_sidecars(destination: Path) -> None:
    for suffix in ("-wal", "-shm"):
        Path(str(destination) + suffix).unlink(missing_ok=True)


def _unchanged_result(
    rel: str,
    destination: Path,
    cache: dict,
    attempt: int,
    warnings: list[str],
) -> dict:
    return {
        "name": rel,
        "bytes": destination.stat().st_size,
        "wal_frames_applied": 0,
        "wal_frames_skipped": 0,
        "wal_commit_dbsize": int(cache.get("wal_commit_dbsize") or 0),
        "attempts": attempt,
        "warnings": list(warnings),
        "sha256": cache["sha256"],
        "skipped": True,
        "reused_baseline": False,
        "db_copied": False,
        "cache_status": "unchanged",
    }


def snapshot_one(
    rel: str, db_dir: Path, out_dir: Path, keys: dict, attempts: int = 3
) -> dict:
    src = db_dir / rel
    if not src.is_file():
        raise FileNotFoundError(src)
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    key = key_for(keys, rel)
    destination = out_dir / rel
    cache_path = _cache_path(destination)
    last_error: Exception | None = None
    retry_warnings: list[str] = []
    for attempt in range(1, attempts + 1):
        try:
            db_fingerprint, wal_fingerprint = _source_fingerprints(src)
            cache = _load_cache(cache_path)
            if _cache_unchanged(
                cache, src, destination, key, db_fingerprint, wal_fingerprint
            ) and _source_fingerprints(src) == (db_fingerprint, wal_fingerprint):
                _remove_destination_sidecars(destination)
                return _unchanged_result(
                    rel, destination, cache, attempt, retry_warnings
                )

            reuse = _cache_reusable(
                cache, src, destination, key, db_fingerprint
            )
            fell_back = False
            with tempfile.TemporaryDirectory(prefix="wecom_snapshot_") as td:
                work = Path(td)
                copied_db = work / "source.db"
                copied_wal = work / "source.db-wal"
                copied = copy_stable_pair(
                    src, copied_db, copied_wal, not reuse
                )
                plain = work / "plain.db"
                warnings: list[str] = []
                patched = skipped = commit_dbsize = committed_frames = 0
                encrypted = False

                if reuse and not _fingerprints_equal(
                    cache["db_fingerprint"], copied["db_fingerprint"]
                ):
                    warnings.append("source DB changed; decrypted baseline invalidated")
                    reuse = False
                    fell_back = True
                if reuse:
                    wal_header = validate_wal_header(copied_wal)
                    if not _baseline_wal_compatible(cache, copied_wal):
                        warnings.append(
                            "current WAL does not extend the cached committed prefix; "
                            "decrypted baseline invalidated"
                        )
                        reuse = False
                        fell_back = True
                if not reuse and not copied["db_copied"]:
                    copied = copy_stable_pair(src, copied_db, copied_wal)

                if reuse:
                    encrypted = True
                    try:
                        shutil.copy2(destination, plain)
                    except OSError as exc:
                        warnings.append(f"decrypted baseline unreadable: {exc}; full rebuild")
                        reuse = False
                        fell_back = True
                    else:
                        wal_result = apply_committed_wal(
                            copied_wal, plain, key, header=wal_header
                        )
                        patched = wal_result.applied
                        skipped = wal_result.skipped
                        commit_dbsize = wal_result.commit_dbsize
                        committed_frames = wal_result.committed_frames
                        warnings.extend(wal_result.warnings)
                        try:
                            check_db(plain)
                        except SnapshotError as exc:
                            warnings.append(
                                f"reused baseline failed integrity_check: {exc}; full rebuild"
                            )
                            reuse = False
                            fell_back = True

                if not reuse:
                    if not copied["db_copied"]:
                        copied = copy_stable_pair(src, copied_db, copied_wal)
                    with copied_db.open("rb") as fh:
                        head = fh.read(PAGE)
                    validate_database_page_size(head)
                    wal_header = validate_wal_header(copied_wal)
                    patched = skipped = commit_dbsize = committed_frames = 0
                    if is_plain_sqlite_page(head):
                        encrypted = False
                        shutil.copy2(copied_db, plain)
                        plain_wal = Path(str(plain) + "-wal")
                        plain_wal.unlink(missing_ok=True)
                        if copied_wal.exists():
                            shutil.copy2(copied_wal, plain_wal)
                        checkpoint = checkpoint_copied_db(plain)
                        patched = checkpoint["checkpointed_frames"]
                    else:
                        encrypted = True
                        if key is None:
                            raise SnapshotError("no verified key for encrypted DB")
                        decrypt_wxwork_database(str(copied_db), str(plain), key)
                        wal_result = apply_committed_wal(
                            copied_wal, plain, key, header=wal_header
                        )
                        patched = wal_result.applied
                        skipped = wal_result.skipped
                        commit_dbsize = wal_result.commit_dbsize
                        committed_frames = wal_result.committed_frames
                        warnings.extend(wal_result.warnings)
                    check_db(plain)

                if encrypted:
                    prefix_bytes, prefix_hash = _wal_prefix_metadata(
                        copied_wal, committed_frames
                    )
                else:
                    prefix_bytes, prefix_hash = 0, None
                replace_snapshot_file(plain, destination)
                snapshot_hash = _sha256_file(destination)
                new_cache = {
                    "version": CACHE_VERSION,
                    "source": str(src.resolve()),
                    "key_sha256": _key_sha256(key),
                    "db_fingerprint": copied["db_fingerprint"],
                    "wal_fingerprint": copied["wal_fingerprint"],
                    "destination_fingerprint": _file_fingerprint(destination),
                    "encrypted": encrypted,
                    "wal_committed_bytes": prefix_bytes,
                    "wal_committed_sha256": prefix_hash,
                    "wal_commit_dbsize": commit_dbsize,
                    "sha256": snapshot_hash,
                }
                try:
                    _write_cache(cache_path, new_cache)
                except OSError as exc:
                    warnings.append(f"snapshot cache write failed: {exc}")

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
                    "sha256": snapshot_hash,
                    "skipped": False,
                    "reused_baseline": reuse,
                    "db_copied": bool(copied["db_copied"]),
                    "cache_status": (
                        "wal_reused" if reuse else "full_fallback" if fell_back else "full"
                    ),
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
