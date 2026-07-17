#!/usr/bin/env python3
"""Conversation scoring, task state, and fair multi-thread reply scheduling."""
from __future__ import annotations

import collections
import concurrent.futures
import hashlib
import json
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, Iterable, List, Optional, Tuple

from memory_store import MemoryStore


TASK_LABELS = {
    "queued": "排队等待",
    "scoring": "正在分析是否适合接话",
    "retrieving_memory": "正在检索永久记忆",
    "reading_culture": "正在读取人物关系和群梗",
    "reranking": "正在重排相关历史",
    "generating": "正在生成回复",
    "generating_image": "正在生成图片",
    "media_deciding": "正在选择回复媒介",
    "selecting_voice": "正在匹配语音",
    "selecting_face": "正在匹配表情",
    "waiting_media_channel": "正在等待媒体通道",
    "sending": "正在发送回复",
    "completed": "回复完毕",
    "skipped": "决定旁听",
    "failed": "回复失败",
    "cancelled": "任务已取消",
}
FINAL_STATES = {"completed", "skipped", "failed", "cancelled"}


def extract_image_generation_prompt(text: str) -> str:
    """Extract an explicit image request without treating ordinary image talk as a command."""
    raw = re.sub(r"^@\S+\s*", "", str(text or "").strip()).strip()
    patterns = (
        r"^/生图\s+(.+)$",
        # “画个哈士奇”“画一只猫”“帮我画月球”：画/绘制本身就是强绘图动词。
        r"^(?:(?:帮我|给我|请|麻烦)\s*)?(?:画|绘制)\s*"
        r"(?:(?:一张|张|一幅|幅|一个|个|一只|只|一下)\s*)?"
        r"(?:(?:图片|图|画|插画|海报)\s*)?[：:]?\s*(.+)$",
        # “生成一张哈士奇”或“生成图片：哈士奇”。不接受“生成一个报告”。
        r"^(?:(?:帮我|给我|请|麻烦)\s*)?(?:生成|制作)\s*"
        r"(?:(?:一张|张|一幅|幅)\s*(?:(?:图片|图|画|插画|海报)\s*)?|"
        r"(?:图片|图|画|插画|海报)\s*)[：:]?\s*(.{2,})$",
        # “来张哈士奇的图”“整一张月球猫图片”，必须明确带图像名词。
        r"^(?:(?:帮我|给我|请|麻烦)\s*)?(?:来|整|搞|做)\s*"
        r"(?:(?:一张|张|一幅|幅|一个|个)\s*)?(.{2,}?)\s*(?:的)?(?:图片|图|画|插画|海报)$",
    )
    for pattern in patterns:
        match = re.match(pattern, raw, re.I | re.S)
        if not match:
            continue
        prompt = match.group(1).strip(" ，。！？:：")
        if not prompt or re.fullmatch(r"(?:图|图片|画|一张|一幅|一个|个)", prompt):
            continue
        if re.search(r"(?:是什么意思|什么意思|怎么用|为什么|为何)[?？]?$", prompt):
            continue
        return prompt[:2000]
    return ""


def media_suppression(text: str) -> set[str]:
    """Return media types the user explicitly asked not to receive."""
    raw = re.sub(r"^@\S+?[\s\u2005]+", "", str(text or "").strip()).strip()
    compact = re.sub(r"\s+", "", raw)
    suppressed: set[str] = set()
    if re.search(r"(?:只|仅)(?:要|用|发|回|回复)?(?:文字|文本)|(?:文字|文本)(?:就行|回复|回答)", compact):
        suppressed.update({"voice", "face"})
    if re.search(r"(?:别|不要|不用|不许|禁止|停止)(?:再)?(?:给我)?(?:发|用|来|整|搞)?(?:任何)?(?:语音|语音包|声音|音频)", compact):
        suppressed.add("voice")
    if re.search(r"(?:别|不要|不用|不许|禁止|停止)(?:再)?(?:给我)?(?:发|用|来|整|搞)?(?:任何)?(?:表情|表情包|梗图|动图)", compact):
        suppressed.add("face")
    return suppressed


