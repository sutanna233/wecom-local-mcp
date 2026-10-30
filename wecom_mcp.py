"""Local stdio MCP server for searching the verified WeCom message store.

Transport: newline-delimited JSON-RPC 2.0 over stdin/stdout (MCP stdio).
No TCP listener, external upload, or notification channel is created.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
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
SERVER_VERSION = "1.4.0"
PROTOCOL_VERSION = "2024-11-05"
MSG_TYPES = {
    0: "文本/混合",
    2: "文本",
    4: "图片",
    7: "语音",
    13: "在线表格",
    14: "图片",
    15: "图片/文件",
    20: "媒体消息",
    29: "表情",
    35: "链接分享",
    36: "微信/外部消息",
    38: "应用消息",
    40: "通话/音视频",
    123: "混合消息",
    503: "状态",
    1002: "@提及",
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


def _normalized_content(value: Any) -> str:
    """Trim and collapse whitespace for ledger-duplicate detection."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    return " ".join(text.split())


def message_dedupe_key(row: sqlite3.Row) -> str:
    """Hash conversation + second + normalized body, never server identity.

    Distinct canonical msgids can still be duplicate ledger entries. Keeping
    this key independent from server_id/message_id makes those copies visible.
    """
    value = json.dumps(
        [str(row["roomid"] or ""), _integer(row["msgtime"]), _normalized_content(row["content"])],
        ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _flag_bits(flag: int) -> list[int]:
    """Return zero-based positions of set bits (for example, 5 -> [0, 2])."""
    return [bit for bit in range(max(0, flag).bit_length()) if flag & (1 << bit)]


def _as_list(value: Any) -> list:
    if value is None or value == "":
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def _is_mention_all(value: Any) -> bool:
    text = str(value or "").strip().casefold().lstrip("@").replace(" ", "")
    return text in {"all", "everyone", "所有人", "全体成员"}


_TEXT_MENTION_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])@([A-Za-z0-9_\-\u4e00-\u9fff]+"
    r"(?:/[A-Za-z0-9_\-]+(?: [A-Za-z0-9_\-]+)*)?)"
)


def _text_mentions(content: str) -> list[str]:
    """Extract display names from ordinary @text, excluding email-like forms."""
    if not content:
        return []
    return [match.group(1).strip() for match in _TEXT_MENTION_RE.finditer(content)]


def _mention_summary(raw: dict, content: str, msgtype: int) -> tuple[list, list, list[dict], bool]:
    ids = _as_list(raw.get("at_user_id"))
    names = _as_list(raw.get("at_user"))
    mentions = []
    for index in range(max(len(ids), len(names))):
        user_id = ids[index] if index < len(ids) else None
        name = names[index] if index < len(names) else ""
        # Tolerate old hand-built raw values containing {user_id, name} objects.
        if isinstance(user_id, dict):
            item = user_id
            user_id = item.get("user_id", item.get("id"))
            if not name:
                name = item.get("name", item.get("at_user", ""))
        if isinstance(name, dict):
            item = name
            name = item.get("name", item.get("at_user", ""))
            if user_id is None:
                user_id = item.get("user_id", item.get("id"))
        if user_id is None and name in (None, ""):
            continue
        mentions.append({"user_id": user_id, "name": name or ""})
    for name in _text_mentions(content):
        if _is_mention_all(name):
            continue
        if not any(str(item.get("name") or "").lstrip("@") == name for item in mentions):
            mentions.append({"user_id": None, "name": "@" + name})
    mention_all = any(_is_mention_all(value) for value in [*ids, *names])
    if not mention_all:
        mention_all = any(_is_mention_all(name) for name in _text_mentions(content))
    return ids, names, mentions, mention_all


