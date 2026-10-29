from __future__ import annotations

import shutil
import sqlite3
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from Crypto.Cipher import AES

from prepare_offline_db import prepare_offline_db, validate_messages_schema
from wecom_snapshot import (
    FRAME_HEADER,
    PAGE,
    WAL_HEADER,
    WAL_MAGIC_LE,
    WAL_VERSION,
    SourceChangedError,
    SnapshotError,
    UnsupportedPageSize,
    WalFormatError,
    apply_committed_wal,
    apply_wal,
    copy_stable_pair,
    open_readonly,
    snapshot_one,
    validate_wal_header,
)
from wxwork_crypto import derive_wxsqlite3_aes128_page_key, generate_initial_vector

KEY = bytes.fromhex("00112233445566778899aabbccddeeff")
SALTS = (111, 222)


def independent_checksum(
    data: bytes, state: tuple[int, int] = (0, 0), byteorder: str = "<"
) -> tuple[int, int]:
    """Test-local implementation of the SQLite WAL rolling checksum."""
    s1, s2 = state
    for offset in range(0, len(data), 8):
        x, y = struct.unpack(f"{byteorder}II", data[offset : offset + 8])
        s1 = (s1 + x + s2) & 0xFFFFFFFF
        s2 = (s2 + y + s1) & 0xFFFFFFFF
    return s1, s2


def encrypt_page(page: bytes, pgno: int) -> bytes:
    return AES.new(
        derive_wxsqlite3_aes128_page_key(KEY, pgno),
        AES.MODE_CBC,
        generate_initial_vector(pgno),
    ).encrypt(page)


def make_wal(
    frames: list[dict],
    *,
    page_size: int = PAGE,
    corrupt_header_checksum: bool = False,
) -> bytes:
    header = bytearray(
        struct.pack(">IIIIII", WAL_MAGIC_LE, WAL_VERSION, page_size, 0, *SALTS)
        + bytes(8)
    )
    checksum = independent_checksum(bytes(header[:24]))
    if corrupt_header_checksum:
        checksum = (checksum[0] ^ 1, checksum[1])
    header[24:32] = struct.pack(">II", *checksum)
    checksum = independent_checksum(bytes(header[:24]))

    output = bytearray(header)
    for spec in frames:
        pgno = spec["pgno"]
        dbsize = spec.get("dbsize", 0)
        salts = spec.get("salts", SALTS)
        plain_page = spec.get("page", bytes([pgno & 0xFF]) * PAGE)
        encrypted = encrypt_page(plain_page, pgno)
        first = struct.pack(">II", pgno, dbsize)
        checksum = independent_checksum(first, checksum)
        checksum = independent_checksum(encrypted, checksum)
        stored = (checksum[0] ^ 1, checksum[1]) if spec.get("bad_checksum") else checksum
        output.extend(first)
        output.extend(struct.pack(">II", *salts))
        output.extend(struct.pack(">II", *stored))
        output.extend(encrypted)
    return bytes(output)


class WalCommitBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.work = Path(self.temp.name)
        self.plain = self.work / "plain.db"
        self.wal = self.work / "source.db-wal"

    def tearDown(self):
        self.temp.cleanup()

    def write_wal(self, data: bytes):
        self.wal.write_bytes(data)
        return self.wal

    def test_latest_commit_dbsize_and_uncommitted_tail(self):
        original = [bytes([index + 1]) * PAGE for index in range(4)]
        self.plain.write_bytes(b"".join(original))
        frames = [
            {"pgno": 2, "page": b"A" * PAGE},
            {"pgno": 3, "dbsize": 2, "page": b"B" * PAGE},
            {"pgno": 2, "page": b"C" * PAGE},
            {"pgno": 4, "dbsize": 3, "page": b"D" * PAGE},
            {"pgno": 2, "page": b"E" * PAGE},
        ]
        self.write_wal(make_wal(frames) + b"partial-tail")
        result = apply_committed_wal(self.wal, self.plain, KEY)

        data = self.plain.read_bytes()
        self.assertEqual((result.applied, result.skipped), (4, 1))
        self.assertEqual((result.committed_frames, result.commit_dbsize), (4, 3))
        self.assertEqual(len(data), 3 * PAGE)
        self.assertEqual(data[:PAGE], original[0])
        self.assertEqual(data[PAGE : 2 * PAGE], b"C" * PAGE)
        self.assertEqual(data[2 * PAGE :], b"B" * PAGE)
        self.assertNotIn(b"E" * PAGE, data)
        self.assertTrue(any("uncommitted" in item for item in result.warnings))
        self.assertTrue(any("incomplete" in item for item in result.warnings))

    def test_stale_salt_discards_itself_and_all_later_frames(self):
        self.plain.write_bytes(bytes(2 * PAGE))
        frames = [
            {"pgno": 2, "dbsize": 2, "page": b"A" * PAGE},
            {"pgno": 2, "dbsize": 2, "page": b"B" * PAGE, "salts": (999, 999)},
            {"pgno": 2, "dbsize": 2, "page": b"C" * PAGE},
        ]
        self.write_wal(make_wal(frames))
        result = apply_committed_wal(self.wal, self.plain, KEY)
        self.assertEqual((result.applied, result.skipped), (1, 2))
        self.assertEqual(self.plain.read_bytes()[PAGE:], b"A" * PAGE)
        self.assertTrue(any("stale salt" in item for item in result.warnings))

    def test_bad_frame_checksum_restores_previous_commit_boundary(self):
        self.plain.write_bytes(bytes(2 * PAGE))
        frames = [
            {"pgno": 2, "dbsize": 2, "page": b"A" * PAGE},
            {"pgno": 2, "dbsize": 2, "page": b"B" * PAGE, "bad_checksum": True},
        ]
        self.write_wal(make_wal(frames))
        result = apply_committed_wal(self.wal, self.plain, KEY)
        self.assertEqual((result.applied, result.skipped), (1, 1))
        self.assertEqual(self.plain.read_bytes()[PAGE:], b"A" * PAGE)
        self.assertTrue(any("checksum mismatch" in item for item in result.warnings))

    def test_uncommitted_only_wal_leaves_base_untouched(self):
        base = b"Z" * (2 * PAGE)
        self.plain.write_bytes(base)
        self.write_wal(make_wal([{"pgno": 2, "page": b"A" * PAGE}]))
        result = apply_wal(self.wal, self.plain, KEY)
        self.assertEqual(result, (0, 1))
        self.assertEqual(self.plain.read_bytes(), base)

    def test_bad_header_checksum_and_non4096_page_fail_fast(self):
        self.plain.write_bytes(bytes(2 * PAGE))
        with self.subTest("header checksum"):
            self.write_wal(make_wal([], corrupt_header_checksum=True))
            with self.assertRaisesRegex(WalFormatError, "header checksum"):
                apply_committed_wal(self.wal, self.plain, KEY)
        with self.subTest("page size"):
            self.write_wal(make_wal([], page_size=8192))
            with self.assertRaisesRegex(UnsupportedPageSize, "8192"):
                apply_committed_wal(self.wal, self.plain, KEY)