def extract_explicit_media_kind(text: str) -> str:
    """Recognize affirmative voice/face requests while respecting negation."""
    raw = re.sub(r"^@\S+?[\s\u2005]+", "", str(text or "").strip()).strip()
    suppressed = media_suppression(raw)
    if raw.startswith("/发语音"):
        return "voice"
    if raw.startswith("/发表情"):
        return "face"
    if "voice" not in suppressed and re.search(r"(?:发|来|整|搞).{0,8}(?:语音|语音包|声音|音频)", raw):
        return "voice"
    if "face" not in suppressed and re.search(r"(?:发|来|整|搞).{0,8}(?:表情|表情包|梗图|动图)", raw):
        return "face"
    return ""


@dataclass
class BrainConfig:
    mode: str = "veteran"
    threshold: float = 52.0
    scoring_mode: str = "local_fast"
    rerank_candidates: int = 12
    bot_aliases: List[str] = field(default_factory=lambda: ["小风"])
    followup_window_seconds: int = 120
    merge_window_ms: int = 2500
    global_workers: int = 8
    per_group_workers: int = 3
    model_concurrency: int = 6
    mute_duration_seconds: int = 180
    factor_weights: Dict[str, float] = field(default_factory=lambda: {
        "involvement": 18, "continuity": 14, "memory": 16, "value": 14,
        "humor": 14, "emotion": 10, "timing": 14,
    })
    modifiers: Dict[str, float] = field(default_factory=lambda: {
        "same_member_followup": 18, "exact_meme": 15, "high_vector": 12,
        "media_match": 8, "useful_after_silence": 6, "unfinished_fast_exchange": -25,
        "growing_burst": -15, "already_answered": -20, "low_information": -15,
    })

    @classmethod
    def from_raw(cls, raw: Dict[str, Any]) -> "BrainConfig":
        value = raw.get("reply_strategy") if isinstance(raw.get("reply_strategy"), dict) else {}
        mode = str(value.get("mode") or "veteran")
        presets = {"reserved": 78.0, "natural": 65.0, "veteran": 52.0}
        default_weights = cls().factor_weights
        default_modifiers = cls().modifiers
        weights = value.get("factor_weights") if isinstance(value.get("factor_weights"), dict) else {}
        modifiers = value.get("modifiers") if isinstance(value.get("modifiers"), dict) else {}
        cleaned_weights = {key: max(0.0, float(weights.get(key, val))) for key, val in default_weights.items()}
        total = sum(cleaned_weights.values()) or 1.0
        cleaned_weights = {key: round(val * 100.0 / total, 4) for key, val in cleaned_weights.items()}
        return cls(
            mode=mode,
            threshold=max(0.0, min(100.0, float(value.get("threshold", presets.get(mode, 52))))),
            scoring_mode=str(value.get("scoring_mode") or "local_fast") if str(value.get("scoring_mode") or "local_fast") in {"local_fast", "model_deep"} else "local_fast",
            rerank_candidates=max(4, min(24, int(value.get("rerank_candidates", 12)))),
            bot_aliases=[str(x).strip() for x in value.get("bot_aliases", ["小风"]) if str(x).strip()],
            followup_window_seconds=max(10, min(600, int(value.get("followup_window_seconds", 120)))),
            merge_window_ms=max(0, min(10000, int(value.get("merge_window_ms", 2500)))),
            global_workers=max(1, min(16, int(value.get("global_workers", 8)))),
            per_group_workers=max(1, min(6, int(value.get("per_group_workers", 3)))),
            model_concurrency=max(1, min(16, int(value.get("model_concurrency", 6)))),
            mute_duration_seconds=max(10, min(86400, int(value.get("mute_duration_seconds", 180)))),
            factor_weights=cleaned_weights,
            modifiers={key: float(modifiers.get(key, val)) for key, val in default_modifiers.items()},
        )


@dataclass
class ReplyTask:
    task_id: str
    trace_id: str
    thread_id: str
    group_id: str
    group_name: str
    user_id: str
    sender_name: str
    message_id: str
    question: str
    state: str = "queued"
    state_label: str = TASK_LABELS["queued"]
    score: Optional[float] = None
    threshold: Optional[float] = None
    medium: str = ""
    model: str = ""
    result: str = ""
    error: str = ""
    details: Dict[str, Any] = field(default_factory=dict)
    queued_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    updated_at: float = field(default_factory=time.time)

    def as_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


