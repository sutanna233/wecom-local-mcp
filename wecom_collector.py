"""企业微信本地快照采集：跨表稳定身份、全量重扫与事务化更新。"""
from __future__ import annotations

import argparse
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


def table_exists(con: sqlite3.Connection, table: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def collect(decrypted: Path, store: Store, self_id: int | None) -> tuple[int, int]:
    """全量重扫以覆盖低位点晚到、内容变化和撤回标志；返回扫描数和新增数。"""
    message_db = decrypted / "message.db"
    convs = {c["conversation_id"]: c for c in export.discover_conversations(str(decrypted))}
    users = export._load_user_map(str(decrypted))
    members = export._load_group_member_names(str(decrypted))
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
            rows = con.execute(
                f'SELECT "{table}" AS source_table, message_id, server_id, sequence, '
                "sender_id, conversation_id, content_type, send_time, flag, "
                "content, extra_content, local_extra_content "
                f'FROM "{table}" ORDER BY message_id'
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--poll", type=float, default=15.0)
    ap.add_argument("--db", default=str(ROOT / "data" / "wecom_messages.sqlite"))
    args = ap.parse_args()
    if not args.once and not args.watch:
        args.once = True
    config_path = Path(os.environ.get("WECOM_CONFIG", str(VENDOR / "config.json")))
    db_dir, out_dir, keys = load_settings(config_path)
    store = Store(args.db)
    self_id = export._infer_self_id(str(db_dir))
    log_path = configure_logging()
    LOG.info("collector started; log=%s", log_path)
    first = True
    failures = 0
    try:
        while True:
            try:
                names = list(DEFAULT_NAMES) if first else ["message.db", "session.db", "message_lookup.db"]
                for name in names:
                    snapshot_one(name, db_dir, out_dir, keys)
                scanned, inserted = collect(out_dir, store, self_id)
                LOG.info("SYNC scanned=%d inserted=%d stored=%d", scanned, inserted, store.count())
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
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
