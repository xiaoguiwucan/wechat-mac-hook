#!/usr/bin/env python3
"""Queue every existing SQLite message for idempotent PostgreSQL replication."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memory_store import DEFAULT_DB, MemoryStore, now_ts  # noqa: E402


def parsed(value: str, default: object) -> object:
    try:
        return json.loads(value or "")
    except (TypeError, ValueError):
        return default


def queue_messages(store: MemoryStore, dry_run: bool = False) -> dict[str, int]:
    with store.connect() as db:
        rows = db.execute("SELECT * FROM messages ORDER BY event_time,created_at").fetchall()
        existing = {
            str(row["event_id"])
            for row in db.execute("SELECT event_id FROM durable_outbox").fetchall()
        }
        missing = [row for row in rows if str(row["event_id"]) not in existing]
        if dry_run:
            return {"messages": len(rows), "already_queued": len(existing), "queued": len(missing)}
        for row in missing:
            payload = {
                "event_id": str(row["event_id"]),
                "trace_id": str(row["trace_id"] or ""),
                "direction": str(row["direction"] or "incoming"),
                "account_id": "current-wechat",
                "group_id": str(row["group_id"]),
                "group_name": str(row["group_name"] or ""),
                "user_id": str(row["user_id"] or ""),
                "sender_name": str(row["sender_name"] or ""),
                "message_id": str(row["message_id"] or ""),
                "event_time": int(row["event_time"] or 0),
                "text": str(row["text"] or ""),
                "raw_message": str(row["raw_message"] or ""),
                "segments": parsed(str(row["segments_json"] or "[]"), []),
                "raw": parsed(str(row["raw_json"] or "{}"), {}),
                "source": "sqlite_backfill",
            }
            db.execute(
                """INSERT OR IGNORE INTO durable_outbox(
                     event_id,payload_json,status,next_attempt_at,updated_at)
                   VALUES(?,?,'pending',0,?)""",
                (payload["event_id"], json.dumps(payload, ensure_ascii=False), now_ts()),
            )
    return {"messages": len(rows), "already_queued": len(existing), "queued": len(missing)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    result = queue_messages(MemoryStore(Path(args.db).expanduser()), args.dry_run)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
