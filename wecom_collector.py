"""企业微信本地快照采集：跨表稳定身份、全量重扫与事务化更新。"""
from __future__ import annotations

import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENDOR = ROOT / "third_party" / "wecom-decrypt"
sys.path.insert(0, str(VENDOR))

import export_wxwork_messages as export  # noqa: E402
from store import Store  # noqa: E402
from wecom_snapshot import DEFAULT_NAMES, load_settings, open_readonly, snapshot_one  # noqa: E402

TABLES = ("message_table", "message_small_table", "kf_message_tableV1")
WATCH_NAMES = ("message.db", "session.db", "message_lookup.db")
_SOURCE_PRIORITY = {"message_small_table": 1, "kf_message_tableV1": 2, "message_table": 3}
LOG = logging.getLogger("wecom_collector")


def configure_logging() -> Path:
    log_dir = ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "collector.log"
    if not LOG.handlers:
        handler = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        LOG.addHandler(handler)
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        LOG.addHandler(console)
    LOG.setLevel(logging.INFO)
    return path


class CollectorAlreadyRunning(RuntimeError):
    def __init__(self, pid=0):
        super().__init__(f"collector already running; pid={pid or 'unknown'}")
        self.pid = pid


class CollectorInstance:
    """当前用户目录级互斥；锁持有期间原子发布 PID 与心跳。"""
    def __init__(self, log_dir=ROOT / "logs"):
        self.log_dir = Path(log_dir)
        self.lock_path = self.log_dir / "collector.lock"
        self.pid_path = self.log_dir / "collector.pid"
        self.started_at = int(time.time())
        self._handle = None

    def _read_pid(self):
        try:
            return int(json.loads(self.pid_path.read_text(encoding="utf-8")).get("pid") or 0)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return 0

    def acquire(self):
        self.log_dir.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise CollectorAlreadyRunning(self._read_pid()) from exc
        self._handle = handle
        self.heartbeat()
        return self

    def heartbeat(self):
        if self._handle is None:
            return
        payload = {
            "pid": os.getpid(), "started_at": self.started_at,
            "heartbeat": int(time.time()),
        }
        staging = self.pid_path.with_suffix(".pid.tmp")
        staging.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        os.replace(staging, self.pid_path)

    def close(self):
        if self._handle is None:
            return
        try:
            if self._read_pid() == os.getpid():
                self.pid_path.unlink(missing_ok=True)
            self._handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None