class StableCopyAndSnapshotTests(unittest.TestCase):
    def test_db_and_wal_changes_are_both_detected(self):
        for changed_file in ("db", "wal"):
            with self.subTest(changed_file=changed_file), tempfile.TemporaryDirectory() as td:
                work = Path(td)
                source = work / "message.db"
                wal = work / "message.db-wal"
                source.write_bytes(b"db-before")
                wal.write_bytes(b"wal-before")
                original_copy = shutil.copy2

                def mutating_copy(src, dst, **kwargs):
                    result = original_copy(src, dst, **kwargs)
                    if changed_file == "db" and Path(src) == source:
                        source.write_bytes(b"db-after-longer")
                    if changed_file == "wal" and Path(src) == wal:
                        wal.write_bytes(b"wal-after-longer")
                    return result

                with mock.patch("wecom_snapshot.shutil.copy2", side_effect=mutating_copy):
                    with self.assertRaisesRegex(
                        SourceChangedError, changed_file.upper()
                    ):
                        copy_stable_pair(
                            source, work / "copy.db", work / "copy.db-wal"
                        )

    def test_wal_appearing_during_db_copy_is_detected(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            source = work / "message.db"
            source.write_bytes(b"db")
            original_copy = shutil.copy2

            def add_wal(src, dst, **kwargs):
                result = original_copy(src, dst, **kwargs)
                Path(str(source) + "-wal").write_bytes(b"new-wal")
                return result

            with mock.patch("wecom_snapshot.shutil.copy2", side_effect=add_wal):
                with self.assertRaisesRegex(SourceChangedError, "WAL"):
                    copy_stable_pair(source, work / "copy.db", work / "copy.db-wal")

    def test_non4096_snapshot_does_not_retry(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            source = work / "message.db"
            header = bytearray(PAGE)
            header[:16] = b"SQLite format 3\x00"
            header[16:18] = struct.pack(">H", 8192)
            source.write_bytes(header)
            with mock.patch("wecom_snapshot.time.sleep") as sleep:
                with self.assertRaisesRegex(UnsupportedPageSize, "8192"):
                    snapshot_one("message.db", work, work / "out", {}, attempts=3)
                sleep.assert_not_called()

    def test_retry_context_and_replacement_sidecar_cleanup(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            source = work / "message.db"
            con = sqlite3.connect(source)
            con.execute("CREATE TABLE sample(id INTEGER PRIMARY KEY, value TEXT)")
            con.execute("INSERT INTO sample(value) VALUES ('ok')")
            con.commit()
            con.close()
            out = work / "out"
            out.mkdir()
            destination = out / "message.db"
            destination.write_bytes(b"old")
            Path(str(destination) + "-wal").write_bytes(b"stale-wal")
            Path(str(destination) + "-shm").write_bytes(b"stale-shm")

            original_copy = copy_stable_pair
            calls = 0

            def fail_once(*args):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise SourceChangedError("DB and WAL changed during copy")
                return original_copy(*args)

            with (
                mock.patch("wecom_snapshot.copy_stable_pair", side_effect=fail_once),
                mock.patch("wecom_snapshot.time.sleep"),
                self.assertLogs("wecom_snapshot", level="WARNING") as logs,
            ):
                result = snapshot_one(
                    "message.db", work, out, {}, attempts=2
                )
            self.assertEqual(result["attempts"], 2)
            self.assertIn("attempt 1/2", result["warnings"][0])
            self.assertTrue(any("retrying" in line for line in logs.output))
            self.assertFalse(Path(str(destination) + "-wal").exists())
            self.assertFalse(Path(str(destination) + "-shm").exists())
            con = open_readonly(destination)
            try:
                self.assertEqual(
                    con.execute("SELECT value FROM sample").fetchone()[0], "ok"
                )
            finally:
                con.close()


class OfflinePreparationTests(unittest.TestCase):
    @staticmethod
    def create_wecom_db(path: Path) -> sqlite3.Connection:
        con = sqlite3.connect(path)
        con.execute("PRAGMA page_size=4096")
        con.execute("PRAGMA journal_mode=WAL")
        con.execute(
            """CREATE TABLE message_table(
                message_id INTEGER PRIMARY KEY,
                server_id TEXT,
                sequence INTEGER,
                sender_id TEXT,
                conversation_id TEXT,
                content_type INTEGER,
                send_time INTEGER,
                flag INTEGER,
                content BLOB,
                extra_content BLOB,
                local_extra_content BLOB
            )"""
        )
        con.execute(
            """INSERT INTO message_table(
                message_id,server_id,sequence,sender_id,conversation_id,
                content_type,send_time,flag,content,extra_content,local_extra_content
            ) VALUES(1,'server-1',1,'sender','room',2,123,0,?,?,?)""",
            (b"text", b"", b""),
        )
        con.commit()
        return con

    def test_copy_checkpoint_integrity_schema_and_original_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            source = work / "message.db"
            con = self.create_wecom_db(source)
            try:
                wal = Path(str(source) + "-wal")
                self.assertGreater(wal.stat().st_size, WAL_HEADER)
                # A real SQLite WAL independently validates our header checksum code.
                self.assertIsNotNone(validate_wal_header(wal))
                before_db = source.stat()
                before_wal = wal.stat()
                destination = work / "offline" / "message.db"
                destination.parent.mkdir()
                destination.write_bytes(b"old")
                Path(str(destination) + "-wal").write_bytes(b"stale")
                Path(str(destination) + "-shm").write_bytes(b"stale")

                result = prepare_offline_db(source, destination)
                self.assertEqual(result["integrity_check"], "ok")
                self.assertEqual(result["schema"]["kind"], "wecom")
                self.assertEqual(result["checkpoint"]["busy"], 0)
                self.assertGreater(destination.stat().st_size, source.stat().st_size)
                self.assertFalse(Path(str(destination) + "-wal").exists())
                self.assertFalse(Path(str(destination) + "-shm").exists())
                self.assertEqual(source.stat().st_size, before_db.st_size)
                self.assertEqual(source.stat().st_mtime_ns, before_db.st_mtime_ns)
                self.assertEqual(wal.stat().st_size, before_wal.st_size)
                self.assertEqual(wal.stat().st_mtime_ns, before_wal.st_mtime_ns)

                offline = open_readonly(destination)
                try:
                    self.assertEqual(
                        offline.execute(
                            "SELECT server_id,conversation_id FROM message_table"
                        ).fetchone(),
                        ("server-1", "room"),
                    )
                finally:
                    offline.close()
            finally:
                con.close()

    def test_local_messages_schema_is_supported(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            source = work / "store.sqlite"
            con = sqlite3.connect(source)
            con.execute(
                """CREATE TABLE messages(
                    msgid TEXT PRIMARY KEY,seq INTEGER,roomid TEXT,sender TEXT,
                    msgtime INTEGER,msgtype TEXT,content TEXT,raw TEXT,fetched_at INTEGER
                )"""
            )
            con.commit()
            con.close()
            result = prepare_offline_db(source, work / "offline.sqlite", attempts=1)
            self.assertEqual(result["schema"], {"kind": "local", "tables": ["messages"]})

    def test_missing_messages_columns_are_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            source = work / "message.db"
            con = sqlite3.connect(source)
            con.execute("CREATE TABLE message_table(message_id INTEGER PRIMARY KEY)")
            con.commit()
            con.close()
            with self.assertRaisesRegex(SnapshotError, "missing columns"):
                prepare_offline_db(source, work / "offline.db", attempts=1)
            self.assertFalse((work / "offline.db").exists())


if __name__ == "__main__":
    unittest.main()
