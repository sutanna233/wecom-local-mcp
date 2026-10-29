"""SQLite 落库、稳定消息身份迁移与群名缓存。零外部依赖。"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from urllib.parse import quote

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    msgid      TEXT PRIMARY KEY,
    seq        INTEGER,
    roomid     TEXT,
    sender     TEXT,
    sender_id  INTEGER,
    msgtime    INTEGER,
    msgtype    INTEGER,
    content    TEXT,
    raw        TEXT,
    fetched_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_messages_room_time ON messages(roomid, msgtime);
CREATE INDEX IF NOT EXISTS idx_messages_time_id ON messages(msgtime DESC, msgid DESC);

CREATE TABLE IF NOT EXISTS hits (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    msgid    TEXT NOT NULL,
    rule     TEXT NOT NULL,
    roomid   TEXT,
    msgtime  INTEGER,
    snippet  TEXT,
    pushed   INTEGER DEFAULT 0,
    UNIQUE(msgid, rule)
);

CREATE TABLE IF NOT EXISTS rooms (
    roomid     TEXT PRIMARY KEY,
    name       TEXT,
    updated_at INTEGER
);

CREATE TABLE IF NOT EXISTS state (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""

_MESSAGE_COLUMNS = (
    "msgid", "seq", "roomid", "sender", "sender_id", "msgtime",
    "msgtype", "content", "raw", "fetched_at",
)
_IDENTITY_VERSION = "2"


def _as_int(value, default=0):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


def _canonical_msgid(row, old_msgid=""):
    """与采集/导出共用的 v2 规则；server_id 必须结合会话命名空间。"""
    cid = str(row.get("conversation_id") or row.get("roomid") or "")
    server_id = _as_int(row.get("server_id"))
    if server_id and cid:
        return f"wxwork:v2:s:{quote(cid, safe='')}:{server_id}"

    table = str(row.get("source_table") or "")
    message_id = _as_int(row.get("message_id"))
    if not table and old_msgid.startswith("wxwork:") and old_msgid.count(":") == 2:
        _, table, local_id = old_msgid.split(":", 2)
        message_id = _as_int(local_id)
    if table and message_id:
        return f"wxwork:v2:l:{quote(table, safe='')}:{message_id}"

    # 异常旧数据没有本地 ID 时仍给出确定性身份，避免重新打开数据库反复迁移。
    fingerprint = json.dumps(
        [cid, _as_int(row.get("sequence") or row.get("seq")),
         _as_int(row.get("sender_id")), _as_int(row.get("send_time") or row.get("msgtime")),
         _as_int(row.get("content_type") or row.get("msgtype")),
         str(row.get("content") or ""), old_msgid],
        ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")
    return "wxwork:v2:f:" + hashlib.sha256(fingerprint).hexdigest()


def _nested_raw(raw_text):
    try:
        payload = json.loads(raw_text or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    nested = payload.get("raw")
    return nested if isinstance(nested, dict) else payload


def _source_priority(raw):
    return {"message_small_table": 1, "kf_message_tableV1": 2, "message_table": 3}.get(
        str(raw.get("source_table") or ""), 0
    )


class Store:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)
        self._db.commit()
        self._migrate_message_identity()

    # ---------- 旧库迁移 ----------
    def _migrate_message_identity(self) -> None:
        with self._lock:
            version = self._db.execute(
                "SELECT v FROM state WHERE k='messages_identity_version'"
            ).fetchone()
            columns = {row[1] for row in self._db.execute("PRAGMA table_info(messages)")}
            if version and version[0] == _IDENTITY_VERSION and "sender_id" in columns:
                return

            try:
                self._db.execute("BEGIN IMMEDIATE")
                selected = ["msgid", "seq", "roomid", "sender", "msgtime", "msgtype",
                            "content", "raw", "fetched_at"]
                if "sender_id" in columns:
                    selected.insert(4, "sender_id")
                old_rows = self._db.execute(
                    f"SELECT {','.join(selected)} FROM messages"
                ).fetchall()
                old_hits = self._db.execute(
                    "SELECT id,msgid,rule,roomid,msgtime,snippet,pushed FROM hits ORDER BY id"
                ).fetchall()

                winners = {}
                id_map = {}
                for values in old_rows:
                    item = dict(zip(selected, values))
                    raw = _nested_raw(item["raw"])
                    identity_row = dict(raw)
                    identity_row.setdefault("conversation_id", item["roomid"])
                    identity_row.setdefault("sequence", item["seq"])
                    identity_row.setdefault("send_time", item["msgtime"])
                    identity_row.setdefault("content_type", item["msgtype"])
                    identity_row.setdefault("content", item["content"])
                    new_id = _canonical_msgid(identity_row, item["msgid"])
                    id_map[item["msgid"]] = new_id
                    sender_id = item.get("sender_id")
                    if sender_id is None:
                        sender_id = _as_int(raw.get("sender_id"))
                    candidate = (
                        new_id, item["seq"], item["roomid"], item["sender"], sender_id,
                        item["msgtime"], item["msgtype"], item["content"], item["raw"],
                        item["fetched_at"],
                    )
                    rank = (_source_priority(raw), len(item["content"] or ""),
                            _as_int(item["fetched_at"]))
                    if new_id not in winners or rank >= winners[new_id][0]:
                        winners[new_id] = (rank, candidate)

                # hits 没有外键，显式随 canonical ID 迁移并合并重复规则。
                migrated_hits = {}
                for hit_id, old_id, rule, roomid, msgtime, snippet, pushed in old_hits:
                    new_id = id_map.get(old_id, old_id)
                    key = (new_id, rule)
                    value = [hit_id, new_id, rule, roomid, msgtime, snippet, int(pushed or 0)]
                    if key not in migrated_hits:
                        migrated_hits[key] = value
                    else:
                        previous = migrated_hits[key]
                        previous[0] = min(previous[0], hit_id)
                        previous[5] = previous[5] or snippet
                        previous[6] = max(previous[6], int(pushed or 0))

                self._db.execute("ALTER TABLE messages RENAME TO messages_identity_v1")
                self._db.execute("""
                    CREATE TABLE messages (
                        msgid TEXT PRIMARY KEY,
                        seq INTEGER,
                        roomid TEXT,
                        sender TEXT,
                        sender_id INTEGER,
                        msgtime INTEGER,
                        msgtype INTEGER,
                        content TEXT,
                        raw TEXT,
                        fetched_at INTEGER
                    )
                """)
                self._db.executemany(
                    "INSERT INTO messages (msgid,seq,roomid,sender,sender_id,msgtime,"
                    "msgtype,content,raw,fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    [entry[1] for entry in winners.values()],
                )
                self._db.execute("DROP TABLE messages_identity_v1")
                self._db.execute(
                    "CREATE INDEX idx_messages_room_time ON messages(roomid, msgtime)"
                )
                self._db.execute(
                    "CREATE INDEX idx_messages_time_id ON messages(msgtime DESC, msgid DESC)"
                )
                self._db.execute(
                    "CREATE INDEX idx_messages_sender_id ON messages(sender_id)"
                )
                self._db.execute("DELETE FROM hits")
                self._db.executemany(
                    "INSERT INTO hits (id,msgid,rule,roomid,msgtime,snippet,pushed) "
                    "VALUES (?,?,?,?,?,?,?)", list(migrated_hits.values())
                )
                self._db.execute(
                    "INSERT INTO state(k,v) VALUES('messages_identity_version',?) "
                    "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (_IDENTITY_VERSION,)
                )
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise

    # ---------- 消息 ----------
    @staticmethod
    def _message_params(msg: dict) -> tuple:
        raw = msg.get("raw") if isinstance(msg.get("raw"), dict) else {}
        sender_id = msg.get("sender_id")
        if sender_id is None:
            sender_id = raw.get("sender_id")
        return (
            msg.get("msgid"), _as_int(msg.get("seq")), msg.get("roomid"), msg.get("from"),
            _as_int(sender_id), _as_int(msg.get("msgtime")), _as_int(msg.get("msgtype")),
            msg.get("content"), json.dumps(msg, ensure_ascii=False), int(time.time()),
        )

    def save_messages(self, messages) -> int:
        """在一个事务中新增或更新消息，返回真正新增的数量。"""
        params = [self._message_params(msg) for msg in messages]
        if not params:
            return 0
        sql = """INSERT INTO messages
                 (msgid,seq,roomid,sender,sender_id,msgtime,msgtype,content,raw,fetched_at)
                 VALUES (?,?,?,?,?,?,?,?,?,?)
                 ON CONFLICT(msgid) DO UPDATE SET
                   seq=excluded.seq, roomid=excluded.roomid, sender=excluded.sender,
                   sender_id=excluded.sender_id, msgtime=excluded.msgtime,
                   msgtype=excluded.msgtype, content=excluded.content,
                   raw=excluded.raw, fetched_at=excluded.fetched_at"""
        with self._lock:
            existing = set()
            ids = list(dict.fromkeys(row[0] for row in params))
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                placeholders = ",".join("?" for _ in chunk)
                existing.update(
                    row[0] for row in self._db.execute(
                        f"SELECT msgid FROM messages WHERE msgid IN ({placeholders})", chunk
                    )
                )
            with self._db:
                self._db.executemany(sql, params)
        seen = set()
        inserted = 0
        for row in params:
            if row[0] not in existing and row[0] not in seen:
                inserted += 1
            seen.add(row[0])
        return inserted

    def save_message(self, msg: dict) -> bool:
        return self.save_messages([msg]) > 0

    def count(self) -> int:
        with self._lock:
            return int(self._db.execute("SELECT COUNT(*) FROM messages").fetchone()[0])

    def recent(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT msgid,roomid,sender,sender_id,msgtime,msgtype,content FROM messages "
                "ORDER BY msgtime DESC LIMIT ?", (limit,)).fetchall()
        keys = ("msgid", "roomid", "sender", "sender_id", "msgtime", "msgtype", "content")
        return [dict(zip(keys, r)) for r in rows]

    # ---------- 命中 ----------
    def save_hit(self, msgid: str, rule: str, roomid: str, msgtime: int, snippet: str) -> bool:
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO hits (msgid,rule,roomid,msgtime,snippet) VALUES (?,?,?,?,?)",
                (msgid, rule, roomid, msgtime, snippet))
            self._db.commit()
            return cur.rowcount > 0

    def mark_pushed(self, msgid: str, rule: str) -> None:
        with self._lock:
            self._db.execute("UPDATE hits SET pushed=1 WHERE msgid=? AND rule=?", (msgid, rule))
            self._db.commit()

    def pending_hits(self, limit: int = 200) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id,msgid,rule,roomid,msgtime,snippet FROM hits WHERE pushed=0 "
                "ORDER BY id LIMIT ?", (limit,)).fetchall()
        keys = ("id", "msgid", "rule", "roomid", "msgtime", "snippet")
        return [dict(zip(keys, r)) for r in rows]

    # ---------- 位点 ----------
    def get_state(self, k: str, default: str = "") -> str:
        with self._lock:
            row = self._db.execute("SELECT v FROM state WHERE k=?", (k,)).fetchone()
        return row[0] if row else default

    def set_state(self, k: str, v: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO state(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))
            self._db.commit()

    # ---------- 群名 ----------
    def room_name(self, roomid: str) -> str:
        with self._lock:
            row = self._db.execute("SELECT name FROM rooms WHERE roomid=?", (roomid,)).fetchone()
        return row[0] if row and row[0] else roomid

    def set_room_name(self, roomid: str, name: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO rooms(roomid,name,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(roomid) DO UPDATE SET name=excluded.name, updated_at=excluded.updated_at",
                (roomid, name, int(time.time())))
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()
