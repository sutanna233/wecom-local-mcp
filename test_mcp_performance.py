"""100k-row correctness/performance check for MCP rooms and trigram queries."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import time
from pathlib import Path
from unittest import mock

import wecom_mcp as mcp

ROW_COUNT = 100_000
ROOM_COUNT = 1_000
BATCH_SIZE = 5_000
T0 = 1_700_000_000
TARGET = "稀有三字关键词"


def make_database(path: Path) -> float:
    started = time.perf_counter()
    con = sqlite3.connect(path)
    con.executescript("""
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
        CREATE TABLE messages (
            msgid TEXT PRIMARY KEY,seq INTEGER,roomid TEXT,sender TEXT,sender_id INTEGER,
            msgtime INTEGER,msgtype INTEGER,flag INTEGER NOT NULL DEFAULT 0,
            content TEXT,raw TEXT,fetched_at INTEGER
        );
        CREATE INDEX idx_messages_room_time ON messages(roomid,msgtime);
        CREATE INDEX idx_messages_time_id ON messages(msgtime DESC,msgid DESC);
        CREATE TABLE rooms (
            roomid TEXT PRIMARY KEY,name TEXT,updated_at INTEGER,
            message_count INTEGER NOT NULL DEFAULT 0,
            last_time INTEGER NOT NULL DEFAULT 0,kind TEXT NOT NULL DEFAULT ''
        );
    """)

    def batch(start: int, stop: int):
        for i in range(start, stop):
            room = f"R:{i % ROOM_COUNT:04d}"
            sender_id = i % 50
            flag = i & 7
            targeted = i % 10_000 == 0 or i == 12_345
            content = f"前缀 {TARGET} 后缀 {i}" if targeted else f"普通消息 performance filler {i % 1000} 内容"
            raw = json.dumps({
                "conversation": f"会话{room}", "conversation_kind": "群聊",
                "sender_id": sender_id, "flag": flag,
            }, ensure_ascii=False)
            yield (f"m{i:06d}", i, room, f"用户{sender_id}", sender_id,
                   T0 + i, 2, flag, content, raw, T0)

    sql = "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?)"
    for start in range(0, ROW_COUNT, BATCH_SIZE):
        con.executemany(sql, batch(start, min(start + BATCH_SIZE, ROW_COUNT)))
    con.execute(
        "INSERT INTO rooms(roomid,name,updated_at,message_count,last_time,kind) "
        "SELECT roomid,'会话'||roomid,0,COUNT(*),MAX(msgtime),'群聊' FROM messages GROUP BY roomid"
    )
    con.execute(
        "CREATE VIRTUAL TABLE messages_fts USING fts5("
        "content,content='messages',content_rowid='rowid',tokenize='trigram')"
    )
    con.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
    con.commit()
    con.close()
    return time.perf_counter() - started


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="wecom_mcp_perf_") as td:
        database = Path(td) / "messages.sqlite"
        build_seconds = make_database(database)
        with mock.patch.object(mcp, "DB_PATH", database):
            def measure(callable_, *args):
                started = time.perf_counter()
                value = callable_(*args)
                return value, time.perf_counter() - started

            first, list_seconds = measure(mcp.list_conversations, {"limit": 50})
            assert (first["total"], first["count"], first["next_offset"]) == (ROOM_COUNT, 50, 50)
            assert all(row["message_count"] == ROW_COUNT // ROOM_COUNT for row in first["conversations"])
            remaining = mcp.list_conversations({"limit": 50, "offset": 500})
            assert (remaining["total"], remaining["count"], remaining["offset"]) == (ROOM_COUNT, 50, 500)
            named = mcp.list_conversations({"query": "0999"})
            assert named["total"] == 1 and named["conversations"][0]["message_count"] == 100

            indexed, fts_seconds = measure(mcp.search_messages, {"query": TARGET, "limit": 100})
            assert indexed["total"] == 11, indexed["total"]
            expected_ids = {f"m{i:06d}" for i in range(0, ROW_COUNT, 10_000)} | {"m012345"}
            assert {row["msgid"] for row in indexed["messages"]} == expected_ids
            assert all(row["dup_count"] == 1 for row in indexed["messages"])

            with mock.patch.object(mcp, "_trigram_fts_available", return_value=False):
                fallback, instr_seconds = measure(mcp.search_messages, {"query": TARGET, "limit": 100})
                short = mcp.search_messages({"query": "稀有", "limit": 100})
            assert fallback == indexed
            assert short["total"] == indexed["total"]
            assert short["messages"] == indexed["messages"]

            filtered = mcp.search_messages({
                "query": TARGET, "sender": "用户45", "msgtype": 2,
                "start_time": T0 + 12_345, "end_time": T0 + 12_346,
                "flag_mask": 7, "flag_value": 1,
            })
            assert filtered["total"] == 1 and filtered["messages"][0]["msgid"] == "m012345"
            counted = mcp.count_messages({"query": TARGET, "group_by": "msgtype"})
            assert counted["total"] == 11 and counted["groups"][0]["count"] == 11
            recent = mcp.recent_messages({"start_time": T0 + 12_345, "end_time": T0 + 12_346})
            assert recent["total"] == 1 and recent["messages"][0]["msgid"] == "m012345"

            with mcp.closing(mcp.connect()) as con:
                source, _ = mcp._message_search_source(con, TARGET)
            assert "JOIN" in source and "messages_fts" in source

        speedup = instr_seconds / fts_seconds if fts_seconds else float("inf")
        print(f"MCP_PERF_ROWS={ROW_COUNT}")
        print(f"MCP_PERF_ROOMS={ROOM_COUNT}")
        print(f"MCP_PERF_BUILD_SECONDS={build_seconds:.3f}")
        print(f"MCP_PERF_LIST_CACHE_SECONDS={list_seconds:.4f}")
        print(f"MCP_PERF_FTS_SECONDS={fts_seconds:.4f}")
        print(f"MCP_PERF_INSTR_SECONDS={instr_seconds:.4f}")
        print(f"MCP_PERF_SPEEDUP={speedup:.2f}x")
        print("MCP_PERF_CORRECTNESS=OK")


if __name__ == "__main__":
    main()
