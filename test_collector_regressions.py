from __future__ import annotations

import json
import base64
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "third_party" / "wecom-decrypt"))

import export_wxwork_messages as export  # noqa: E402
import wecom_collector as collector_module  # noqa: E402
from store import Store  # noqa: E402
from wecom_collector import collect  # noqa: E402


MESSAGE_SCHEMA = """
CREATE TABLE {table} (
    message_id INTEGER PRIMARY KEY,
    server_id INTEGER,
    sequence INTEGER,
    sender_id INTEGER,
    conversation_id TEXT,
    content_type INTEGER,
    send_time INTEGER,
    flag INTEGER,
    content BLOB,
    extra_content BLOB,
    local_extra_content BLOB
)
"""


def make_message_row(table="message_table", message_id=1, server_id=100,
                     conversation_id="R:1", **overrides):
    row = {
        "source_table": table,
        "message_id": message_id,
        "server_id": server_id,
        "sequence": 10,
        "sender_id": 7,
        "conversation_id": conversation_id,
        "content_type": 2,
        "send_time": 1000,
        "flag": 0,
        "content": "hello",
        "extra_content": "",
        "local_extra_content": "",
    }
    row.update(overrides)
    return row


def insert_message(con, table, **row):
    values = make_message_row(table=table, **row)
    columns = [key for key in values if key != "source_table"]
    con.execute(
        f'INSERT INTO "{table}" ({",".join(columns)}) VALUES ({",".join("?" for _ in columns)})',
        [values[key] for key in columns],
    )


def create_source(directory: Path):
    con = sqlite3.connect(directory / "message.db")
    con.execute(MESSAGE_SCHEMA.format(table="message_table"))
    con.execute(MESSAGE_SCHEMA.format(table="message_small_table"))
    insert_message(con, "message_table", message_id=10, server_id=100, content="before")
    insert_message(con, "message_table", message_id=20, server_id=101, sequence=11,
                   send_time=1001, content="second")
    insert_message(con, "message_small_table", message_id=1, server_id=100, content="before")
    con.commit()
    con.close()

    con = sqlite3.connect(directory / "session.db")
    con.execute("""CREATE TABLE conversation_table (
        id TEXT PRIMARY KEY, name TEXT, roomname_remark TEXT,
        last_message_time INTEGER, last_message_id INTEGER
    )""")
    con.execute("INSERT INTO conversation_table VALUES ('R:1','测试群','',0,0)")
    con.commit()
    con.close()

    con = sqlite3.connect(directory / "user.db")
    con.execute("""CREATE TABLE user_table (
        id INTEGER PRIMARY KEY, name TEXT, english_name TEXT, real_name TEXT,
        account TEXT, external_corp_name TEXT, external_job TEXT
    )""")
    con.execute("INSERT INTO user_table VALUES (7,'Alice','','','','','')")
    con.execute("INSERT INTO user_table VALUES (8,'','Bob','','','','')")
    con.commit()
    con.close()


class CanonicalIdentityTests(unittest.TestCase):
    def test_server_identity_is_cross_table_and_conversation_scoped(self):
        main = make_message_row()
        small = make_message_row(table="message_small_table", message_id=99)
        self.assertEqual(export.canonical_msgid(main), export.canonical_msgid(small))
        other_room = make_message_row(conversation_id="R:2")
        self.assertNotEqual(export.canonical_msgid(main), export.canonical_msgid(other_room))

    def test_zero_server_id_uses_stable_local_fallback(self):
        row = make_message_row(server_id=0)
        self.assertEqual(export.canonical_msgid(row), "wxwork:v2:l:message_table:1")

    def test_sender_name_and_m_conversation_fallbacks(self):
        msg = export._build_message(make_message_row(), {}, {7: "Alice"}, {}, 7)
        self.assertEqual(msg["sender"], "Alice")
        self.assertEqual(msg["sender_id"], 7)
        self.assertEqual(export._conversation_kind("M:99"), "多人会话")
        self.assertEqual(
            export._name_from_conversation_id("M:99", {7: "Alice", 8: "Bob"}, 7, [7, 8]),
            "Bob的多人会话",
        )
        self.assertEqual(export._name_from_conversation_id("M:99", {}, 7), "多人会话(M:99)")