def summary(row: sqlite3.Row, content_limit: int = 5000) -> dict:
    raw = nested_raw(row)
    content = row["content"] or ""
    timestamp = _integer(row["msgtime"])
    msgtype = _integer(row["msgtype"])
    sender_id = _row_value(row, "sender_id")
    if sender_id in (None, ""):
        sender_id = raw.get("sender_id")
    flag = _row_value(row, "flag")
    if flag in (None, ""):
        flag = raw.get("flag")
    flag = _integer(flag)
    at_user_id, at_user, mentions, mention_all = _mention_summary(raw, content, msgtype)
    result = {
        "msgid": row["msgid"],
        "conversation_id": row["roomid"],
        "conversation": raw.get("display_name") or raw.get("conversation") or raw.get("conversation_id") or row["roomid"],
        "sender": row["sender"] or "",
        "sender_id": sender_id,
        "msgtime": timestamp,
        "time": raw.get("time") or format_time(timestamp),
        "msgtype": msgtype,
        "type_name": raw.get("type_name") or message_type_name(msgtype),
        "content": content[:content_limit],
        "flag": flag,
        "flag_bits": _flag_bits(flag),
        "at_user_id": at_user_id,
        "at_user": at_user,
        "mentions": mentions,
        "mention_all": mention_all,
        "dedupe_key": message_dedupe_key(row),
        "dup_count": max(1, _integer(_row_value(row, "dup_count"), 1)),
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
    legacy = f"COALESCE({_raw_value('$.raw.sender_id')}, {_raw_value('$.sender_id')})"
    if "sender_id" in columns:
        return f"COALESCE(sender_id,{legacy})"
    return legacy


def _flag_expression(columns: set[str]) -> str:
    legacy = f"COALESCE({_raw_value('$.raw.flag')}, {_raw_value('$.flag')})"
    value = f"COALESCE(flag,{legacy})" if "flag" in columns else legacy
    return f"CAST(COALESCE({value},0) AS INTEGER)"


def _message_select(columns: set[str], *, full: bool = False, source: str = " FROM messages") -> str:
    fields = ["msgid", "roomid", "sender", "msgtime", "msgtype", "content", "raw"]
    if full:
        fields.extend([
            "seq" if "seq" in columns else "NULL AS seq",
            "fetched_at" if "fetched_at" in columns else "NULL AS fetched_at",
        ])
    fields.extend([
        f"{_sender_id_expression(columns)} AS sender_id",
        f"{_flag_expression(columns)} AS flag",
    ])
    return "SELECT " + ",".join(fields) + source


def _int_argument(args: dict, name: str, default: Any = None) -> int | None:
    value = args.get(name, default)
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer") from None


def _message_where(args: dict, columns: set[str]) -> tuple[str, list]:
    clauses, params = [], []
    query = str(args.get("query") or "")
    if query.strip():
        # Literal substring semantics: LIKE wildcards in a query stay literal.
        clauses.append("instr(lower(COALESCE(content,'')),lower(?)) > 0")
        params.append(query)
    if args.get("conversation_id"):
        clauses.append("roomid = ?")
        params.append(args["conversation_id"])
    sender_needle = str(args.get("sender") or "")
    if sender_needle:
        sender_id = _sender_id_expression(columns)
        clauses.append(
            "(instr(lower(COALESCE(sender,'')),lower(?)) > 0 "
            f"OR CAST({sender_id} AS TEXT) = ?)"
        )
        params.extend([sender_needle, sender_needle])
    msgtype = _int_argument(args, "msgtype")
    if msgtype is not None:
        clauses.append("CAST(msgtype AS INTEGER) = ?")
        params.append(msgtype)
    start = parse_time(args.get("start_time"))
    end = parse_time(args.get("end_time"), is_end=True)
    if start is not None:
        clauses.append("msgtime >= ?")
        params.append(start)
    if end is not None:
        clauses.append("msgtime < ?")
        params.append(end)
    flag_mask = _int_argument(args, "flag_mask")
    flag_value = _int_argument(args, "flag_value")
    if flag_mask is not None:
        clauses.append(f"(({_flag_expression(columns)}) & ?) = ?")
        params.extend([flag_mask, 0 if flag_value is None else flag_value])
    elif flag_value is not None:
        raise ValueError("flag_value requires flag_mask")
    return (" WHERE " + " AND ".join(clauses) if clauses else ""), params


def _trigram_fts_available(con: sqlite3.Connection) -> bool:
    row = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='messages_fts'"
    ).fetchone()
    return bool(row and row["sql"] and "trigram" in row["sql"].casefold())


def _message_search_source(con: sqlite3.Connection, query: str) -> tuple[str, list]:
    """Use the external-content trigram rowid mapping for long substrings.

    The original instr predicate remains in _message_where as a correctness
    filter. Short queries and legacy stores retain their full-table fallback.
    """
    plain = " FROM messages"
    if not query.strip() or len(query) < 3 or not _trigram_fts_available(con):
        return plain, []
    match = '"' + query.replace('"', '""') + '"'
    source = (
        " FROM messages JOIN ("
        "SELECT rowid AS fts_rowid FROM messages_fts WHERE messages_fts MATCH ?"
        ") AS fts ON fts.fts_rowid=messages.rowid"
    )
    return source, [match]


