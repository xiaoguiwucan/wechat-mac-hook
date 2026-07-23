#!/usr/bin/env python3
"""Read-only WeChat 4.x history import into the edge buffer.

The source is the plaintext snapshot produced by the existing wechat-decrypt
snapshot service. This script never opens the live WeChat databases and never
writes to the snapshot. MemoryStore's unique event_id and durable outbox make
repeated imports idempotent and forward every imported row to PostgreSQL.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from memory_store import DEFAULT_DB, MemoryStore  # noqa: E402


DEFAULT_SOURCE = Path(
    "/Users/zkx/Documents/linux-wechat-agent/runtime/memory/wechat_memory.sqlite"
)
DEFAULT_MEDIA_ROOT = Path("/Users/zkx/Documents/linux-wechat-agent/runtime")
MEDIA_TYPES = {
    3: "image",
    34: "record",
    43: "video",
    47: "face",
    49: "file",
}


def readonly(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.resolve()}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def resolve_media(media_root: Path, row: Optional[sqlite3.Row]) -> Dict[str, Any]:
    if row is None:
        return {}
    relative = str(row["media_path"] or "")
    source = str(row["source_path"] or "")
    candidate = media_root / relative if relative else Path()
    if candidate.is_file():
        source = str(candidate)
    elif source.startswith("/app/config/xwechat_files/"):
        source = str(
            Path.home()
            / "Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files"
            / source.removeprefix("/app/config/xwechat_files/")
        )
    return {
        "file": source,
        "status": str(row["status"] or "metadata_only"),
        "mime_type": str(row["mime_type"] or ""),
        "original_md5": str(row["original_md5"] or ""),
        "source_path": str(row["source_path"] or ""),
    }


def split_sender(content: str, outgoing: bool) -> tuple[str, str]:
    if outgoing:
        return "current-wechat", content
    match = re.match(r"^([^:\n]{1,128}):\s?(.*)$", content, flags=re.S)
    if not match:
        return "", content
    return match.group(1).strip(), match.group(2)


def run(source: Path, target: Path, media_root: Path, dry_run: bool) -> Dict[str, Any]:
    if not source.is_file():
        raise FileNotFoundError(source)
    src = readonly(source)
    media = {
        str(row["message_uid"]): row
        for row in src.execute("SELECT * FROM message_media")
    }
    store = None if dry_run else MemoryStore(target)
    result: Dict[str, Any] = {
        "source": str(source),
        "target": str(target),
        "scanned": 0,
        "inserted": 0,
        "duplicates": 0,
        "groups": {},
        "media": {"ready": 0, "missing": 0, "metadata_only": 0},
    }
    query = """
        SELECT m.*, c.display_name AS group_name
        FROM messages m JOIN chats c ON c.username=m.chat_username
        WHERE c.is_group=1
        ORDER BY m.create_time,m.local_id
    """
    for row in src.execute(query):
        result["scanned"] += 1
        uid = str(row["message_uid"])
        group_id = str(row["chat_username"])
        group_name = str(row["group_name"] or row["chat_display_name"] or group_id)
        content = str(row["message_content"] or row["compress_content"] or "")
        outgoing = int(row["status"] or 0) == 2
        user_id, text = split_sender(content, outgoing)
        sender_name = "我" if outgoing else user_id
        media_row = media.get(uid)
        media_data = resolve_media(media_root, media_row)
        media_type = str((media_row["media_type"] if media_row else "") or "")
        segment_type = "face" if media_type == "sticker" else (
            media_type or MEDIA_TYPES.get(int(row["local_type"] or 0), "")
        )
        segments = []
        if segment_type:
            segments.append({
                "type": segment_type,
                "data": {
                    **media_data,
                    "wechat_local_type": int(row["local_type"] or 0),
                    "wechat_local_id": int(row["local_id"] or 0),
                },
            })
            path = Path(str(media_data.get("file") or ""))
            status = "ready" if path.is_file() else (
                "metadata_only" if not media_data.get("file") else "missing"
            )
            result["media"][status] += 1
        item = {
            "event_id": f"wechat4-history:{uid}",
            "trace_id": f"history-{uid[:20]}",
            "direction": "outgoing" if outgoing else "incoming",
            "account_id": "current-wechat",
            "group_id": group_id,
            "group_name": group_name,
            "user_id": user_id or "unknown",
            "sender_name": sender_name or "群成员",
            "message_id": str(row["server_id"] or row["local_id"] or uid),
            "event_time": int(row["create_time"] or 0),
            "text": text,
            "raw_message": content,
            "segments": segments,
            "raw": {
                "source": "wechat4-readonly-snapshot",
                "message_uid": uid,
                "local_id": row["local_id"],
                "server_id": row["server_id"],
                "local_type": row["local_type"],
                "type_label": row["type_label"],
                "content_sha256": row["content_sha256"],
                "media_status": str(media_row["status"] if media_row else ""),
            },
            "source": "wechat4-history",
        }
        inserted = True if dry_run else bool(store and store.add_message(item))
        result["inserted" if inserted else "duplicates"] += 1
        stats = result["groups"].setdefault(
            group_id,
            {"name": group_name, "messages": 0, "earliest": 0, "latest": 0},
        )
        ts = int(row["create_time"] or 0)
        stats["messages"] += 1
        stats["earliest"] = min(stats["earliest"] or ts, ts)
        stats["latest"] = max(stats["latest"], ts)
    src.close()
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--target", type=Path, default=DEFAULT_DB)
    parser.add_argument("--media-root", type=Path, default=DEFAULT_MEDIA_ROOT)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = run(args.source, args.target, args.media_root, args.dry_run)
    body = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(body + "\n", encoding="utf-8")
    print(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
