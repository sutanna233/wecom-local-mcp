from __future__ import annotations

import shutil
import sqlite3
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from Crypto.Cipher import AES

from wecom_snapshot import (
    PAGE,
    WAL_MAGIC_LE,
    WAL_VERSION,
    copy_stable_pair,
    decrypt_wxwork_database,
    snapshot_one,
)
from wxwork_crypto import derive_wxsqlite3_aes128_page_key, generate_initial_vector

KEY = bytes.fromhex("00112233445566778899aabbccddeeff")
SALTS = (111, 222)


def checksum(data: bytes, state=(0, 0)):
    s1, s2 = state
    for offset in range(0, len(data), 8):
        x, y = struct.unpack("<II", data[offset : offset + 8])
        s1 = (s1 + x + s2) & 0xFFFFFFFF
        s2 = (s2 + y + s1) & 0xFFFFFFFF
    return s1, s2


def encrypt_page(page: bytes, pgno: int) -> bytes:
    cipher = AES.new(
        derive_wxsqlite3_aes128_page_key(KEY, pgno),
        AES.MODE_CBC,
        generate_initial_vector(pgno),
    )
    if pgno != 1:
        return cipher.encrypt(page)
    # Inverse of wxwork_crypto's page-1 mode: bytes 16..24 remain plaintext,
    # while bytes 8..16 carry the first ciphertext block fragment.
    encrypted_tail = cipher.encrypt(page[16:])
    output = bytearray(PAGE)
    output[:8] = b"WXWORKDB"
    output[8:16] = encrypted_tail[:8]
    output[16:] = encrypted_tail
    output[16:24] = page[16:24]
    return bytes(output)


