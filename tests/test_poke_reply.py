import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from ai_reply.ai_reply_server import AIReplyService, AppConfig, PokeReplyConfig
from memory_store import MemoryStore


class PokeReplyTests(unittest.TestCase):
    def setUp(self):
        self.group = "123456@chatroom"
        self.service = AIReplyService(AppConfig(target_groups={self.group: "测试群"}, poke_reply=PokeReplyConfig(
            enabled=True, text_enabled=True, texts=["回复A", "回复B"], cooldown_seconds=1,
        )))

    def test_standard_notice_poke_is_recognized_only_for_bot(self):
        raw = {"post_type": "notice", "notice_type": "notify", "sub_type": "poke",
               "group_id": self.group, "self_id": "bot", "target_id": "bot", "user_id": "member"}
        self.assertIsNotNone(self.service.parse_poke_event(raw))
        raw["target_id"] = "someone_else"
        self.assertIsNone(self.service.parse_poke_event(raw))

    def test_zero_cooldown_keeps_consecutive_pokes(self):
        self.service.cfg.poke_reply.cooldown_seconds = 0
        base = {"post_type": "notice", "notice_type": "notify", "sub_type": "poke",
                "group_id": self.group, "self_id": "bot", "target_id": "bot", "user_id": "member"}
        started = []

        class ImmediateThread:
            def __init__(self, target, args, **_kwargs):
                self.target = target
                self.args = args

            def start(self):
                started.append(self.args[0].event_id)

        with patch("ai_reply.ai_reply_server.threading.Thread", ImmediateThread):
            first = self.service.enqueue_raw({**base, "time": 100, "message_id": "poke-1"})
            second = self.service.enqueue_raw({**base, "time": 100, "message_id": "poke-2"})
        self.assertEqual(first, (True, "poke_reply_queued"))
        self.assertEqual(second, (True, "poke_reply_queued"))
        self.assertEqual(len(started), 2)

    def test_pat_xml_is_recognized_but_plain_text_is_not(self):
        base = {"post_type": "message", "message_type": "group", "group_id": self.group, "self_id": "bot"}
        bot_pat = '<sysmsg type="pat"><pat><fromusername>member</fromusername><pattedusername>bot</pattedusername></pat></sysmsg>'
        event = self.service.parse_poke_event({**base, "user_id": self.group, "raw_message": bot_pat})
        self.assertIsNotNone(event)
        self.assertEqual(event.user_id, "member")
        other_pat = '<sysmsg type="pat"><pat><fromusername>member</fromusername><pattedusername>someone_else</pattedusername></pat></sysmsg>'
        self.assertIsNone(self.service.parse_poke_event({**base, "raw_message": other_pat}))
        self.assertIsNone(self.service.parse_poke_event({**base, "raw_message": '<sysmsg type="pat"><pat/></sysmsg>'}))
        self.assertIsNone(self.service.parse_poke_event({**base, "raw_message": "我们说说拍一拍功能"}))

    def test_real_wechat_pat_xml_only_replies_when_bot_is_patted(self):
        base = {"post_type": "message", "message_type": "group", "group_id": self.group,
                "self_id": "wxid_bot", "user_id": self.group}
        template = '''<sysmsg type="pat"><pat>
          <fromusername>wxid_actor</fromusername>
          <chatusername>123456@chatroom</chatusername>
          <pattedusername>{target}</pattedusername>
          <patsuffix><![CDATA[的脑袋瓜子]]></patsuffix>
        </pat></sysmsg>'''
        event = self.service.parse_poke_event({**base, "raw_message": template.format(target="wxid_bot")})
        self.assertIsNotNone(event)
        self.assertEqual(event.user_id, "wxid_actor")
        self.assertIsNone(self.service.parse_poke_event({**base, "raw_message": template.format(target="wxid_friend")}))

    def test_text_reply_selects_one_configured_line(self):
        evt = self.service.parse_poke_event({"post_type": "notice", "notice_type": "poke",
            "group_id": self.group, "self_id": "bot", "target_id": "bot", "user_id": "member"})
        sent = []
        self.service.send_group_msg = lambda group_id, text, trace_id, event: sent.append((group_id, text))
        with patch("ai_reply.ai_reply_server.random.choice", return_value="text"):
            # choose() is called again for the line; provide deterministic list behavior instead.
            with patch("ai_reply.ai_reply_server.random.choice", side_effect=lambda values: values[0]):
                self.service.handle_poke_reply(evt)
        self.assertEqual(sent[0][0], self.group)
        self.assertIn(sent[0][1], {"回复A", "回复B"})

    def test_image_reply_uses_dedicated_fast_path(self):
        self.service.cfg.poke_reply = PokeReplyConfig(
            enabled=True, text_enabled=False, image_enabled=True, face_ids=[7], cooldown_seconds=1,
        )
        evt = self.service.parse_poke_event({"post_type": "notice", "notice_type": "poke",
            "group_id": self.group, "self_id": "bot", "target_id": "bot", "user_id": "member"})
        self.service.store.face_asset = lambda face_id: {"id": face_id, "enabled": 1, "file": "/tmp/face.gif"}
        calls = []
        self.service.send_poke_face_fast = lambda event, item: calls.append((event.group_id, item["id"]))
        self.service.send_face_pack_tool = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("拍一拍不得进入普通表情工具流程")
        )
        with patch("ai_reply.ai_reply_server.random.choice", side_effect=lambda values: values[0]):
            self.service.handle_poke_reply(evt)
        self.assertEqual(calls, [(self.group, 7)])

    def test_fast_face_does_not_wait_for_upload_media_semaphore(self):
        evt = self.service.parse_poke_event({"post_type": "notice", "notice_type": "poke",
            "group_id": self.group, "self_id": "bot", "target_id": "bot", "user_id": "member"})

        class ForbiddenSemaphore:
            def __enter__(self):
                raise AssertionError("原生表情不得等待上传媒体信号量")

            def __exit__(self, *_args):
                return False

        self.service.media_send_semaphore = ForbiddenSemaphore()
        calls = []
        self.service.post_web_admin = lambda path, payload, timeout: calls.append((path, payload, timeout)) or {"state": "sent"}
        self.service.send_poke_face_fast(evt, {"id": 7, "file": "/tmp/face.gif"})
        self.assertEqual(calls[0][0], "/api/faces/send")
        self.assertTrue(calls[0][1]["fast_path"])
        self.assertEqual(calls[0][2], 4)

    def test_image_reply_never_cascades_to_more_assets_after_send_failure(self):
        self.service.cfg.poke_reply = PokeReplyConfig(
            enabled=True, text_enabled=False, image_enabled=True, face_ids=[2, 7], cooldown_seconds=1,
        )
        evt = self.service.parse_poke_event({"post_type": "notice", "notice_type": "poke",
            "group_id": self.group, "self_id": "bot", "target_id": "bot", "user_id": "member"})
        self.service.store.face_asset = lambda face_id: {
            "id": face_id, "enabled": 1, "file": f"/tmp/face-{face_id}.gif",
        }
        calls = []

        def send_fast(_event, item):
            calls.append(item["id"])
            if item["id"] == 2:
                raise RuntimeError("cdnKey or aesKey empty")

        self.service.send_poke_face_fast = send_fast
        with patch("ai_reply.ai_reply_server.random.shuffle", side_effect=lambda values: None), \
             patch("ai_reply.ai_reply_server.random.choice", side_effect=lambda values: values[0]):
            self.service.handle_poke_reply(evt)
        self.assertEqual(calls, [2])

    def test_failed_asset_is_removed_from_instant_random_pool(self):
        self.service.cfg.poke_reply = PokeReplyConfig(
            enabled=True, text_enabled=False, image_enabled=True, face_ids=[8, 7], cooldown_seconds=1,
        )
        evt = self.service.parse_poke_event({"post_type": "notice", "notice_type": "poke",
            "group_id": self.group, "self_id": "bot", "target_id": "bot", "user_id": "member"})
        assets = {
            8: {"id": 8, "enabled": 1, "file": "/tmp/bad.gif", "success_count": 0, "failure_count": 1},
            7: {"id": 7, "enabled": 1, "file": "/tmp/good.png", "success_count": 1, "failure_count": 0},
        }
        self.service.store.face_asset = lambda face_id: assets[face_id]
        calls = []
        self.service.send_poke_face_fast = lambda _event, item: calls.append(item["id"])
        with patch("ai_reply.ai_reply_server.random.shuffle", side_effect=lambda values: None), \
             patch("ai_reply.ai_reply_server.random.choice", side_effect=lambda values: values[0]):
            self.service.handle_poke_reply(evt)
        self.assertEqual(calls, [7])

    def test_uploaded_static_or_animated_file_can_become_face_asset(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "reaction.gif"
            path.write_bytes(b"GIF89a" + b"\0" * 32)
            store = MemoryStore(Path(directory) / "memory.sqlite3")
            item = store.import_face_asset(str(path), "拍一拍测试")
            self.assertEqual(Path(item["file"]).resolve(), path.resolve())
            self.assertEqual(int(item["enabled"]), 1)


if __name__ == "__main__":
    unittest.main()