class TaskRegistry:
    def __init__(self, store: MemoryStore, event_callback: Optional[Callable[[Dict[str, Any]], None]] = None):
        self.store = store
        self.event_callback = event_callback
        self.lock = threading.RLock()
        self.tasks: Dict[str, ReplyTask] = {}

    def create(self, evt: Any, thread_id: str) -> ReplyTask:
        task = ReplyTask(
            task_id="reply-" + uuid.uuid4().hex[:16], trace_id=str(evt.trace_id), thread_id=thread_id,
            group_id=str(evt.group_id), group_name=str(evt.group_name), user_id=str(evt.user_id),
            sender_name=str(evt.sender_name), message_id=str(evt.message_id), question=str(evt.text)[:1000],
        )
        with self.lock:
            self.tasks[task.task_id] = task
        self._persist_emit(task)
        return task

    def update(self, task: ReplyTask, state: Optional[str] = None, **changes: Any) -> ReplyTask:
        with self.lock:
            if state:
                task.state = state
                task.state_label = TASK_LABELS.get(state, state)
                if state not in {"queued"} and task.started_at is None:
                    task.started_at = time.time()
                if state in FINAL_STATES:
                    task.completed_at = time.time()
            for key, value in changes.items():
                if hasattr(task, key):
                    setattr(task, key, value)
            if state == "sending":
                task.state_label = {
                    "text": "正在发送文字", "voice": "正在发送语音", "face": "正在发送表情",
                }.get(task.medium, TASK_LABELS["sending"])
            task.updated_at = time.time()
        self._persist_emit(task)
        return task

    def _persist_emit(self, task: ReplyTask) -> None:
        self.store.save_reply_task(task.as_dict())
        if self.event_callback:
            self.event_callback({"type": "reply_task", "task": task.as_dict(), "time": time.time()})

    def snapshot(self, limit: int = 100) -> Dict[str, Any]:
        with self.lock:
            rows = sorted((x.as_dict() for x in self.tasks.values()), key=lambda x: x["updated_at"], reverse=True)[:limit]
        active = [x for x in rows if x["state"] not in FINAL_STATES]
        return {
            "items": rows, "active": len(active),
            "queued": sum(x["state"] == "queued" for x in rows),
            "completed_recent": sum(x["state"] == "completed" and time.time() - (x.get("completed_at") or 0) <= 60 for x in rows),
        }


