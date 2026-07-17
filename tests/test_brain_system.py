from __future__ import annotations

import base64
import json
import concurrent.futures
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from brain_engine import BrainConfig, OpportunityScorer, ReplyScheduler, TaskRegistry
from embedding_service import EmbeddingConfig, EmbeddingService
from ai_reply.ai_reply_server import AIReplyService, AppConfig, ImageGenerationConfig, MediaReplyConfig
from memory_store import MemoryStore


def event(group: str, user: str, message_id: str, text: str, reply_id: str = "") -> SimpleNamespace:
    segments = [{"type": "text", "data": {"text": text}}]
    if reply_id:
        segments.insert(0, {"type": "reply", "data": {"id": reply_id}})
    return SimpleNamespace(
        trace_id="trace-" + message_id,
        group_id=group,
        group_name=group,
        user_id=user,
        sender_name=user,
        message_id=message_id,
        event_id=message_id,
        text=text,
        self_id="bot",
        raw={"message": segments},
    )


class BrainSystemTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name) / "memory.sqlite3")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_deleted_penalties_are_not_configurable_or_scored(self) -> None:
        cfg = BrainConfig()
        forbidden = {
            "directed_other_member", "bot_recent_share", "bot_spoke_recently",
            "serious_media_mismatch", "material_cooldown", "meme_cooldown",
        }
        self.assertTrue(forbidden.isdisjoint(cfg.modifiers))
        scorer = OpportunityScorer(cfg)
        evt = event("g", "u", "m", "小风你怎么看")
        result = scorer.local_score(evt, [], {"items": [], "culture": {}}, None)
        self.assertTrue(result["mandatory"])
        self.assertTrue(forbidden.isdisjoint({x["signal"] for x in result["reasons"]}))

    def test_fast_local_scoring_is_the_default(self) -> None:
        cfg = BrainConfig.from_raw({"reply_strategy": {}})
        self.assertEqual(cfg.scoring_mode, "local_fast")
        self.assertEqual(cfg.rerank_candidates, 12)
        self.assertTrue(cfg.mention_user_on_reply)
        scorer = OpportunityScorer(cfg)
        evt = event("g", "u", "m", "小风，数据库索引应该怎么建？")
        local = scorer.local_score(evt, [], {"items": [], "culture": {}}, None)
        factors, reason = scorer.local_factors(evt, [], {"items": [], "culture": {}}, local, None)
        self.assertEqual(set(factors), set(cfg.factor_weights))
        self.assertTrue(all(0 <= value <= 100 for value in factors.values()))
        self.assertIn("本地快速评分", reason)

    def test_reply_mention_can_be_overridden_per_group(self) -> None:
        cfg = BrainConfig.from_raw({"reply_strategy": {
            "mention_user_on_reply": True,
            "group_overrides": {
                "quiet@chatroom": {"mention_user_on_reply": False},
                "loud@chatroom": {"mention_user_on_reply": True},
            },
        }})
        self.assertFalse(cfg.for_group("quiet@chatroom").mention_user_on_reply)
        self.assertTrue(cfg.for_group("loud@chatroom").mention_user_on_reply)
        self.assertTrue(cfg.for_group("other@chatroom").mention_user_on_reply)

    def test_current_persisted_message_is_not_its_own_duplicate(self) -> None:
        cfg = BrainConfig()
        scorer = OpportunityScorer(cfg)
        evt = event("g", "u", "m-current", "哈喽 在吗")
        recent = [{"event_id": "m-current", "message_id": "m-current", "direction": "incoming",
                   "user_id": "u", "text": "哈喽 在吗", "event_time": time.time()}]
        result = scorer.local_score(evt, recent, {"items": [], "culture": {}}, None)
        self.assertNotIn("low_information", {x["signal"] for x in result["reasons"]})
        self.assertEqual(result["pre_score"], 34)

    def test_fuse_sixty_but_only_rerank_first_twelve(self) -> None:
        class SearchStore:
            def search_messages(self, **_kwargs):
                return [{"event_id": f"lex-{i}", "text": f"测试历史 {i}"} for i in range(30)]

            def culture_context(self, *_args, **_kwargs):
                return {"aliases": [], "relations": [], "memes": []}

            def semantic_search(self, *_args, **_kwargs):
                return [{"object_type": "message", "object_id": f"sem-{i}", "text": f"向量历史 {i}", "score": 1 - i / 100} for i in range(50)]

        service = EmbeddingService(SearchStore(), EmbeddingConfig())
        service.embed = lambda *_args, **_kwargs: [[0.0] * 4096]
        reranked_counts = []

        def fake_rerank(_query, documents, top_n=12):
            reranked_counts.append(len(documents))
            return [{"index": i, "relevance_score": 1 - i / 100} for i in range(min(top_n, len(documents)))]

        service.rerank = fake_rerank
        result = service.search("测试", "group-a", limit=12, rerank_candidates=12)
        self.assertEqual(result["recalled_count"], 60)
        self.assertEqual(result["reranked_count"], 12)
        self.assertEqual(reranked_counts, [12])
        self.assertEqual(len(result["items"]), 12)
        social = service.search("哈喽 在吗", "group-a", limit=12, rerank_candidates=12)
        self.assertEqual(social["reranked_count"], 0)
        self.assertEqual(social["rerank_skipped_reason"], "simple_social")
        social_help = service.search("我需要帮忙 在吗", "group-a", limit=12, rerank_candidates=12)
        self.assertEqual(social_help["reranked_count"], 0)
        self.assertEqual(social_help["rerank_skipped_reason"], "simple_social")
        self.assertEqual(reranked_counts, [12])

    def test_ambiguous_history_query_expands_reranker_to_twenty_four(self) -> None:
        class SearchStore:
            def search_messages(self, **_kwargs):
                return [{"event_id": f"lex-{i}", "text": f"很久以前的群梗 {i}"} for i in range(30)]

            def culture_context(self, *_args, **_kwargs):
                return {"aliases": [], "relations": [], "memes": []}

            def semantic_search(self, *_args, **_kwargs):
                return [{"object_type": "message", "object_id": f"sem-{i}", "text": f"上次那个梗 {i}", "score": .8 - i / 200} for i in range(60)]

        service = EmbeddingService(SearchStore(), EmbeddingConfig())
        service.embed = lambda *_args, **_kwargs: [[0.0] * 4096]
        batches = []

        def low_confidence(_query, documents, top_n=12):
            batches.append(len(documents))
            return [{"index": i, "relevance_score": .40 - i / 1000} for i in range(min(top_n, len(documents)))]

        service.rerank = low_confidence
        result = service.search("上次那个梗是谁说的", "group-a", limit=12, rerank_candidates=12)
        self.assertEqual(batches, [12, 12])
        self.assertEqual(result["reranked_count"], 24)
        self.assertTrue(result["expanded_second_batch"])

    def test_structured_people_meme_time_and_fts_routes_are_group_isolated(self) -> None:
        yesterday = int(time.time()) - 86400
        self.store.add_message({"event_id": "old-a", "direction": "incoming", "group_id": "group-a",
                                "user_id": "u-a", "sender_name": "王师傅", "message_id": "old-a",
                                "event_time": yesterday, "text": "老王又在画饼", "raw_message": "老王又在画饼"})
        self.store.add_message({"event_id": "old-b", "direction": "incoming", "group_id": "group-b",
                                "user_id": "u-b", "sender_name": "王师傅", "message_id": "old-b",
                                "event_time": yesterday, "text": "老王又在画饼", "raw_message": "老王又在画饼"})
        self.store.upsert_alias("group-a", "u-a", "老王", .8, ["old-a"])
        self.store.upsert_meme("group-a", "画饼梗", "项目延期时用", ["画饼"], ["old-a"], confidence=.8)
        self.assertEqual([row["event_id"] for row in self.store.search_messages_fts("老王又在画饼", "group-a")], ["old-a"])
        self.assertEqual([row["event_id"] for row in self.store.route_people("group-a", "老王以前说了什么")], ["old-a"])
        self.assertEqual(self.store.route_memes("group-a", "上次画饼梗")[0]["name"], "画饼梗")
        self.assertTrue(self.store.search_time_messages("昨天的消息", "group-a"))
        self.assertFalse(any(row.get("group_id") == "group-b" for row in self.store.route_people("group-a", "老王")))

    def test_face_marker_is_intercepted_and_never_treated_as_text(self) -> None:
        service = object.__new__(AIReplyService)
        decision = service.parse_reply_decision("@风\u2005 /发表情 走开啊别拍我")
        self.assertEqual(decision.medium, "face")
        self.assertEqual(decision.media_query, "走开啊别拍我")
        self.assertEqual(decision.text, "")

    def test_image_generation_requests_are_mandatory_and_prompt_is_extracted(self) -> None:
        scorer = OpportunityScorer(BrainConfig())
        evt = event("group-a", "user-a", "image-1", "帮我画一张穿宇航服的橘猫")
        result = scorer.local_score(evt, [], {"items": [], "culture": {}}, None)
        self.assertTrue(result["mandatory"])
        self.assertTrue(result["explicit_media"])
        self.assertEqual(AIReplyService.extract_image_generation_prompt(evt.text), "穿宇航服的橘猫")
        self.assertEqual(AIReplyService.extract_image_generation_prompt("/生图 复古未来城市"), "复古未来城市")
        self.assertEqual(AIReplyService.extract_image_generation_prompt("画个哈士奇"), "哈士奇")
        self.assertEqual(AIReplyService.extract_image_generation_prompt("画个猫"), "猫")
        self.assertEqual(AIReplyService.extract_image_generation_prompt("画一只猫"), "猫")
        self.assertEqual(AIReplyService.extract_image_generation_prompt("这张图挺好看"), "")
        self.assertEqual(AIReplyService.extract_image_generation_prompt("来个笑话"), "")
        self.assertEqual(AIReplyService.extract_image_generation_prompt("画蛇添足是什么意思"), "")

    def test_image_generation_config_is_loaded_separately_from_chat_channel(self) -> None:
        path = Path(self.tmp.name) / "config.json"
        path.write_text(json.dumps({
            "target_groups": [{"id": "group-a@chatroom", "name": "A"}],
            "ai": {"base_url": "https://chat.example/v1", "model": "chat-model"},
            "image_generation": {"enabled": True, "base_url": "https://image.example/v1",
                                 "model": "image-model", "size": "1536x1024", "quality": "high",
                                 "timeout_seconds": 240, "response_format": "url"},
        }, ensure_ascii=False), encoding="utf-8")
        cfg = AppConfig.from_file(path)
        self.assertEqual(cfg.ai.model, "chat-model")
        self.assertTrue(cfg.image_generation.enabled)
        self.assertEqual(cfg.image_generation.model, "image-model")
        self.assertEqual(cfg.image_generation.base_url, "https://image.example/v1")
        self.assertEqual(cfg.image_generation.size, "1536x1024")

    def test_image_generation_accepts_b64_json_and_saves_result(self) -> None:
        png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")

        class ImageHandler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                request = json.loads(self.rfile.read(length))
                self.server.request_payload = request
                body = json.dumps({"data": [{"b64_json": base64.b64encode(png).decode(),
                                               "revised_prompt": "一只橘猫"}]}).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), ImageHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            service = object.__new__(AIReplyService)
            service.cfg = SimpleNamespace(image_generation=ImageGenerationConfig(
                enabled=True, base_url=f"http://127.0.0.1:{server.server_port}/v1",
                model="mock-image", size="1024x1024", quality="standard", timeout_seconds=10,
                response_format="b64_json",
            ))
            with patch("ai_reply.ai_reply_server.DEFAULT_HOME", Path(self.tmp.name)):
                result = service.generate_image("一只橘猫")
            self.assertEqual(Path(result["file"]).read_bytes(), png)
            self.assertEqual(server.request_payload["model"], "mock-image")
            self.assertEqual(server.request_payload["prompt"], "一只橘猫")
            self.assertEqual(result["revised_prompt"], "一只橘猫")
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_image_generation_retries_one_temporary_upstream_cooldown(self) -> None:
        png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")

        class ImageHandler(BaseHTTPRequestHandler):
            attempts = 0

            def log_message(self, *_args):
                return

            def do_POST(self):
                type(self).attempts += 1
                length = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(length)
                if type(self).attempts == 1:
                    body = json.dumps({"error": {"code": "upstream_cooling",
                                                  "message": "上游账号正在冷却"}}).encode()
                    self.send_response(429); self.send_header("Retry-After", "1")
                else:
                    body = json.dumps({"data": [{"b64_json": base64.b64encode(png).decode()}]}).encode()
                    self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), ImageHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            service = object.__new__(AIReplyService)
            service.cfg = SimpleNamespace(image_generation=ImageGenerationConfig(
                enabled=True, base_url=f"http://127.0.0.1:{server.server_port}/v1",
                model="mock-image", response_format="b64_json",
            ))
            with patch("ai_reply.ai_reply_server.DEFAULT_HOME", Path(self.tmp.name)), \
                    patch("ai_reply.ai_reply_server.time.sleep") as sleep_mock:
                result = service.generate_image("猫")
            self.assertEqual(ImageHandler.attempts, 2)
            sleep_mock.assert_called_once_with(1.0)
            self.assertEqual(result["request_attempts"], 2)
            self.assertEqual(Path(result["file"]).read_bytes(), png)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_image_generation_uses_actual_jpeg_extension(self) -> None:
        jpeg = b"\xff\xd8\xff\xe0" + b"jpeg-test"

        class ImageHandler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(length)
                body = json.dumps({"data": [{"b64_json": base64.b64encode(jpeg).decode()}]}).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), ImageHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            service = object.__new__(AIReplyService)
            service.cfg = SimpleNamespace(image_generation=ImageGenerationConfig(
                enabled=True, base_url=f"http://127.0.0.1:{server.server_port}/v1",
                model="mock-image", response_format="b64_json",
            ))
            with patch("ai_reply.ai_reply_server.DEFAULT_HOME", Path(self.tmp.name)):
                result = service.generate_image("测试 JPEG")
            self.assertTrue(result["file"].endswith(".jpg"))
            self.assertEqual(result["mime_type"], "image/jpeg")
            self.assertEqual(Path(result["file"]).read_bytes(), jpeg)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_member_name_resolution_never_exposes_wxid(self) -> None:
        user_id = "wxid_member_123"
        self.store.upsert_member("group-large", user_id, user_id, user_id)
        self.store.upsert_member("group-known", user_id, "风", "风")
        self.assertEqual(self.store.resolve_member_name("group-large", user_id, user_id), "风")
        self.assertEqual(self.store.resolve_member_name("group-large", "wxid_unknown", "wxid_unknown"), "群友")

    def test_member_name_resolution_recovers_quoted_display_name(self) -> None:
        group_id = "18725461928@chatroom"
        user_id = "wxid_8f3s1m3giuy022"
        self.store.upsert_member(group_id, user_id, user_id, "群友")
        self.store.upsert_member("another-group", user_id, "跨群昵称", "跨群昵称")
        quoted = (
            "<msg><appmsg><refermsg><chatusr>wxid_8f3s1m3giuy022</chatusr>"
            "<displayname>粉嘟嘟.</displayname><content>历史消息</content>"
            "</refermsg></appmsg></msg>"
        )
        self.store.add_message({
            "event_id": "quoted-name", "direction": "incoming", "group_id": group_id,
            "user_id": "other-member", "sender_name": "其他成员", "message_id": "quoted-name",
            "event_time": time.time(), "text": "引用历史消息", "raw_message": quoted,
        })
        self.assertEqual(self.store.resolve_member_name(group_id, user_id, user_id), "粉嘟嘟.")
        with self.store.connect() as db:
            row = db.execute(
                "SELECT display_name FROM members WHERE group_id=? AND user_id=?",
                (group_id, user_id),
            ).fetchone()
        self.assertEqual(row["display_name"], "粉嘟嘟.")

    def test_parse_event_uses_known_human_name_for_internal_nickname(self) -> None:
        service = object.__new__(AIReplyService)
        service.store = self.store
        service.cfg = SimpleNamespace(target_groups={"group-large": "大群"})
        self.store.upsert_member("group-known", "wxid_member_123", "风", "风")
        evt, reason = service.parse_event({
            "post_type": "message", "message_type": "group", "group_id": "group-large",
            "user_id": "wxid_member_123", "self_id": "bot", "message_id": "m-name",
            "time": int(time.time()), "sender": {"user_id": "wxid_member_123", "nickname": "wxid_member_123"},
            "message": [{"type": "text", "data": {"text": "在吗"}}],
        })
        self.assertEqual(reason, "ok")
        self.assertIsNotNone(evt)
        self.assertEqual(evt.sender_name, "风")

    def test_parse_event_repairs_chatroom_sender_from_raw_prefix(self) -> None:
        service = object.__new__(AIReplyService)
        service.store = self.store
        group_id = "18725461928@chatroom"
        service.cfg = SimpleNamespace(target_groups={group_id: "PT站看片狂魔小群"})
        evt, reason = service.parse_event({
            "post_type": "message", "message_type": "group", "group_id": group_id,
            "user_id": group_id, "self_id": "bot", "message_id": "m-legacy-user",
            "time": int(time.time()), "sender": {"user_id": group_id, "nickname": group_id},
            "raw_message": "saarjoye:\n姆巴佩回家了？",
            "message": [{"type": "text", "data": {"text": "姆巴佩回家了？"}}],
        })
        self.assertEqual(reason, "ok")
        self.assertIsNotNone(evt)
        self.assertEqual(evt.user_id, "saarjoye")
        self.assertEqual(evt.sender_name, "群友")
        self.assertFalse(evt.sender_name.endswith("@chatroom"))

    def test_model_face_marker_sends_only_media(self) -> None:
        service = object.__new__(AIReplyService)
        service.cfg = SimpleNamespace(
            media_reply=MediaReplyConfig(face_probability=1), dry_run=False,
            reply_prefix="", send_delay_seconds=0, max_reply_chars=600,
        )
        service.command_reply = lambda _evt: None
        service.generate_reply = lambda _evt: "@风\u2005 /发表情 走开啊别拍我"
        service.select_face_pack_item = lambda _evt, _query: {"id": 6, "file": "/tmp/face.gif"}
        sent_media = []
        service.send_face_pack_tool = lambda _evt, query, quiet=False, selected_item=None: sent_media.append((query, selected_item["id"])) or "__NO_TEXT_REPLY__"
        service.send_group_msg = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("不得发送 marker 文字"))
        service._task_for_event = lambda _evt: None
        histories = []
        service._record_history = lambda _evt, value: histories.append(value)
        evt = event("group-a", "user-a", "marker", "太好笑了")
        service.handle_event(evt)
        self.assertEqual(sent_media, [("走开啊别拍我", 6)])
        self.assertIn("无文字回复", histories[0])

    def test_face_index_does_not_fall_back_to_unrelated_recent_asset(self) -> None:
        gif = Path(self.tmp.name) / "face.gif"
        gif.write_bytes(b"GIF89a" + b"\x00" * 32)
        self.store.add_message({
            "event_id": "face-event", "direction": "incoming", "group_id": "group-a", "user_id": "u-a",
            "message_id": "face-message", "event_time": time.time(), "text": "", "raw_message": "<emoji md5=\"abcdabcdabcdabcd\" />",
            "segments": [{"type": "face", "data": {"file": str(gif)}}],
        })
        media_id = self.store.media("group-a", "image", 1)[0]["id"]
        self.store.save_media_annotation(media_id, "走开啊 别拍我", "婴儿摆手表情", tags=["婴儿", "搞笑"], keywords=["走开啊", "别拍我"])
        matched = self.store.search_face_assets("走开啊别拍我", "group-a")
        self.assertEqual(len(matched), 1)
        self.assertGreaterEqual(matched[0]["match_score"], .65)
        self.assertGreaterEqual(self.store.search_face_assets("太好笑了", "group-a")[0]["match_score"], .65)
        self.assertEqual(self.store.search_face_assets("一只蓝色飞机", "group-a"), [])

    def test_media_reply_defaults_are_context_gated(self) -> None:
        cfg = MediaReplyConfig.from_raw({})
        self.assertEqual(cfg.voice_probability, .15)
        self.assertEqual(cfg.face_probability, .20)
        self.assertEqual(cfg.voice_min_fit, 55)
        self.assertEqual(cfg.face_min_fit, 45)
        self.assertEqual(cfg.min_candidate_confidence, .65)

    def test_legacy_shared_media_fit_migrates_to_separate_thresholds(self) -> None:
        cfg = MediaReplyConfig.from_raw({"media_reply": {"min_fit": 80}})
        self.assertEqual(cfg.voice_min_fit, 80)
        self.assertEqual(cfg.face_min_fit, 80)

    def test_model_selected_text_is_not_overridden_by_fit_or_probability(self) -> None:
        service = object.__new__(AIReplyService)
        service.cfg = SimpleNamespace(media_reply=MediaReplyConfig(
            voice_probability=1, face_probability=1, voice_min_fit=55,
            face_min_fit=45, min_candidate_confidence=.65,
        ))
        service._task_for_event = lambda _evt: None
        service.select_voice_pack_item = lambda *_args, **_kwargs: {"id": 1, "title": "语音"}
        service.voice_candidate_confidence = lambda _item: .9
        service.select_face_pack_item = lambda *_args, **_kwargs: {
            "id": 2, "image_summary": "表情", "match_score": .8,
        }
        evt = event("group-a", "user-a", "media-gates", "@AI小助手给我找几个黑丝美腿番号")
        decision = SimpleNamespace(
            medium="text", voice_fit=54, face_fit=46, media_query="搞笑", intent="调侃",
        )
        medium, item, details = service.choose_auto_medium(evt, decision)
        self.assertEqual(medium, "text")
        self.assertEqual(item, {})
        self.assertEqual(details["voice_gate"], "model_selected_text")
        self.assertEqual(details["face_gate"], "model_selected_text")
        self.assertEqual(details["selected_medium"], "text")

    def test_negative_face_request_is_not_treated_as_explicit_media(self) -> None:
        scorer = OpportunityScorer(BrainConfig())
        evt = event("group-a", "user-a", "negative-face", "@AI小助手别发表情给我找番号")
        result = scorer.local_score(evt, [], {"items": [], "culture": {}}, None)
        self.assertFalse(result["explicit_media"])

        service = object.__new__(AIReplyService)
        service.cfg = SimpleNamespace(media_reply=MediaReplyConfig(
            voice_probability=1, face_probability=1, voice_min_fit=1,
            face_min_fit=1, min_candidate_confidence=0,
        ))
        service._task_for_event = lambda _evt: None
        service.select_voice_pack_item = lambda *_args, **_kwargs: {}
        service.voice_candidate_confidence = lambda _item: 0
        service.select_face_pack_item = lambda *_args, **_kwargs: {
            "id": 1, "image_summary": "走开啊别拍我", "match_score": 1,
        }
        decision = SimpleNamespace(
            medium="face", voice_fit=0, face_fit=100, media_query="拒绝", intent="拒绝",
        )
        medium, item, details = service.choose_auto_medium(evt, decision)
        self.assertEqual((medium, item), ("text", {}))
        self.assertEqual(details["face_gate"], "user_suppressed")

    def test_model_medium_preference_is_not_discarded(self) -> None:
        service = object.__new__(AIReplyService)
        service.cfg = SimpleNamespace(media_reply=MediaReplyConfig(
            voice_probability=1, face_probability=1, voice_min_fit=55,
            face_min_fit=45, min_candidate_confidence=.65,
        ))
        service._task_for_event = lambda _evt: None
        service.select_voice_pack_item = lambda *_args, **_kwargs: {}
        service.voice_candidate_confidence = lambda _item: 0
        service.select_face_pack_item = lambda *_args, **_kwargs: {
            "id": 9, "image_summary": "大笑表情", "match_score": .86,
        }
        evt = event("group-a", "user-a", "media-preferred", "太好笑了")
        decision = SimpleNamespace(
            medium="face", voice_fit=8, face_fit=42, media_query="大笑 搞笑", intent="搞笑",
        )
        medium, item, details = service.choose_auto_medium(evt, decision)
        self.assertEqual(medium, "face")
        self.assertEqual(item["id"], 9)
        self.assertEqual(details["face_fit"], 42)
        self.assertEqual(details["face_effective_fit"], 45)
        self.assertEqual(details["requested_medium"], "face")

    def test_face_vector_candidate_can_beat_weak_lexical_match(self) -> None:
        service = object.__new__(AIReplyService)
        service.cfg = SimpleNamespace(media_reply=MediaReplyConfig(global_face_assets=True))
        service.store = SimpleNamespace(search_face_assets=lambda *_args, **_kwargs: [
            {"id": 1, "file": "/tmp/weak.gif", "match_score": .41, "image_summary": "弱关键词"},
        ])
        evt = event("group-a", "user-a", "vector-face", "太好笑了")
        evt.raw["_brain_memory"] = {"asset_candidates": [
            {"object_type": "face_asset", "id": 2, "file": "/tmp/good.gif", "enabled": 1,
             "vector_score": .88, "image_summary": "捧腹大笑"},
        ]}
        selected = service.select_face_pack_item(evt, "大笑 搞笑")
        self.assertEqual(selected["id"], 2)
        self.assertEqual(selected["match_reason"], "向量语义匹配")

    def test_fast_pipeline_does_not_call_remote_opportunity_scorer(self) -> None:
        cfg = BrainConfig(threshold=0, scoring_mode="local_fast", rerank_candidates=12)
        registry = TaskRegistry(self.store)
        service = object.__new__(AIReplyService)
        service.cfg = SimpleNamespace(brain=cfg)
        service.store = self.store
        service.task_registry = registry
        service.scorer = OpportunityScorer(cfg)
        service.embedding_service = SimpleNamespace(search=lambda *_args, **_kwargs: {
            "items": [], "culture": {}, "error": "", "timings_ms": {"total": 1.0},
            "recalled_count": 0, "reranked_count": 0,
        })
        service.score_reply_opportunity = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("remote opportunity scorer must not run in local_fast mode")
        )
        evt = event("group-a", "user-a", "m-fast", "小风，这个索引怎么建？")
        task = registry.create(evt, "thread-fast")
        service.handle_event = lambda _evt: registry.update(task, "completed", medium="text", result="完成")
        service.handle_brain_task(task, evt)
        self.assertEqual(task.state, "completed")
        self.assertEqual(task.model, "local-fast")
        self.assertLess(task.details["timings_ms"]["scoring"], 100)

    def test_alias_trigger_must_bypass_final_threshold(self) -> None:
        cfg = BrainConfig(threshold=80, scoring_mode="local_fast", rerank_candidates=12)
        registry = TaskRegistry(self.store)
        service = object.__new__(AIReplyService)
        service.cfg = SimpleNamespace(brain=cfg)
        service.store = self.store
        service.task_registry = registry
        local = {
            "pre_score": 34, "mandatory": True, "at_self": False, "reply_id": "",
            "alias_hit": "小风", "explicit_media": False, "reasons": [],
        }
        service.scorer = SimpleNamespace(
            local_score=lambda *_args, **_kwargs: dict(local),
            local_factors=lambda *_args, **_kwargs: ({key: 50 for key in cfg.factor_weights}, "别名触发"),
            final_score=lambda *_args, **_kwargs: 61.2,
        )
        service.embedding_service = SimpleNamespace(search=lambda *_args, **_kwargs: {
            "items": [], "culture": {}, "error": "", "timings_ms": {"total": 1.0},
            "recalled_count": 0, "reranked_count": 0,
        })
        evt = event("group-a", "user-a", "m-hard-threshold", "小风，你在吗")
        task = registry.create(evt, "thread-hard-threshold")
        service.handle_event = lambda _evt: registry.update(task, "completed", medium="text", result="已回复")
        service.handle_brain_task(task, evt)
        self.assertEqual(task.state, "completed")
        self.assertEqual(task.result, "已回复")
        self.assertEqual(task.score, 61.2)
        self.assertEqual(task.details["threshold_gate"], "mandatory_bypass")
        self.assertTrue(task.details["below_threshold"])
        self.assertTrue(task.details["trigger_was_mandatory"])
        self.assertEqual(task.details["mandatory_reason"], "bot_alias")

    def test_cdata_wrapped_at_self_must_bypass_final_threshold(self) -> None:
        cfg = BrainConfig(threshold=100, scoring_mode="local_fast", bot_aliases=[])
        registry = TaskRegistry(self.store)
        service = object.__new__(AIReplyService)
        service.cfg = SimpleNamespace(brain=cfg)
        service.store = self.store
        service.task_registry = registry
        service.scorer = OpportunityScorer(cfg)
        service.embedding_service = SimpleNamespace(search=lambda *_args, **_kwargs: {
            "items": [], "culture": {}, "error": "", "timings_ms": {"total": 1.0},
            "recalled_count": 0, "reranked_count": 0,
        })
        evt = event("group-a", "user-a", "m-cdata-at", "@AI小助手 直接把架构代码给我")
        evt.raw["message"].append({"type": "at", "data": {"qq": "<![CDATA[bot]]>"}})
        task = registry.create(evt, "thread-cdata-at")
        service.handle_event = lambda _evt: registry.update(task, "completed", medium="text", result="已回复")
        service.handle_brain_task(task, evt)
        self.assertEqual(task.state, "completed")
        self.assertEqual(task.result, "已回复")
        self.assertTrue(task.details["trigger_was_mandatory"])
        self.assertEqual(task.details["mandatory_reason"], "at_self")

        evt_other = event("group-a", "user-a", "m-cdata-at-other", "@其他人 你看看")
        evt_other.raw["message"].append({"type": "at", "data": {"qq": "<![CDATA[someone-else]]>"}})
        local = service.scorer.local_score(evt_other, [], {"items": [], "culture": {}}, None)
        self.assertFalse(local["at_self"])
        self.assertFalse(local["mandatory"])

    def test_ordinary_message_still_obeys_final_threshold(self) -> None:
        cfg = BrainConfig(threshold=80, scoring_mode="local_fast", rerank_candidates=12)
        registry = TaskRegistry(self.store)
        service = object.__new__(AIReplyService)
        service.cfg = SimpleNamespace(brain=cfg)
        service.store = self.store
        service.task_registry = registry
        local = {
            "pre_score": 34, "mandatory": False, "at_self": False, "reply_id": "",
            "alias_hit": "", "explicit_media": False, "reasons": [],
        }
        service.scorer = SimpleNamespace(
            local_score=lambda *_args, **_kwargs: dict(local),
            local_factors=lambda *_args, **_kwargs: ({key: 50 for key in cfg.factor_weights}, "普通消息"),
            final_score=lambda *_args, **_kwargs: 61.2,
        )
        service.embedding_service = SimpleNamespace(search=lambda *_args, **_kwargs: {
            "items": [], "culture": {}, "error": "", "timings_ms": {"total": 1.0},
            "recalled_count": 0, "reranked_count": 0,
        })
        evt = event("group-a", "user-a", "m-ordinary-threshold", "大家在聊什么")
        task = registry.create(evt, "thread-ordinary-threshold")
        service.handle_event = lambda _evt: (_ for _ in ()).throw(AssertionError("普通低分消息不得进入回复生成"))
        service.handle_brain_task(task, evt)
        self.assertEqual(task.state, "skipped")
        self.assertEqual(task.result, "score_below_threshold")
        self.assertEqual(task.details["threshold_gate"], "below_threshold")

    def test_explicit_media_command_still_bypasses_opportunity_threshold(self) -> None:
        cfg = BrainConfig(threshold=100, scoring_mode="local_fast")
        registry = TaskRegistry(self.store)
        service = object.__new__(AIReplyService)
        service.cfg = SimpleNamespace(brain=cfg)
        service.task_registry = registry
        evt = event("group-a", "user-a", "m-explicit-face", "/发表情 走开啊别拍我")
        task = registry.create(evt, "thread-explicit-face")
        service.handle_event = lambda _evt: registry.update(task, "completed", medium="face", result="sent")
        service.handle_brain_task(task, evt)
        self.assertEqual(task.state, "completed")
        self.assertEqual(task.medium, "face")

        image_evt = event("group-a", "user-a", "m-explicit-image", "@小风 /生图 一只坐在月球上的橘猫")
        image_task = registry.create(image_evt, "thread-explicit-image")
        service.handle_event = lambda _evt: registry.update(
            image_task, "completed", medium="image", result="generated.jpg"
        )
        service.handle_brain_task(image_task, image_evt)
        self.assertEqual(image_task.state, "completed")
        self.assertEqual(image_task.medium, "image")
        self.assertEqual(image_task.result, "generated.jpg")

        natural_evt = event("group-a", "user-a", "m-natural-image", "画个哈士奇")
        natural_task = registry.create(natural_evt, "thread-natural-image")
        service.handle_event = lambda _evt: registry.update(
            natural_task, "completed", medium="image", result="husky.jpg"
        )
        service.handle_brain_task(natural_task, natural_evt)
        self.assertEqual(natural_task.state, "completed")
        self.assertEqual(natural_task.medium, "image")
        self.assertEqual(natural_task.result, "husky.jpg")

    def test_group_blacklist_is_checked_after_message_persistence(self) -> None:
        service = object.__new__(AIReplyService)
        service.store = self.store
        service.state_lock = threading.RLock()
        service.seen = {}
        service.schedule_culture_learning = lambda _evt: None
        service.cfg = SimpleNamespace(
            log_all_group_messages=False, enabled=True, target_groups={"group-a": "A"},
            ignore_self_messages=False, allowed_user_ids=[], ignored_user_ids=[],
            ignored_group_members={"group-a": ["bot-user"]}, ignore_prefixes=[],
            require_keyword=False, trigger_keywords=[],
        )
        evt = event("group-a", "bot-user", "m-blacklisted", "其他机器人的回复")
        evt.raw_message = evt.text
        evt.media_types = []
        evt.timestamp = int(time.time())
        service.persist_incoming(evt)
        self.assertFalse(service.should_reply(evt))
        saved = self.store.search_messages(group_id="group-a", user_id="bot-user", limit=10)
        self.assertEqual([row["event_id"] for row in saved], ["m-blacklisted"])

    def test_custom_threshold_below_twenty_is_respected_by_prefilter(self) -> None:
        cfg = BrainConfig(threshold=15, scoring_mode="local_fast")
        registry = TaskRegistry(self.store)
        service = object.__new__(AIReplyService)
        service.cfg = SimpleNamespace(brain=cfg)
        service.store = self.store
        service.task_registry = registry
        service.scorer = OpportunityScorer(cfg)
        calls = []
        service.embedding_service = SimpleNamespace(search=lambda *_args, **_kwargs: calls.append(True) or {
            "items": [], "culture": {}, "error": "", "timings_ms": {"total": 1.0},
            "recalled_count": 0, "reranked_count": 0,
        })
        evt = event("group-a", "user-a", "m-new", "我需要帮忙 在吗")
        self.store.add_message({"event_id": "m-old", "direction": "incoming", "group_id": "group-a",
                                "user_id": "user-a", "message_id": "m-old", "event_time": time.time() - 5,
                                "text": "我需要帮忙 在吗", "raw_message": "我需要帮忙 在吗"})
        task = registry.create(evt, "thread-threshold")
        service.handle_event = lambda _evt: registry.update(task, "completed", medium="text", result="在，怎么了")
        service.handle_brain_task(task, evt)
        self.assertEqual(calls, [True])
        self.assertEqual(task.state, "completed")

    def test_same_thread_serial_and_different_groups_parallel(self) -> None:
        cfg = BrainConfig(global_workers=4, per_group_workers=3)
        registry = TaskRegistry(self.store)
        scheduler = ReplyScheduler(cfg, registry)
        scheduler.start()
        lock = threading.Lock()
        active_threads: set[str] = set()
        overlaps: list[str] = []
        active_total = 0
        max_active = 0
        done = threading.Event()
        completed = 0

        def handler(task, _evt):
            nonlocal active_total, max_active, completed
            with lock:
                if task.thread_id in active_threads:
                    overlaps.append(task.thread_id)
                active_threads.add(task.thread_id)
                active_total += 1
                max_active = max(max_active, active_total)
            time.sleep(0.12)
            registry.update(task, "completed")
            with lock:
                active_threads.remove(task.thread_id)
                active_total -= 1
                completed += 1
                if completed == 3:
                    done.set()

        first = event("group-a", "user-a", "m1", "部署什么时候完成")
        followup = event("group-a", "user-a", "m2", "然后呢", "m1")
        other = event("group-b", "user-b", "m3", "另一个群的问题")
        t1 = scheduler.submit(first, handler)
        t2 = scheduler.submit(followup, handler)
        scheduler.submit(other, handler)
        self.assertEqual(t1.thread_id, t2.thread_id)
        self.assertTrue(done.wait(5), "reply tasks did not finish")
        scheduler.stop()
        self.assertEqual(overlaps, [])
        self.assertGreaterEqual(max_active, 2)

    def test_ordinary_messages_merge_but_explicit_followup_stays_queued(self) -> None:
        cfg = BrainConfig(global_workers=1, per_group_workers=1, merge_window_ms=2500)
        registry = TaskRegistry(self.store)
        scheduler = ReplyScheduler(cfg, registry)
        # Submit before start so the merge window is deterministic.
        first = scheduler.submit(event("group-a", "user-a", "m1", "我先补充项目背景"), lambda *_: None)
        merged = scheduler.submit(event("group-a", "user-a", "m2", "项目背景是昨天开始的"), lambda *_: None)
        followup = scheduler.submit(event("group-a", "user-a", "m3", "然后呢？", "m2"), lambda *_: None)
        self.assertEqual(merged.state, "cancelled")
        self.assertEqual(merged.details["merged_into"], first.task_id)
        self.assertEqual(followup.state, "queued")
        self.assertEqual(scheduler.snapshot()["queued"], 2)
        scheduler.stop()

    def test_same_member_new_topic_creates_a_new_thread(self) -> None:
        cfg = BrainConfig()
        scheduler = ReplyScheduler(cfg, TaskRegistry(self.store))
        first = scheduler.submit(event("group-a", "user-a", "m1", "天气预报明天会下雨吗"), lambda *_: None)
        second = scheduler.submit(event("group-a", "user-a", "m2", "数据库索引应该怎么建"), lambda *_: None)
        self.assertNotEqual(first.thread_id, second.thread_id)
        scheduler.stop()

    def test_4096_float_vector_is_persisted_and_group_isolated(self) -> None:
        vector = [0.0] * 4096
        vector[17] = 1.0
        self.store.upsert_embedding("message", "one", "group-a", "经典老梗", "test-model", vector)
        self.store.upsert_embedding("message", "two", "group-b", "其他群内容", "test-model", vector)
        rows = self.store.semantic_search(vector, "group-a", "test-model", 10)
        self.assertEqual([row["object_id"] for row in rows], ["one"])
        self.assertEqual(rows[0]["dimensions"], 4096)
        self.assertGreater(rows[0]["score"], 0.99)

    def test_voice_item_delete_removes_search_and_embedding_state(self) -> None:
        voice_file = Path(self.tmp.name) / "low-quality.silk"
        voice_file.write_bytes(b"voice-data")
        pack_id = self.store.upsert_voice_pack("低质量包", "测试")
        self.assertTrue(self.store.add_voice_item(
            pack_id, "测试", "低质量语音", "低质量语音", str(voice_file), "silk", voice_file.stat().st_size,
        ))
        item_id = int(self.store.voice_items(pack_id=pack_id, limit=1)[0]["id"])
        self.store.upsert_embedding("voice_pack", str(item_id), "__global__", "低质量语音", "test-model", [1.0, 0.0])
        result = self.store.delete_voice_item(item_id)
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(self.store.voice_item(item_id), {})
        self.assertEqual(self.store.search_voice_items("低质量语音", limit=10), [])
        with self.store.connect() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM semantic_embeddings WHERE object_type='voice_pack' AND object_id=?", (str(item_id),)
            ).fetchone()[0], 0)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM embedding_jobs WHERE object_type='voice_pack' AND object_id=?", (str(item_id),)
            ).fetchone()[0], 0)
        self.assertEqual(self.store.voice_pack(pack_id)["item_count"], 0)

    def test_deleted_voice_cannot_be_resurrected_by_running_embedding_job(self) -> None:
        ghost_job = {
            "id": 999999, "object_type": "voice_pack", "object_id": "404",
            "group_id": "__global__", "text": "已经删除的语音",
        }
        completed = self.store.upsert_embeddings_batch([ghost_job], [[1.0, 0.0]], "test-model")
        self.assertEqual(completed, 0)
        with self.store.connect() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM semantic_embeddings WHERE object_type='voice_pack' AND object_id='404'"
            ).fetchone()[0], 0)

    def test_delete_voice_pack_removes_all_items_and_pack(self) -> None:
        pack_id = self.store.upsert_voice_pack("整包删除", "测试")
        for index in range(2):
            path = Path(self.tmp.name) / f"pack-{index}.silk"
            path.write_bytes(f"voice-{index}".encode())
            self.store.add_voice_item(pack_id, "测试", f"语音{index}", f"语音{index}", str(path), "silk")
        result = self.store.delete_voice_pack(pack_id)
        self.assertEqual(result["deleted"], 2)
        self.assertEqual(self.store.voice_pack(pack_id), {})
        self.assertEqual(self.store.voice_items(pack_id=pack_id, limit=10), [])

    def test_permanent_culture_is_saved_and_enqueued(self) -> None:
        self.store.upsert_alias("group-a", "user-a", "老王", 0.2, ["m1"])
        self.store.upsert_meme("group-a", "又在画饼", "项目延期时的固定玩笑", ["画饼"], ["m2"], confidence=0.1)
        culture = self.store.culture_context("group-a", "", 20)
        self.assertEqual(culture["aliases"][0]["alias"], "老王")
        self.assertEqual(culture["memes"][0]["name"], "又在画饼")
        jobs = self.store.pending_embedding_jobs(20)
        self.assertEqual({row["object_type"] for row in jobs}, {"alias", "meme"})

    def test_reply_task_state_is_persistent(self) -> None:
        registry = TaskRegistry(self.store)
        task = registry.create(event("group-a", "user-a", "m1", "问题"), "thread-a")
        registry.update(task, "retrieving_memory", score=55, threshold=52)
        registry.update(task, "completed", medium="text", result="回答")
        row = self.store.reply_tasks("group-a", 10)[0]
        self.assertEqual(row["state"], "completed")
        self.assertEqual(row["medium"], "text")
        self.assertIsNotNone(row["completed_at"])

    def test_eight_concurrent_sqlite_writers_do_not_corrupt_state(self) -> None:
        def write(index: int) -> None:
            registry = TaskRegistry(self.store)
            task = registry.create(event(f"group-{index % 2}", f"user-{index}", f"m-{index}", f"问题 {index}"), f"thread-{index}")
            self.store.upsert_alias(f"group-{index % 2}", f"user-{index}", f"外号-{index}", 0.5, [f"m-{index}"])
            registry.update(task, "completed", medium="text", result=f"回答 {index}")

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(write, range(8)))
        self.assertEqual(len(self.store.reply_tasks(limit=20)), 8)
        self.assertEqual(self.store.stats()["aliases"], 8)


if __name__ == "__main__":
    unittest.main()
