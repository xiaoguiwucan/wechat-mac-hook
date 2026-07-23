import unittest
from types import SimpleNamespace

from ai_reply.ai_reply_server import AIReplyService, RouterConfig


class HermesCapabilityRoutingTests(unittest.TestCase):
    def test_weather_uses_hermes_even_when_fast_router_is_disabled(self):
        service = AIReplyService.__new__(AIReplyService)
        service.cfg = SimpleNamespace(router=RouterConfig(enabled=False))
        event = SimpleNamespace(
            text="上海现在天气怎么样",
            sender_name="成员",
        )
        route = service.fast_route(event, {"items": []}, [], True)
        self.assertTrue(route["automation_required"])
        self.assertEqual(route["risk_level"], "read")
        self.assertEqual(route["hermes_mode"], "answer")

    def test_model_realtime_limitation_is_intercepted(self):
        self.assertTrue(AIReplyService.reply_needs_hermes(
            "我无法获取实时天气数据，建议查看天气应用。"
        ))
        self.assertFalse(AIReplyService.reply_needs_hermes(
            "上海属于亚热带季风气候。"
        ))


if __name__ == "__main__":
    unittest.main()
