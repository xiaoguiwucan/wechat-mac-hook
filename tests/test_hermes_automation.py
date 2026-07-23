import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from hermes_automation import HermesAutomationService, HermesConfig
from memory_store import MemoryStore


class HermesAutomationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.temp.name) / "memory.sqlite3")
        self.service = HermesAutomationService(
            self.store, lambda *_: None,
            HermesConfig(False, "http://127.0.0.1:8642", "", self.temp.name, 1, 60),
        )
        self.event = SimpleNamespace(
            group_id="g@chatroom", user_id="member", event_id="event-1",
            message_id="message-1", text="运行测试", trace_id="trace-1",
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_member_write_is_denied(self):
        result = self.service.submit(self.event, {
            "risk_level": "write", "automation_intent": "运行测试",
        })
        self.assertFalse(result["accepted"])

    def test_admin_write_is_queued_and_idempotent(self):
        self.store.save_group_admins("g@chatroom", [{
            "user_id": "member", "display_name": "管理员", "role": "admin",
            "permissions": [], "source": "test", "enabled": True,
        }])
        route = {"risk_level": "write", "automation_intent": "运行测试"}
        first = self.service.submit(self.event, route)
        second = self.service.submit(self.event, route)
        self.assertTrue(first["accepted"])
        self.assertTrue(second["accepted"])
        self.assertTrue(second["duplicate"])

    def test_high_risk_always_waits_for_confirmation(self):
        result = self.service.submit(self.event, {
            "risk_level": "high", "automation_intent": "强制推送",
        })
        self.assertFalse(result["accepted"])
        self.assertTrue(result["approval_required"])


if __name__ == "__main__":
    unittest.main()
