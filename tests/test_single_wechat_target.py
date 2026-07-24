import json
import unittest
from pathlib import Path

from web_admin.server import (
    ACTION_SCRIPTS,
    normalize_auto_login_config,
    onebot_health_payload_ready,
)


ROOT = Path(__file__).resolve().parents[1]


class SingleWeChatTargetTests(unittest.TestCase):
    def test_manifest_locks_current_official_install(self):
        target = json.loads((ROOT / "config" / "wechat_target.json").read_text())
        self.assertEqual(target["app"], "/Applications/WeChat.app")
        self.assertEqual(target["bundle_id"], "com.tencent.xinWeChat")
        self.assertEqual(target["version"], "4.1.11.53")
        self.assertEqual(target["build"], "269109")
        self.assertEqual(target["frida_mode"], "gadget")
        self.assertEqual(target["frida_gadget_version"], "17.8.0")
        self.assertEqual(target["frida_gadget_addr"], "127.0.0.1:27042")
        self.assertTrue(target["onebot_conf"].endswith("4_1_11_53_mac.json"))

    def test_only_current_runtime_address_config_is_exposed(self):
        configs = sorted((ROOT / "tools" / "onebot" / "wechat_version").glob("*.json"))
        self.assertEqual([path.name for path in configs], ["4_1_11_53_mac.json"])

    def test_launch_and_actions_have_no_multi_instance_entry_points(self):
        launch = (ROOT / "scripts" / "launch_wechat.sh").read_text()
        multi_flag = "multi" + "_" + "open"
        banned = ("--allow_" + multi_flag, "--" + multi_flag, "open -n")
        for token in banned:
            self.assertNotIn(token, launch)
        self.assertIn('/usr/bin/open -a "$APP"', launch)
        self.assertEqual(ACTION_SCRIPTS["launch_wechat"], "launch_wechat.sh")
        self.assertEqual(ACTION_SCRIPTS["start_onebot"], "start_onebot.sh")
        self.assertEqual(ACTION_SCRIPTS["stop_backend"], "stop_backend.sh")

    def test_onebot_uses_gadget_instead_of_pid_attach(self):
        start = (ROOT / "scripts" / "start_onebot.sh").read_text()
        self.assertIn("'-type=gadget'", start)
        self.assertIn("f'-gadget_addr={gadget_addr}'", start)
        self.assertNotIn("'-type=local'", start)

        installer = (ROOT / "scripts" / "install_frida_gadget.sh").read_text()
        self.assertIn('APP" != "/Applications/WeChat.app"', installer)
        self.assertIn("拒绝在运行中修改签名", installer)
        self.assertNotIn("--entitlements", installer)
        self.assertNotIn("open -n", installer)

    def test_account_automation_off_but_onebot_recovery_on(self):
        self.assertFalse(normalize_auto_login_config({})["enabled"])
        config = json.loads((ROOT / "config" / "ai_reply_config.example.json").read_text())
        self.assertFalse(config["wechat_auto_login"]["enabled"])
        self.assertTrue(config["onebot_monitor"]["auto_recover"])

    def test_cancelled_frida_context_is_not_reported_ready(self):
        self.assertFalse(onebot_health_payload_ready({
            "status": "ok",
            "frida": {
                "loaded": True,
                "error": "parse frida rpc result",
                "raw": "context cancelled",
            },
        }))
        self.assertTrue(onebot_health_payload_ready({
            "status": "ok",
            "frida": {"send_ready": True, "upload_x0_ready": True},
        }))


if __name__ == "__main__":
    unittest.main()
