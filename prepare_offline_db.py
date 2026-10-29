"""Prepare a standalone, checked offline SQLite DB from a live DB/WAL pair.

Only the disposable copy is opened by SQLite for checkpointing. The source is
read with file copies and is never modified. WeCom ``message_*`` tables and the
local ``messages`` store schema are both supported.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sqlite3
import tempfile
import time
from pathlib import Path

from wecom_snapshot import (
    PAGE,
    SnapshotError,
    UnsupportedPageSize,
    check_db,
    checkpoint_copied_db,
    copy_stable_pair,
    open_readonly,
    replace_snapshot_file,
    validate_database_page_size,
    validate_wal_header,
)

LOG = logging.getLogger(__name__)
SQLITE_HEADER = b"SQLite format 3\x00"
WECOM_MESSAGE_TABLES = (
    "message_table",
    "message_small_table",
    "kf_message_tableV1",
)
WECOM_REQUIRED_COLUMNS = frozenset(
    {
        "message_id",
        "server_id",
        "sequence",
        "sender_id",
        "conversation_id",
        "content_type",
        "send_time",
        "flag",
        "content",
        "extra_content",
        "local_extra_content",
    }
)
LOCAL_REQUIRED_COLUMNS = frozenset(
    {
        "msgid",
        "seq",
        "roomid",
        "sender",
        "msgtime",
        "msgtype",
        "content",
        "raw",
        "fetched_at",
    }
)


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    # Table names come only from the constants above, never from CLI input.
    return {row[1] for row in con.execute(f'PRAGMA table_info("{table}")')}


def validate_messages_schema(con: sqlite3.Connection) -> dict:
    """Require at least one known messages table and all columns used by readers."""
    tables = {
        row[0]
        for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    wecom_tables = [table for table in WECOM_MESSAGE_TABLES if table in tables]
    if wecom_tables:
        checked = []
        for table in wecom_tables:
            missing = sorted(WECOM_REQUIRED_COLUMNS - _columns(con, table))
            if missing:
                raise SnapshotError(
                    f"messages schema {table} missing columns: {', '.join(missing)}"
                )
            checked.append(table)
        return {"kind": "wecom", "tables": checked}
    if "messages" in tables:
        missing = sorted(LOCAL_REQUIRED_COLUMNS - _columns(con, "messages"))
        if missing:
            raise SnapshotError(
                f"messages schema messages missing columns: {', '.join(missing)}"
            )
        return {"kind": "local", "tables": ["messages"]}
    raise SnapshotError(
        "messages schema not found; expected a WeCom message table or local messages table"
    )


def prepare_offline_db(
    source: Path, destination: Path, attempts: int = 3
) -> dict:
    """Copy, checkpoint, integrity-check, schema-check, and install an offline DB."""
    source = Path(source)
    destination = Path(destination)
    if not source.is_file():
        raise FileNotFoundError(source)
    if source.resolve() == destination.resolve():
        raise ValueError("destination must be different from the source DB")
    if attempts < 1:
        raise ValueError("attempts must be at least 1")

    retry_warnings: list[str] = []
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with tempfile.TemporaryDirectory(prefix="prepare_offline_db_") as td:
                work = Path(td)
                copied_db = work / "offline.db"
                copied_wal = work / "offline.db-wal"
                copied = copy_stable_pair(source, copied_db, copied_wal)
                with copied_db.open("rb") as db_file:
                    header = db_file.read(PAGE)
                validate_database_page_size(header)
                if not header.startswith(SQLITE_HEADER):
                    raise SnapshotError(
                        "offline preparation requires a plain SQLite DB; decrypt to a separate copy first"
                    )
                validate_wal_header(copied_wal)
                checkpoint = checkpoint_copied_db(copied_db)
                check_db(copied_db)
                con = open_readonly(copied_db)
                try:
                    schema = validate_messages_schema(con)
                finally:
                    con.close()
                replace_snapshot_file(copied_db, destination)
                warnings = list(retry_warnings)
                for warning in warnings:
                    LOG.warning("%s: %s", source.name, warning)
                return {
                    "source": str(source),
                    "database": str(destination),
                    "bytes": destination.stat().st_size,
                    "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
                    "wal_copied": copied["wal_copied"],
                    "checkpoint": checkpoint,
                    "integrity_check": "ok",
                    "schema": schema,
                    "attempts": attempt,
                    "warnings": warnings,
                }
        except UnsupportedPageSize:
            raise
        except Exception as exc:
            last_error = exc
            if attempt < attempts:
                delay = 0.15 * attempt
                warning = (
                    f"attempt {attempt}/{attempts} failed: {exc}; "
                    f"retrying in {delay:.2f}s"
                )
                retry_warnings.append(warning)
                LOG.warning("%s: %s", source.name, warning)
                time.sleep(delay)
    raise SnapshotError(
        f"{source}: offline preparation failed after {attempts} attempt(s); "
        f"last error: {last_error}"
    ) from last_error


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="plain SQLite source DB")
    parser.add_argument("destination", type=Path, help="standalone output DB")
    parser.add_argument("--attempts", type=int, default=3)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    result = prepare_offline_db(args.source, args.destination, args.attempts)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