def _populate_dup_counts(con: sqlite3.Connection, rows: list[sqlite3.Row], items: list[dict]) -> None:
    """Count distinct msgids for page keys without decoding the full raw store."""
    pairs = list(dict.fromkeys(
        (str(row["roomid"] or ""), _integer(row["msgtime"])) for row in rows
    ))
    if not pairs:
        return
    clauses = " OR ".join("(roomid = ? AND msgtime = ?)" for _ in pairs)
    params = [value for pair in pairs for value in pair]
    candidates = con.execute(
        "SELECT msgid,roomid,msgtime,content FROM messages "
        f"WHERE {clauses}",
        params,
    ).fetchall()
    requested = {item["dedupe_key"] for item in items}
    grouped: dict[str, set] = {}
    for candidate in candidates:
        key = message_dedupe_key(candidate)
        if key in requested:
            grouped.setdefault(key, set()).add(candidate["msgid"])
    for row, item in zip(rows, items):
        item["dup_count"] = len(grouped.get(item["dedupe_key"], {row["msgid"]}))


def _summaries(con: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[dict]:
    items = [summary(row) for row in rows]
    _populate_dup_counts(con, rows, items)
    return items


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


def _cached_conversations(con: sqlite3.Connection) -> list[dict] | None:
    if not _table_exists(con, "rooms"):
        return None
    columns = _columns(con, "rooms")
    if not {"message_count", "last_time"} <= columns:
        return None
    rows = con.execute(
        "SELECT roomid,name,kind,message_count,last_time FROM rooms "
        "WHERE message_count>0 ORDER BY last_time DESC,roomid DESC"
    ).fetchall()
    aggregated = []
    for row in rows:
        cid = row["roomid"] or ""
        display = row["name"] or cid
        if cid.startswith(("M:", "S:")) and not _usable_contact_name(display, cid):
            display = _incoming_contact_name(con, cid) or f"未知联系人 ({cid})"
        timestamp = _integer(row["last_time"])
        aggregated.append({
            "conversation_id": cid,
            "conversation": display,
            "kind": row["kind"] or _conversation_kind(cid),
            "message_count": _integer(row["message_count"]),
            "last_time": format_time(timestamp),
            "last_timestamp": timestamp,
        })
    return aggregated


def list_conversations(args: dict) -> dict:
    query = str(args.get("query") or "").casefold()
    limit = clamp_limit(args.get("limit"), 50, 200)
    offset = clamp_offset(args.get("offset"))
    with closing(connect()) as con:
        aggregated = _cached_conversations(con)
        if aggregated is None:
            # Legacy stores aggregate once; only the latest raw value per room
            # is decoded for display metadata.
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
    limit = clamp_limit(args.get("limit"), 20, 100)
    offset = clamp_offset(args.get("offset"))
    with closing(connect()) as con:
        columns = _columns(con)
        where, params = _message_where(args, columns)
        source, source_params = _message_search_source(con, query)

        def execute_query(from_sql: str, from_params: list) -> tuple[int, list[sqlite3.Row]]:
            query_params = [*from_params, *params]
            total = con.execute(
                "SELECT COUNT(*)" + from_sql + where, query_params
            ).fetchone()[0]
            rows = con.execute(
                _message_select(columns, source=from_sql)
                + where + " ORDER BY msgtime DESC,msgid DESC LIMIT ? OFFSET ?",
                [*query_params, limit, offset],
            ).fetchall()
            return total, rows

        try:
            total, rows = execute_query(source, source_params)
        except sqlite3.OperationalError:
            if not source_params:
                raise
            # A stale/corrupt optional index must preserve legacy availability.
            total, rows = execute_query(" FROM messages", [])
        found = _summaries(con, rows)
    result = _page(found, total, offset, "messages")
    result["query"] = query
    return result


def recent_messages(args: dict) -> dict:
    limit = clamp_limit(args.get("limit"), 20, 100)
    offset = clamp_offset(args.get("offset"))
    with closing(connect()) as con:
        columns = _columns(con)
        where, params = _message_where(args, columns)
        total = con.execute("SELECT COUNT(*) FROM messages" + where, params).fetchone()[0]
        rows = con.execute(
            _message_select(columns) + where + " ORDER BY msgtime DESC,msgid DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        found = _summaries(con, rows)
    return _page(found, total, offset, "messages")


_COUNT_DIMENSIONS = {"conversation", "sender", "msgtype", "day"}


def _count_dimensions(value: Any) -> list[str]:
    if value is None:
        return ["conversation"]
    if isinstance(value, str):
        values = value.split(",")
    elif isinstance(value, (list, tuple)):
        values = value
    else:
        raise ValueError("group_by must be a string or an array of strings")
    dimensions = []
    for item in values:
        if not isinstance(item, str):
            raise ValueError("group_by must contain only strings")
        name = item.strip()
        if name not in _COUNT_DIMENSIONS:
            raise ValueError("group_by supports conversation, sender, msgtype, day")
        if name not in dimensions:
            dimensions.append(name)
    if not dimensions:
        raise ValueError("group_by must not be empty")
    return dimensions


def count_messages(args: dict) -> dict:
    """Read-only counts, optionally grouped by one or more local dimensions."""
    dimensions = _count_dimensions(args.get("group_by"))
    with closing(connect()) as con:
        columns = _columns(con)
        where, params = _message_where(args, columns)
        total = con.execute("SELECT COUNT(*) FROM messages" + where, params).fetchone()[0]
        selected = []
        for name in dimensions:
            if name == "conversation":
                selected.append("roomid AS conversation_id")
            elif name == "sender":
                selected.extend([
                    "COALESCE(sender,'') AS sender",
                    f"{_sender_id_expression(columns)} AS sender_id",
                ])
            elif name == "msgtype":
                selected.append("CAST(msgtype AS INTEGER) AS msgtype")
            elif name == "day":
                selected.append("date(CAST(msgtime AS INTEGER),'unixepoch','localtime') AS day")
        aliases = [part.rsplit(" AS ", 1)[1] for part in selected]
        order = ",".join(aliases)
        rows = con.execute(
            "SELECT " + ",".join(selected) + ",COUNT(*) AS count FROM messages"
            + where + " GROUP BY " + ",".join(aliases)
            + " ORDER BY count DESC" + ("," + order if order else ""),
            params,
        ).fetchall()
    groups = []
    for row in rows:
        item = {name: row[name] for name in aliases}
        if "msgtype" in item:
            item["type_name"] = message_type_name(item["msgtype"])
        item["count"] = row["count"]
        groups.append(item)
    return {"total": total, "count": total, "group_by": dimensions, "groups": groups}


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
        _populate_dup_counts(con, [row], [result])
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
_FLAG_MASK_DESCRIPTION = "按 (flag & flag_mask) == flag_value 过滤；flag_value 省略时为 0。"
TOOLS = [
    {"name": "list_conversations", "description": "列出本地已同步的企业微信会话及消息数量。",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 200}, "offset": {"type": "integer", "default": 0, "minimum": 0}}}},
    {"name": "search_messages", "description": "查询本地企业微信消息；query 可省略，保留会话、发送人、类型、时间及 flag 过滤。",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "conversation_id": {"type": "string"}, "sender": {"type": "string"}, "msgtype": {"type": "integer"}, "start_time": {"type": ["integer", "string"], "description": _TIME_START_DESCRIPTION}, "end_time": {"type": ["integer", "string"], "description": _TIME_END_DESCRIPTION}, "flag_mask": {"type": "integer", "description": _FLAG_MASK_DESCRIPTION}, "flag_value": {"type": "integer", "default": 0}, "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 100}, "offset": {"type": "integer", "default": 0, "minimum": 0}}}},
    {"name": "recent_messages", "description": "读取本地最近消息，支持会话、半开时间窗、flag 过滤和分页。",
     "inputSchema": {"type": "object", "properties": {"conversation_id": {"type": "string"}, "sender": {"type": "string"}, "msgtype": {"type": "integer"}, "start_time": {"type": ["integer", "string"], "description": _TIME_START_DESCRIPTION}, "end_time": {"type": ["integer", "string"], "description": _TIME_END_DESCRIPTION}, "flag_mask": {"type": "integer", "description": _FLAG_MASK_DESCRIPTION}, "flag_value": {"type": "integer", "default": 0}, "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 100}, "offset": {"type": "integer", "default": 0, "minimum": 0}}}},
    {"name": "count_messages", "description": "只读统计本地消息，按 conversation、sender、msgtype、day 的一个或多个维度分组。",
     "inputSchema": {"type": "object", "properties": {"group_by": {"type": ["string", "array"], "items": {"type": "string", "enum": ["conversation", "sender", "msgtype", "day"]}, "default": "conversation"}, "query": {"type": "string"}, "conversation_id": {"type": "string"}, "sender": {"type": "string"}, "msgtype": {"type": "integer"}, "start_time": {"type": ["integer", "string"], "description": _TIME_START_DESCRIPTION}, "end_time": {"type": ["integer", "string"], "description": _TIME_END_DESCRIPTION}, "flag_mask": {"type": "integer", "description": _FLAG_MASK_DESCRIPTION}, "flag_value": {"type": "integer", "default": 0}}}},
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
    if name == "count_messages":
        return count_messages(args)
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
