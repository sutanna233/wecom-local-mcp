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


def create_store(path: Path, *, sender_column: bool = True, flag_column: bool = False,
                 messages: list[dict] | None = None) -> None:
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    sender_ddl = "sender_id INTEGER," if sender_column else ""
    flag_ddl = "flag INTEGER," if flag_column else ""
    con.execute(
        "CREATE TABLE messages ("
        "msgid TEXT PRIMARY KEY,seq INTEGER,roomid TEXT,sender TEXT,"
        f"{sender_ddl}msgtime INTEGER,msgtype TEXT,{flag_ddl}content TEXT,raw TEXT,fetched_at INTEGER)"
    )
    con.execute("CREATE TABLE rooms (roomid TEXT PRIMARY KEY,name TEXT,updated_at INTEGER)")
    for index, message in enumerate(messages if messages is not None else fixture_messages()):
        columns = ["msgid", "seq", "roomid", "sender"]
        values = [message["msgid"], message["seq"], message["roomid"], message["sender"]]
        if sender_column:
            columns.append("sender_id")
            values.append(message["sender_id"])
        columns.extend(["msgtime", "msgtype"])
        values.extend([message["msgtime"], message["msgtype"]])
        if flag_column:
            columns.append("flag")
            values.append(message.get("flag"))
        columns.extend(["content", "raw", "fetched_at"])
        raw = {"raw": message["raw"]} if index % 2 else message["raw"]
        values.extend([
            message["content"], json.dumps(raw, ensure_ascii=False), 1_700_000_000 + index,
        ])
        placeholders = ",".join("?" for _ in columns)
        con.execute(f"INSERT INTO messages ({','.join(columns)}) VALUES ({placeholders})", values)
    con.commit()
    con.close()


def add_performance_indexes(path: Path, *, rooms: bool = True, fts: bool = True) -> None:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    if rooms:
        con.execute("ALTER TABLE rooms ADD COLUMN message_count INTEGER NOT NULL DEFAULT 0")
        con.execute("ALTER TABLE rooms ADD COLUMN last_time INTEGER NOT NULL DEFAULT 0")
        con.execute("ALTER TABLE rooms ADD COLUMN kind TEXT NOT NULL DEFAULT ''")
        stats = con.execute(
            "SELECT roomid,COUNT(*) AS message_count,MAX(msgtime) AS last_time "
            "FROM messages GROUP BY roomid"
        ).fetchall()
        for stat in stats:
            latest = con.execute(
                "SELECT raw FROM messages WHERE roomid=? ORDER BY msgtime DESC,msgid DESC LIMIT 1",
                (stat["roomid"],),
            ).fetchone()
            raw = mcp.nested_raw(latest)
            cid = stat["roomid"] or ""
            name = raw.get("display_name") or raw.get("conversation") or cid
            kind = raw.get("conversation_kind") or raw.get("kind") or mcp._conversation_kind(cid)
            con.execute(
                "INSERT INTO rooms(roomid,name,updated_at,message_count,last_time,kind) VALUES(?,?,0,?,?,?) "
                "ON CONFLICT(roomid) DO UPDATE SET name=excluded.name,message_count=excluded.message_count,"
                "last_time=excluded.last_time,kind=excluded.kind",
                (cid, name, stat["message_count"], stat["last_time"], kind),
            )
    if fts:
        con.execute(
            "CREATE VIRTUAL TABLE messages_fts USING fts5("
            "content,content='messages',content_rowid='rowid',tokenize='trigram')"
        )
        con.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
    con.commit()
    con.close()


