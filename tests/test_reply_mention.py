import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ai_reply.ai_reply_server import AIReplyService
from brain_engine import BrainConfig


class _Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return b'{"status":"ok","retcode":0,"data":{"message_id":"sent"}}'


class _Store:
    def resolve_member_name(self, _group_id, _user_id, _fallback):
        return "风"

    def add_message(self, _payload):
        return None


class ReplyMentionTest(unittest.TestCase):
    def service(self, enabled: bool):
        service = AIReplyService.__new__(AIReplyService)
        service.cfg = SimpleNamespace(
            onebot_api="http://127.0.0.1:58080",
            target_groups={"g@chatroom": "测试群"},
            brain=BrainConfig.from_raw({"reply_strategy": {
                "group_overrides": {
                    "g@chatroom": {"mention_user_on_reply": enabled},
                },
            }}),
        )
        service.store = _Store()
        service.recent_errors = []
        return service

    @staticmethod
    def event():
        return SimpleNamespace(
            group_id="g@chatroom", user_id="wxid_user", self_id="wxid_bot",
            sender_name="风", message_id="m1",
        )

    def segments(self, enabled: bool, quote: bool = False):
        service = self.service(enabled)
        with patch("urllib.request.urlopen", return_value=_Response()) as mocked:
            service._send_group_msg_locked(
                "g@chatroom", "回答正文", "trace", self.event(), quote_message=quote
            )
        return json.loads(mocked.call_args.args[0].data.decode("utf-8"))["message"]

    def test_disabled_sends_plain_text_without_at(self):
        self.assertEqual([item["type"] for item in self.segments(False)], ["text"])

    def test_enabled_adds_at_before_text(self):
        self.assertEqual([item["type"] for item in self.segments(True)], ["at", "text", "text"])

    def test_quote_never_adds_at_even_when_enabled(self):
        self.assertEqual([item["type"] for item in self.segments(True, True)], ["reply", "text"])


if __name__ == "__main__":
    unittest.main()
