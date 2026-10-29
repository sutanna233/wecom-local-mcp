"""Local stdio MCP server for searching the verified WeCom message store.

Transport: newline-delimited JSON-RPC 2.0 over stdin/stdout (MCP stdio).
No TCP listener, external upload, or notification channel is created.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("WECOM_MCP_DB", str(ROOT / "data" / "wecom_messages.sqlite")))
SYNC_CONFIG = Path(os.environ.get("WECOM_CONFIG", str(ROOT / "third_party" / "wecom-decrypt" / "config.json")))
READ_ONLY = os.environ.get("WECOM_MCP_READ_ONLY", "1").lower() not in {"0", "false", "no"}
SERVER_NAME = "wecom-local-messages"
SERVER_VERSION = "1.2.0"
PROTOCOL_VERSION = "2024-11-05"
MSG_TYPES = {
    0: "文本/混合",
    2: "文本",
    4: "图片",
    7: "语音",
    15: "图片/文件",
    38: "应用消息",
    40: "通话/音视频",
    503: "状态",
    1011: "会议通知",
}
_sync_lock = threading.Lock()


def log(message: str) -> None:
    data = (message + "\n").encode("utf-8", errors="replace")
    stream = getattr(sys.stderr, "buffer", None)
    if stream is not None:
        stream.write(data)
        stream.flush()
    else:
        sys.stderr.write(data.decode("utf-8"))
        sys.stderr.flush()


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def parse_time(value: Any, *, is_end: bool = False) -> int | None:
    """Parse Unix seconds/milliseconds or ISO-8601 into Unix seconds.

    Naive ISO values and date-only values use the server's local timezone. A
    date-only end denotes the whole local date and is advanced to the next
    midnight; callers always apply the resulting end as an exclusive bound.
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        result = int(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            result = int(float(text))
        except ValueError:
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                raise ValueError("time must be Unix seconds or an ISO-8601 string") from None
            # A date-only ISO string produces a naive midnight. Advancing the
            # naive datetime keeps the next midnight correct across DST jumps.
            if len(text) == 10 and is_end:
                parsed += timedelta(days=1)
            result = int(parsed.timestamp())
    else:
        raise ValueError("time must be Unix seconds or an ISO-8601 string")
    if result > 20_000_000_000:
        result //= 1000
    return result


def format_time(value: Any) -> str:
    try:
        timestamp = int(value or 0)
    except (TypeError, ValueError):
        return ""
    if timestamp > 20_000_000_000:
        timestamp //= 1000
    if timestamp <= 0:
        return ""
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")


def clamp_limit(value: Any, default: int = 20, maximum: int = 100) -> int:
    try:
        return max(1, min(maximum, int(value if value is not None else default)))
    except (TypeError, ValueError):
        raise ValueError("limit must be an integer") from None


def clamp_offset(value: Any) -> int:
    try:
        offset = int(value if value is not None else 0)
    except (TypeError, ValueError):
        raise ValueError("offset must be a non-negative integer") from None
    if offset < 0:
        raise ValueError("offset must be a non-negative integer")
    return offset


def nested_raw(row: sqlite3.Row) -> dict:
    try:
        value = json.loads(row["raw"] or "{}")
        if not isinstance(value, dict):
            return {}
        nested = value.get("raw")
        return nested if isinstance(nested, dict) else value
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _row_value(row: sqlite3.Row, key: str, default: Any = None) -> Any:
    return row[key] if key in row.keys() else default


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


def message_type_name(value: Any) -> str:
    numeric = _integer(value)
    return MSG_TYPES.get(numeric, f"未知({value if value not in (None, '') else numeric})")


def summary(row: sqlite3.Row, content_limit: int = 5000) -> dict:
    raw = nested_raw(row)
    content = row["content"] or ""
    timestamp = _integer(row["msgtime"])
    msgtype = _integer(row["msgtype"])
    sender_id = _row_value(row, "sender_id")
    if sender_id in (None, ""):
        sender_id = raw.get("sender_id")
    result = {
        "msgid": row["msgid"],
        "conversation_id": row["roomid"],
        "conversation": raw.get("display_name") or raw.get("conversation") or raw.get("conversation_id") or row["roomid"],
        "sender": row["sender"] or "",
        "sender_id": sender_id,
        "msgtime": timestamp,
        "time": raw.get("time") or format_time(timestamp),
        "msgtype": msgtype,
        "type_name": message_type_name(row["msgtype"]),
        "content": content[:content_limit],
    }
    if len(content) > content_limit:
        result["content_truncated"] = True
    return result


def _columns(con: sqlite3.Connection, table: str = "messages") -> set[str]:
    return {row["name"] for row in con.execute(f"PRAGMA table_info({table})")}


def _table_exists(con: sqlite3.Connection, table: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _raw_value(path: str) -> str:
    # json_valid prevents a malformed legacy raw value from breaking an entire
    # query. The path is always a server-defined constant, never user input.
    return f"CASE WHEN json_valid(raw) THEN json_extract(raw, '{path}') END"


def _sender_id_expression(columns: set[str]) -> str:
    if "sender_id" in columns:
        return "sender_id"
    return f"COALESCE({_raw_value('$.raw.sender_id')}, {_raw_value('$.sender_id')})"


def _message_select(columns: set[str], *, full: bool = False) -> str:
    fields = ["msgid", "roomid", "sender", "msgtime", "msgtype", "content", "raw"]
    if full:
        fields.extend([
            "seq" if "seq" in columns else "NULL AS seq",
            "fetched_at" if "fetched_at" in columns else "NULL AS fetched_at",
        ])
    fields.append(f"{_sender_id_expression(columns)} AS sender_id")
    return "SELECT " + ",".join(fields) + " FROM messages"


def _page(items: list[dict], total: int, offset: int, key: str) -> dict:
    has_more = offset + len(items) < total
    return {
        "count": len(items),
        "total": total,
        "offset": offset,
        "has_more": has_more,
        "next_offset": offset + len(items) if has_more else None,
        key: items,
    }


def _conversation_kind(conversation_id: str) -> str:
    if conversation_id.startswith("R:"):
        return "群聊"
    if conversation_id.startswith("S:"):
        return "单聊"
    if conversation_id.startswith("M:"):
        return "微信联系人"
    if conversation_id.startswith("O:"):
        return "应用/公众号"
    if conversation_id.startswith("Y:"):
        return "系统会话"
    return "其他"


def _usable_contact_name(value: Any, conversation_id: str) -> bool:
    text = str(value or "").strip()
    return bool(
        text
        and text not in {conversation_id, "我", "系统"}
        and not text.isdecimal()
        and not text.startswith("未知")
    )


def _incoming_contact_name(con: sqlite3.Connection, conversation_id: str) -> str | None:
    sent = f"COALESCE({_raw_value('$.raw.is_sent')}, {_raw_value('$.is_sent')})"
    # Old rows without is_sent can still contribute a plausible named sender;
    # explicit self-sent rows are excluded. No raw JSON is decoded in Python.
    rows = con.execute(
        "SELECT sender FROM messages WHERE roomid=? "
        "AND sender IS NOT NULL AND trim(sender)<>'' "
        "AND trim(sender) NOT IN ('我','系统') "
        f"AND ({sent} IS NULL OR lower(CAST({sent} AS TEXT)) IN ('0','false')) "
        "ORDER BY msgtime DESC,msgid DESC",
        (conversation_id,),
    )
    for row in rows:
        if _usable_contact_name(row["sender"], conversation_id):
            return str(row["sender"]).strip()
    return None


def list_conversations(args: dict) -> dict:
    # SQLite performs the full-history aggregation. Only one raw value per
    # conversation is decoded for display metadata, rather than every message.
    query = str(args.get("query") or "").casefold()
    limit = clamp_limit(args.get("limit"), 50, 200)
    offset = clamp_offset(args.get("offset"))
    with closing(connect()) as con:
        rows = con.execute(
            "WITH ranked AS ("
            "SELECT roomid,msgtime,msgid,raw,"
            "COUNT(*) OVER (PARTITION BY roomid) AS message_count,"
            "ROW_NUMBER() OVER (PARTITION BY roomid ORDER BY msgtime DESC,msgid DESC) AS rn "
            "FROM messages) "
            "SELECT roomid,msgtime,msgid,raw,message_count FROM ranked WHERE rn=1 "
            "ORDER BY msgtime DESC,msgid DESC"
        ).fetchall()
        room_names = {}
        if _table_exists(con, "rooms"):
            room_names = {
                row["roomid"]: row["name"]
                for row in con.execute("SELECT roomid,name FROM rooms WHERE name IS NOT NULL")
            }
        aggregated = []
        for row in rows:
            cid = row["roomid"] or ""
            raw = nested_raw(row)
            display = raw.get("display_name") or raw.get("conversation") or room_names.get(cid) or cid
            if display == cid and room_names.get(cid):
                display = room_names[cid]
            if cid.startswith(("M:", "S:")) and not _usable_contact_name(display, cid):
                display = _incoming_contact_name(con, cid) or f"未知联系人 ({cid})"
            kind = raw.get("conversation_kind") or raw.get("kind") or _conversation_kind(cid)
            timestamp = _integer(row["msgtime"])
            aggregated.append({
                "conversation_id": cid,
                "conversation": display,
                "kind": kind,
                "message_count": _integer(row["message_count"]),
                "last_time": format_time(timestamp),
                "last_timestamp": timestamp,
            })
    filtered = [
        item for item in aggregated
        if not query or query in f"{item['conversation']} {item['conversation_id']}".casefold()
    ]
    return _page(filtered[offset:offset + limit], len(filtered), offset, "conversations")


def search_messages(args: dict) -> dict:
    query = str(args.get("query") or "")
    if not query.strip():
        raise ValueError("query is required")
    limit = clamp_limit(args.get("limit"), 20, 100)
    offset = clamp_offset(args.get("offset"))
    start = parse_time(args.get("start_time"))
    end = parse_time(args.get("end_time"), is_end=True)
    clauses, params = [], []
    if args.get("conversation_id"):
        clauses.append("roomid = ?")
        params.append(args["conversation_id"])
    if start is not None:
        clauses.append("msgtime >= ?")
        params.append(start)
    if end is not None:
        clauses.append("msgtime < ?")
        params.append(end)
    # instr gives literal substring semantics (LIKE wildcards in a query stay
    # literal). SQLite lower handles ASCII case folding and preserves Chinese.
    clauses.append("instr(lower(COALESCE(content,'')),lower(?)) > 0")
    params.append(query)
    with closing(connect()) as con:
        columns = _columns(con)
        sender_needle = str(args.get("sender") or "")
        if sender_needle:
            sender_id = _sender_id_expression(columns)
            clauses.append(
                "(instr(lower(COALESCE(sender,'')),lower(?)) > 0 "
                f"OR CAST({sender_id} AS TEXT) = ?)"
            )
            params.extend([sender_needle, sender_needle])
        where = " WHERE " + " AND ".join(clauses)
        total = con.execute("SELECT COUNT(*) FROM messages" + where, params).fetchone()[0]
        rows = con.execute(
            _message_select(columns) + where + " ORDER BY msgtime DESC,msgid DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
    found = [summary(row) for row in rows]
    result = _page(found, total, offset, "messages")
    result["query"] = query
    return result


def recent_messages(args: dict) -> dict:
    limit = clamp_limit(args.get("limit"), 20, 100)
    offset = clamp_offset(args.get("offset"))
    clauses, params = [], []
    if args.get("conversation_id"):
        clauses.append("roomid = ?")
        params.append(args["conversation_id"])
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with closing(connect()) as con:
        columns = _columns(con)
        total = con.execute("SELECT COUNT(*) FROM messages" + where, params).fetchone()[0]
        rows = con.execute(
            _message_select(columns) + where + " ORDER BY msgtime DESC,msgid DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
    return _page([summary(row) for row in rows], total, offset, "messages")


def get_message(args: dict) -> dict:
    msgid = str(args.get("msgid") or "")
    if not msgid:
        raise ValueError("msgid is required")
    with closing(connect()) as con:
        columns = _columns(con)
        row = con.execute(_message_select(columns, full=True) + " WHERE msgid=?", (msgid,)).fetchone()
    if row is None:
        raise ValueError("message not found")
    result = summary(row, content_limit=1_000_000)
    result["seq"] = row["seq"]
    result["fetched_at"] = row["fetched_at"]
    result["raw"] = nested_raw(row)
    return result


def sync_messages(_args: dict) -> dict:
    # Explicitly enabled with WECOM_MCP_READ_ONLY=0; no background thread or
    # external service is created.
    if READ_ONLY:
        raise ValueError("sync_messages is disabled in read-only mode; set WECOM_MCP_READ_ONLY=0 on the local collector host")
    from wecom_collector import collect
    from wecom_snapshot import DEFAULT_NAMES, load_settings, snapshot_one
    from store import Store
    import export_wxwork_messages as export
    with _sync_lock:
        db_dir, out_dir, keys = load_settings(SYNC_CONFIG)
        for name in DEFAULT_NAMES:
            snapshot_one(name, db_dir, out_dir, keys)
        store = Store(str(DB_PATH))
        try:
            scanned, inserted = collect(out_dir, store, export._infer_self_id(str(db_dir)))
            total = store.count()
        finally:
            store.close()
    return {"scanned": scanned, "inserted": inserted, "stored": total, "database": str(DB_PATH)}


_TIME_START_DESCRIPTION = "起始时间（含）。Unix 秒/毫秒或 ISO-8601；date-only 和无偏移 ISO 按服务器本地时区。"
_TIME_END_DESCRIPTION = "结束时间（不含）。date-only 表示包含该本地日期整天，内部取次日午夜；带偏移 ISO 按偏移转换。"
TOOLS = [
    {"name": "list_conversations", "description": "列出本地已同步的企业微信会话及消息数量。",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 200}, "offset": {"type": "integer", "default": 0, "minimum": 0}}}},
    {"name": "search_messages", "description": "按正文关键词搜索本地企业微信消息，可限定会话、发送人名称/ID 和半开时间范围。",
     "inputSchema": {"type": "object", "required": ["query"], "properties": {"query": {"type": "string"}, "conversation_id": {"type": "string"}, "sender": {"type": "string"}, "start_time": {"type": ["integer", "string"], "description": _TIME_START_DESCRIPTION}, "end_time": {"type": ["integer", "string"], "description": _TIME_END_DESCRIPTION}, "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 100}, "offset": {"type": "integer", "default": 0, "minimum": 0}}}},
    {"name": "recent_messages", "description": "读取本地最近消息，可限定一个会话并分页。",
     "inputSchema": {"type": "object", "properties": {"conversation_id": {"type": "string"}, "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 100}, "offset": {"type": "integer", "default": 0, "minimum": 0}}}},
    {"name": "get_message", "description": "根据 search_messages 返回的 msgid 读取完整消息及解析后的原始字段。",
     "inputSchema": {"type": "object", "required": ["msgid"], "properties": {"msgid": {"type": "string"}}}},
    {"name": "sync_messages", "description": "显式刷新本地数据库和 WAL 快照，再增量写入消息库；只读原企微文件，不外发。",
     "inputSchema": {"type": "object", "properties": {}}},
]


def call_tool(name: str, args: dict) -> dict:
    if name == "list_conversations":
        return list_conversations(args)
    if name == "search_messages":
        return search_messages(args)
    if name == "recent_messages":
        return recent_messages(args)
    if name == "get_message":
        return get_message(args)
    if name == "sync_messages":
        return sync_messages(args)
    raise ValueError(f"unknown tool: {name}")


def response_result(value: dict) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}], "structuredContent": value}


def _error(rid: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _handle_single(request: Any) -> dict | None:
    if not isinstance(request, dict):
        return _error(None, -32600, "Invalid Request")
    if request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
        return _error(request.get("id"), -32600, "Invalid Request")
    method = request["method"]
    if "id" not in request:
        return None
    rid = request["id"]
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": "仅访问当前本机已同步的企业微信消息。先 search_messages，再用 msgid 调用 get_message。sync_messages 是显式本地刷新。",
        }}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method == "tools/list":
        available = [tool for tool in TOOLS if not READ_ONLY or tool["name"] != "sync_messages"]
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": available}}
    if method == "tools/call":
        params = request.get("params") or {}
        try:
            result = call_tool(params.get("name", ""), params.get("arguments") or {})
            return {"jsonrpc": "2.0", "id": rid, "result": response_result(result)}
        except Exception as exc:
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": str(exc)}], "isError": True,
            }}
    return _error(rid, -32601, "Method not found")


def handle(request: Any) -> dict | list | None:
    if isinstance(request, list):
        if not request:
            return _error(None, -32600, "Invalid Request")
        answers = []
        for item in request:
            # Nested batches are not JSON-RPC requests.
            answer = _error(None, -32600, "Invalid Request") if isinstance(item, list) else _handle_single(item)
            if answer is not None:
                answers.append(answer)
        return answers or None
    return _handle_single(request)


def main() -> int:
    log(f"{SERVER_NAME} {SERVER_VERSION}; stdio only; db={DB_PATH}")
    # MCP stdio is UTF-8 regardless of the Windows ANSI code page. Use the
    # binary streams so PYTHONIOENCODING=gbk or a Chinese Windows locale
    # cannot corrupt either incoming requests or outgoing JSON-RPC frames.
    stdin = getattr(sys.stdin, "buffer", sys.stdin)
    stdout = getattr(sys.stdout, "buffer", sys.stdout)
    for raw_line in stdin:
        line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
        if not line.strip():
            continue
        try:
            request = json.loads(line)
        except Exception as exc:
            answer = _error(None, -32700, str(exc))
        else:
            try:
                answer = handle(request)
            except Exception as exc:
                answer = _error(None, -32603, str(exc))
        if answer is not None:
            payload = json.dumps(answer, ensure_ascii=False, separators=(",", ":")) + "\n"
            if hasattr(stdout, "write") and stdout is not sys.stdout:
                stdout.write(payload.encode("utf-8"))
            else:
                stdout.write(payload)
            stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
