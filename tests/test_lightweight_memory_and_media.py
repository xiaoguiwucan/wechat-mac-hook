import tempfile
import unittest
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ai_reply.ai_reply_server import AIChannel, AIReplyService
from embedding_service import EmbeddingConfig, EmbeddingService
from memory_store import MemoryStore


class LightweightMemoryAndMediaTests(unittest.TestCase):
    def test_local_hash_embedding_is_deterministic_and_normalized(self):
        with tempfile.TemporaryDirectory() as temp:
            store = MemoryStore(Path(temp) / "memory.sqlite3")
            cfg = EmbeddingConfig(
                enabled=True, backend="local_hash", model="local-hash-chargram-v1",
                dimensions=4096,
            )
            service = EmbeddingService(store, cfg)
            first, second = service.embed(["上海天气", "上海天气"])
            self.assertEqual(first, second)
            self.assertEqual(len(first), 4096)
            self.assertAlmostEqual(sum(value * value for value in first), 1.0, places=5)

    def test_gif_is_normalized_to_png_before_vision_request(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "face.gif"
            path.write_bytes(
                b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff"
                b"!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00"
                b"\x00\x02\x02D\x01\x00;"
            )
            service = AIReplyService.__new__(AIReplyService)
            value = service.local_image_data_url(str(path))
            self.assertTrue(value.startswith("data:image/png;base64,"))

    def test_corrupt_image_is_rejected_before_remote_ocr(self):
        service = AIReplyService.__new__(AIReplyService)
        with self.assertRaisesRegex(ValueError, "损坏|格式无效"):
            service._normalized_image(b"\xff\xd8\xffbroken", "image/jpeg")

    def test_reasoning_only_length_response_retries_with_more_tokens(self):
        service = AIReplyService.__new__(AIReplyService)
        service.get_api_key = lambda _channel: "test-key"
        channel = AIChannel(
            id="deepseek", name="deepseek", provider="openai_compatible",
            base_url="https://example.test/v1", api_key_env="TEST_KEY",
            model="deepseek-v4-flash", timeout_seconds=4,
        )
        evt = SimpleNamespace(group_id="g@chatroom", trace_id="trace")
        bodies = [
            {"choices": [{"finish_reason": "length", "message": {
                "content": "", "reasoning_content": "still reasoning",
            }}]},
            {"choices": [{"finish_reason": "stop", "message": {"content": "{\"ok\":true}"}}]},
        ]
        requests = []

        class Response:
            def __init__(self, payload):
                self.payload = payload
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                return None
            def read(self):
                return json.dumps(self.payload).encode()

        def fake_urlopen(request, timeout=0):
            requests.append(json.loads(request.data))
            return Response(bodies[len(requests) - 1])

        with patch("ai_reply.ai_reply_server.urllib.request.urlopen", fake_urlopen):
            reply, error = service.request_channel_messages(
                channel, [{"role": "user", "content": "route"}], 192, 0, evt
            )
        self.assertEqual(reply, '{"ok":true}')
        self.assertEqual(error, "")
        self.assertEqual([item["max_tokens"] for item in requests], [192, 384])


if __name__ == "__main__":
    unittest.main()