class OpportunityScorer:
    QUESTION_WORDS = ("吗", "呢", "怎么", "为什么", "谁", "啥", "什么", "多少", "能不能", "是不是", "有没有", "咋")

    def __init__(self, cfg: BrainConfig):
        self.cfg = cfg

    @staticmethod
    def _mention_target(value: Any) -> str:
        """Normalize OneBot mention ids, including WeChat CDATA-wrapped values."""
        target = str(value or "").strip()
        match = re.fullmatch(r"<!\[CDATA\[(.*?)\]\]>", target, re.S)
        if match:
            target = match.group(1).strip()
        return target.strip("\"'").strip()

    def local_score(self, evt: Any, recent: List[Dict[str, Any]], memory: Dict[str, Any],
                    last_bot_reply: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        text = str(evt.text or "").strip()
        raw_segments = evt.raw.get("message") if isinstance(evt.raw, dict) else []
        at_self = any(
            isinstance(seg, dict) and seg.get("type") == "at" and
            self._mention_target(
                (seg.get("data") or {}).get("qq") or (seg.get("data") or {}).get("user_id") or ""
            ) == self._mention_target(evt.self_id)
            for seg in (raw_segments or [])
        )
        reply_id = next((str((seg.get("data") or {}).get("id") or "") for seg in (raw_segments or [])
                         if isinstance(seg, dict) and seg.get("type") == "reply"), "")
        alias_hit = next((x for x in self.cfg.bot_aliases if x and x in text), "")
        explicit_media = bool(extract_image_generation_prompt(text) or extract_explicit_media_kind(text))
        mandatory = bool(at_self or reply_id or alias_hit or explicit_media)
        score = 24.0 if len(text) >= 3 else 10.0
        reasons: List[Dict[str, Any]] = []
        if any(word in text for word in self.QUESTION_WORDS) or "?" in text or "？" in text:
            score += 10
            reasons.append({"signal": "question_or_opening", "value": 10})
        if last_bot_reply and str(last_bot_reply.get("user_id") or "") == str(evt.user_id):
            delta = int(time.time()) - int(last_bot_reply.get("event_time") or 0)
            if 0 <= delta <= self.cfg.followup_window_seconds:
                value = self.cfg.modifiers["same_member_followup"]
                score += value
                reasons.append({"signal": "same_member_followup", "value": value})
        if memory.get("culture", {}).get("memes"):
            value = self.cfg.modifiers["exact_meme"]
            score += value
            reasons.append({"signal": "exact_meme", "value": value})
        semantic = memory.get("items") or []
        if semantic and float(semantic[0].get("score") or semantic[0].get("rerank_score") or 0) >= 0.75:
            value = self.cfg.modifiers["high_vector"]
            score += value
            reasons.append({"signal": "high_vector", "value": value})
        if any(str(row.get("object_type") or "") in {"media", "voice_pack"} for row in semantic[:8]):
            value = self.cfg.modifiers["media_match"]
            score += value
            reasons.append({"signal": "media_match", "value": value})

        def event_seconds(row: Dict[str, Any]) -> float:
            raw = float(row.get("event_time") or 0)
            return raw / 1000.0 if raw > 10_000_000_000 else raw

        now = float(getattr(evt, "timestamp", 0) or time.time())
        incoming = [
            row for row in recent
            if row.get("direction") == "incoming"
            and (not getattr(evt, "event_id", "") or str(row.get("event_id") or "") != str(evt.event_id))
            and (not getattr(evt, "message_id", "") or str(row.get("message_id") or "") != str(evt.message_id))
        ]
        if last_bot_reply and now - event_seconds(last_bot_reply) >= 1200 and len(text) >= 3:
            value = self.cfg.modifiers["useful_after_silence"]
            score += value
            reasons.append({"signal": "useful_after_silence", "value": value})
        fast_rows = [row for row in incoming if 0 <= now - event_seconds(row) <= 8]
        fast_people = {str(row.get("user_id") or "") for row in fast_rows if str(row.get("user_id") or "") != str(evt.user_id)}
        if len(fast_rows) >= 4 and len(fast_people) >= 2 and not re.search(r"[。！？?!]$", text):
            value = self.cfg.modifiers["unfinished_fast_exchange"]
            score += value
            reasons.append({"signal": "unfinished_fast_exchange", "value": value})
        burst_rows = [row for row in recent if 0 <= now - event_seconds(row) <= 30]
        if len(burst_rows) > 8 and len(fast_rows) >= 3:
            value = self.cfg.modifiers["growing_burst"]
            score += value
            reasons.append({"signal": "growing_burst", "value": value})
        last_out_index = next((i for i in range(len(recent) - 1, -1, -1) if recent[i].get("direction") == "outgoing"), -1)
        if last_out_index >= 1:
            prior = str(recent[last_out_index - 1].get("text") or recent[last_out_index - 1].get("raw_message") or "").strip()
            current_tokens, prior_tokens = _topic_tokens(text), _topic_tokens(prior)
            similarity = len(current_tokens & prior_tokens) / max(1, len(current_tokens | prior_tokens))
            if similarity >= 0.65 and not any(word in text for word in ("补充", "但是", "不过", "不对", "还有")):
                value = self.cfg.modifiers["already_answered"]
                score += value
                reasons.append({"signal": "already_answered", "value": value})
        repeated = any(text and text == str(row.get("text") or "").strip() for row in incoming[-5:])
        system_like = bool(re.search(r"^(?:系统通知|群公告|你已|撤回了一条消息|加入了群聊)", text))
        if len(text) <= 2 or re.fullmatch(r"https?://\S+", text) or repeated or system_like:
            value = self.cfg.modifiers["low_information"]
            score += value
            reasons.append({"signal": "low_information", "value": value})
        return {
            "pre_score": max(0.0, min(100.0, score)), "mandatory": mandatory,
            "at_self": at_self, "reply_id": reply_id, "alias_hit": alias_hit,
            "explicit_media": explicit_media, "reasons": reasons,
        }

    def final_score(self, factors: Dict[str, Any], modifiers: Iterable[Dict[str, Any]]) -> float:
        score = 0.0
        for key, weight in self.cfg.factor_weights.items():
            score += max(0.0, min(100.0, float(factors.get(key, 0)))) * weight / 100.0
        score += sum(float(x.get("value") or 0) for x in modifiers)
        return round(max(0.0, min(100.0, score)), 2)

    def local_factors(self, evt: Any, recent: List[Dict[str, Any]], memory: Dict[str, Any],
                      local: Dict[str, Any], last_bot_reply: Optional[Dict[str, Any]]) -> Tuple[Dict[str, float], str]:
        """Compute the seven social-opportunity dimensions without a second LLM call."""
        text = str(evt.text or "").strip()
        culture = memory.get("culture") or {}
        items = memory.get("items") or []
        aliases = culture.get("aliases") or []
        relations = culture.get("relations") or []
        memes = culture.get("memes") or []
        top_similarity = max(
            (float(row.get("score") or 0) for row in items if row.get("score") is not None),
            default=0.0,
        )
        recent_incoming = [row for row in recent if row.get("direction") == "incoming"]
        same_member_recent = any(
            str(row.get("user_id") or "") == str(evt.user_id) for row in recent_incoming[-5:-1]
        )
        recent_similarity = max(
            (_topic_similarity(text, str(row.get("text") or row.get("raw_message") or "")) for row in recent[-8:]),
            default=0.0,
        )
        is_question = any(word in text for word in self.QUESTION_WORDS) or "?" in text or "？" in text
        correction = bool(re.search(r"(?:不对|其实|应该是|补充|但是|不过|还有|等等)", text))
        humor_signal = bool(re.search(r"(?:哈哈|笑死|绷不住|离谱|逆天|又来|典|乐|草|6{2,})", text, re.I))
        emotional = bool(re.search(r"(?:气死|难受|烦|开心|激动|崩溃|无语|牛逼|卧槽|救命)", text))
        complete = bool(re.search(r"[。！？?!…]$", text)) or len(text) >= 10
        followup = False
        if last_bot_reply and str(last_bot_reply.get("user_id") or "") == str(evt.user_id):
            raw_time = float(last_bot_reply.get("event_time") or 0)
            raw_time = raw_time / 1000.0 if raw_time > 10_000_000_000 else raw_time
            followup = 0 <= float(getattr(evt, "timestamp", 0) or time.time()) - raw_time <= self.cfg.followup_window_seconds

        involvement = 22 + (58 if local.get("mandatory") else 0) + (12 if aliases or relations else 0) + (8 if items else 0)
        continuity = 20 + (65 if local.get("reply_id") else 0) + (35 if followup else 0) + (18 if same_member_recent else 0) + recent_similarity * 32
        memory_score = 12 + min(36, len(items) * 4) + min(22, len(aliases) * 8 + len(relations) * 6) + min(24, len(memes) * 10) + max(0.0, top_similarity) * 22
        value = 28 + (42 if is_question else 0) + (20 if correction else 0) + min(18, len(text) / 4)
        humor = 18 + (38 if humor_signal else 0) + min(30, len(memes) * 12) + (10 if relations else 0)
        emotion = 48 + (28 if emotional else 0) + (8 if humor_signal else 0)
        timing = 38 + (30 if complete else 0) + (18 if local.get("mandatory") else 0) + (12 if len(text) >= 5 else 0)
        reason_signals = {str(item.get("signal")) for item in local.get("reasons") or []}
        if "unfinished_fast_exchange" in reason_signals:
            timing -= 35
        if "growing_burst" in reason_signals:
            timing -= 20
        if "already_answered" in reason_signals:
            value -= 35
        if "low_information" in reason_signals:
            value -= 30
            timing -= 20
        factors = {
            "involvement": involvement, "continuity": continuity, "memory": memory_score,
            "value": value, "humor": humor, "emotion": emotion, "timing": timing,
        }
        factors = {key: round(max(0.0, min(100.0, float(value))), 2) for key, value in factors.items()}
        evidence = [key for key, enabled in (
            ("明确触发", local.get("mandatory")), ("连续追问", followup or same_member_recent),
            ("永久记忆", bool(items or aliases or relations or memes)), ("提问", is_question),
            ("玩笑语境", humor_signal or bool(memes)),
        ) if enabled]
        return factors, "本地快速评分" + ("：" + "、".join(evidence) if evidence else "")


def conversation_thread_id(evt: Any, aliases: Iterable[str]) -> str:
    segments = evt.raw.get("message") if isinstance(evt.raw, dict) else []
    reply_id = next((str((x.get("data") or {}).get("id") or "") for x in (segments or [])
                     if isinstance(x, dict) and x.get("type") == "reply"), "")
    if reply_id:
        return f"{evt.group_id}:reply:{reply_id}"
    text = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(evt.text).lower())
    for alias in aliases:
        text = text.replace(str(alias).lower(), "")
    topic = hashlib.sha1(text[:80].encode("utf-8", "ignore")).hexdigest()[:8] if text else "general"
    return f"{evt.group_id}:user:{evt.user_id}:topic:{topic}"


