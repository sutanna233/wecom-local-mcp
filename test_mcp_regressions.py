"""Synthetic-store and wire-protocol regressions for wecom_mcp.py."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import wecom_mcp as mcp

ROOT = Path(__file__).resolve().parent
T0 = 1_704_067_200  # 2024-01-01 00:00:00 UTC


def fixture_messages() -> list[dict]:
    return [
        {"msgid": "m1", "seq": 1, "roomid": "R:team", "sender": "张三", "sender_id": 1001,
         "msgtime": T0, "msgtype": "2", "content": "中文项目 Alpha",
         "raw": {"conversation": "项目群", "sender_id": 1001, "is_sent": False, "time": ""}},
        {"msgid": "m2", "seq": 2, "roomid": "R:team", "sender": "李四", "sender_id": 1002,
         "msgtime": T0 + 60, "msgtype": 4, "content": "中文项目 Beta",
         "raw": {"conversation": "项目群", "sender_id": 1002, "is_sent": False}},
        {"msgid": "m3", "seq": 3, "roomid": "R:team", "sender": "张三", "sender_id": 1001,
         "msgtime": T0 + 120, "msgtype": 2, "content": "中文项目 Gamma",
         "raw": {"conversation": "项目群", "sender_id": 1001, "is_sent": True}},
        {"msgid": "m4", "seq": 4, "roomid": "M:456", "sender": "叶垚炜/Ye Yaowei", "sender_id": 456,
         "msgtime": T0 + 180, "msgtype": 2, "content": "联系人消息",
         "raw": {"conversation": "M:456", "sender_id": 456, "is_sent": False}},
        # Latest M: message is self-sent and has no formatted time. The display
        # name must come from m4 and last_time must fall back to msgtime.
        {"msgid": "m5", "seq": 5, "roomid": "M:456", "sender": "我", "sender_id": 999,
         "msgtime": T0 + 240, "msgtype": 2, "content": "我回复",
         "raw": {"conversation": "M:456", "sender_id": 999, "is_sent": True, "time": ""}},
        {"msgid": "m6", "seq": 6, "roomid": "M:999", "sender": "我", "sender_id": 999,
         "msgtime": T0 + 300, "msgtype": 2, "content": "没有可推断的联系人名",
         "raw": {"conversation": "M:999", "sender_id": 999, "is_sent": True}},
        {"msgid": "m7", "seq": 7, "roomid": "S:100_999", "sender": "王五", "sender_id": 100,
         "msgtime": T0 + 360, "msgtype": 2, "content": "边界开始",
         "raw": {"conversation": "王五", "sender_id": 100, "is_sent": False}},
        {"msgid": "m8", "seq": 8, "roomid": "S:100_999", "sender": "王五", "sender_id": 100,
         "msgtime": T0 + 420, "msgtype": 2, "content": "边界结束",
         "raw": {"conversation": "王五", "sender_id": 100, "is_sent": False}},
    ]


def create_store(path: Path, *, sender_column: bool = True, messages: list[dict] | None = None) -> None:
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    sender_ddl = "sender_id INTEGER," if sender_column else ""
    con.execute(
        "CREATE TABLE messages ("
        "msgid TEXT PRIMARY KEY,seq INTEGER,roomid TEXT,sender TEXT,"
        f"{sender_ddl}msgtime INTEGER,msgtype TEXT,content TEXT,raw TEXT,fetched_at INTEGER)"
    )
    con.execute("CREATE TABLE rooms (roomid TEXT PRIMARY KEY,name TEXT,updated_at INTEGER)")
    for index, message in enumerate(messages if messages is not None else fixture_messages()):
        columns = ["msgid", "seq", "roomid", "sender"]
        values = [message["msgid"], message["seq"], message["roomid"], message["sender"]]
        if sender_column:
            columns.append("sender_id")
            values.append(message["sender_id"])
        columns.extend(["msgtime", "msgtype", "content", "raw", "fetched_at"])
        raw = {"raw": message["raw"]} if index % 2 else message["raw"]
        values.extend([
            message["msgtime"], message["msgtype"], message["content"],
            json.dumps(raw, ensure_ascii=False), 1_700_000_000 + index,
        ])
        placeholders = ",".join("?" for _ in columns)
        con.execute(f"INSERT INTO messages ({','.join(columns)}) VALUES ({placeholders})", values)
    con.commit()
    con.close()


class MCPRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="wecom_mcp_test_")
        self.addCleanup(self.tempdir.cleanup)
        self.work = Path(self.tempdir.name)
        self.db = self.work / "messages.sqlite"
        create_store(self.db)
        self.db_patch = mock.patch.object(mcp, "DB_PATH", self.db)
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)

    def test_initialize_is_fixed_and_json_rpc_batch(self) -> None:
        response = mcp.handle({
            "jsonrpc": "2.0", "id": "init", "method": "initialize",
            "params": {"protocolVersion": "2099-01-01", "capabilities": {}},
        })
        self.assertEqual(response["result"]["protocolVersion"], "2024-11-05")
        batch = mcp.handle([
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": "two", "method": "tools/list"},
        ])
        self.assertIsInstance(batch, list)
        self.assertEqual([item["id"] for item in batch], [1, "two"])
        self.assertEqual(batch[0]["result"], {})
        self.assertEqual(mcp.handle([])["error"]["code"], -32600)
        self.assertIsNone(mcp.handle([{"jsonrpc": "2.0", "method": "notifications/initialized"}]))
        self.assertEqual(mcp.handle([42])[0]["error"]["code"], -32600)

    def test_time_bounds_date_only_and_timezone(self) -> None:
        start = T0 + 360
        end = T0 + 420
        result = mcp.search_messages({"query": "边界", "start_time": start, "end_time": end})
        self.assertEqual([item["msgid"] for item in result["messages"]], ["m7"])
        self.assertEqual(mcp.parse_time(1_704_067_200_123), T0)
        aware = "2024-01-01T08:00:00+08:00"
        expected = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp())
        self.assertEqual(mcp.parse_time(aware), expected)

        local_start = int(datetime(2024, 1, 1).timestamp())
        local_next = int(datetime(2024, 1, 2).timestamp())
        dated = [
            {"msgid": "day1", "seq": 1, "roomid": "R:dates", "sender": "甲", "sender_id": 1,
             "msgtime": local_start, "msgtype": 2, "content": "日期边界第一天", "raw": {}},
            {"msgid": "day2", "seq": 2, "roomid": "R:dates", "sender": "乙", "sender_id": 2,
             "msgtime": local_next, "msgtype": 2, "content": "日期边界第二天", "raw": {}},
        ]
        create_store(self.db, messages=dated)
        result = mcp.search_messages({"query": "日期边界", "start_time": "2024-01-01", "end_time": "2024-01-01"})
        self.assertEqual([item["msgid"] for item in result["messages"]], ["day1"])
        self.assertEqual(mcp.parse_time("2024-01-01", is_end=True), local_next)

    def test_search_recent_and_conversation_pagination(self) -> None:
        first = mcp.search_messages({"query": "中文项目", "limit": 2})
        self.assertEqual((first["count"], first["total"], first["offset"]), (2, 3, 0))
        self.assertTrue(first["has_more"])
        self.assertEqual(first["next_offset"], 2)
        last = mcp.search_messages({"query": "中文项目", "limit": 2, "offset": first["next_offset"]})
        self.assertEqual((last["count"], last["total"], last["offset"]), (1, 3, 2))
        self.assertFalse(last["has_more"])
        self.assertIsNone(last["next_offset"])
        self.assertEqual({item["msgid"] for item in first["messages"] + last["messages"]}, {"m1", "m2", "m3"})

        recent = mcp.recent_messages({"limit": 3, "offset": 6})
        self.assertEqual((recent["count"], recent["total"], recent["offset"]), (2, 8, 6))
        self.assertFalse(recent["has_more"])
        conversations = mcp.list_conversations({"limit": 2})
        self.assertEqual((conversations["count"], conversations["total"]), (2, 4))
        self.assertEqual(conversations["next_offset"], 2)
        remaining = mcp.list_conversations({"limit": 2, "offset": 2})
        self.assertEqual(remaining["count"], 2)
        self.assertFalse(remaining["has_more"])

    def test_sender_name_and_id_with_new_and_old_schemas(self) -> None:
        for sender_column in (True, False):
            with self.subTest(sender_column=sender_column):
                path = self.work / f"sender-{sender_column}.sqlite"
                create_store(path, sender_column=sender_column)
                with mock.patch.object(mcp, "DB_PATH", path):
                    by_name = mcp.search_messages({"query": "联系人", "sender": "叶垚炜"})
                    by_id = mcp.search_messages({"query": "联系人", "sender": "456"})
                    self.assertEqual([row["msgid"] for row in by_name["messages"]], ["m4"])
                    self.assertEqual([row["msgid"] for row in by_id["messages"]], ["m4"])
                    self.assertEqual(by_id["messages"][0]["sender_id"], 456)

    def test_malformed_legacy_raw_does_not_break_queries(self) -> None:
        con = sqlite3.connect(self.db)
        con.execute(
            "INSERT INTO messages(msgid,seq,roomid,sender,sender_id,msgtime,msgtype,content,raw,fetched_at) "
            "VALUES('bad',9,'R:bad','甲',1,?,'2','坏数据','{bad',1)", (T0 + 500,)
        )
        con.commit()
        con.close()
        result = mcp.search_messages({"query": "坏数据", "sender": "1"})
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["messages"][0]["msgtype"], 2)

    def test_message_type_and_conversation_metadata(self) -> None:
        message = mcp.search_messages({"query": "Alpha"})["messages"][0]
        self.assertEqual(message["msgtype"], 2)
        self.assertEqual(message["type_name"], "文本")
        self.assertEqual(message["time"], mcp.format_time(T0))
        full = mcp.get_message({"msgid": "m4"})
        self.assertEqual(full["raw"]["sender_id"], 456)
        self.assertEqual(full["seq"], 4)

        named = mcp.list_conversations({"query": "叶垚炜"})
        self.assertEqual(named["count"], 1)
        item = named["conversations"][0]
        self.assertEqual(item["conversation_id"], "M:456")
        self.assertEqual(item["conversation"], "叶垚炜/Ye Yaowei")
        self.assertEqual(item["kind"], "微信联系人")
        self.assertEqual(item["last_time"], mcp.format_time(T0 + 240))
        self.assertEqual(item["last_timestamp"], T0 + 240)
        unknown = mcp.list_conversations({"query": "M:999"})["conversations"][0]
        self.assertIn("未知", unknown["conversation"])
        self.assertIn("M:999", unknown["conversation"])
        self.assertEqual(unknown["kind"], "微信联系人")

    def test_real_stdio_utf8_batch_chinese_and_pagination(self) -> None:
        env = os.environ.copy()
        env.update({
            "PYTHONIOENCODING": "gbk",
            "PYTHONUTF8": "0",
            "WECOM_MCP_DB": str(self.db),
            "WECOM_MCP_READ_ONLY": "1",
        })
        requests = [
            {"jsonrpc": "2.0", "id": "init", "method": "initialize",
             "params": {"protocolVersion": "2030-01-01", "capabilities": {}}},
            [
                {"jsonrpc": "2.0", "id": "tools", "method": "tools/list"},
                {"jsonrpc": "2.0", "id": "page1", "method": "tools/call",
                 "params": {"name": "search_messages", "arguments": {"query": "中文项目", "limit": 2}}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": "page2", "method": "tools/call",
                 "params": {"name": "search_messages", "arguments": {"query": "中文项目", "limit": 2, "offset": 2}}},
            ],
        ]
        wire = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in requests).encode("utf-8")
        proc = subprocess.run(
            [sys.executable, str(ROOT / "wecom_mcp.py")], cwd=ROOT, env=env,
            input=wire, capture_output=True, timeout=30, check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", errors="replace"))
        lines = proc.stdout.decode("utf-8").splitlines()
        self.assertEqual(len(lines), 2, lines)
        init, batch = [json.loads(line) for line in lines]
        self.assertEqual(init["result"]["protocolVersion"], "2024-11-05")
        self.assertEqual([item["id"] for item in batch], ["tools", "page1", "page2"])
        page1 = batch[1]["result"]["structuredContent"]
        page2 = batch[2]["result"]["structuredContent"]
        self.assertEqual((page1["total"], page1["count"], page1["next_offset"]), (3, 2, 2))
        self.assertEqual((page2["total"], page2["count"], page2["has_more"]), (3, 1, False))
        self.assertIn("中文项目", page1["messages"][0]["content"])
        self.assertIn("叶垚炜", mcp.list_conversations({"query": "叶垚炜"})["conversations"][0]["conversation"])


if __name__ == "__main__":
    unittest.main()
