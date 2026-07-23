from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_reply.ai_reply_server import AIReplyService, AppConfig, PokeReplyConfig, VisionOCRConfig
from brain_engine import BrainConfig
from memory_store import MemoryStore


def text_event(group: str, message_id: str, text: str, user: str = "member") -> dict:
    return {
        "post_type": "message", "message_type": "group", "group_id": group,
        "self_id": "bot", "user_id": user, "message_id": message_id,
        "time": int(time.time() * 1000), "sender": {"nickname": "测试群友"},
        "message": [{"type": "text", "data": {"text": text}}],
        "raw_message": f"{user}:\n{text}",
    }


class MuteAndImageContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name) / "memory.sqlite3")
        self.group = "123456@chatroom"
        self.service = AIReplyService(AppConfig(
            target_groups={self.group: "测试群"},
            brain=BrainConfig(mute_duration_seconds=180),
            poke_reply=PokeReplyConfig(enabled=True, text_enabled=True, texts=["在"]),
            vision_ocr=VisionOCRConfig(enabled=True, auto_analyze=True),
        ))
        self.service.store = self.store

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_mute_command_is_silent_and_bypasses_scheduler(self) -> None:
        with patch.object(self.service.scheduler, "submit") as submit:
            result = self.service.enqueue_raw(text_event(self.group, "m1", "闭嘴！"))
            blocked = self.service.enqueue_raw(text_event(self.group, "m2", "@小风 必须回答"))
        self.assertEqual(result, (False, "group_muted"))
        self.assertEqual(blocked, (False, "group_muted"))
        submit.assert_not_called()
        mute = self.store.group_reply_mute(self.group)
        self.assertTrue(mute["active"])
        self.assertGreaterEqual(mute["remaining_seconds"], 179)

    def test_mute_is_group_scoped_and_also_blocks_poke(self) -> None:
        self.store.set_group_reply_mute(self.group, 180, "member", "m1")
        poke = {
            "post_type": "notice", "notice_type": "notify", "sub_type": "poke",
            "group_id": self.group, "self_id": "bot", "target_id": "bot", "user_id": "member",
        }
        self.assertEqual(self.service.enqueue_raw(poke), (False, "group_muted"))
        self.assertFalse(self.store.group_reply_mute("other@chatroom")["active"])

    def test_expired_mute_is_removed(self) -> None:
        self.store.set_group_reply_mute(self.group, 1)
        with self.store.connect() as db:
            db.execute("UPDATE group_reply_mutes SET muted_until=? WHERE group_id=?", (time.time() - 1, self.group))
        self.assertFalse(self.store.group_reply_mute(self.group)["active"])

    def test_latest_image_question_never_blocks_on_pending_ocr(self) -> None:
        image_path = Path(self.tmp.name) / "latest.jpg"
        image_path.write_bytes(b"fake-jpeg")
        raw = text_event(self.group, "img-1", "", "member")
        raw["message"] = [{"type": "image", "data": {"file": image_path.as_uri()}}]
        raw["raw_message"] = "member:\n[image]"
        image_evt, _ = self.service.parse_event(raw)
        self.assertIsNotNone(image_evt)
        self.service.persist_incoming(image_evt)
        question_evt, _ = self.service.parse_event(text_event(self.group, "q-1", "我刚发了什么图"))
        self.assertIsNotNone(question_evt)
        started = time.monotonic()
        with patch.object(self.service, "analyze_image_with_vision") as analyze:
            latest = self.service.prepare_latest_image_context(question_evt)
        self.assertLess(time.monotonic() - started, 0.1)
        self.assertIn(latest["status"], {"indexed", "ocr_queued", "ocr_running", "ocr_failed"})
        analyze.assert_not_called()

    def test_restart_recovery_uses_persisted_event_id(self) -> None:
        image_path = Path(self.tmp.name) / "recovered.jpg"
        image_path.write_bytes(b"fake-jpeg")
        raw = text_event(self.group, "img-recovery", "", "member")
        raw["message"] = [{"type": "image", "data": {"file": image_path.as_uri()}}]
        raw["raw_message"] = "member:\n[image]"
        self.store.add_message({
            "event_id": "stable-db-event", "trace_id": "stable-trace", "direction": "incoming",
            "group_id": self.group, "group_name": "测试群", "user_id": "member",
            "sender_name": "测试群友", "message_id": "img-recovery", "event_time": raw["time"],
            "text": "[image]", "raw_message": raw["raw_message"], "segments": raw["message"], "raw": raw,
        })
        self.assertEqual(self.service.recover_pending_media_analysis(), 1)
        recovered = self.service.media_events.get_nowait()
        self.assertEqual(recovered.event_id, "stable-db-event")
        self.assertEqual(recovered.trace_id, "stable-trace")

    def test_image_dedup_keeps_visual_summary(self) -> None:
        for event_id in ("e1", "e2"):
            raw = text_event(self.group, "same-message", "", "member")
            raw["message"] = [{"type": "image", "data": {"file": f"file:///tmp/{event_id}.jpg"}}]
            raw["raw_message"] = f"member:\n[{event_id}]"
            self.store.add_message({
                "event_id": event_id, "trace_id": event_id, "direction": "incoming",
                "group_id": self.group, "group_name": "测试群", "user_id": "member",
                "sender_name": "测试群友", "message_id": "same-message", "event_time": int(time.time()),
                "text": "[image]", "raw_message": raw["raw_message"], "segments": raw["message"], "raw": raw,
            })
        rows = self.store.media(self.group, "image", 10)
        annotated = min(rows, key=lambda row: int(row["id"]))
        self.store.save_media_annotation(int(annotated["id"]), "", "最新图片的正确摘要", "ocr_done", ["测试"], ["图片"])
        self.store.deduplicate_media_items(self.group, "image")
        remaining = self.store.media(self.group, "image", 10)
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["status"], "ocr_done")
        self.assertEqual(remaining[0]["image_summary"], "最新图片的正确摘要")

    def test_quoted_image_uses_exact_message_id_instead_of_latest_sender_image(self) -> None:
        quoted_path = Path(self.tmp.name) / "rabbit.jpg"
        quoted_path.write_bytes(b"rabbit")
        original = text_event(self.group, "rabbit-message", "", "other-member")
        original["message"] = [{"type": "image", "data": {"file": quoted_path.as_uri(), "md5": "a" * 32}}]
        original["raw_message"] = "other-member:\n[image]"
        image_evt, _ = self.service.parse_event(original)
        self.assertIsNotNone(image_evt)
        self.service.persist_incoming(image_evt)
        rabbit = self.store.media_by_event(image_evt.event_id)[0]
        self.store.save_media_annotation(int(rabbit["id"]), "", "一只垂耳兔站在笼子里", "ocr_done", ["兔子"], ["垂耳兔"])

        wrong_path = Path(self.tmp.name) / "wrong.jpg"
        wrong_path.write_bytes(b"wrong")
        wrong = text_event(self.group, "latest-wrong", "", "member")
        wrong["time"] = int(original["time"]) + 1000
        wrong["message"] = [{"type": "image", "data": {"file": wrong_path.as_uri()}}]
        wrong["raw_message"] = "member:\n[image]"
        wrong_evt, _ = self.service.parse_event(wrong)
        self.assertIsNotNone(wrong_evt)
        self.service.persist_incoming(wrong_evt)
        wrong_media = self.store.media_by_event(wrong_evt.event_id)[0]
        self.store.save_media_annotation(int(wrong_media["id"]), "", "一张完全无关的 API 截图", "ocr_done")

        quote_xml = (
            '<?xml version="1.0"?><msg><appmsg><title>这是啥图</title><type>57</type>'
            '<refermsg><type>3</type><svrid>rabbit-message</svrid><fromusr>123456@chatroom</fromusr>'
            '<chatusr>other-member</chatusr><displayname>小灯</displayname>'
            '<content>&lt;msg&gt;&lt;img md5="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" /&gt;&lt;/msg&gt;</content>'
            '<createtime>1784168063</createtime></refermsg></appmsg></msg>'
        )
        quote_raw = text_event(self.group, "quote-question", quote_xml, "member")
        quote_raw["time"] = int(original["time"]) + 2000
        quote_evt, _ = self.service.parse_event(quote_raw)
        self.assertIsNotNone(quote_evt)
        self.assertEqual(quote_evt.text, "这是啥图")
        self.assertEqual(quote_evt.raw["_quoted_message"]["message_id"], "rabbit-message")
        self.assertTrue(any(segment.get("type") == "reply" for segment in quote_evt.raw["message"]))
        selected = self.service.prepare_latest_image_context(quote_evt)
        self.assertEqual(selected["id"], rabbit["id"])
        self.assertEqual(selected["image_summary"], "一只垂耳兔站在笼子里")
        self.assertEqual(selected["_context_source"], "quoted_image")

    def test_missing_quoted_image_never_falls_back_to_unrelated_latest_image(self) -> None:
        quote_xml = (
            '<msg><appmsg><title>这是什么图</title><refermsg><type>3</type>'
            '<svrid>missing-image</svrid><content>&lt;msg&gt;&lt;img md5="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" /&gt;&lt;/msg&gt;'
            '</content></refermsg></appmsg></msg>'
        )
        quote_evt, _ = self.service.parse_event(text_event(self.group, "missing-quote", quote_xml))
        self.assertIsNotNone(quote_evt)
        selected = self.service.prepare_latest_image_context(quote_evt)
        self.assertEqual(selected["status"], "referenced_image_not_indexed")
        self.assertEqual(selected["id"], 0)


if __name__ == "__main__":
    unittest.main()
