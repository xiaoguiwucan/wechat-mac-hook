import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from group_admin import GroupAdminService, menu_card
from memory_store import MemoryStore


class GroupAdminTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = MemoryStore(root / "memory.sqlite3")
        self.config = root / "config.json"
        self.config.write_text(json.dumps({
            "reply_strategy": {"mode": "natural", "threshold": 65},
            "media_reply": {"automatic_enabled": True, "voice_probability": .15, "face_probability": .2},
            "ignored_group_members": {},
            "group_personalities": {},
        }), encoding="utf-8")
        self.reloads = 0

        def reload():
            self.reloads += 1
            return {"applied": True}

        self.service = GroupAdminService(self.store, self.config, reload_callback=reload)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def evt(self, group: str, user: str, command: str, message: str = "m1"):
        return SimpleNamespace(
            group_id=group, group_name="值班群", user_id=user, sender_name="风",
            self_id="bot", message_id=message, trace_id="trace-" + message,
            text=command, raw={"post_type": "message", "message_type": "group", "message": []},
        )

    def grant(self, group: str, user: str, role: str = "admin") -> None:
        self.store.save_group_admins(group, [{
            "user_id": user, "display_name": "风", "role": role,
            "permissions": [], "source": "directory", "enabled": True,
        }])

    def test_public_menu_and_group_isolation(self) -> None:
        self.grant("a@chatroom", "wxid_admin")
        self.assertIn("本群管理员", self.service.handle(self.evt("a@chatroom", "wxid_admin", "#菜单"))["card"])
        self.assertIn("普通群友", self.service.handle(self.evt("b@chatroom", "wxid_admin", "#菜单"))["card"])
        denied = self.service.handle(self.evt("b@chatroom", "wxid_admin", "#阈值 80"))
        self.assertFalse(denied["authorized"])
        self.assertIn("操作未执行", denied["card"])

    def test_group_threshold_hot_reload_and_idempotency(self) -> None:
        self.grant("a@chatroom", "wxid_admin")
        result = self.service.handle(self.evt("a@chatroom", "wxid_admin", "#阈值 78", "same"))
        self.assertTrue(result["authorized"])
        config = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(config["reply_strategy"]["group_overrides"]["a@chatroom"]["threshold"], 78)
        self.assertNotIn("b@chatroom", config["reply_strategy"]["group_overrides"])
        duplicate = self.service.handle(self.evt("a@chatroom", "wxid_admin", "#阈值 78", "same"))
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(self.reloads, 1)
        self.assertEqual(len(self.store.group_admin_audit("a@chatroom", 20)), 1)

    def test_group_reply_mention_switch(self) -> None:
        self.grant("a@chatroom", "wxid_admin")
        result = self.service.handle(self.evt("a@chatroom", "wxid_admin", "#艾特 关", "mention"))
        self.assertTrue(result["authorized"])
        config = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertFalse(
            config["reply_strategy"]["group_overrides"]["a@chatroom"]["mention_user_on_reply"]
        )
        self.assertIn("回复艾特提问人", result["card"])
        detail = menu_card("值班群", {"role": "admin", "permissions": []}, "策略")
        self.assertIn("#艾特 开 / 关", detail)

    def test_custom_permission_and_personality(self) -> None:
        self.store.save_group_admins("a@chatroom", [{
            "user_id": "wxid_mod", "display_name": "小王", "role": "custom",
            "permissions": ["personality.manage"], "source": "manual", "enabled": True,
        }])
        ok = self.service.handle(self.evt("a@chatroom", "wxid_mod", "#性格 设置 会接梗的损友"))
        self.assertTrue(ok["authorized"])
        config = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(config["group_personalities"]["a@chatroom"]["prompt"], "会接梗的损友")
        denied = self.service.handle(self.evt("a@chatroom", "wxid_mod", "#表情 80", "m2"))
        self.assertFalse(denied["authorized"])

    def test_admin_cannot_be_blacklisted(self) -> None:
        self.store.save_group_admins("a@chatroom", [
            {"user_id": "wxid_admin", "display_name": "风", "role": "admin", "permissions": [], "source": "directory"},
            {"user_id": "wxid_other", "display_name": "小王", "role": "admin", "permissions": [], "source": "directory"},
        ])
        event = self.evt("a@chatroom", "wxid_admin", "#屏蔽 @小王")
        event.raw["message"] = [{"type": "at", "data": {"user_id": "wxid_other"}}]
        result = self.service.handle(event)
        self.assertIn("不能屏蔽本群管理员", result["card"])
        config = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(config["ignored_group_members"], {})

    def test_menu_is_plain_text_and_never_exposes_wxid(self) -> None:
        card = menu_card("值班群", {
            "role": "admin", "permissions": [], "user_id": "wxid_secret",
        })
        self.assertIn("✦ 小风 · 群管理台", card)
        self.assertIn("#状态", card)
        self.assertIn("#菜单 媒介", card)
        self.assertLessEqual(len(card.splitlines()), 18)
        self.assertNotIn("wxid_secret", card)
        self.assertNotIn("| 权限 |", card)
        detail = menu_card("值班群", {
            "role": "admin", "permissions": [], "user_id": "wxid_secret",
        }, "媒介")
        self.assertIn("#自动媒介 开 / 关", detail)
        self.assertIn("返回主菜单：#菜单", detail)


if __name__ == "__main__":
    unittest.main()
