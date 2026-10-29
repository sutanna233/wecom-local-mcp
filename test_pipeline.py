from __future__ import annotations

import sqlite3
import struct
import sys
import tempfile
import unittest
from pathlib import Path

from Crypto.Cipher import AES

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "third_party" / "wecom-decrypt"))
from wxwork_crypto import (  # noqa: E402
    PAGE_SZ,
    derive_wxsqlite3_aes128_page_key,
    generate_initial_vector,
)
from wecom_snapshot import apply_wal  # noqa: E402
from store import Store  # noqa: E402
import export_wxwork_messages as export  # noqa: E402


class PipelineTests(unittest.TestCase):
    def test_content_decode_matches_known_text(self):
        raw = bytes.fromhex("0a0f0800120b0a09") + "我问问".encode()
        self.assertEqual(export.decode_content(raw), "我问问")

    def test_wal_patch_and_stale_frame_skip(self):
        key = bytes.fromhex("00112233445566778899aabbccddeeff")
        plain_page = bytes((i * 7) % 256 for i in range(PAGE_SZ))
        page_key = derive_wxsqlite3_aes128_page_key(key, 2)
        encrypted = AES.new(page_key, AES.MODE_CBC, generate_initial_vector(2)).encrypt(plain_page)
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            plain = work / "plain.db"
            plain.write_bytes(bytes(PAGE_SZ * 2))
            wal = work / "source.db-wal"
            header = bytearray(32)
            header[16:24] = struct.pack(">II", 111, 222)
            frame = bytearray(24)
            frame[:4] = struct.pack(">I", 2)
            frame[8:16] = struct.pack(">II", 111, 222)
            stale = bytearray(frame)
            stale[8:16] = struct.pack(">II", 999, 999)
            wal.write_bytes(bytes(header) + bytes(frame) + encrypted + bytes(stale) + encrypted)
            patched, skipped = apply_wal(wal, plain, key)
            self.assertEqual((patched, skipped), (1, 1))
            self.assertEqual(plain.read_bytes()[PAGE_SZ:], plain_page)

    def test_store_deduplicates_namespaced_message_id(self):
        with tempfile.TemporaryDirectory() as td:
            store = Store(str(Path(td) / "messages.sqlite"))
            msg = {"msgid": "wxwork:message_table:1", "seq": 1, "roomid": "R:1",
                   "from": "sender", "msgtime": 1, "msgtype": 2, "content": "text"}
            self.assertTrue(store.save_message(msg))
            self.assertFalse(store.save_message(msg))
            self.assertEqual(len(store.recent()), 1)
            store.close()


if __name__ == "__main__":
    unittest.main()
