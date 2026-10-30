"""SQLite 落库、稳定消息身份迁移与群名缓存。零外部依赖。"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from collections import defaultdict
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
    flag       INTEGER NOT NULL DEFAULT 0,
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
    "msgtype", "flag", "content", "raw", "fetched_at",
)
_IDENTITY_VERSION = "2"
_ROOM_STATS_VERSION = "1"
_FTS_VERSION = "1"


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


def _room_kind(roomid):
    return {
        "R:": "群聊", "S:": "单聊", "M:": "多人会话",
        "O:": "应用/公众号", "Y:": "系统会话",
    }.get(str(roomid or "")[:2], "其他")


def _room_metadata(roomid, raw_text):
    raw = _nested_raw(raw_text)
    name = str(raw.get("display_name") or raw.get("conversation") or "")
    kind = str(raw.get("conversation_kind") or raw.get("kind") or _room_kind(roomid))
    return name, kind


class Store:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self._fts_available = False
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)
        self._db.commit()
        self._migrate_message_identity()
        self._migrate_room_stats()
        self._migrate_message_fts()

    # ---------- 旧库迁移 ----------
    def _migrate_message_identity(self) -> None:
        with self._lock:
            version = self._db.execute(
                "SELECT v FROM state WHERE k='messages_identity_version'"
            ).fetchone()
            columns = {row[1] for row in self._db.execute("PRAGMA table_info(messages)")}
            if (version and version[0] == _IDENTITY_VERSION
                    and {"sender_id", "flag"}.issubset(columns)):
                return

            try:
                self._db.execute("BEGIN IMMEDIATE")
                selected = ["msgid", "seq", "roomid", "sender", "msgtime", "msgtype"]
                if "flag" in columns:
                    selected.append("flag")
                selected.extend(["content", "raw", "fetched_at"])
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
                    flag = item.get("flag")
                    if flag is None:
                        flag = raw.get("flag")
                    candidate = (
                        new_id, item["seq"], item["roomid"], item["sender"], sender_id,
                        item["msgtime"], item["msgtype"], _as_int(flag),
                        item["content"], item["raw"], item["fetched_at"],
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
                        flag INTEGER NOT NULL DEFAULT 0,
                        content TEXT,
                        raw TEXT,
                        fetched_at INTEGER
                    )
                """)
                self._db.executemany(
                    "INSERT INTO messages (msgid,seq,roomid,sender,sender_id,msgtime,"
                    "msgtype,flag,content,raw,fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
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

    def _migrate_room_stats(self) -> None:
        with self._lock:
            columns = {row[1] for row in self._db.execute("PRAGMA table_info(rooms)")}
            version = self._db.execute(
                "SELECT v FROM state WHERE k='rooms_stats_version'"
            ).fetchone()
            with self._db:
                for name, ddl in (
                    ("message_count", "INTEGER NOT NULL DEFAULT 0"),
                    ("last_time", "INTEGER NOT NULL DEFAULT 0"),
                    ("kind", "TEXT NOT NULL DEFAULT ''"),
                ):
                    if name not in columns:
                        self._db.execute(f"ALTER TABLE rooms ADD COLUMN {name} {ddl}")
                self._db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_messages_sender_id ON messages(sender_id)"
                )
                self._db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_rooms_last_time "
                    "ON rooms(last_time DESC,roomid)"
                )
                if version and version[0] == _ROOM_STATS_VERSION:
                    return

                self._db.execute("UPDATE rooms SET message_count=0,last_time=0")
                rows = self._db.execute(
                    "SELECT roomid,COUNT(*),MAX(msgtime),raw FROM messages "
                    "WHERE roomid IS NOT NULL AND roomid<>'' GROUP BY roomid"
                ).fetchall()
                now = int(time.time())
                for roomid, count, last_time, raw_text in rows:
                    name, kind = _room_metadata(roomid, raw_text)
                    self._db.execute(
                        "INSERT INTO rooms(roomid,name,updated_at,message_count,last_time,kind) "
                        "VALUES (?,?,?,?,?,?) ON CONFLICT(roomid) DO UPDATE SET "
                        "message_count=excluded.message_count,last_time=excluded.last_time,"
                        "name=CASE WHEN excluded.name<>'' AND "
                        "(rooms.name IS NULL OR rooms.name='' OR rooms.name=rooms.roomid) "
                        "THEN excluded.name ELSE rooms.name END,"
                        "kind=excluded.kind",
                        (roomid, name, now, count, _as_int(last_time), kind),
                    )
                self._db.execute(
                    "INSERT INTO state(k,v) VALUES('rooms_stats_version',?) "
                    "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (_ROOM_STATS_VERSION,),
                )

    def _migrate_message_fts(self) -> None:
        with self._lock:
            exists = self._db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages_fts'"
            ).fetchone() is not None
            version = self._db.execute(
                "SELECT v FROM state WHERE k='messages_fts_version'"
            ).fetchone()
            if exists and version and version[0] == _FTS_VERSION:
                self._fts_available = True
                return
            try:
                with self._db:
                    if exists:
                        self._db.execute("DROP TABLE messages_fts")
                    self._db.execute(
                        "CREATE VIRTUAL TABLE messages_fts USING fts5("
                        "content,content='messages',content_rowid='rowid',tokenize='trigram')"
                    )
                    self._db.execute(
                        "INSERT INTO messages_fts(messages_fts) VALUES('rebuild')"
                    )
                    self._db.execute(
                        "INSERT INTO state(k,v) VALUES('messages_fts_version',?) "
                        "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (_FTS_VERSION,),
                    )
                self._fts_available = True
            except sqlite3.OperationalError:
                # 老 SQLite 没有 FTS5/trigram 时保留普通 instr 查询能力。
                self._db.rollback()
                self._fts_available = False

    # ---------- 消息 ----------
    @staticmethod
    def _message_params(msg: dict) -> tuple:
        raw = msg.get("raw") if isinstance(msg.get("raw"), dict) else {}
        sender_id = msg.get("sender_id")
        if sender_id is None:
            sender_id = raw.get("sender_id")
        flag = msg.get("flag")
        if flag is None:
            flag = raw.get("flag")
        return (
            msg.get("msgid"), _as_int(msg.get("seq")), msg.get("roomid"), msg.get("from"),
            _as_int(sender_id), _as_int(msg.get("msgtime")), _as_int(msg.get("msgtype")),
            _as_int(flag), msg.get("content"), json.dumps(msg, ensure_ascii=False), int(time.time()),
        )

    def _upsert_room_stats(self, roomid, raw_text, count, last_time, exact):
        name, kind = _room_metadata(roomid, raw_text)
        count_value = (
            "excluded.message_count" if exact
            else "max(0,rooms.message_count+excluded.message_count)"
        )
        time_value = (
            "excluded.last_time" if exact
            else "max(rooms.last_time,excluded.last_time)"
        )
        self._db.execute(
            "INSERT INTO rooms(roomid,name,updated_at,message_count,last_time,kind) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(roomid) DO UPDATE SET "
            f"message_count={count_value},last_time={time_value},"
            "name=CASE WHEN excluded.name<>'' AND "
            "(rooms.name IS NULL OR rooms.name='' OR rooms.name=rooms.roomid) "
            "THEN excluded.name ELSE rooms.name END,"
            "kind=excluded.kind",
            (roomid, name, int(time.time()), count, last_time, kind),
        )

    def _update_room_stats(self, latest, existing):
        deltas = defaultdict(int)
        candidates = {}
        recompute = set()
        metadata = {}
        for msgid, row in latest.items():
            new_room = str(row[2] or "")
            new_time = _as_int(row[5])
            if new_room and (new_room not in metadata or new_time >= metadata[new_room][0]):
                metadata[new_room] = (new_time, row[9])
            old = existing.get(msgid)
            old_room = str(old[2] or "") if old else ""
            old_time = _as_int(old[3]) if old else 0
            if old is None:
                if new_room:
                    deltas[new_room] += 1
                    candidates[new_room] = max(candidates.get(new_room, 0), new_time)
            elif old_room == new_room:
                if new_time < old_time and new_room:
                    recompute.add(new_room)
                elif new_room:
                    candidates[new_room] = max(candidates.get(new_room, 0), new_time)
            else:
                if old_room:
                    deltas[old_room] -= 1
                    recompute.add(old_room)
                if new_room:
                    deltas[new_room] += 1
                    candidates[new_room] = max(candidates.get(new_room, 0), new_time)

        for roomid in set(deltas) | set(candidates) | recompute | set(metadata):
            raw_text = metadata[roomid][1] if roomid in metadata else None
            if roomid in recompute:
                count, last_time = self._db.execute(
                    "SELECT COUNT(*),COALESCE(MAX(msgtime),0) FROM messages WHERE roomid=?",
                    (roomid,),
                ).fetchone()
                self._upsert_room_stats(roomid, raw_text, count, _as_int(last_time), True)
            else:
                self._upsert_room_stats(
                    roomid, raw_text, deltas[roomid], candidates.get(roomid, 0), False
                )

    def _delete_fts_rows(self, existing):
        if self._fts_available and existing:
            self._db.executemany(
                "INSERT INTO messages_fts(messages_fts,rowid,content) VALUES('delete',?,?)",
                [(row[0], row[4]) for row in existing.values()],
            )

    def _insert_fts_rows(self, ids):
        if not self._fts_available:
            return
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            placeholders = ",".join("?" for _ in chunk)
            rows = self._db.execute(
                f"SELECT rowid,content FROM messages WHERE msgid IN ({placeholders})",
                chunk,
            ).fetchall()
            self._db.executemany(
                "INSERT INTO messages_fts(rowid,content) VALUES(?,?)", rows
            )

    def save_messages(self, messages) -> int:
        """在一个事务中新增或更新消息，返回真正新增的数量。"""
        params = [self._message_params(msg) for msg in messages]
        if not params:
            return 0
        latest = {row[0]: row for row in params}
        sql = """INSERT INTO messages
                 (msgid,seq,roomid,sender,sender_id,msgtime,msgtype,flag,content,raw,fetched_at)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?)
                 ON CONFLICT(msgid) DO UPDATE SET
                   seq=excluded.seq, roomid=excluded.roomid, sender=excluded.sender,
                   sender_id=excluded.sender_id, msgtime=excluded.msgtime,
                   msgtype=excluded.msgtype, flag=excluded.flag, content=excluded.content,
                   raw=excluded.raw, fetched_at=excluded.fetched_at"""
        with self._lock:
            existing = {}
            ids = list(latest)
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                placeholders = ",".join("?" for _ in chunk)
                existing.update(
                    (row[1], row)
                    for row in self._db.execute(
                        "SELECT rowid,msgid,roomid,msgtime,content FROM messages "
                        f"WHERE msgid IN ({placeholders})", chunk
                    )
                )
            with self._db:
                self._delete_fts_rows(existing)
                self._db.executemany(sql, params)
                self._insert_fts_rows(ids)
                self._update_room_stats(latest, existing)
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
                "SELECT msgid,roomid,sender,sender_id,msgtime,msgtype,flag,content FROM messages "
                "ORDER BY msgtime DESC LIMIT ?", (limit,)).fetchall()
        keys = ("msgid", "roomid", "sender", "sender_id", "msgtime", "msgtype", "flag", "content")
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
