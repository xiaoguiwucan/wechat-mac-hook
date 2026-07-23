#!/usr/bin/env python3
"""Reliable SQLite-outbox replication to PostgreSQL and MinIO.

The WeChat callback commits to SQLite first. This worker performs at-least-once
delivery to the central archive; PostgreSQL uniqueness constraints make replay
idempotent. Optional dependencies are loaded lazily so the current single
WeChat keeps working while the central services are unavailable.
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from memory_store import MemoryStore


ROOT = Path(__file__).resolve().parent
DEFAULT_MEDIA_CACHE = (
    Path.home() / "Library" / "Application Support" / "WeChatAgent" / "durable-media-cache"
)


@dataclass
class DurableConfig:
    enabled: bool
    postgres_dsn: str
    minio_endpoint: str
    minio_access_key: str
    minio_secret_key: str
    minio_bucket: str
    minio_secure: bool
    account_id: str
    batch_size: int
    poll_seconds: float

    @classmethod
    def from_env(cls) -> "DurableConfig":
        dsn = os.getenv("WECHAT_POSTGRES_DSN", "").strip()
        enabled_raw = os.getenv("WECHAT_DURABLE_ENABLED", "1" if dsn else "0")
        return cls(
            enabled=enabled_raw.strip().lower() in {"1", "true", "yes", "on"},
            postgres_dsn=dsn,
            minio_endpoint=os.getenv("WECHAT_MINIO_ENDPOINT", "127.0.0.1:9000"),
            minio_access_key=os.getenv("WECHAT_MINIO_ACCESS_KEY", ""),
            minio_secret_key=os.getenv("WECHAT_MINIO_SECRET_KEY", ""),
            minio_bucket=os.getenv("WECHAT_MINIO_BUCKET", "wechat-media"),
            minio_secure=os.getenv("WECHAT_MINIO_SECURE", "0").lower() in {"1", "true", "yes"},
            account_id=os.getenv("WECHAT_ACCOUNT_ID", "current-wechat"),
            batch_size=max(1, min(500, int(os.getenv("WECHAT_DURABLE_BATCH_SIZE", "100")))),
            poll_seconds=max(0.1, min(30.0, float(os.getenv("WECHAT_DURABLE_POLL_SECONDS", "0.5")))),
        )


class DurableSyncService:
    def __init__(self, store: MemoryStore, config: Optional[DurableConfig] = None):
        self.store = store
        self.cfg = config or DurableConfig.from_env()
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.worker = threading.Thread(target=self._loop, name="durable-sync", daemon=True)
        self.lock = threading.RLock()
        self._pg: Any = None
        self._minio: Any = None
        self.state: Dict[str, Any] = {
            "enabled": self.cfg.enabled,
            "connected": False,
            "processed": 0,
            "media_uploaded": 0,
            "last_error": "",
            "last_sync_at": "",
        }

    def start(self) -> None:
        if self.cfg.enabled and not self.worker.is_alive():
            self.worker.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.wake_event.set()
        try:
            if self._pg is not None:
                self._pg.close()
        except Exception:
            pass

    def notify(self) -> None:
        self.wake_event.set()

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            result = dict(self.state)
        try:
            result["outbox"] = self.store.durable_outbox_stats()
        except Exception as exc:
            result["outbox_error"] = str(exc)
        return result

    def _connect(self) -> Any:
        if self._pg is not None and not getattr(self._pg, "closed", True):
            return self._pg
        if not self.cfg.postgres_dsn:
            raise RuntimeError("WECHAT_POSTGRES_DSN is empty")
        import psycopg  # type: ignore
        from psycopg.rows import dict_row  # type: ignore

        self._pg = psycopg.connect(
            self.cfg.postgres_dsn, autocommit=True, row_factory=dict_row,
            connect_timeout=3,
        )
        schema = ROOT / "infrastructure" / "postgres" / "init" / "001_schema.sql"
        with self._pg.cursor() as cur:
            cur.execute(schema.read_text(encoding="utf-8"))
        with self.lock:
            self.state["connected"] = True
            self.state["last_error"] = ""
        return self._pg

    def _minio_client(self) -> Any:
        if self._minio is not None:
            return self._minio
        if not self.cfg.minio_access_key or not self.cfg.minio_secret_key:
            return None
        from minio import Minio  # type: ignore
        from minio.versioningconfig import ENABLED, VersioningConfig  # type: ignore

        self._minio = Minio(
            self.cfg.minio_endpoint,
            access_key=self.cfg.minio_access_key,
            secret_key=self.cfg.minio_secret_key,
            secure=self.cfg.minio_secure,
        )
        if not self._minio.bucket_exists(self.cfg.minio_bucket):
            self._minio.make_bucket(self.cfg.minio_bucket)
        try:
            self._minio.set_bucket_versioning(
                self.cfg.minio_bucket, VersioningConfig(ENABLED),
            )
        except Exception:
            pass
        return self._minio

    @staticmethod
    def _media_segments(payload: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
        for segment in payload.get("segments") or []:
            if isinstance(segment, dict) and str(segment.get("type") or "") in {
                "image", "record", "video", "file", "face"
            }:
                yield segment

    @staticmethod
    def _source_value(segment: Dict[str, Any]) -> str:
        data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
        return str(data.get("file") or data.get("url") or "")

    def _materialize(self, source: str) -> Optional[Path]:
        if not source:
            return None
        parsed = urllib.parse.urlparse(source)
        if parsed.scheme == "file":
            path = Path(urllib.parse.unquote(parsed.path))
            return path if path.is_file() else None
        path = Path(source).expanduser()
        if path.is_file():
            return path
        if parsed.scheme not in {"http", "https"}:
            return None
        DEFAULT_MEDIA_CACHE.mkdir(parents=True, exist_ok=True)
        cache_key = hashlib.sha256(source.encode("utf-8", "ignore")).hexdigest()
        target = DEFAULT_MEDIA_CACHE / cache_key
        if target.is_file():
            return target
        req = urllib.request.Request(source, headers={"User-Agent": "WeChatAgent/1.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read(100 * 1024 * 1024 + 1)
        if len(body) > 100 * 1024 * 1024:
            raise RuntimeError("media exceeds 100MB archive limit")
        target.write_bytes(body)
        return target

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _archive_media(self, pg: Any, payload: Dict[str, Any]) -> None:
        client = self._minio_client()
        for segment in self._media_segments(payload):
            media_type = str(segment.get("type") or "media")
            source = self._source_value(segment)
            metadata = json.dumps(segment, ensure_ascii=False)
            path: Optional[Path] = None
            digest = ""
            object_key = ""
            mime = ""
            size = 0
            status = "metadata_only"
            error = ""
            try:
                path = self._materialize(source)
                if path:
                    digest = self._sha256(path)
                    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                    size = path.stat().st_size
                    suffix = path.suffix.lower()[:12]
                    object_key = f"sha256/{digest[:2]}/{digest}{suffix}"
                    if client is not None:
                        client.fput_object(
                            self.cfg.minio_bucket, object_key, str(path), content_type=mime,
                            metadata={"sha256": digest, "event-id": str(payload["event_id"])[:200]},
                        )
                        status = "ready"
                        with self.lock:
                            self.state["media_uploaded"] += 1
                    else:
                        status = "local_only"
            except Exception as exc:
                status, error = "retry", str(exc)[:1000]
            with pg.cursor() as cur:
                cur.execute(
                    """INSERT INTO media_objects(
                         event_id,group_id,media_type,source_file,source_url,object_key,
                         sha256,mime_type,byte_size,status,metadata,error,updated_at)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,now())
                       ON CONFLICT(event_id,media_type,source_file) DO UPDATE SET
                         object_key=excluded.object_key,sha256=excluded.sha256,
                         mime_type=excluded.mime_type,byte_size=excluded.byte_size,
                         status=excluded.status,metadata=excluded.metadata,error=excluded.error,
                         updated_at=now()""",
                    (
                        payload["event_id"], payload["group_id"], media_type, source,
                        source if source.startswith(("http://", "https://")) else "",
                        object_key, digest, mime, size, status, metadata, error,
                    ),
                )

    def sync_payload(self, payload: Dict[str, Any]) -> None:
        pg = self._connect()
        account_id = str(payload.get("account_id") or self.cfg.account_id)
        with pg.transaction():
            with pg.cursor() as cur:
                cur.execute(
                    """INSERT INTO chat_events(
                         event_id,trace_id,direction,account_id,group_id,group_name,user_id,
                         sender_name,message_id,event_time,text,raw_message,segments,raw_event,source)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s)
                       ON CONFLICT(account_id,group_id,direction,message_id,event_time) DO UPDATE SET
                         group_name=excluded.group_name,sender_name=excluded.sender_name,
                         text=excluded.text,raw_message=excluded.raw_message,
                         segments=excluded.segments,raw_event=excluded.raw_event
                       RETURNING event_id""",
                    (
                        payload["event_id"], payload.get("trace_id", ""),
                        payload.get("direction", "incoming"), account_id,
                        payload["group_id"], payload.get("group_name", ""),
                        payload.get("user_id", ""), payload.get("sender_name", ""),
                        payload.get("message_id", ""), int(payload.get("event_time") or time.time()),
                        payload.get("text", ""), payload.get("raw_message", ""),
                        json.dumps(payload.get("segments") or [], ensure_ascii=False),
                        json.dumps(payload.get("raw") or {}, ensure_ascii=False),
                        payload.get("source", "event"),
                    ),
                )
                row = cur.fetchone()
                canonical_event_id = str(
                    (row or {}).get("event_id") or payload["event_id"]
                )
                cur.execute(
                    """INSERT INTO graph_sync_jobs(event_id,group_id,status)
                       VALUES(%s,%s,'pending') ON CONFLICT(event_id) DO NOTHING""",
                    (canonical_event_id, payload["group_id"]),
                )
            canonical_payload = dict(payload)
            canonical_payload["event_id"] = canonical_event_id
            self._archive_media(pg, canonical_payload)

    def _loop(self) -> None:
        try:
            self._connect()
        except Exception as exc:
            with self.lock:
                self.state["connected"] = False
                self.state["last_error"] = str(exc)[:1000]
        while not self.stop_event.is_set():
            rows = self.store.pending_durable_outbox(self.cfg.batch_size)
            if not rows:
                self.wake_event.wait(self.cfg.poll_seconds)
                self.wake_event.clear()
                continue
            for row in rows:
                if self.stop_event.is_set():
                    return
                try:
                    payload = row.get("payload") or {}
                    if not payload.get("event_id"):
                        raise RuntimeError("invalid durable payload")
                    self.sync_payload(payload)
                    self.store.mark_durable_outbox_synced(int(row["seq"]))
                    with self.lock:
                        self.state["processed"] += 1
                        self.state["last_sync_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                        self.state["last_error"] = ""
                except Exception as exc:
                    self._pg = None
                    self.store.mark_durable_outbox_retry(int(row["seq"]), str(exc))
                    with self.lock:
                        self.state["connected"] = False
                        self.state["last_error"] = str(exc)[:1000]
                    break

    def recent_context(self, group_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        pg = self._connect()
        with pg.cursor() as cur:
            cur.execute(
                """SELECT event_id,direction,group_id,user_id,sender_name,message_id,
                          event_time,text,raw_message
                   FROM chat_events WHERE group_id=%s
                   ORDER BY event_time DESC,created_at DESC LIMIT %s""",
                (group_id, max(1, min(100, int(limit)))),
            )
            rows = list(cur.fetchall())
        rows.reverse()
        return rows

    def search_messages(self, query: str, group_id: str, limit: int = 30) -> List[Dict[str, Any]]:
        pg = self._connect()
        with pg.cursor() as cur:
            cur.execute(
                """SELECT event_id,direction,group_id,user_id,sender_name,message_id,
                          event_time,text,raw_message
                   FROM chat_events
                   WHERE group_id=%s
                     AND ((coalesce(sender_name,'') || ' ' || coalesce(text,'') || ' ' ||
                           coalesce(raw_message,'')) &@~ %s)
                   ORDER BY event_time DESC LIMIT %s""",
                (group_id, query, max(1, min(100, int(limit)))),
            )
            return list(cur.fetchall())