def _topic_tokens(text: str) -> set[str]:
    cleaned = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(text).lower())
    latin = set(re.findall(r"[a-z0-9]{2,}", cleaned))
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]", cleaned))
    return latin | {chinese[i:i + 2] for i in range(max(0, len(chinese) - 1))}


def _topic_similarity(left: str, right: str) -> float:
    a, b = _topic_tokens(left), _topic_tokens(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


class ReplyScheduler:
    def __init__(self, cfg: BrainConfig, registry: TaskRegistry):
        self.cfg = cfg
        self.registry = registry
        # Keep a fixed upper bound so runtime limit changes do not require replacing
        # the pool (or the AI process). Dispatch still obeys cfg.global_workers.
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=16, thread_name_prefix="reply")
        self.lock = threading.Condition(threading.RLock())
        self.group_queues: Dict[str, Deque[Tuple[ReplyTask, Any, Callable[[ReplyTask, Any], None]]]] = collections.defaultdict(collections.deque)
        self.group_order: Deque[str] = collections.deque()
        self.active_by_group: Dict[str, int] = collections.defaultdict(int)
        self.active_threads: set[str] = set()
        self.message_threads: Dict[str, str] = {}
        self.recent_threads: Dict[str, Deque[Tuple[float, str, str, str]]] = collections.defaultdict(
            lambda: collections.deque(maxlen=80)
        )
        self.active_total = 0
        self.stopped = False
        self.dispatcher = threading.Thread(target=self._dispatch_loop, name="reply-dispatcher", daemon=True)

    def start(self) -> None:
        self.dispatcher.start()

    def stop(self) -> None:
        with self.lock:
            self.stopped = True
            self.lock.notify_all()
        self.executor.shutdown(wait=False, cancel_futures=True)

    def reconfigure(self, cfg: BrainConfig) -> None:
        with self.lock:
            self.cfg = cfg
            self.lock.notify_all()

    def submit(self, evt: Any, handler: Callable[[ReplyTask, Any], None]) -> ReplyTask:
        thread_id = self._resolve_thread(evt)
        task = self.registry.create(evt, thread_id)
        with self.lock:
            queue_ = self.group_queues[str(evt.group_id)]
            explicit_followup = bool(re.search(r"[?？]|(?:怎么|为什么|咋|然后呢|那呢|还有呢|能不能|是不是)", str(evt.text)))
            for queued_task, queued_evt, _queued_handler in reversed(queue_):
                if queued_task.thread_id != thread_id:
                    continue
                within_window = (time.time() - queued_task.queued_at) * 1000 <= self.cfg.merge_window_ms
                if within_window and not explicit_followup:
                    queued_evt.text = (str(queued_evt.text).rstrip() + "\n" + str(evt.text).strip()).strip()
                    queued_evt.message_id = str(evt.message_id or queued_evt.message_id)
                    queued_task.question = queued_evt.text[:1000]
                    queued_task.message_id = queued_evt.message_id
                    self.registry.update(queued_task, details={**queued_task.details, "merged_messages":
                                         int(queued_task.details.get("merged_messages") or 1) + 1})
                    self.registry.update(task, "cancelled", result="merged_into:" + queued_task.task_id,
                                         details={"merged_into": queued_task.task_id})
                    if str(evt.message_id or ""):
                        self.message_threads[str(evt.message_id)] = thread_id
                    return task
                break
            queue_.append((task, evt, handler))
            if str(evt.group_id) not in self.group_order:
                self.group_order.append(str(evt.group_id))
            if str(evt.message_id or ""):
                self.message_threads[str(evt.message_id)] = thread_id
            self.recent_threads[str(evt.group_id)].append(
                (time.time(), thread_id, str(evt.user_id), str(evt.text))
            )
            self.lock.notify_all()
        return task

    def _resolve_thread(self, evt: Any) -> str:
        segments = evt.raw.get("message") if isinstance(evt.raw, dict) else []
        reply_id = next((str((x.get("data") or {}).get("id") or "") for x in (segments or [])
                         if isinstance(x, dict) and x.get("type") == "reply"), "")
        with self.lock:
            if reply_id and reply_id in self.message_threads:
                return self.message_threads[reply_id]
            now = time.time()
            recent = list(self.recent_threads[str(evt.group_id)])
            # A brief follow-up from the same member normally continues the thread,
            # even when it is only “然后呢” and has little lexical overlap.
            for created, thread_id, user_id, text in reversed(recent):
                age = now - created
                if age > self.cfg.followup_window_seconds:
                    break
                similarity = _topic_similarity(str(evt.text), text)
                short_followup = len(str(evt.text).strip()) <= 10 and bool(
                    re.search(r"^(?:然后|然后呢|那呢|还有呢|所以|为啥|怎么说|继续|展开|细说|对吗|是吗)", str(evt.text).strip())
                )
                if user_id == str(evt.user_id) and (short_followup or similarity >= 0.18):
                    return thread_id
            # Multiple members discussing the same topic share a thread. Groups are
            # isolated because the candidate deque is keyed by group ID.
            for created, thread_id, _user_id, text in reversed(recent):
                if now - created > 150:
                    break
                if _topic_similarity(str(evt.text), text) >= 0.32:
                    return thread_id
        return conversation_thread_id(evt, self.cfg.bot_aliases)

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            positions: Dict[str, int] = {}
            position = 1
            for group_id in self.group_order:
                for task, _, _ in self.group_queues[group_id]:
                    positions[task.task_id] = position
                    position += 1
            return {
                "global_workers": self.cfg.global_workers, "active_workers": self.active_total,
                "queued": sum(len(x) for x in self.group_queues.values()),
                "active_threads": len(self.active_threads), "per_group": dict(self.active_by_group),
                "queue_positions": positions,
            }

    def _dispatch_loop(self) -> None:
        while True:
            with self.lock:
                self.lock.wait_for(lambda: self.stopped or self._has_runnable(), timeout=0.5)
                if self.stopped:
                    return
                item = self._next_runnable()
                if not item:
                    continue
                task, evt, handler = item
                self.active_total += 1
                self.active_by_group[task.group_id] += 1
                self.active_threads.add(task.thread_id)
            future = self.executor.submit(self._run, task, evt, handler)
            future.add_done_callback(lambda _f, t=task: self._finished(t))

    def _has_runnable(self) -> bool:
        if self.active_total >= self.cfg.global_workers:
            return False
        return any(
            queue_ and self.active_by_group[group_id] < self.cfg.per_group_workers and
            any(task.thread_id not in self.active_threads for task, _, _ in queue_)
            for group_id, queue_ in self.group_queues.items()
        )

    def _next_runnable(self) -> Optional[Tuple[ReplyTask, Any, Callable[[ReplyTask, Any], None]]]:
        for _ in range(len(self.group_order)):
            group_id = self.group_order[0]
            self.group_order.rotate(-1)
            queue_ = self.group_queues[group_id]
            if not queue_ or self.active_by_group[group_id] >= self.cfg.per_group_workers:
                continue
            for index, item in enumerate(queue_):
                if item[0].thread_id not in self.active_threads:
                    del queue_[index]
                    return item
        return None

    def _run(self, task: ReplyTask, evt: Any, handler: Callable[[ReplyTask, Any], None]) -> None:
        try:
            handler(task, evt)
        except Exception as exc:
            self.registry.update(task, "failed", error=str(exc))

    def _finished(self, task: ReplyTask) -> None:
        with self.lock:
            self.active_total = max(0, self.active_total - 1)
            self.active_by_group[task.group_id] = max(0, self.active_by_group[task.group_id] - 1)
            self.active_threads.discard(task.thread_id)
            self.lock.notify_all()