def encrypt_db(plain: Path, encrypted: Path):
    data = plain.read_bytes()
    if len(data) % PAGE:
        raise AssertionError("test DB is not page aligned")
    encrypted.write_bytes(
        b"".join(
            encrypt_page(data[offset : offset + PAGE], offset // PAGE + 1)
            for offset in range(0, len(data), PAGE)
        )
    )


def create_db(path: Path, values: list[str]):
    con = sqlite3.connect(path)
    con.execute("PRAGMA page_size=4096")
    con.execute("PRAGMA journal_mode=DELETE")
    con.execute("CREATE TABLE items(id INTEGER PRIMARY KEY, value TEXT)")
    con.executemany("INSERT INTO items(value) VALUES (?)", [(value,) for value in values])
    con.commit()
    con.close()


def add_value(path: Path, value: str):
    con = sqlite3.connect(path)
    con.execute("INSERT INTO items(value) VALUES (?)", (value,))
    con.commit()
    con.close()


def pages(path: Path) -> list[bytes]:
    data = path.read_bytes()
    return [data[offset : offset + PAGE] for offset in range(0, len(data), PAGE)]


def make_wal(transactions: list[list[bytes]], salts=SALTS) -> bytes:
    header = bytearray(
        struct.pack(">IIIIII", WAL_MAGIC_LE, WAL_VERSION, PAGE, 0, *salts)
        + bytes(8)
    )
    header[24:32] = struct.pack(">II", *checksum(bytes(header[:24])))
    state = checksum(bytes(header[:24]))
    output = bytearray(header)
    for transaction in transactions:
        for index, plain_page in enumerate(transaction, 1):
            dbsize = len(transaction) if index == len(transaction) else 0
            encrypted = encrypt_page(plain_page, index)
            first = struct.pack(">II", index, dbsize)
            state = checksum(first, state)
            state = checksum(encrypted, state)
            output.extend(first)
            output.extend(struct.pack(">II", *salts))
            output.extend(struct.pack(">II", *state))
            output.extend(encrypted)
    return bytes(output)


class SnapshotBaselineReuseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.work = Path(self.temp.name)
        self.db_dir = self.work / "source"
        self.out_dir = self.work / "out"
        self.db_dir.mkdir()
        self.base = self.work / "base.sqlite"
        create_db(self.base, ["base"])
        self.source = self.db_dir / "message.db"
        encrypt_db(self.base, self.source)
        self.keys = {"message.db": {"enc_key": KEY.hex()}}

    def tearDown(self):
        self.temp.cleanup()

    def snapshot(self):
        return snapshot_one(
            "message.db", self.db_dir, self.out_dir, self.keys, attempts=1
        )

    def target_with_wal_versions(self):
        target2 = self.work / "target2.sqlite"
        shutil.copyfile(self.base, target2)
        add_value(target2, "wal-1")
        target3 = self.work / "target3.sqlite"
        shutil.copyfile(target2, target3)
        add_value(target3, "wal-2")
        wal1 = make_wal([pages(target2)])
        wal2 = make_wal([pages(target2), pages(target3)])
        self.assertTrue(wal2.startswith(wal1))
        return target2, target3, wal1, wal2

    def test_unchanged_skip_and_wal_only_baseline_reuse(self):
        _target2, target3, wal1, wal2 = self.target_with_wal_versions()
        wal_path = Path(str(self.source) + "-wal")
        wal_path.write_bytes(wal1)
        with (
            mock.patch(
                "wecom_snapshot.decrypt_wxwork_database",
                wraps=decrypt_wxwork_database,
            ) as decrypt,
            mock.patch("wecom_snapshot.copy_stable_pair", wraps=copy_stable_pair) as copy_pair,
        ):
            first = self.snapshot()
            self.assertEqual(first["cache_status"], "full")
            self.assertTrue(first["db_copied"])
            self.assertEqual(decrypt.call_count, 1)

            unchanged = self.snapshot()
            self.assertTrue(unchanged["skipped"])
            self.assertEqual(unchanged["cache_status"], "unchanged")
            self.assertEqual(decrypt.call_count, 1)
            self.assertEqual(copy_pair.call_count, 1)

            wal_path.write_bytes(wal2)
            reused = self.snapshot()
            self.assertTrue(reused["reused_baseline"])
            self.assertFalse(reused["db_copied"])
            self.assertEqual(reused["cache_status"], "wal_reused")
            self.assertEqual(reused["wal_commit_dbsize"], len(pages(target3)))
            self.assertEqual(decrypt.call_count, 1)
            self.assertEqual(copy_pair.call_count, 2)
        self.assertEqual((self.out_dir / "message.db").read_bytes(), target3.read_bytes())

    def test_main_db_change_forces_full_decrypt(self):
        with mock.patch(
            "wecom_snapshot.decrypt_wxwork_database", wraps=decrypt_wxwork_database
        ) as decrypt:
            self.snapshot()
            target = self.work / "changed.sqlite"
            shutil.copyfile(self.base, target)
            add_value(target, "main-db-change")
            encrypt_db(target, self.source)
            result = self.snapshot()
            self.assertEqual(result["cache_status"], "full")
            self.assertFalse(result["reused_baseline"])
            self.assertTrue(result["db_copied"])
            self.assertEqual(decrypt.call_count, 2)
        self.assertEqual((self.out_dir / "message.db").read_bytes(), target.read_bytes())

    def test_corrupt_cache_and_destination_safely_rebuild(self):
        with mock.patch(
            "wecom_snapshot.decrypt_wxwork_database", wraps=decrypt_wxwork_database
        ) as decrypt:
            self.snapshot()
            cache = self.out_dir / "message.db.snapshot-cache.json"
            cache.write_text("{broken", encoding="utf-8")
            from_bad_cache = self.snapshot()
            self.assertEqual(from_bad_cache["cache_status"], "full")
            self.assertEqual(decrypt.call_count, 2)

            (self.out_dir / "message.db").write_bytes(b"corrupt-destination")
            from_bad_destination = self.snapshot()
            self.assertEqual(from_bad_destination["cache_status"], "full")
            self.assertEqual(decrypt.call_count, 3)
        self.assertEqual((self.out_dir / "message.db").read_bytes(), self.base.read_bytes())

    def test_incompatible_wal_generation_falls_back_to_full(self):
        _target2, target3, wal1, _wal2 = self.target_with_wal_versions()
        wal_path = Path(str(self.source) + "-wal")
        wal_path.write_bytes(wal1)
        with mock.patch(
            "wecom_snapshot.decrypt_wxwork_database", wraps=decrypt_wxwork_database
        ) as decrypt:
            self.snapshot()
            self.assertEqual(decrypt.call_count, 1)
            wal_path.write_bytes(make_wal([pages(target3)], salts=(999, 1000)))
            result = self.snapshot()
            self.assertEqual(result["cache_status"], "full_fallback")
            self.assertFalse(result["reused_baseline"])
            self.assertTrue(result["db_copied"])
            self.assertEqual(decrypt.call_count, 2)
            self.assertTrue(any("committed prefix" in item for item in result["warnings"]))
        self.assertEqual((self.out_dir / "message.db").read_bytes(), target3.read_bytes())


if __name__ == "__main__":
    unittest.main()
