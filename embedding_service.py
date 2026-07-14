#!/usr/bin/env python3
"""Local OMLX embedding, reranking, and permanent-memory backfill."""
from __future__ import annotations

import json
import collections
import concurrent.futures
import hashlib
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from memory_store import MemoryStore


DEFAULT_QUERY_INSTRUCTION = (
    "Given a Chinese WeChat group conversation, retrieve past messages, member aliases, "
    "group memes, images, voice transcripts and reaction assets that are most useful for "
    "producing a context-appropriate reply."
)

SIMPLE_SOCIAL_RE = re.compile(
    r"^(?:哈喽|哈啰|嗨|hi|hello|你好|您好|在吗|有人吗|忙吗|早安|早上好|晚安|谢谢|多谢|哈哈|哈哈哈|收到|好的|好嘞|行|可以)"
    r"(?:[\s,，。.!！?？~～]*(?:在吗|有人吗|忙吗|呀|啊|吗|呢|哈|哦|噢|喽|啦)?)?[\s,，。.!！?？~～]*$",
    re.I,
)


@dataclass
class EmbeddingConfig:
    enabled: bool = True
    base_url: str = "http://127.0.0.1:8017/v1"
    model: str = "Qwen3-Embedding-8B-mxfp8"
    reranker_model: str = "Qwen3-Reranker-4B-mxfp8"
    dimensions: int = 4096
    batch_size: int = 32
    timeout_seconds: int = 120
    query_instruction: str = DEFAULT_QUERY_INSTRUCTION
    auto_backfill: bool = True
    vector_limit: int = 60
    fts_limit: int = 30
    person_limit: int = 12
    meme_limit: int = 12
    time_limit: int = 20
    media_limit: int = 16
    fusion_limit: int = 60
    adaptive_rerank: bool = True
    rerank_cache_seconds: int = 600

    @classmethod
    def from_raw(cls, raw: Dict[str, Any]) -> "EmbeddingConfig":
        value = raw.get("embedding") if isinstance(raw.get("embedding"), dict) else {}
        retrieval = raw.get("retrieval") if isinstance(raw.get("retrieval"), dict) else {}
        return cls(
            enabled=bool(value.get("enabled", True)),
            base_url=str(value.get("base_url") or "http://127.0.0.1:8017/v1").rstrip("/"),
            model=str(value.get("model") or "Qwen3-Embedding-8B-mxfp8"),
            reranker_model=str(value.get("reranker_model") or "Qwen3-Reranker-4B-mxfp8"),
            dimensions=max(32, min(4096, int(value.get("dimensions", 4096)))),
            batch_size=max(1, min(64, int(value.get("batch_size", 32)))),
            timeout_seconds=max(5, min(600, int(value.get("timeout_seconds", 120)))),
            query_instruction=str(value.get("query_instruction") or DEFAULT_QUERY_INSTRUCTION),
            auto_backfill=bool(value.get("auto_backfill", True)),
            vector_limit=max(12, min(200, int(retrieval.get("vector_limit", 60)))),
            fts_limit=max(8, min(100, int(retrieval.get("fts_limit", 30)))),
            person_limit=max(4, min(50, int(retrieval.get("person_limit", 12)))),
            meme_limit=max(4, min(50, int(retrieval.get("meme_limit", 12)))),
            time_limit=max(4, min(100, int(retrieval.get("time_limit", 20)))),
            media_limit=max(4, min(100, int(retrieval.get("media_limit", 16)))),
            fusion_limit=max(12, min(100, int(retrieval.get("fusion_limit", 60)))),
            adaptive_rerank=bool(retrieval.get("adaptive_rerank", True)),
            rerank_cache_seconds=max(0, min(3600, int(retrieval.get("rerank_cache_seconds", 600)))),
        )