def v13_fixture_messages() -> list[dict]:
    return [
        {"msgid": "d1", "seq": 1, "roomid": "R:a", "sender": "甲", "sender_id": 1,
         "msgtime": T0, "msgtype": 13, "content": "  同一条\t消息 ", "flag": 5,
         "raw": {"flag": 5, "type_name": "原始类型", "server_id": 11}},
        # Different msgid/server_id, but the same ledger body after whitespace folding.
        {"msgid": "d2", "seq": 2, "roomid": "R:a", "sender": "甲", "sender_id": 1,
         "msgtime": T0, "msgtype": 13, "content": "同一条\n消息", "flag": 2,
         "raw": {"flag": 2, "server_id": 22}},
        {"msgid": "d3", "seq": 3, "roomid": "R:a", "sender": "乙", "sender_id": 2,
         "msgtime": T0 + 60, "msgtype": 1002, "content": "1001;1002", "flag": 4,
         "raw": {"flag": 4, "at_user_id": [1001, 1002], "at_user": ["@甲", "@乙"]}},
        {"msgid": "d4", "seq": 4, "roomid": "R:b", "sender": "丙", "sender_id": 3,
         "msgtime": T0 + 120, "msgtype": 2, "content": "其他", "flag": 0,
         "raw": {"flag": 0, "at_user_id": "all", "at_user": "@所有人"}},
    ]


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

    def test_v13_optional_query_flag_types_mentions_and_dedupe(self) -> None:
        for flag_column in (False, True):
            with self.subTest(flag_column=flag_column):
                path = self.work / f"v13-{flag_column}.sqlite"
                create_store(path, flag_column=flag_column, messages=v13_fixture_messages())
                with mock.patch.object(mcp, "DB_PATH", path):
                    self.assertEqual(mcp.search_messages({})["total"], 4)
                    filtered = mcp.search_messages({"conversation_id": "R:a", "sender": "甲"})
                    self.assertEqual({row["msgid"] for row in filtered["messages"]}, {"d1", "d2"})
                    self.assertEqual(mcp.search_messages({"msgtype": 1002})["messages"][0]["msgid"], "d3")
                    recent = mcp.recent_messages({"start_time": T0 + 60, "end_time": T0 + 120})
                    self.assertEqual([row["msgid"] for row in recent["messages"]], ["d3"])
                    flagged = mcp.recent_messages({"flag_mask": 3, "flag_value": 1})
                    self.assertEqual([row["msgid"] for row in flagged["messages"]], ["d1"])
                    self.assertEqual(mcp.search_messages({"flag_mask": 2})["total"], 3)

                    first = mcp.get_message({"msgid": "d1"})
                    second = mcp.get_message({"msgid": "d2"})
                    self.assertEqual(first["type_name"], "原始类型")
                    self.assertEqual(second["type_name"], "在线表格")
                    self.assertEqual(first["flag"], 5)
                    self.assertEqual(first["flag_bits"], [0, 2])
                    self.assertEqual(first["dedupe_key"], second["dedupe_key"])
                    self.assertEqual(len(first["dedupe_key"]), 64)
                    self.assertEqual((first["dup_count"], second["dup_count"]), (2, 2))
                    self.assertEqual(mcp.search_messages({})["total"], 4)  # no physical dedupe

                    mentioned = mcp.get_message({"msgid": "d3"})
                    self.assertEqual(mentioned["at_user_id"], [1001, 1002])
                    self.assertEqual(mentioned["at_user"], ["@甲", "@乙"])
                    self.assertEqual(mentioned["mentions"], [
                        {"user_id": 1001, "name": "@甲"},
                        {"user_id": 1002, "name": "@乙"},
                    ])
                    self.assertFalse(mentioned["mention_all"])
                    legacy_all = mcp.get_message({"msgid": "d4"})
                    self.assertEqual(legacy_all["at_user_id"], ["all"])
                    self.assertTrue(legacy_all["mention_all"])
                    with self.assertRaisesRegex(ValueError, "requires flag_mask"):
                        mcp.search_messages({"flag_value": 1})

    def test_v13_count_messages_dimensions_and_filters(self) -> None:
        create_store(self.db, flag_column=True, messages=v13_fixture_messages())
        by_conversation = mcp.count_messages({})
        self.assertEqual((by_conversation["total"], by_conversation["count"]), (4, 4))
        self.assertEqual(by_conversation["group_by"], ["conversation"])
        self.assertEqual(
            {row["conversation_id"]: row["count"] for row in by_conversation["groups"]},
            {"R:a": 3, "R:b": 1},
        )
        by_sender = mcp.count_messages({"group_by": "sender", "conversation_id": "R:a"})
        self.assertEqual({(row["sender"], row["sender_id"], row["count"]) for row in by_sender["groups"]}, {
            ("甲", 1, 2), ("乙", 2, 1),
        })
        by_type = mcp.count_messages({"group_by": "msgtype"})
        self.assertEqual({row["msgtype"]: row["count"] for row in by_type["groups"]}, {2: 1, 13: 2, 1002: 1})
        self.assertEqual({row["type_name"] for row in by_type["groups"]}, {"文本", "在线表格", "@提及"})
        day = datetime.fromtimestamp(T0).strftime("%Y-%m-%d")
        by_day = mcp.count_messages({"group_by": ["day"]})
        self.assertEqual(by_day["groups"], [{"day": day, "count": 4}])
        combined = mcp.count_messages({"group_by": "conversation,msgtype", "query": "同一条"})
        self.assertEqual(combined["total"], 2)
        self.assertEqual(combined["groups"], [{"conversation_id": "R:a", "msgtype": 13, "type_name": "在线表格", "count": 2}])
        window = mcp.count_messages({"start_time": T0 + 60, "end_time": T0 + 120})
        self.assertEqual(window["total"], 1)
        self.assertEqual(mcp.count_messages({"flag_mask": 3, "flag_value": 1})["total"], 1)

    def test_v13_tools_and_local_type_names(self) -> None:
        response = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        tools = {tool["name"]: tool for tool in response["result"]["tools"]}
        self.assertIn("count_messages", tools)
        self.assertNotIn("required", tools["search_messages"]["inputSchema"])
        expected = {13: "在线表格", 20: "媒体消息", 29: "表情", 35: "链接分享",
                    36: "微信/外部消息", 123: "混合消息", 1002: "@提及"}
        self.assertEqual({key: mcp.message_type_name(key) for key in expected}, expected)

    def test_plain_text_mentions_and_mention_all(self) -> None:
        messages = [{"msgid": "t1", "seq": 1, "roomid": "R:mentions", "sender": "甲", "sender_id": 1,
                     "msgtime": T0, "msgtype": 2,
                     "content": "请 @叶垚炜/Ye Yaowei 处理，@所有人 注意；邮箱 a@b.com 不算提及",
                     "raw": {"conversation": "提及测试"}}]
        create_store(self.db, messages=messages)
        item = mcp.search_messages({"query": "处理"})["messages"][0]
        self.assertIn({"user_id": None, "name": "@叶垚炜/Ye Yaowei"}, item["mentions"])
        self.assertTrue(item["mention_all"])
        self.assertNotIn({"user_id": None, "name": "@b"}, item["mentions"])

    def test_rooms_statistics_cache_matches_legacy_without_raw_decoding(self) -> None:
        baseline = mcp.list_conversations({"limit": 2})
        baseline_remaining = mcp.list_conversations({"limit": 2, "offset": 2})
        baseline_named = mcp.list_conversations({"query": "叶垚炜"})
        add_performance_indexes(self.db, rooms=True, fts=False)
        with mock.patch.object(mcp, "nested_raw", side_effect=AssertionError("raw decoded")):
            cached = mcp.list_conversations({"limit": 2})
            cached_remaining = mcp.list_conversations({"limit": 2, "offset": 2})
            cached_named = mcp.list_conversations({"query": "叶垚炜"})
        self.assertEqual(cached, baseline)
        self.assertEqual(cached_remaining, baseline_remaining)
        self.assertEqual(cached_named, baseline_named)

    def test_trigram_join_matches_instr_and_short_query_falls_back(self) -> None:
        con = sqlite3.connect(self.db)
        con.execute(
            "INSERT INTO messages(msgid,seq,roomid,sender,sender_id,msgtime,msgtype,content,raw,fetched_at) "
            "VALUES('quoted',9,'R:team','张三',1001,?,'2','a\"b 100%_值','{}',1)",
            (T0 + 500,),
        )
        con.commit()
        con.close()
        queries = ["中文项目", "alpha", "边界", 'a"b', "0%_"]
        baseline = {query: mcp.search_messages({"query": query, "limit": 100}) for query in queries}
        add_performance_indexes(self.db, rooms=False, fts=True)
        with mcp.closing(mcp.connect()) as con:
            long_source, long_params = mcp._message_search_source(con, "中文项目")
            short_source, short_params = mcp._message_search_source(con, "边界")
        self.assertIn("JOIN", long_source)
        self.assertIn("messages_fts", long_source)
        self.assertEqual(long_params, ['"中文项目"'])
        self.assertEqual((short_source, short_params), (" FROM messages", []))
        for query in queries:
            with self.subTest(query=query):
                self.assertEqual(mcp.search_messages({"query": query, "limit": 100}), baseline[query])

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