class StoreMigrationTests(unittest.TestCase):
    def test_legacy_rows_and_hits_migrate_and_deduplicate(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "legacy.sqlite"
            con = sqlite3.connect(path)
            con.executescript("""
                CREATE TABLE messages (
                    msgid TEXT PRIMARY KEY, seq INTEGER, roomid TEXT, sender TEXT,
                    msgtime INTEGER, msgtype TEXT, content TEXT, raw TEXT, fetched_at INTEGER
                );
                CREATE INDEX idx_messages_room_time ON messages(roomid,msgtime);
                CREATE TABLE hits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, msgid TEXT NOT NULL,
                    rule TEXT NOT NULL, roomid TEXT, msgtime INTEGER, snippet TEXT,
                    pushed INTEGER DEFAULT 0, UNIQUE(msgid,rule)
                );
                CREATE TABLE rooms (roomid TEXT PRIMARY KEY,name TEXT,updated_at INTEGER);
                CREATE TABLE state (k TEXT PRIMARY KEY,v TEXT);
            """)
            raw = json.dumps(make_message_row(), ensure_ascii=False)
            for table, local_id in (("message_table", 10), ("message_small_table", 1)):
                old_id = f"wxwork:{table}:{local_id}"
                nested = make_message_row(table=table, message_id=local_id)
                outer = {"msgid": old_id, "raw": nested}
                con.execute(
                    "INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?)",
                    (old_id, 10, "R:1", "Alice", 1000, 2, "hello",
                     json.dumps(outer, ensure_ascii=False), 1),
                )
                con.execute(
                    "INSERT INTO hits(msgid,rule,roomid,msgtime,snippet,pushed) VALUES (?,?,?,?,?,?)",
                    (old_id, "rule", "R:1", 1000, "hello", int(table == "message_table")),
                )
            con.commit()
            con.close()

            store = Store(str(path))
            self.assertEqual(store.count(), 1)
            row = store._db.execute("SELECT msgid,sender_id FROM messages").fetchone()
            self.assertEqual(row[0], export.canonical_msgid(make_message_row()))
            self.assertEqual(row[1], 7)
            self.assertEqual(store._db.execute("SELECT COUNT(*) FROM hits").fetchone()[0], 1)
            self.assertEqual(store._db.execute("SELECT pushed FROM hits").fetchone()[0], 1)
            columns = [r[1] for r in store._db.execute("PRAGMA table_info(messages)")]
            self.assertEqual(columns, [
                "msgid", "seq", "roomid", "sender", "sender_id", "msgtime",
                "msgtype", "content", "raw", "fetched_at",
            ])
            store.close()

            # 已迁移库再次打开不会重复改写或去重。
            store = Store(str(path))
            self.assertEqual(store.count(), 1)
            store.close()
            self.assertNotEqual(raw, "")


class CollectorTests(unittest.TestCase):
    def test_full_rescan_updates_recall_and_captures_late_low_id(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            create_source(root)
            store = Store(str(root / "derived.sqlite"))
            scanned, inserted = collect(root, store, self_id=7)
            self.assertEqual((scanned, inserted), (3, 2))
            self.assertEqual(store.count(), 2)
            self.assertTrue(all(item["msgid"].startswith("wxwork:v2:s:") for item in store.recent()))
            self.assertEqual({item["sender_id"] for item in store.recent()}, {7})

            con = sqlite3.connect(root / "message.db")
            con.execute("UPDATE message_table SET content='after',flag=9 WHERE message_id=10")
            insert_message(con, "message_table", message_id=5, server_id=102, sender_id=8,
                           sequence=9, send_time=999, content="late")
            con.commit()
            con.close()

            scanned, inserted = collect(root, store, self_id=7)
            self.assertEqual((scanned, inserted), (4, 1))
            self.assertEqual(store.count(), 3)
            by_id = {
                row[0]: row for row in store._db.execute(
                    "SELECT msgid,content,raw,sender,sender_id FROM messages"
                )
            }
            updated = by_id[export.canonical_msgid(make_message_row(message_id=10))]
            self.assertEqual(updated[1], "after")
            self.assertEqual(json.loads(updated[2])["raw"]["flag"], 9)
            late = by_id[export.canonical_msgid(make_message_row(
                message_id=5, server_id=102, sender_id=8, sequence=9, send_time=999, content="late"
            ))]
            self.assertEqual((late[3], late[4]), ("Bob", 8))
            store.close()

    def test_export_counts_and_rows_share_canonical_dedupe(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            create_source(root)
            output = root / "out"
            cfg = {"decrypted_dir": str(root), "output_dir": str(output), "self_id": 7}
            with patch.object(export, "_load_config", return_value=cfg):
                conversations = export.discover_conversations(str(root))
                self.assertEqual(conversations[0]["message_count"], 2)
                result = export.export_messages(formats={"json"})
            self.assertEqual(result["message_count"], 2)
            files = list(output.glob("*/messages.json"))
            self.assertEqual(len(files), 1)
            payload = json.loads(files[0].read_text(encoding="utf-8"))
            self.assertEqual(payload["message_count"], 2)
            self.assertEqual(len({msg["msgid"] for msg in payload["messages"]}), 2)


class ParserRegressionTests(unittest.TestCase):
    def test_protobuf_tag_is_not_part_of_chinese_text(self):
        raw = base64.b64decode("CkgIABJECkLlr7nnmoTvvIzlsLHmmK/ov5nkuKrvvIzmr4/lpKnnlKjkuIDnlKjvvIzorqnku5bovoXliqnovoXliqnlt6XkvZw=")
        self.assertEqual(export.decode_content(raw), "对的，就是这个，每天用一用，让他辅助辅助工作")

    def test_real_leading_letter_is_preserved(self):
        raw = bytes.fromhex("0a130800120f0a0d61e7 8f ade5a1abe5ae8ce4ba86".replace(" ", ""))
        self.assertEqual(export.decode_content(raw), "a班填完了")

    def test_plain_filename_is_not_misread_as_protobuf(self):
        filename = "2026.10.9打磨手指割伤事故分析--四不放过分析(1).doc"
        raw = base64.b64decode(
            "CrACMzA4MTk1MDIwMTAyMDQ4MThkMzA4MThhMDIwMTAwMDIwNDVjYzI4ZWQwMDIwMzBmNDI0MjAyMDRmMDkxNTA3MDAyMDQ2YWM5ZWY0NjA0NGM0ZTQ1NTc0OTQ0MzE1ZjY0Mzc2NDY0MzAzOTM4NjM2NjMwMzkzMTM1MzAzNzMwMzY2MTYzNjEzMDMxMzEzODMwMzAzMDM2MzMzNDM3Mzg1ZjYzMzkzMTY2NjIzNTMzNjUyZDMxNjU2MjMxMmQzNDM5NjIzMzJkNjEzNjYyMzUyZDYzMzE2MTMxMzczMTM3Mzg2NTY1MzMzNzAyMDMxMDAwMDUwMjAzMDA4ODEwMDQxMGQyYjI5Nzk2NTEyOTNlMzIwYmVkYWIyNmFmM2QzNDM2MDIwMTA1MDIwMTAwMDQwMBJCMjAyNi4xMC455omT56Oo5omL5oyH5Ymy5Lyk5LqL5pWF5YiG5p6QLS3lm5vkuI3mlL7ov4fliIbmnpAoMSkuZG9jIICQAkIgMzUzNTYxNjMzNTY1MzAzNTM3NjQzNDM2MzIzODM5NjFSIGQyYjI5Nzk2NTEyOTNlMzIwYmVkYWIyNmFmM2QzNDM2cAB4AIIBAIoBAJIBAJgBgJAC4gIDZG9jogYA"
        )
        self.assertTrue(export.decode_content(raw).startswith(filename), export.decode_content(raw))


class WatchRetryTests(unittest.TestCase):
    def test_watch_logs_and_retries_instead_of_dying(self):
        fake_store = unittest.mock.MagicMock()
        with (
            patch.object(collector_module, "load_settings", return_value=(Path("db"), Path("out"), {})),
            patch.object(collector_module, "Store", return_value=fake_store),
            patch.object(collector_module, "configure_logging", return_value=Path("test.log")),
            patch.object(collector_module, "snapshot_one", side_effect=[RuntimeError("boom"), KeyboardInterrupt]) as snap,
            patch.object(collector_module.time, "sleep") as sleep,
            patch.object(collector_module.LOG, "exception") as log_exception,
            patch.object(sys, "argv", ["wecom_collector.py", "--watch"]),
        ):
            self.assertEqual(collector_module.main(), 0)
        self.assertEqual(snap.call_count, 2)
        sleep.assert_called_once()
        log_exception.assert_called_once()
        fake_store.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
