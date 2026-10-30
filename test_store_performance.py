from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import wecom_collector as collector_module
from store import SCHEMA, Store
from test_collector_regressions import create_source, insert_message
from wecom_collector import CollectorAlreadyRunning, CollectorInstance, collect


def make_msg(msgid, roomid="R:1", msgtime=1, content="文本", name="测试群"):
    return {
        "msgid": msgid,
        "roomid": roomid,
        "msgtime": msgtime,
        "content": content,
        "raw": {"conversation": name, "conversation_kind": "群聊"},
    }


def fts_ids(store, query):
    return {
        row[0]
        for row in store._db.execute(
            "SELECT m.msgid FROM messages_fts AS f "
            "JOIN messages AS m ON m.rowid=f.rowid WHERE messages_fts MATCH ?",
            (query,),
        )
    }


def compatible_search(store, query):
    # MCP 的约定：中文/任意 Unicode 三字符以上走 trigram，短查询回退 instr。
    if len(query) >= 3:
        return fts_ids(store, query)
    return {
        row[0] for row in store._db.execute(
            "SELECT msgid FROM messages WHERE instr(content,?)>0", (query,)
        )
    }


class RoomStatsAndFtsTests(unittest.TestCase):
    def test_stats_and_external_content_fts_follow_updates(self):
        with tempfile.TemporaryDirectory() as td:
            store = Store(str(Path(td) / "messages.sqlite"))
            store.save_messages([
                make_msg("m1", msgtime=10, content="安全生产"),
                make_msg("m2", msgtime=20, content="普通文本"),
                make_msg("m3", roomid="R:2", msgtime=30, content="另一个群", name="二群"),
            ])
            rooms = {
                row[0]: row[1:]
                for row in store._db.execute(
                    "SELECT roomid,message_count,last_time,name,kind FROM rooms"
                )
            }
            self.assertEqual(rooms["R:1"], (2, 20, "测试群", "群聊"))
            self.assertEqual(rooms["R:2"], (1, 30, "二群", "群聊"))
            self.assertEqual(compatible_search(store, "安全生产"), {"m1"})
            self.assertEqual(fts_ids(store, "安全"), set())
            self.assertEqual(compatible_search(store, "安全"), {"m1"})

            # 跨会话移动和时间回退都必须重算旧/新房间统计，并移除旧 FTS token。
            self.assertFalse(store.save_message(
                make_msg("m1", roomid="R:2", msgtime=5, content="设备维修", name="二群")
            ))
            self.assertEqual(fts_ids(store, "安全生产"), set())
            self.assertEqual(fts_ids(store, "设备维修"), {"m1"})
            rooms = {
                row[0]: row[1:3]
                for row in store._db.execute(
                    "SELECT roomid,message_count,last_time FROM rooms"
                )
            }
            self.assertEqual(rooms, {"R:1": (1, 20), "R:2": (2, 30)})
            store.save_message(make_msg("m3", roomid="R:2", msgtime=5, content="另一个群"))
            self.assertEqual(
                store._db.execute(
                    "SELECT message_count,last_time FROM rooms WHERE roomid='R:2'"
                ).fetchone(),
                (2, 5),
            )
            store.close()

            store = Store(str(Path(td) / "messages.sqlite"))
            self.assertEqual(store.count(), 3)
            self.assertEqual(fts_ids(store, "设备维修"), {"m1"})
            self.assertEqual(store._db.execute(
                "SELECT SUM(message_count) FROM rooms"
            ).fetchone()[0], 3)
            store.close()

    def test_existing_database_migrates_stats_and_rebuilds_fts(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "old.sqlite"
            con = sqlite3.connect(path)
            con.executescript(SCHEMA)
            raw = json.dumps({"raw": {
                "conversation": "旧群", "conversation_kind": "群聊",
            }}, ensure_ascii=False)
            con.execute(
                "INSERT INTO messages(msgid,roomid,msgtime,content,raw) VALUES (?,?,?,?,?)",
                ("legacy", "R:old", 123, "历史安全记录", raw),
            )
            con.execute("INSERT INTO state VALUES ('messages_identity_version','2')")
            con.execute("INSERT INTO rooms(roomid,name) VALUES ('R:old','旧群')")
            con.commit()
            con.close()

            store = Store(str(path))
            self.assertEqual(store._db.execute(
                "SELECT message_count,last_time,name,kind FROM rooms WHERE roomid='R:old'"
            ).fetchone(), (1, 123, "旧群", "群聊"))
            self.assertEqual(fts_ids(store, "安全记录"), {"legacy"})
            store.close()


class CollectorIncrementalTests(unittest.TestCase):
    def test_lookback_incremental_and_periodic_full(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            create_source(root)
            store = Store(str(root / "derived.sqlite"))
            self.assertEqual(collect(root, store, 7), (3, 2))
            con = sqlite3.connect(root / "message.db")
            con.execute("UPDATE message_table SET content='after' WHERE message_id=10")
            insert_message(con, "message_table", message_id=5, server_id=103, content="late")
            insert_message(con, "message_table", message_id=30, server_id=104, content="new")
            con.commit()
            con.close()

            # message_table 回看 10/20/30，small 表回看 1；低位点 5 留给定期全量。
            with patch.object(
                collector_module.export, "discover_conversations",
                side_effect=AssertionError("incremental must not count all messages"),
            ):
                self.assertEqual(collect(root, store, 7, full=False, lookback=15), (4, 1))
            self.assertEqual(store.count(), 3)
            self.assertEqual(store._db.execute(
                "SELECT content FROM messages WHERE content='after'"
            ).fetchone()[0], "after")
            self.assertEqual(collect(root, store, 7, full=True), (5, 1))
            self.assertEqual(store.count(), 4)
            store.close()

    def test_main_skips_unchanged_and_uses_incremental_between_full_scans(self):
        fake_store = MagicMock()
        fake_store.count.return_value = 2
        version = ["a"]
        sleeps = [0]

        def snapshot(*_args):
            return {"sha256": version[0]}

        def sleep(_seconds):
            sleeps[0] += 1
            if sleeps[0] == 1:
                version[0] = "b"
            elif sleeps[0] == 2:
                version[0] = "c"
            elif sleeps[0] >= 4:
                raise KeyboardInterrupt

        with (
            patch.object(collector_module, "load_settings", return_value=(Path("db"), Path("out"), {})),
            patch.object(collector_module, "Store", return_value=fake_store),
            patch.object(collector_module, "CollectorInstance") as instance_type,
            patch.object(collector_module, "configure_logging", return_value=Path("test.log")),
            patch.object(collector_module, "snapshot_one", side_effect=snapshot),
            patch.object(collector_module, "collect", return_value=(3, 2)) as collect_mock,
            patch.object(collector_module.time, "sleep", side_effect=sleep),
            patch.object(collector_module.time, "monotonic", side_effect=[1.0, 2.0, 4000.0, 4000.0]),
            patch.object(sys, "argv", ["wecom_collector.py", "--watch"]),
        ):
            instance_type.return_value = MagicMock()
            self.assertEqual(collector_module.main(), 0)
        self.assertEqual(collect_mock.call_count, 3)
        self.assertTrue(collect_mock.call_args_list[0].kwargs["full"])
        self.assertFalse(collect_mock.call_args_list[1].kwargs["full"])
        self.assertTrue(collect_mock.call_args_list[2].kwargs["full"])


class CollectorInstanceTests(unittest.TestCase):
    def test_mutex_pid_heartbeat_and_stale_file_recovery(self):
        with tempfile.TemporaryDirectory() as td:
            first = CollectorInstance(td).acquire()
            payload = json.loads((Path(td) / "collector.pid").read_text(encoding="utf-8"))
            self.assertGreater(payload["pid"], 0)
            self.assertGreaterEqual(payload["heartbeat"], payload["started_at"])
            second = CollectorInstance(td)
            with self.assertRaises(CollectorAlreadyRunning):
                second.acquire()
            first.close()
            self.assertFalse((Path(td) / "collector.pid").exists())

            # 即使 PID 文件异常残留，也只由实际持锁状态决定是否可启动。
            (Path(td) / "collector.pid").write_text('{"pid":999999}', encoding="utf-8")
            second.acquire()
            second.heartbeat()
            second.close()


class HundredThousandRowBenchmark(unittest.TestCase):
    def test_100k_bulk_and_incremental_correctness(self):
        count = 100_000
        with tempfile.TemporaryDirectory() as td:
            store = Store(str(Path(td) / "benchmark.sqlite"))
            messages = [
                make_msg(
                    f"m{i}", roomid=f"R:{i % 100}", msgtime=i,
                    content="安全生产目标" if i == count - 1 else "普通文本",
                    name=f"群{i % 100}",
                )
                for i in range(count)
            ]
            started = time.perf_counter()
            self.assertEqual(store.save_messages(messages), count)
            bulk_seconds = time.perf_counter() - started
            self.assertEqual(store.count(), count)
            self.assertEqual(store._db.execute(
                "SELECT SUM(message_count) FROM rooms"
            ).fetchone()[0], count)
            self.assertEqual(store._db.execute(
                "SELECT message_count,last_time FROM rooms WHERE roomid='R:0'"
            ).fetchone(), (count // 100, 99_900))
            self.assertEqual(fts_ids(store, "安全生产"), {f"m{count - 1}"})
            self.assertEqual(compatible_search(store, "安全"), {f"m{count - 1}"})

            changes = [
                make_msg(f"m{i}", roomid=f"R:{i % 100}", msgtime=i, content="设备维修更新")
                for i in range(100)
            ] + [
                make_msg(f"m{i}", roomid=f"R:{i % 100}", msgtime=i, content="新增文本")
                for i in range(count, count + 100)
            ]
            started = time.perf_counter()
            self.assertEqual(store.save_messages(changes), 100)
            incremental_seconds = time.perf_counter() - started
            self.assertEqual(store.count(), count + 100)
            self.assertEqual(store._db.execute(
                "SELECT SUM(message_count) FROM rooms"
            ).fetchone()[0], count + 100)
            self.assertEqual(len(fts_ids(store, "设备维修")), 100)
            print(
                f"PERF rows={count} bulk={bulk_seconds:.3f}s "
                f"incremental_200={incremental_seconds:.3f}s"
            )
            store.close()


if __name__ == "__main__":
    unittest.main()