def table_exists(con: sqlite3.Connection, table: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _incremental_conversations(decrypted, users, self_id):
    """只读取会话表；避免 discover_conversations 的全消息计数扫描。"""
    result = {}
    session_db = decrypted / "session.db"
    if not session_db.exists():
        return result
    con = export._open_db(str(session_db))
    try:
        if export._table_exists(con, "conversation_table"):
            for row in con.execute(
                "SELECT id,name,roomname_remark,last_message_time,last_message_id "
                "FROM conversation_table"
            ):
                cid = row["id"]
                if not cid:
                    continue
                result[cid] = {
                    "conversation_id": cid,
                    "display_name": row["roomname_remark"] or row["name"] or
                    export._name_from_conversation_id(cid, users, self_id),
                    "kind": export._conversation_kind(cid),
                }
    finally:
        con.close()
    return result


def collect(decrypted: Path, store: Store, self_id: int | None,
            full: bool = True, lookback: int = 512) -> tuple[int, int]:
    """全量或高位点增量采集；lookback 覆盖近期本地 ID 的内容/撤回更新。"""
    message_db = decrypted / "message.db"
    users = export._load_user_map(str(decrypted))
    members = export._load_group_member_names(str(decrypted))
    if full:
        convs = {c["conversation_id"]: c for c in export.discover_conversations(str(decrypted))}
    else:
        convs = _incremental_conversations(decrypted, users, self_id)
    lookback = max(0, int(lookback))
    scanned = 0
    pending = {}
    priorities = {}
    highwater = {table: int(store.get_state(f"wxwork_cursor::{table}", "0") or 0)
                 for table in TABLES}

    con = open_readonly(message_db)
    con.row_factory = sqlite3.Row
    try:
        for table in TABLES:
            if not table_exists(con, table):
                continue
            where = "" if full else "WHERE message_id > ?"
            params = () if full else (max(0, highwater[table] - lookback),)
            rows = con.execute(
                f'SELECT "{table}" AS source_table, message_id, server_id, sequence, '
                "sender_id, conversation_id, content_type, send_time, flag, "
                "content, extra_content, local_extra_content "
                f'FROM "{table}" {where} ORDER BY message_id',
                params,
            )
            for row in rows:
                scanned += 1
                msgid = export.canonical_msgid(row)
                msg = export._build_message(row, convs, users, members, self_id)
                msg["msgid"] = msgid
                stored = {
                    "msgid": msgid,
                    "seq": int(row["sequence"] or 0),
                    "roomid": msg["conversation_id"],
                    "from": msg["sender"],
                    "sender_id": int(row["sender_id"] or 0),
                    "msgtime": int(row["send_time"] or 0),
                    "msgtype": int(row["content_type"] or 0),
                    "flag": int(row["flag"] or 0),
                    "content": msg["display_content"],
                    "raw": msg,
                }
                priority = _SOURCE_PRIORITY.get(table, 0)
                # 跨表同一身份只落一条；主表内容优先，同级后扫描的本地版本优先。
                if msgid not in pending or priority >= priorities[msgid]:
                    pending[msgid] = stored
                    priorities[msgid] = priority
                highwater[table] = max(highwater[table], int(row["message_id"] or 0))
    finally:
        con.close()

    inserted = store.save_messages(pending.values())
    for table in TABLES:
        store.set_state(f"wxwork_cursor::{table}", str(highwater[table]))
    return scanned, inserted


def _snapshot_signature(results):
    signature = []
    for name in WATCH_NAMES:
        result = results.get(name)
        if not isinstance(result, dict) or not result.get("sha256"):
            return None
        signature.append((name, result["sha256"]))
    return tuple(signature)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--poll", type=float, default=15.0)
    ap.add_argument("--db", default=str(ROOT / "data" / "wecom_messages.sqlite"))
    ap.add_argument("--lookback", type=int, default=512)
    ap.add_argument("--full-interval", type=float, default=3600.0)
    args = ap.parse_args()
    if not args.once and not args.watch:
        args.once = True
    config_path = Path(os.environ.get("WECOM_CONFIG", str(VENDOR / "config.json")))
    log_path = configure_logging()
    instance = CollectorInstance()
    try:
        instance.acquire()
    except CollectorAlreadyRunning as exc:
        LOG.warning("%s", exc)
        return 0
    store = None
    try:
        db_dir, out_dir, keys = load_settings(config_path)
        store = Store(args.db)
        self_id = export._infer_self_id(str(db_dir))
        LOG.info("collector started; log=%s", log_path)
        first = True
        failures = 0
        last_signature = None
        next_full = 0.0
        try:
            while True:
                try:
                    instance.heartbeat()
                    names = list(DEFAULT_NAMES) if first else list(WATCH_NAMES)
                    results = {name: snapshot_one(name, db_dir, out_dir, keys) for name in names}
                    signature = _snapshot_signature(results)
                    now = time.monotonic()
                    full = first or now >= next_full
                    if signature is not None and signature == last_signature:
                        LOG.info("SYNC skipped; source unchanged")
                    else:
                        scanned, inserted = collect(
                            out_dir, store, self_id, full=full, lookback=args.lookback
                        )
                        if full:
                            next_full = now + max(0.0, args.full_interval)
                        last_signature = signature
                        LOG.info(
                            "SYNC scanned=%d inserted=%d stored=%d full=%s",
                            scanned, inserted, store.count(), full,
                        )
                    first = False
                    failures = 0
                    if args.once:
                        break
                    time.sleep(max(1.0, args.poll))
                except KeyboardInterrupt:
                    raise
                except Exception:
                    failures += 1
                    LOG.exception("sync failed (%d consecutive); collector will retry", failures)
                    if args.once:
                        raise
                    time.sleep(min(60.0, max(2.0, 2.0 ** min(failures, 6))))
        except KeyboardInterrupt:
            LOG.info("STOPPED")
    finally:
        if store is not None:
            store.close()
        instance.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