class EmbeddingService:
    def __init__(self, store: MemoryStore, cfg: EmbeddingConfig,
                 event_callback: Optional[Callable[[Dict[str, Any]], None]] = None):
        self.store = store
        self.cfg = cfg
        self.event_callback = event_callback
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.worker = threading.Thread(target=self._backfill_loop, name="embedding-backfill", daemon=True)
        self.state_lock = threading.RLock()
        self.inference_condition = threading.Condition(threading.RLock())
        self.inference_busy = False
        self.live_waiters = 0
        self.latencies: Dict[str, collections.deque[float]] = {
            "embedding": collections.deque(maxlen=100),
            "reranker": collections.deque(maxlen=100),
        }
        self.model_status_cache: Dict[str, Any] = {"time": 0.0, "models": {}}
        self.rerank_cache: "collections.OrderedDict[str, tuple[float, List[Dict[str, Any]]]]" = collections.OrderedDict()
        self.rerank_cache_lock = threading.RLock()
        self.state: Dict[str, Any] = {
            "running": False, "paused": False, "processed": 0, "failed": 0,
            "last_error": "", "model": cfg.model, "reranker_model": cfg.reranker_model,
        }

    def start(self) -> None:
        if self.cfg.enabled and not self.worker.is_alive():
            self.store.recover_interrupted_work()
            if self.cfg.auto_backfill:
                self.store.enqueue_all_embeddings()
            self.worker.start()

    def stop(self) -> None:
        self.stop_event.set()

    def set_paused(self, paused: bool) -> None:
        if paused:
            self.pause_event.set()
        else:
            self.pause_event.clear()
        with self.state_lock:
            self.state["paused"] = paused
        self._emit("memory_backfill")

    def snapshot(self) -> Dict[str, Any]:
        with self.state_lock:
            result = dict(self.state)
        result.update({"enabled": self.cfg.enabled, "dimensions": self.cfg.dimensions})
        for kind, values in self.latencies.items():
            ordered = sorted(values)
            result[f"{kind}_p50_ms"] = round(ordered[len(ordered) // 2], 1) if ordered else None
            result[f"{kind}_p95_ms"] = round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 1) if ordered else None
        result["local_models"] = self._model_status()
        try:
            stats = self.store.stats()
            result.update({"pending": stats.get("embedding_pending", 0), "vectors": stats.get("vectors", 0)})
        except Exception:
            pass
        return result

    def _emit(self, event_type: str, **extra: Any) -> None:
        if self.event_callback:
            self.event_callback({"type": event_type, "time": time.time(), **self.snapshot(), **extra})

    @staticmethod
    def _local_omlx_key(base_url: str) -> str:
        if not base_url.startswith(("http://127.0.0.1", "http://localhost")):
            return ""
        try:
            settings = json.loads((Path.home() / ".omlx" / "settings.json").read_text(encoding="utf-8"))
            return str((settings.get("auth") or {}).get("api_key") or "")
        except (OSError, ValueError, TypeError):
            return ""

    def _request(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.cfg.base_url + path, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        key = self._local_omlx_key(self.cfg.base_url)
        if key:
            req.add_header("Authorization", "Bearer " + key)
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.timeout_seconds) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace") if exc.fp else ""
            raise RuntimeError(f"oMLX HTTP {exc.code}: {body[:800]}") from exc

    def _inference_request(self, path: str, payload: Dict[str, Any], live: bool) -> Dict[str, Any]:
        with self.inference_condition:
            if live:
                self.live_waiters += 1
            try:
                self.inference_condition.wait_for(
                    lambda: not self.inference_busy and (live or self.live_waiters == 0)
                )
                self.inference_busy = True
            finally:
                if live:
                    self.live_waiters = max(0, self.live_waiters - 1)
        started = time.monotonic()
        try:
            return self._request(path, payload)
        finally:
            kind = "reranker" if path == "/rerank" else "embedding"
            self.latencies[kind].append((time.monotonic() - started) * 1000)
            with self.inference_condition:
                self.inference_busy = False
                self.inference_condition.notify_all()

    def _model_status(self) -> Dict[str, Any]:
        now = time.time()
        if now - float(self.model_status_cache.get("time") or 0) < 5:
            return dict(self.model_status_cache.get("models") or {})
        try:
            req = urllib.request.Request(self.cfg.base_url + "/models/status")
            key = self._local_omlx_key(self.cfg.base_url)
            if key:
                req.add_header("Authorization", "Bearer " + key)
            with urllib.request.urlopen(req, timeout=3) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
            wanted = {self.cfg.model, self.cfg.reranker_model}
            models = {
                str(item.get("id")): {
                    "loaded": bool(item.get("loaded")), "is_loading": bool(item.get("is_loading")),
                    "actual_size": item.get("actual_size"), "estimated_size": item.get("estimated_size"),
                    "engine_type": item.get("engine_type"),
                }
                for item in payload.get("models") or [] if str(item.get("id")) in wanted
            }
            self.model_status_cache = {"time": now, "models": models}
            return models
        except Exception:
            return dict(self.model_status_cache.get("models") or {})

    def embed(self, texts: Iterable[str], query: bool = False) -> List[List[float]]:
        values = [str(x).strip() for x in texts]
        if not values:
            return []
        if query:
            values = [f"Instruct: {self.cfg.query_instruction}\nQuery:{x}" for x in values]
        obj = self._inference_request("/embeddings", {
            "model": self.cfg.model,
            "input": values,
            "dimensions": self.cfg.dimensions,
            "encoding_format": "float",
            "truncation": True,
        }, live=query)
        rows = sorted(obj.get("data") or [], key=lambda x: int(x.get("index", 0)))
        return [[float(v) for v in row.get("embedding") or []] for row in rows]

    def rerank(self, query: str, documents: List[str], top_n: int = 12) -> List[Dict[str, Any]]:
        if not documents or not self.cfg.reranker_model:
            return [{"index": i, "relevance_score": 1.0} for i in range(min(len(documents), top_n))]
        ranked: List[Dict[str, Any]] = []
        # Rerank every recalled item, but keep each generative-reranker prompt small.
        # Qwen3-Reranker latency grows sharply when 50 long documents share one prompt.
        for offset in range(0, len(documents), 12):
            chunk = documents[offset:offset + 12]
            obj = self._inference_request("/rerank", {
                "model": self.cfg.reranker_model,
                "query": f"Instruct: {self.cfg.query_instruction}\nQuery: {query}",
                "documents": chunk,
                "top_n": len(chunk),
                "return_documents": False,
            }, live=True)
            for item in obj.get("results") or []:
                ranked.append({**item, "index": offset + int(item.get("index", 0))})
        ranked.sort(key=lambda item: float(item.get("relevance_score") or 0), reverse=True)
        return ranked[:max(1, min(len(ranked), int(top_n)))]

    def cached_rerank(self, query: str, candidate_ids: List[str], documents: List[str], top_n: int) -> tuple[List[Dict[str, Any]], bool]:
        raw_key = json.dumps([self.cfg.reranker_model, " ".join(query.lower().split()), candidate_ids], ensure_ascii=False)
        key = hashlib.sha256(raw_key.encode("utf-8", "ignore")).hexdigest()
        now = time.time()
        with self.rerank_cache_lock:
            cached = self.rerank_cache.get(key)
            if cached and now - cached[0] <= self.cfg.rerank_cache_seconds:
                self.rerank_cache.move_to_end(key)
                return [dict(item) for item in cached[1]], True
            if cached:
                self.rerank_cache.pop(key, None)
        ranked = self.rerank(query, documents, top_n)
        with self.rerank_cache_lock:
            self.rerank_cache[key] = (now, [dict(item) for item in ranked])
            while len(self.rerank_cache) > 200:
                self.rerank_cache.popitem(last=False)
        return ranked, False

    @staticmethod
    def _candidate_key(row: Dict[str, Any]) -> tuple[str, str]:
        return (
            str(row.get("object_type") or "message"),
            str(row.get("object_id") or row.get("event_id") or row.get("id") or ""),
        )

    @staticmethod
    def _candidate_text(row: Dict[str, Any]) -> str:
        return str(row.get("text") or row.get("raw_message") or row.get("searchable_text") or row.get("image_summary") or "").replace("\n", " ")[:160]

    @staticmethod
    def _expanded_query(query: str, context_messages: Optional[List[Dict[str, Any]]], sender_name: str) -> str:
        lines = []
        for row in (context_messages or [])[-4:]:
            body = str(row.get("text") or row.get("raw_message") or "").strip().replace("\n", " ")
            if body and body != str(query).strip():
                lines.append(f"{row.get('sender_name') or row.get('user_id')}: {body[:180]}")
        prefix = f"当前成员：{sender_name}\n" if sender_name else ""
        context = "\n".join(lines[-3:])
        return (prefix + ("最近同线程上下文：\n" + context + "\n" if context else "") + "当前消息：" + str(query)).strip()

    def search(self, query: str, group_id: str, limit: int = 12,
               stage_callback: Optional[Callable[[str], None]] = None,
               rerank_candidates: int = 12,
               context_messages: Optional[List[Dict[str, Any]]] = None,
               sender_name: str = "") -> Dict[str, Any]:
        started = time.monotonic()
        expanded_query = self._expanded_query(query, context_messages, sender_name)
        route_started = time.monotonic()
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=6, thread_name_prefix="memory-route")
        fts_search = getattr(self.store, "search_messages_fts", None)
        people_search = getattr(self.store, "route_people", None)
        meme_search = getattr(self.store, "route_memes", None)
        time_search = getattr(self.store, "search_time_messages", None)
        media_search = getattr(self.store, "route_media", None)
        route_futures = {
            "fts": executor.submit(fts_search, query, group_id, self.cfg.fts_limit) if fts_search else
                   executor.submit(self.store.search_messages, query=query, group_id=group_id, limit=self.cfg.fts_limit),
            "culture": executor.submit(self.store.culture_context, group_id, query, 12),
            "people": executor.submit(people_search, group_id, query, self.cfg.person_limit) if people_search else executor.submit(lambda: []),
            "meme": executor.submit(meme_search, group_id, query, self.cfg.meme_limit) if meme_search else executor.submit(lambda: []),
            "time": executor.submit(time_search, query, group_id, self.cfg.time_limit) if time_search else executor.submit(lambda: []),
            "media": executor.submit(media_search, group_id, query, self.cfg.media_limit) if media_search else executor.submit(lambda: []),
        }
        semantic: List[Dict[str, Any]] = []
        asset_candidates: List[Dict[str, Any]] = []
        error = ""
        embedding_started = time.monotonic()
        if self.cfg.enabled and query.strip():
            try:
                vectors = self.embed([expanded_query], query=True)
                if vectors:
                    semantic = self.store.semantic_search(vectors[0], group_id, self.cfg.model, self.cfg.vector_limit)
                    global_rows = self.store.semantic_search(vectors[0], "__global__", self.cfg.model, 30)
                    for row in global_rows:
                        object_type = str(row.get("object_type") or "")
                        if object_type == "face_asset":
                            asset = self.store.face_asset(int(row.get("object_id") or 0)) if hasattr(self.store, "face_asset") else {}
                        elif object_type == "voice_pack":
                            asset = self.store.voice_item(int(row.get("object_id") or 0)) if hasattr(self.store, "voice_item") else {}
                        else:
                            continue
                        if asset:
                            asset_candidates.append({**asset, "object_type": object_type, "vector_score": row.get("score")})
            except Exception as exc:
                error = str(exc)
        embedding_ms = (time.monotonic() - embedding_started) * 1000
        route_results: Dict[str, Any] = {}
        for name, future in route_futures.items():
            try:
                route_results[name] = future.result(timeout=5)
            except Exception as exc:
                route_results[name] = {} if name == "culture" else []
                error = error or f"{name}: {exc}"
        executor.shutdown(wait=False, cancel_futures=True)
        culture = route_results.pop("culture", {}) or {}
        if stage_callback:
            stage_callback("reading_culture")
        route_ms = (time.monotonic() - route_started) * 1000

        weighted_routes: Dict[str, tuple[float, List[Dict[str, Any]]]] = {
            "semantic": (1.0, semantic), "fts": (1.2, route_results.get("fts") or []),
            "people": (1.15, route_results.get("people") or []), "meme": (1.3, route_results.get("meme") or []),
            "time": (1.0, route_results.get("time") or []), "media": (1.0, route_results.get("media") or []),
        }
        fused: Dict[tuple[str, str], Dict[str, Any]] = {}
        pinned_keys: List[tuple[str, str]] = []
        for route_name, (weight, rows) in weighted_routes.items():
            for rank, raw in enumerate(rows):
                row = dict(raw)
                if route_name != "semantic":
                    row.setdefault("object_type", "message" if row.get("event_id") else route_name)
                    row.setdefault("object_id", row.get("event_id") or row.get("id"))
                key = self._candidate_key(row)
                if not key[1]:
                    continue
                entry = fused.setdefault(key, {**row, "route_sources": [], "rrf_score": 0.0})
                entry["rrf_score"] += weight / (60.0 + rank + 1)
                if route_name not in entry["route_sources"]:
                    entry["route_sources"].append(route_name)
                if bool(row.get("exact_route")) or (route_name == "fts" and rank == 0 and str(query).strip() in self._candidate_text(row)):
                    if key not in pinned_keys and len(pinned_keys) < 4:
                        pinned_keys.append(key)
        candidates = sorted(fused.values(), key=lambda row: float(row.get("rrf_score") or 0), reverse=True)
        pinned = [fused[key] for key in pinned_keys if key in fused]
        candidates = pinned + [row for row in candidates if self._candidate_key(row) not in set(pinned_keys)]
        candidates = candidates[:self.cfg.fusion_limit]
        recalled_count = len(candidates)
        rerank_ms = 0.0
        cache_hits = 0
        expanded_second_batch = False
        compact_query = re.sub(r"\s+", "", str(query or ""))
        simple_social = len(compact_query) <= 12 and bool(
            SIMPLE_SOCIAL_RE.fullmatch(str(query or "").strip())
            or re.search(r"(?:在吗|有人吗|忙吗)[。.!！?？~～]*$", compact_query)
        )
        exact_structured = bool(pinned) and len(compact_query) >= 4
        skip_rerank_reason = "simple_social" if simple_social else "exact_structured" if exact_structured else ""
        rerank_pool_size = max(4, min(12, int(rerank_candidates or 12)))
        reranked_count = 0
        if candidates and self.cfg.reranker_model and not skip_rerank_reason:
            try:
                if stage_callback:
                    stage_callback("reranking")
                rerank_started = time.monotonic()
                first = candidates[:rerank_pool_size]
                first_ids = [":".join(self._candidate_key(row)) for row in first]
                ranked, cached = self.cached_rerank(expanded_query, first_ids, [self._candidate_text(x) for x in first], len(first))
                cache_hits += int(cached)
                ranked_rows = [{**first[int(item["index"])], "rerank_score": float(item.get("relevance_score") or 0)}
                               for item in ranked if 0 <= int(item.get("index", -1)) < len(first)]
                reranked_count = len(first)
                scores = [float(item.get("rerank_score") or 0) for item in ranked_rows[:3]]
                historical = bool(re.search(r"(?:上次|之前|以前|曾经|当时|记得|哪个|那个|谁说|很久|去年|前天|昨天|梗|外号|原话)", query))
                low_top = not scores or scores[0] < 0.55
                narrow_gap = len(scores) >= 3 and scores[0] - scores[2] < 0.08
                if self.cfg.adaptive_rerank and historical and (low_top or narrow_gap) and len(candidates) > rerank_pool_size:
                    second = candidates[rerank_pool_size:rerank_pool_size + 12]
                    second_ids = [":".join(self._candidate_key(row)) for row in second]
                    second_ranked, cached = self.cached_rerank(expanded_query, second_ids, [self._candidate_text(x) for x in second], len(second))
                    cache_hits += int(cached)
                    ranked_rows.extend({**second[int(item["index"])], "rerank_score": float(item.get("relevance_score") or 0)}
                                       for item in second_ranked if 0 <= int(item.get("index", -1)) < len(second))
                    ranked_rows.sort(key=lambda row: float(row.get("rerank_score") or 0), reverse=True)
                    reranked_count += len(second)
                    expanded_second_batch = True
                rerank_ms = (time.monotonic() - rerank_started) * 1000
                selected = pinned + [row for row in ranked_rows if self._candidate_key(row) not in set(pinned_keys)]
                candidates = selected[:limit]
            except Exception as exc:
                error = error or str(exc)
                candidates = candidates[:limit]
        else:
            candidates = candidates[:limit]
        return {
            "items": candidates, "culture": culture, "error": error, "model": self.cfg.model,
            "timings_ms": {
                "structured_routes": round(route_ms, 1), "embedding_and_recall": round(embedding_ms, 1),
                "rerank": round(rerank_ms, 1), "total": round((time.monotonic() - started) * 1000, 1),
            },
            "recalled_count": recalled_count,
            "reranked_count": reranked_count,
            "rerank_skipped_reason": skip_rerank_reason,
            "expanded_second_batch": expanded_second_batch,
            "rerank_cache_hits": cache_hits,
            "pinned_count": len(pinned),
            "route_counts": {name: len(rows) for name, (_weight, rows) in weighted_routes.items()},
            "expanded_query": expanded_query[:1000],
            "asset_candidates": asset_candidates,
        }

    def _backfill_loop(self) -> None:
        while not self.stop_event.is_set():
            if self.pause_event.is_set():
                self.stop_event.wait(0.5)
                continue
            # Migration pages contain 100 durable jobs. Each page is split into
            # inference batches of at most cfg.batch_size with a character budget.
            jobs = self.store.pending_embedding_jobs(100)
            if not jobs:
                with self.state_lock:
                    self.state["running"] = False
                self.stop_event.wait(2.0)
                continue
            ids = [int(x["id"]) for x in jobs]
            self.store.mark_embedding_jobs(ids, "running")
            with self.state_lock:
                self.state["running"] = True
            self._emit("memory_backfill")
            completed_ids: set[int] = set()
            try:
                for batch in self._backfill_batches(jobs):
                    texts = [self._embedding_document_text(x["text"]) for x in batch]
                    vectors = self.embed(texts)
                    if len(vectors) != len(batch):
                        raise RuntimeError(f"embedding count mismatch: {len(vectors)} != {len(batch)}")
                    self.store.upsert_embeddings_batch(batch, vectors, self.cfg.model)
                    completed_ids.update(int(x["id"]) for x in batch)
                    with self.state_lock:
                        self.state["processed"] += len(batch)
                        self.state["last_error"] = ""
                    self._emit("memory_backfill")
            except Exception as exc:
                remaining = [job_id for job_id in ids if job_id not in completed_ids]
                self.store.mark_embedding_jobs(remaining, "retry", str(exc))
                with self.state_lock:
                    self.state["failed"] += len(jobs)
                    self.state["last_error"] = str(exc)
                self._emit("memory_backfill", error=str(exc))
                # A model still being downloaded or not loaded returns 404. Keep the
                # durable queue intact without hammering the local model server.
                self.stop_event.wait(30.0 if "404" in str(exc) else 5.0)
            else:
                self._emit("memory_backfill")

    @staticmethod
    def _embedding_document_text(text: str) -> str:
        value = " ".join(str(text or "").split())
        if len(value) <= 1600:
            return value
        return value[:1200] + "\n...\n" + value[-400:]

    def _backfill_batches(self, jobs: List[Dict[str, Any]]) -> Iterable[List[Dict[str, Any]]]:
        batch: List[Dict[str, Any]] = []
        characters = 0
        for job in jobs:
            size = len(self._embedding_document_text(str(job.get("text") or "")))
            if batch and (len(batch) >= self.cfg.batch_size or characters + size > 12000):
                yield batch
                batch, characters = [], 0
            batch.append(job)
            characters += size
        if batch:
            yield batch
