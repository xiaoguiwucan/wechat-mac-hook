import unittest

from web_admin.server import classify_wechat_login_snapshot, normalize_auto_login_config


class WeChatAutoLoginTests(unittest.TestCase):
    def test_logged_in_marker_always_blocks_click_candidate(self):
        result = classify_wechat_login_snapshot([
            {"text": "文件传输助手", "x": .1, "y": .2, "width": .12, "height": .03},
            {"text": "登录", "x": .4, "y": .6, "width": .2, "height": .05},
        ])
        self.assertEqual(result["status"], "logged_in")
        self.assertIsNone(result["candidate"])

    def test_sparse_exact_login_button_is_candidate(self):
        result = classify_wechat_login_snapshot([
            {"text": "微信", "x": .45, "y": .2, "width": .1, "height": .03},
            {"text": "登录", "x": .4, "y": .65, "width": .2, "height": .06},
        ])
        self.assertEqual(result["status"], "login_page")
        self.assertEqual(result["candidate"]["text"], "登录")

    def test_non_exact_or_busy_window_is_never_candidate(self):
        self.assertEqual(classify_wechat_login_snapshot([
            {"text": "登录失败", "x": .4, "y": .65, "width": .2, "height": .06},
        ])["status"], "watching")
        busy = [{"text": str(i), "x": .4, "y": .1, "width": .1, "height": .02} for i in range(20)]
        busy.append({"text": "登录", "x": .4, "y": .65, "width": .2, "height": .06})
        self.assertEqual(classify_wechat_login_snapshot(busy)["status"], "watching")

    def test_config_has_guarded_minimums(self):
        value = normalize_auto_login_config({
            "check_interval_seconds": 1, "cooldown_seconds": 1,
            "required_consecutive_detections": 1, "max_attempts_per_episode": 99,
        })
        self.assertEqual(value["check_interval_seconds"], 10)
        self.assertEqual(value["cooldown_seconds"], 30)
        self.assertEqual(value["required_consecutive_detections"], 2)
        self.assertEqual(value["max_attempts_per_episode"], 5)


if __name__ == "__main__":
    unittest.main()
