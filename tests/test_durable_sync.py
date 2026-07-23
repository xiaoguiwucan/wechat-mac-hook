from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from durable_sync import DurableConfig, DurableSyncService
from memory_store import MemoryStore


class DurableSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name) / "memory.sqlite3")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def add_message(self, event_id: str = "event-1") -> None:
        self.store.add_message({
            "event_id": event_id, "trace_id": "trace", "direction": "incoming",
            "group_id": "g@chatroom", "group_name": "测试群", "user_id": "u",
            "sender_name": "用户", "message_id": "m1", "event_time": 123,
            "text": "记住这句话", "raw_message": "记住这句话",
            "segments": [{"type": "text", "data": {"text": "记住这句话"}}],
            "raw": {"post_type": "message"}, "source": "test",
        })

    def test_message_and_outbox_commit_together(self) -> None:
        self.add_message()
        rows = self.store.pending_durable_outbox()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["payload"]["event_id"], "event-1")
        self.assertEqual(rows[0]["payload"]["group_id"], "g@chatroom")

    def test_duplicate_event_creates_one_outbox_item(self) -> None:
        self.add_message()
        self.add_message()
        self.assertEqual(len(self.store.pending_durable_outbox()), 1)

    def test_retry_then_sync_is_recoverable(self) -> None:
        self.add_message()
        row = self.store.pending_durable_outbox()[0]
        self.store.mark_durable_outbox_retry(int(row["seq"]), "offline")
        stats = self.store.durable_outbox_stats()
        self.assertEqual(stats["pending"], 1)
        with self.store.connect() as db:
            db.execute("UPDATE durable_outbox SET next_attempt_at=0 WHERE seq=?", (row["seq"],))
        self.store.mark_durable_outbox_synced(int(row["seq"]))
        stats = self.store.durable_outbox_stats()
        self.assertEqual(stats["pending"], 0)
        self.assertEqual(stats["synced"], 1)

    def test_disabled_service_never_starts_worker(self) -> None:
        cfg = DurableConfig(
            enabled=False, postgres_dsn="", minio_endpoint="", minio_access_key="",
            minio_secret_key="", minio_bucket="test", minio_secure=False,
            account_id="test", batch_size=10, poll_seconds=0.1,
        )
        service = DurableSyncService(self.store, cfg)
        service.start()
        self.assertFalse(service.worker.is_alive())


if __name__ == "__main__":
    unittest.main()
