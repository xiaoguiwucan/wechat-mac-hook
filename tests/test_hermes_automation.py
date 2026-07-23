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
        self.assertEqual(
            self.store.automation_run(result["run_id"])["status"],
            "awaiting_approval",
        )

    def test_high_risk_can_be_approved_from_owner_console(self):
        pending = self.service.submit_manual("g@chatroom", "生产部署", "high")
        approved = self.service.approve(pending["run_id"])
        self.assertTrue(approved["accepted"])
        self.assertEqual(
            self.store.automation_run(pending["run_id"])["status"],
            "queued",
        )

    def test_owner_console_can_submit_and_stop_write_task(self):
        submitted = self.service.submit_manual("g@chatroom", "运行全部测试", "write")
        self.assertTrue(submitted["accepted"])
        stopped = self.service.stop_run(submitted["run_id"])
        self.assertTrue(stopped["stopped"])
        self.assertEqual(
            self.store.automation_run(submitted["run_id"])["status"],
            "cancelled",
        )

    def test_read_capability_query_is_queued_as_direct_answer(self):
        self.event.text = "上海现在天气怎么样"
        result = self.service.submit(self.event, {
            "risk_level": "read",
            "automation_intent": self.event.text,
            "hermes_mode": "answer",
        })
        self.assertTrue(result["accepted"])
        self.assertIn("调用 Hermes", result["message"])
        queued = self.service.tasks.get_nowait()
        self.assertEqual(queued["purpose"], "answer")

    def test_global_owner_can_create_schedule_from_any_group(self):
        service = HermesAutomationService(
            self.store, lambda *_: None,
            HermesConfig(
                False, "http://127.0.0.1:8642", "", self.temp.name, 1, 60,
                ("owner-user",),
            ),
        )
        event = SimpleNamespace(
            group_id="another@chatroom", user_id="owner-user", event_id="schedule-1",
            message_id="schedule-1", text="1分钟后提醒我起床", trace_id="schedule-1",
        )
        result = service.submit(event, {
            "risk_level": "write",
            "automation_intent": event.text,
        })
        self.assertTrue(result["accepted"])


if __name__ == "__main__":
    unittest.main()
