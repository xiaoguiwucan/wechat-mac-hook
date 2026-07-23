#!/usr/bin/env python3
"""Asynchronous Graphiti/FalkorDB projection with a low-cost local embedder."""
from __future__ import annotations

import asyncio
import hashlib
import math
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional


class HashEmbedder:
    """Deterministic 1024-D character n-gram embedder.

    Graphiti may be pointed at a cloud OpenAI-compatible embedding endpoint
    later. This fallback keeps the temporal graph searchable without loading an
    8B local model into memory.
    """

    dimensions = 1024

    @staticmethod
    def _vector(value: str) -> List[float]:
        text = " ".join(str(value or "").lower().split())
        grams = [text[i:i + 2] for i in range(max(1, len(text) - 1))]
        vector = [0.0] * HashEmbedder.dimensions
        for gram in grams:
            digest = hashlib.blake2b(gram.encode("utf-8", "ignore"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % HashEmbedder.dimensions
            vector[index] += -1.0 if digest[4] & 1 else 1.0
        norm = math.sqrt(sum(item * item for item in vector)) or 1.0
        return [item / norm for item in vector]

    async def create(self, input_data: Any) -> List[float]:
        return self._vector(str(input_data or ""))

    async def create_batch(self, input_data_list: List[str]) -> List[List[float]]:
        return [self._vector(item) for item in input_data_list]


class LexicalCrossEncoder:
    """Small deterministic reranker used only inside the async graph worker."""

    @staticmethod
    def _tokens(value: str) -> set[str]:
        text = " ".join(str(value or "").lower().split())
        chars = {char for char in text if not char.isspace()}
        return chars | {text[i:i + 2] for i in range(max(0, len(text) - 1))}

    async def rank(self, query: str, passages: List[str]) -> List[tuple[str, float]]:
        query_tokens = self._tokens(query)
        scored = []
        for passage in passages:
            passage_tokens = self._tokens(passage)
            score = len(query_tokens & passage_tokens) / max(1, len(query_tokens))
            scored.append((passage, score))
        return sorted(scored, key=lambda item: item[1], reverse=True)


@dataclass
class GraphitiConfig:
    enabled: bool
    postgres_dsn: str
    falkordb_host: str
    falkordb_port: int
    database: str
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    poll_seconds: float

    @classmethod
    def from_env(cls) -> "GraphitiConfig":
        return cls(
            enabled=os.getenv("GRAPHITI_ENABLED", "0").lower() in {"1", "true", "yes", "on"},
            postgres_dsn=os.getenv("WECHAT_POSTGRES_DSN", ""),
            falkordb_host=os.getenv("FALKORDB_HOST", "127.0.0.1"),
            falkordb_port=int(os.getenv("FALKORDB_PORT", "6379")),
            database=os.getenv("FALKORDB_DATABASE", "wechat_memory"),
            llm_base_url=os.getenv("GRAPHITI_LLM_BASE_URL", "http://127.0.0.1:8000/v1"),
            llm_api_key=os.getenv("GRAPHITI_LLM_API_KEY", ""),
            llm_model=os.getenv("GRAPHITI_LLM_MODEL", "grok-chat-fast"),
            poll_seconds=max(0.5, min(30.0, float(os.getenv("GRAPHITI_POLL_SECONDS", "2")))),
        )


class GraphitiBridge:
    def __init__(self, config: Optional[GraphitiConfig] = None):
        self.cfg = config or GraphitiConfig.from_env()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._thread_main, name="graphiti-worker", daemon=True)
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.graphiti: Any = None
        self.lock = threading.RLock()
        self.state: Dict[str, Any] = {
            "enabled": self.cfg.enabled, "ready": False, "processed": 0,
            "failed": 0, "last_error": "", "last_sync_at": "",
        }

    def start(self) -> None:
        if self.cfg.enabled and not self.thread.is_alive():
            self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.loop:
            self.loop.call_soon_threadsafe(lambda: None)

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            return dict(self.state)

    @staticmethod
    def graph_group_id(group_id: str) -> str:
        """Map WeChat's ``@chatroom`` IDs to Graphiti-safe namespaces."""
        digest = hashlib.sha256(str(group_id).encode("utf-8", "ignore")).hexdigest()
        return f"wechat_{digest[:32]}"

    async def _initialize(self) -> None:
        from graphiti_core import Graphiti  # type: ignore
        from graphiti_core.cross_encoder.client import CrossEncoderClient  # type: ignore
        from graphiti_core.driver.falkordb_driver import FalkorDriver  # type: ignore
        from graphiti_core.embedder.client import EmbedderClient  # type: ignore
        from graphiti_core.llm_client.config import LLMConfig  # type: ignore
        from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient  # type: ignore

        class GraphitiHashEmbedder(HashEmbedder, EmbedderClient):
            pass

        class GraphitiLexicalCrossEncoder(LexicalCrossEncoder, CrossEncoderClient):
            pass

        driver = FalkorDriver(
            host=self.cfg.falkordb_host, port=self.cfg.falkordb_port,
            database=self.cfg.database,
        )
        llm = OpenAIGenericClient(LLMConfig(
            api_key=self.cfg.llm_api_key or "local",
            model=self.cfg.llm_model,
            small_model=self.cfg.llm_model,
            base_url=self.cfg.llm_base_url,
            temperature=0,
            max_tokens=4096,
        ), max_tokens=4096, structured_output_mode="json_object")
        self.graphiti = Graphiti(
            graph_driver=driver, llm_client=llm, embedder=GraphitiHashEmbedder(),
            cross_encoder=GraphitiLexicalCrossEncoder(),
            store_raw_episode_content=True, max_coroutines=2,
        )
        await self.graphiti.build_indices_and_constraints()
        with self.lock:
            self.state["ready"] = True
            self.state["last_error"] = ""

    def _connect_pg(self) -> Any:
        import psycopg  # type: ignore
        from psycopg.rows import dict_row  # type: ignore
        return psycopg.connect(
            self.cfg.postgres_dsn, autocommit=True, row_factory=dict_row,
            connect_timeout=3,
        )

    async def _process_pending(self) -> None:
        from graphiti_core.nodes import EpisodeType  # type: ignore

        pg = self._connect_pg()
        try:
            with pg.cursor() as cur:
                cur.execute(
                    """SELECT j.id,j.event_id,j.group_id,e.sender_name,e.user_id,
                              e.event_time,e.text,e.raw_message
                       FROM graph_sync_jobs j JOIN chat_events e ON e.event_id=j.event_id
                       WHERE j.status IN ('pending','retry') AND j.next_attempt_at<=now()
                       ORDER BY j.id LIMIT 10"""
                )
                rows = list(cur.fetchall())
            for row in rows:
                if self.stop_event.is_set():
                    return
                try:
                    body = (
                        f"群成员：{row.get('sender_name') or row.get('user_id')}\n"
                        f"消息：{row.get('text') or row.get('raw_message') or ''}"
                    )
                    reference = datetime.fromtimestamp(
                        int(row.get("event_time") or time.time()) / (
                            1000 if int(row.get("event_time") or 0) > 10_000_000_000 else 1
                        ),
                        timezone.utc,
                    )
                    await self.graphiti.add_episode(
                        name=str(row["event_id"]), episode_body=body,
                        source_description="WeChat group message",
                        reference_time=reference, source=EpisodeType.message,
                        group_id=self.graph_group_id(str(row["group_id"])),
                        update_communities=False,
                    )
                    with pg.cursor() as cur:
                        cur.execute(
                            """UPDATE graph_sync_jobs SET status='synced',error='',
                               updated_at=now() WHERE id=%s""", (row["id"],)
                        )
                    with self.lock:
                        self.state["processed"] += 1
                        self.state["last_sync_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                except Exception as exc:
                    with pg.cursor() as cur:
                        cur.execute(
                            """UPDATE graph_sync_jobs SET status='retry',attempts=attempts+1,
                               error=%s,next_attempt_at=now()+interval '30 seconds',
                               updated_at=now() WHERE id=%s""",
                            (str(exc)[:1000], row["id"]),
                        )
                    with self.lock:
                        self.state["failed"] += 1
                        self.state["last_error"] = str(exc)[:1000]
        finally:
            pg.close()

    async def _worker(self) -> None:
        await self._initialize()
        while not self.stop_event.is_set():
            try:
                await self._process_pending()
            except Exception as exc:
                with self.lock:
                    self.state["ready"] = False
                    self.state["last_error"] = str(exc)[:1000]
            await asyncio.sleep(self.cfg.poll_seconds)

    def _thread_main(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._worker())
        except Exception as exc:
            with self.lock:
                self.state["ready"] = False
                self.state["last_error"] = str(exc)[:1000]
        finally:
            self.loop.close()

    async def _search(self, query: str, group_id: str, limit: int) -> List[Dict[str, Any]]:
        edges = await self.graphiti.search(
            query, group_ids=[self.graph_group_id(group_id)],
            num_results=max(1, min(20, limit)),
        )
        return [
            {
                "object_type": "graph_fact",
                "object_id": str(getattr(edge, "uuid", "")),
                "text": str(getattr(edge, "fact", "") or getattr(edge, "name", "")),
                "valid_at": str(getattr(edge, "valid_at", "") or ""),
                "invalid_at": str(getattr(edge, "invalid_at", "") or ""),
                "source": "graphiti",
            }
            for edge in edges
        ]

    def search(self, query: str, group_id: str,
               limit: int = 8, timeout_ms: int = 200) -> List[Dict[str, Any]]:
        if not self.loop or not self.graphiti or not self.snapshot().get("ready"):
            return []
        future = asyncio.run_coroutine_threadsafe(
            self._search(query, group_id, limit), self.loop
        )
        try:
            return future.result(timeout=max(0.05, timeout_ms / 1000))
        except Exception:
            future.cancel()
            return []
