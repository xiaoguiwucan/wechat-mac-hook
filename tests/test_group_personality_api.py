import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from web_admin import server


class GroupPersonalityApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.config = root / "config.json"
        self.env = root / "ai_reply.env"
        self.env.write_text("", encoding="utf-8")
        self.config.write_text(json.dumps({
            "group_personalities": {
                "other@chatroom": {
                    "enabled": True,
                    "name": "原有配置",
                    "prompt": "不得被其他群的保存操作覆盖",
                },
                "legacy@chatroom": "兼容旧版字符串配置",
            },
        }, ensure_ascii=False), encoding="utf-8")
        self.patches = [
            mock.patch.object(server, "CONFIG_PATH", self.config),
            mock.patch.object(server, "ENV_PATH", self.env),
            mock.patch.object(
                server,
                "ai_json",
                return_value={"status": "ok", "data": {"pid": 12345}},
            ),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def test_read_legacy_string_personality(self) -> None:
        result = server.group_personality_payload("legacy@chatroom")
        self.assertEqual(result["item"]["prompt"], "兼容旧版字符串配置")
        self.assertTrue(result["item"]["enabled"])

    def test_save_is_group_isolated_and_hot_reloaded(self) -> None:
        result = server.save_group_personality({
            "group_id": "target@chatroom",
            "enabled": True,
            "name": "专业技术助手",
            "prompt": "优先给出明确结论和可执行步骤。",
        })
        config = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(
            config["group_personalities"]["target@chatroom"]["name"],
            "专业技术助手",
        )
        self.assertEqual(
            config["group_personalities"]["other@chatroom"]["prompt"],
            "不得被其他群的保存操作覆盖",
        )
        self.assertTrue(result["hot_reload"]["applied"])

    def test_enabled_personality_requires_prompt(self) -> None:
        with self.assertRaisesRegex(ValueError, "填写性格与表达规则"):
            server.save_group_personality({
                "group_id": "target@chatroom",
                "enabled": True,
                "name": "空配置",
                "prompt": "",
            })

    def test_rejects_non_group_identifier(self) -> None:
        with self.assertRaisesRegex(ValueError, "有效的微信群"):
            server.group_personality_payload("wxid_user")


if __name__ == "__main__":
    unittest.main()
