"""End-to-end MCP stdio smoke test against a synthetic local message store."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path


def make_database(path: Path) -> None:
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE messages ("
        "msgid TEXT PRIMARY KEY,seq INTEGER,roomid TEXT,sender TEXT,sender_id INTEGER,"
        "msgtime INTEGER,msgtype TEXT,content TEXT,raw TEXT,fetched_at INTEGER)"
    )
    con.execute("CREATE TABLE rooms (roomid TEXT PRIMARY KEY,name TEXT,updated_at INTEGER)")
    rows = [
        ("wxwork:message_table:1", 1, "S:100_999", "孙乐乐", 100, 1_704_067_200, "2", "我问问",
         {"conversation": "孙乐乐", "conversation_kind": "单聊", "sender_id": 100, "is_sent": False}),
        ("wxwork:message_table:2", 2, "S:100_999", "我", 999, 1_704_067_260, 2, "中文回复",
         {"conversation": "孙乐乐", "conversation_kind": "单聊", "sender_id": 999, "is_sent": True}),
    ]
    for index, row in enumerate(rows):
        *values, raw = row
        con.execute(
            "INSERT INTO messages(msgid,seq,roomid,sender,sender_id,msgtime,msgtype,content,raw,fetched_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)", [*values, json.dumps({"raw": raw}, ensure_ascii=False), 1_700_000_000 + index]
        )
    con.commit()
    con.close()


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="wecom_mcp_smoke_") as td:
        database = Path(td) / "messages.sqlite"
        make_database(database)
        env = os.environ.copy()
        # Regression test for Windows ANSI stdio: the server must still use UTF-8.
        env["PYTHONIOENCODING"] = "gbk"
        env["PYTHONUTF8"] = "0"
        env["WECOM_MCP_DB"] = str(database)
        proc = subprocess.Popen([sys.executable, "wecom_mcp.py"], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", env=env)
        next_id = 0

        def call(method, params=None):
            nonlocal next_id
            next_id += 1
            request = {"jsonrpc": "2.0", "id": next_id, "method": method}
            if params is not None:
                request["params"] = params
            proc.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
            proc.stdin.flush()
            response = json.loads(proc.stdout.readline())
            assert response["id"] == next_id, response
            assert "error" not in response, response
            return response["result"]

        try:
            init = call("initialize", {"protocolVersion": "2099-01-01", "capabilities": {}})
            assert init["protocolVersion"] == "2024-11-05"
            assert init["serverInfo"]["name"] == "wecom-local-messages"

            # One wire-level batch with a notification interleaved: only the two
            # requests receive responses, in request order.
            batch = [
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 3, "method": "ping"},
            ]
            next_id = 3
            proc.stdin.write(json.dumps(batch, ensure_ascii=False) + "\n")
            proc.stdin.flush()
            answers = json.loads(proc.stdout.readline())
            assert [answer["id"] for answer in answers] == [2, 3], answers
            tools = answers[0]["result"]["tools"]
            names = {tool["name"] for tool in tools}
            assert {"search_messages", "get_message", "list_conversations", "recent_messages", "count_messages"} <= names
            assert "sync_messages" not in names  # default read-only mode

            result = call("tools/call", {"name": "search_messages", "arguments": {"query": "我问问", "limit": 1}})
            payload = result["structuredContent"]
            assert (payload["count"], payload["total"], payload["has_more"]) == (1, 1, False), payload
            message = payload["messages"][0]
            assert "我问问" in message["content"]
            assert message["msgtype"] == 2 and message["type_name"] == "文本"
            assert message["flag"] == 0 and message["flag_bits"] == []
            assert message["mentions"] == [] and message["mention_all"] is False
            assert len(message["dedupe_key"]) == 64 and message["dup_count"] == 1
            full = call("tools/call", {"name": "get_message", "arguments": {"msgid": message["msgid"]}})["structuredContent"]
            assert full["msgid"] == message["msgid"]
            assert "我问问" in full["content"]
            conversations = call("tools/call", {"name": "list_conversations", "arguments": {"query": "孙乐乐", "limit": 5}})["structuredContent"]
            assert conversations["count"] == 1
            recent = call("tools/call", {"name": "recent_messages", "arguments": {"limit": 1}})["structuredContent"]
            assert (recent["count"], recent["total"], recent["next_offset"]) == (1, 2, 1)
            unfiltered = call("tools/call", {"name": "search_messages", "arguments": {"conversation_id": "S:100_999"}})["structuredContent"]
            assert unfiltered["total"] == 2
            counted = call("tools/call", {"name": "count_messages", "arguments": {"group_by": "msgtype"}})["structuredContent"]
            assert counted["total"] == 2
            assert counted["groups"] == [{"msgtype": 2, "type_name": "文本", "count": 2}]
            print("MCP_INITIALIZE=OK")
            print("MCP_JSON_RPC_BATCH=OK")
            print("MCP_TOOLS_LIST=OK")
            print("MCP_SEARCH_READ=OK")
            print("MCP_PORTABLE_CONVERSATIONS=OK")
            print("MCP_PAGINATION=OK")
            print("MCP_OPTIONAL_QUERY_AND_COUNT=OK")
            print("MCP_SUMMARY_V13=OK")
            print("MCP_GBK_ENV_UTF8_STDIO=OK")
            print("MCP_TEST_MESSAGE_ID=" + message["msgid"])
        finally:
            proc.stdin.close()
            proc.terminate()
            proc.wait(timeout=10)
            err = proc.stderr.read()
            if err:
                print("SERVER_LOG", err.strip())


if __name__ == "__main__":
    main()
