#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OneBot -> AI -> WeChat group reply bridge for the second WeChat only.

- Listens on 127.0.0.1:36060/onebot (the send_url configured for wechat_chatter OneBot).
- Filters target chatrooms, calls an OpenAI-compatible chat completion API, then sends reply
  through the local OneBot HTTP API at 127.0.0.1:58080/send_group_msg.
"""
from __future__ import annotations

import argparse
import base64
import collections
import hashlib
import hmac
import json
import mimetypes
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import tempfile
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "config" / "ai_reply_config.json"
DEFAULT_HOME = Path.home() / "Library" / "Application Support" / "WeChatSecond"
LOG_PATH = DEFAULT_HOME / "logs" / "ai-reply.log"
PID_PATH = DEFAULT_HOME / "ai-reply.pid"
SAFETY_STATE_PATH = DEFAULT_HOME / "safety-state.json"
MEMORY_PATH = DEFAULT_HOME / "ai-group-memory.json"

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from memory_store import MemoryStore  # noqa: E402


def now_ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def ensure_dirs() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    PID_PATH.parent.mkdir(parents=True, exist_ok=True)


_log_lock = threading.Lock()


def log(level: str, msg: str, **fields: Any) -> None:
    ensure_dirs()
    rec = {"time": now_ts(), "level": level, "msg": msg}
    rec.update(fields)
    line = json.dumps(rec, ensure_ascii=False)
    with _log_lock:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        print(line, flush=True)


def load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_safety_state() -> Dict[str, Any]:
    try:
        return load_json(SAFETY_STATE_PATH)
    except (OSError, ValueError):
        return {"quarantine_until": 0, "reason": "", "triggered_at": 0, "recent_sends": [], "recent_texts": []}


def save_safety_state(state: Dict[str, Any]) -> None:
    ensure_dirs()
    tmp = SAFETY_STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, SAFETY_STATE_PATH)


def env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    if not v:
        return default
    try:
        return float(v)
    except Exception:
        return default


def env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if not v:
        return default
    try:
        return int(v)
    except Exception:
        return default


@dataclass
class AIChannel:
    id: str
    name: str
    base_url: str
    api_key_env: str
    model: str
    provider: str = "openai_compatible"
    timeout_seconds: int = 30
    enabled: bool = True
    priority: int = 0


@dataclass
class AIConfig:
    provider: str = "openai_compatible"
    base_url: str = "https://api.openai.com/v1"
    api_key_env: str = "OPENAI_API_KEY"
    model: str = "gpt-4o-mini"
    temperature: float = 0.3
    max_tokens: int = 600
    timeout_seconds: int = 30
    system_prompt: str = "你是微信群值班助手。用中文简洁回复。"
    channels: List[AIChannel] = field(default_factory=list)
    active_channel_id: str = ""
    auto_failover: bool = True
    failure_cooldown_seconds: int = 60


@dataclass
class SafetyConfig:
    enabled: bool = True
    quarantine_until: int = 0
    min_global_seconds_between_replies: int = 30
    max_replies_per_10_minutes: int = 3
    max_replies_per_hour: int = 10
    max_replies_per_day: int = 30
    duplicate_window_seconds: int = 3600
    duplicate_limit: int = 1
    new_group_cooldown_seconds: int = 86400
    group_activated_at: Dict[str, int] = field(default_factory=dict)


@dataclass
class MemoryConfig:
    enabled: bool = True
    max_turns: int = 12
    summary_enabled: bool = True


@dataclass
class ToolsConfig:
    enabled: bool = True
    allowed: List[str] = field(default_factory=lambda: ["get_status", "get_recent_logs", "list_groups", "test_model_channel", "send_probe", "search_messages", "get_group_memory", "vector_search", "list_personas", "list_media", "send_voice_pack", "send_face_pack"])


@dataclass
class VisionOCRConfig:
    enabled: bool = False
    auto_analyze: bool = True
    base_url: str = ""
    api_key_env: str = "AI_REPLY_VISION_OCR_API_KEY"
    model: str = ""
    timeout_seconds: int = 60
    prompt: str = "请对这张图片进行OCR识别，提取所有可见文字，并用中文给出一句简短图片摘要。"


@dataclass
class ASRConfig:
    enabled: bool = False
    auto_transcribe: bool = True
    base_url: str = ""
    api_key_env: str = "AI_REPLY_ASR_API_KEY"
    model: str = ""
    timeout_seconds: int = 90
    language: str = "zh"
    prompt: str = ""


@dataclass
class AppConfig:
    enabled: bool = True
    listen_host: str = "127.0.0.1"
    listen_port: int = 36060
    onebot_api: str = "http://127.0.0.1:58080"
    target_groups: Dict[str, str] = field(default_factory=dict)  # id -> name
    log_all_group_messages: bool = True
    reply_prefix: str = "AI："
    ignore_prefixes: List[str] = field(default_factory=lambda: ["AI：", "AI:", "🤖"])
    allowed_user_ids: List[str] = field(default_factory=list)
    ignored_user_ids: List[str] = field(default_factory=list)
    ignore_self_messages: bool = False
    trigger_keywords: List[str] = field(default_factory=list)
    require_keyword: bool = False
    min_seconds_between_replies_per_group: float = 2.0
    max_context_messages: int = 8
    max_reply_chars: int = 600
    send_delay_seconds: float = 0.2
    dry_run: bool = False
    personality: str = "专业、克制、直接。回答简洁，不说空话。"
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    ai: AIConfig = field(default_factory=AIConfig)
    vision_ocr: VisionOCRConfig = field(default_factory=VisionOCRConfig)
    asr: ASRConfig = field(default_factory=ASRConfig)

    @classmethod
    def from_file(cls, path: Path) -> "AppConfig":
        raw = load_json(path)
        groups: Dict[str, str] = {}
        for item in raw.get("target_groups", []):
            if isinstance(item, str):
                groups[item] = item
            elif isinstance(item, dict) and item.get("id"):
                groups[str(item["id"])] = str(item.get("name") or item["id"])
        ai_raw = raw.get("ai", {}) or {}
        channel_items = ai_raw.get("channels", []) or []
        channels: List[AIChannel] = []
        for index, item in enumerate(channel_items):
            if not isinstance(item, dict):
                continue
            channel_id = str(item.get("id") or f"channel-{index + 1}").strip()
            base_url = str(item.get("base_url") or "").strip().rstrip("/")
            model = str(item.get("model") or "").strip()
            if not channel_id or not base_url or not model:
                continue
            channels.append(AIChannel(
                id=channel_id,
                name=str(item.get("name") or channel_id),
                provider=str(item.get("provider") or "openai_compatible"),
                base_url=base_url,
                api_key_env=str(item.get("api_key_env") or f"AI_REPLY_CHANNEL_{index + 1}_API_KEY"),
                model=model,
                timeout_seconds=max(3, int(item.get("timeout_seconds", 30))),
                enabled=bool(item.get("enabled", True)),
                priority=int(item.get("priority", index)),
            ))
        if not channels:
            channels.append(AIChannel(
                id="primary",
                name="默认渠道",
                provider=str(os.getenv("AI_REPLY_PROVIDER", ai_raw.get("provider", "openai_compatible"))),
                base_url=str(os.getenv("AI_REPLY_BASE_URL", ai_raw.get("base_url", "https://api.openai.com/v1"))).rstrip("/"),
                api_key_env=str(os.getenv("AI_REPLY_API_KEY_ENV", ai_raw.get("api_key_env", "AI_REPLY_API_KEY"))),
                model=str(os.getenv("AI_REPLY_MODEL", ai_raw.get("model", "gpt-4o-mini"))),
                timeout_seconds=env_int("AI_REPLY_TIMEOUT_SECONDS", int(ai_raw.get("timeout_seconds", 30))),
            ))
        active_channel_id = str(ai_raw.get("active_channel_id") or channels[0].id)
        active_channel = next((x for x in channels if x.id == active_channel_id), channels[0])
        ai = AIConfig(
            provider=active_channel.provider,
            base_url=active_channel.base_url,
            api_key_env=active_channel.api_key_env,
            model=active_channel.model,
            temperature=env_float("AI_REPLY_TEMPERATURE", float(ai_raw.get("temperature", 0.3))),
            max_tokens=env_int("AI_REPLY_MAX_TOKENS", int(ai_raw.get("max_tokens", 600))),
            timeout_seconds=active_channel.timeout_seconds,
            system_prompt=str(os.getenv("AI_REPLY_SYSTEM_PROMPT", ai_raw.get("system_prompt", "你是微信群值班助手。用中文简洁回复。"))),
            channels=channels,
            active_channel_id=active_channel.id,
            auto_failover=bool(ai_raw.get("auto_failover", True)),
            failure_cooldown_seconds=max(5, int(ai_raw.get("failure_cooldown_seconds", 60))),
        )
        safety_raw = raw.get("safety", {}) or {}
        safety = SafetyConfig(
            enabled=bool(safety_raw.get("enabled", True)),
            quarantine_until=int(safety_raw.get("quarantine_until", 0)),
            min_global_seconds_between_replies=max(5, int(safety_raw.get("min_global_seconds_between_replies", 30))),
            max_replies_per_10_minutes=max(1, int(safety_raw.get("max_replies_per_10_minutes", 3))),
            max_replies_per_hour=max(1, int(safety_raw.get("max_replies_per_hour", 10))),
            max_replies_per_day=max(1, int(safety_raw.get("max_replies_per_day", 30))),
            duplicate_window_seconds=max(60, int(safety_raw.get("duplicate_window_seconds", 3600))),
            duplicate_limit=max(1, int(safety_raw.get("duplicate_limit", 1))),
            new_group_cooldown_seconds=max(0, int(safety_raw.get("new_group_cooldown_seconds", 86400))),
            group_activated_at={str(k): int(v) for k, v in (raw.get("group_activated_at", {}) or {}).items()},
        )
        memory_raw = raw.get("memory", {}) or {}
        tools_raw = raw.get("tools", {}) or {}
        vision_raw = raw.get("vision_ocr", {}) or {}
        asr_raw = raw.get("asr", {}) or {}
        # Allow direct AI_REPLY_API_KEY too; api_key_env remains the configured env name.
        return cls(
            enabled=env_bool("AI_REPLY_ENABLED", bool(raw.get("enabled", True))),
            listen_host=str(os.getenv("AI_REPLY_LISTEN_HOST", raw.get("listen_host", "127.0.0.1"))),
            listen_port=env_int("AI_REPLY_LISTEN_PORT", int(raw.get("listen_port", 36060))),
            onebot_api=str(os.getenv("AI_REPLY_ONEBOT_API", raw.get("onebot_api", "http://127.0.0.1:58080"))).rstrip("/"),
            target_groups=groups,
            log_all_group_messages=env_bool("AI_REPLY_LOG_ALL_GROUP_MESSAGES", bool(raw.get("log_all_group_messages", True))),
            reply_prefix=str(os.getenv("AI_REPLY_PREFIX", raw.get("reply_prefix", "AI："))),
            ignore_prefixes=list(raw.get("ignore_prefixes", ["AI：", "AI:", "🤖"])),
            allowed_user_ids=[str(x) for x in raw.get("allowed_user_ids", [])],
            ignored_user_ids=[str(x) for x in raw.get("ignored_user_ids", [])],
            ignore_self_messages=env_bool("AI_REPLY_IGNORE_SELF", bool(raw.get("ignore_self_messages", False))),
            trigger_keywords=[str(x) for x in raw.get("trigger_keywords", [])],
            require_keyword=env_bool("AI_REPLY_REQUIRE_KEYWORD", bool(raw.get("require_keyword", False))),
            min_seconds_between_replies_per_group=env_float("AI_REPLY_GROUP_COOLDOWN", float(raw.get("min_seconds_between_replies_per_group", 2))),
            max_context_messages=env_int("AI_REPLY_MAX_CONTEXT", int(raw.get("max_context_messages", 8))),
            max_reply_chars=env_int("AI_REPLY_MAX_REPLY_CHARS", int(raw.get("max_reply_chars", 600))),
            send_delay_seconds=env_float("AI_REPLY_SEND_DELAY", float(raw.get("send_delay_seconds", 0.2))),
            dry_run=env_bool("AI_REPLY_DRY_RUN", bool(raw.get("dry_run", False))),
            personality=str(raw.get("personality", "专业、克制、直接。回答简洁，不说空话。")),
            memory=MemoryConfig(
                enabled=bool(memory_raw.get("enabled", True)),
                max_turns=max(2, int(memory_raw.get("max_turns", raw.get("max_context_messages", 12)))),
                summary_enabled=bool(memory_raw.get("summary_enabled", True)),
            ),
            tools=ToolsConfig(
                enabled=bool(tools_raw.get("enabled", True)),
                allowed=[str(x) for x in tools_raw.get("allowed", ["get_status", "get_recent_logs", "list_groups", "test_model_channel", "send_probe", "search_messages", "get_group_memory", "vector_search", "list_personas", "list_media", "send_voice_pack", "send_face_pack"])],
            ),
            safety=safety,
            ai=ai,
            vision_ocr=VisionOCRConfig(
                enabled=bool(vision_raw.get("enabled", False)),
                auto_analyze=bool(vision_raw.get("auto_analyze", True)),
                base_url=str(vision_raw.get("base_url") or ai.base_url).rstrip("/"),
                api_key_env=str(vision_raw.get("api_key_env") or "AI_REPLY_VISION_OCR_API_KEY"),
                model=str(vision_raw.get("model") or ai.model),
                timeout_seconds=max(3, int(vision_raw.get("timeout_seconds", 60))),
                prompt=str(vision_raw.get("prompt") or "请对这张图片进行OCR识别，提取所有可见文字，并用中文给出一句简短图片摘要。"),
            ),
            asr=ASRConfig(
                enabled=bool(asr_raw.get("enabled", False)),
                auto_transcribe=bool(asr_raw.get("auto_transcribe", True)),
                base_url=str(asr_raw.get("base_url") or ai.base_url).rstrip("/"),
                api_key_env=str(asr_raw.get("api_key_env") or "AI_REPLY_ASR_API_KEY"),
                model=str(asr_raw.get("model") or ""),
                timeout_seconds=max(3, int(asr_raw.get("timeout_seconds", 90))),
                language=str(asr_raw.get("language") or "zh"),
                prompt=str(asr_raw.get("prompt") or ""),
            ),
        )


@dataclass
class OneBotEvent:
    event_id: str
    group_id: str
    group_name: str
    self_id: str
    user_id: str
    sender_name: str
    text: str
    raw_message: str
    message_id: str
    timestamp: int
    raw: Dict[str, Any]
    trace_id: str = ""
    has_text: bool = True
    media_types: List[str] = field(default_factory=list)


class AIReplyService:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.events: "queue.Queue[OneBotEvent]" = queue.Queue(maxsize=200)
        self.media_events: "queue.Queue[OneBotEvent]" = queue.Queue(maxsize=300)
        self.stop_event = threading.Event()
        self.seen: Dict[str, float] = {}
        self.last_reply_at: Dict[str, float] = {}
        self.channel_unavailable_until: Dict[str, float] = {}
        self.histories: Dict[str, Deque[Tuple[str, str]]] = collections.defaultdict(
            lambda: collections.deque(maxlen=max(2, self.cfg.memory.max_turns if self.cfg.memory.enabled else self.cfg.max_context_messages))
        )
        self.recent_errors: Deque[Dict[str, Any]] = collections.deque(maxlen=30)
        self.store = MemoryStore()
        self.load_memory()
        self.worker = threading.Thread(target=self._worker_loop, name="ai-reply-worker", daemon=True)
        self.media_worker = threading.Thread(target=self._media_worker_loop, name="ai-media-worker", daemon=True)

    def load_memory(self) -> None:
        if not self.cfg.memory.enabled:
            return
        try:
            data = load_json(MEMORY_PATH)
        except (OSError, ValueError):
            return
        for gid, rows in (data.get("histories") or {}).items():
            if not isinstance(rows, list):
                continue
            dq = self.histories[str(gid)]
            for row in rows[-self.cfg.memory.max_turns:]:
                if isinstance(row, list) and len(row) >= 2:
                    dq.append((str(row[0])[:80], str(row[1])[:1200]))

    def save_memory(self) -> None:
        if not self.cfg.memory.enabled:
            return
        ensure_dirs()
        data = {"updated_at": now_ts(), "histories": {gid: list(rows)[-self.cfg.memory.max_turns:] for gid, rows in self.histories.items()}}
        tmp = MEMORY_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, MEMORY_PATH)

    def start(self) -> None:
        self.worker.start()
        self.media_worker.start()

    def enqueue_raw(self, raw: Dict[str, Any], signature: str = "") -> Tuple[bool, str]:
        evt, reason = self.parse_event(raw)
        if not evt:
            return False, reason
        if raw.get("voice_transcript"):
            transcript = str(raw.get("voice_transcript_text") or evt.text).strip()
            transcript = re.sub(r"^\[语音转文字\]\s*", "", transcript).strip()
            if not transcript:
                return False, "empty_voice_transcript"
            # The UI hook sends a synthetic text event. Normalize it before
            # persisting and model inference so it behaves exactly like a
            # group member's ordinary text message.
            evt.text = transcript
            evt.raw_message = transcript
            evt.has_text = True
            self.persist_incoming(evt)
            self.apply_voice_transcript(evt)
            if not self.should_reply(evt):
                return False, "voice_transcript_indexed"
            try:
                self.events.put_nowait(evt)
                log("info", "voice_transcript_queued", group_id=evt.group_id,
                    message_id=evt.message_id, trace_id=evt.trace_id, text=transcript[:240])
                return True, "voice_transcript_queued"
            except queue.Full:
                log("error", "queue_full", group_id=evt.group_id, message_id=evt.message_id, trace_id=evt.trace_id)
                return False, "queue_full"
        self.persist_incoming(evt)
        if self.should_queue_media_analysis(evt):
            try:
                self.media_events.put_nowait(evt)
                log("info", "media_analysis_queued", group_id=evt.group_id, media_types=evt.media_types,
                    message_id=evt.message_id, trace_id=evt.trace_id)
            except queue.Full:
                log("error", "media_analysis_queue_full", group_id=evt.group_id, message_id=evt.message_id, trace_id=evt.trace_id)
        if not evt.has_text:
            return False, "media_only_indexed"
        if not self.should_reply(evt):
            return False, "ignored"
        try:
            self.events.put_nowait(evt)
            return True, "queued"
        except queue.Full:
            log("error", "queue_full", group_id=evt.group_id, message_id=evt.message_id)
            return False, "queue_full"

    def persist_incoming(self, evt: OneBotEvent) -> None:
        sender = evt.raw.get("sender") if isinstance(evt.raw.get("sender"), dict) else {}
        try:
            inserted = self.store.add_message({
                "event_id": evt.event_id,
                "trace_id": evt.trace_id,
                "direction": "incoming",
                "group_id": evt.group_id,
                "group_name": evt.group_name,
                "user_id": evt.user_id,
                "sender_name": evt.sender_name,
                "nickname": sender.get("nickname", ""),
                "card": sender.get("card", ""),
                "message_id": evt.message_id,
                "event_time": evt.timestamp,
                "text": evt.text,
                "raw_message": evt.raw_message,
                "segments": evt.raw.get("message") or [],
                "raw": evt.raw,
                "source": "onebot_callback",
                "selected": evt.group_id in self.cfg.target_groups,
            })
            if inserted:
                log("debug", "message_persisted", group_id=evt.group_id, trace_id=evt.trace_id, message_id=evt.message_id)
        except Exception as exc:
            log("error", "message_persist_error", group_id=evt.group_id, trace_id=evt.trace_id, error=str(exc))

    def apply_voice_transcript(self, evt: OneBotEvent) -> None:
        """Persist WeChat built-in voice-to-text result into the matching record item.

        The transcript is produced by the WeChat UI hook and is persisted into the
        matching record item before it is processed as an ordinary group message.
        """
        raw = evt.raw if isinstance(evt.raw, dict) else {}
        transcript = str(raw.get("voice_transcript_text") or evt.text.replace("[语音转文字]", "")).strip()
        transcript = re.sub(r"^\[语音转文字\]\s*", "", transcript).strip()
        if not transcript:
            return
        voice_message_id = str(raw.get("voice_message_id") or "").strip()
        try:
            rows = self.store.media(evt.group_id, "record", limit=30)
            target = None
            if voice_message_id:
                for row in rows:
                    if str(row.get("message_id") or "") == voice_message_id or voice_message_id in str(row.get("raw_message") or ""):
                        target = row
                        break
            if target is None:
                for row in rows:
                    if not str(row.get("ocr_text") or "").strip() and str(row.get("status") or "") in {"indexed", "waiting_transcript", "metadata_ready"}:
                        target = row
                        break
            if target is None and rows:
                target = rows[0]
            if not target:
                log("warning", "voice_transcript_no_record", group_id=evt.group_id, transcript=transcript[:120], trace_id=evt.trace_id)
                return
            saved = self.store.save_media_annotation(
                int(target["id"]),
                transcript,
                f"微信客户端自动转文字：{transcript}",
                "transcribed",
                ["语音泡", "自动转文字"],
                [transcript, "语音", "转文字", evt.sender_name or evt.user_id],
                "",
            )
            log("info", "voice_transcript_saved", group_id=evt.group_id, media_id=saved.get("id"),
                message_id=voice_message_id, transcript=transcript[:240], trace_id=evt.trace_id)
        except Exception as exc:
            log("error", "voice_transcript_save_error", group_id=evt.group_id, error=str(exc), trace_id=evt.trace_id)

    def parse_event(self, raw: Dict[str, Any]) -> Tuple[Optional[OneBotEvent], str]:
        if raw.get("post_type") != "message":
            return None, "not_message"
        if raw.get("message_type") != "group":
            return None, "not_group"
        group_id = str(raw.get("group_id") or "")
        if not group_id:
            return None, "no_group_id"

        text_parts: List[str] = []
        media_types: List[str] = []
        for m in raw.get("message") or []:
            if not isinstance(m, dict):
                continue
            msg_type = str(m.get("type") or "")
            if msg_type == "text":
                data = m.get("data") or {}
                text_parts.append(str(data.get("text") or ""))
            elif msg_type in {"image", "file", "video", "record", "face"}:
                media_types.append("image" if msg_type == "face" else msg_type)
        text = "".join(text_parts).strip()
        has_text = bool(text)
        if not text and media_types:
            text = " ".join(f"[{x}]" for x in media_types)
        if not text:
            return None, "no_text"

        sender = raw.get("sender") or {}
        message_id = str(raw.get("message_id") or "")
        raw_message = str(raw.get("raw_message") or "")
        event_id_src = f"{group_id}|{message_id}|{raw.get('time')}|{text}|{','.join(media_types)}"
        event_id = hashlib.sha1(event_id_src.encode("utf-8", "ignore")).hexdigest()
        trace_id = "wx-" + hashlib.sha1((event_id_src + "|" + str(time.time_ns())).encode("utf-8", "ignore")).hexdigest()[:12]
        return OneBotEvent(
            event_id=event_id,
            group_id=group_id,
            group_name=self.cfg.target_groups.get(group_id, group_id),
            self_id=str(raw.get("self_id") or ""),
            user_id=str(raw.get("user_id") or sender.get("user_id") or ""),
            sender_name=str(sender.get("nickname") or sender.get("card") or raw.get("user_id") or ""),
            text=text,
            raw_message=raw_message,
            message_id=message_id,
            timestamp=int(raw.get("time") or time.time()),
            raw=raw,
            trace_id=trace_id,
            has_text=has_text,
            media_types=media_types,
        ), "ok"

    def should_reply(self, evt: OneBotEvent) -> bool:
        now = time.time()
        # Compact dedupe cache.
        if len(self.seen) > 1000:
            cutoff = now - 3600
            self.seen = {k: v for k, v in self.seen.items() if v >= cutoff}
        if evt.event_id in self.seen:
            log("debug", "duplicate_event", group_id=evt.group_id, message_id=evt.message_id, trace_id=evt.trace_id)
            return False
        self.seen[evt.event_id] = now

        if self.cfg.log_all_group_messages:
            log("info", "group_message_seen", group_id=evt.group_id, group_name=evt.group_name,
                sender=evt.sender_name, user_id=evt.user_id, text=evt.text[:300], raw_message=evt.raw_message[:300],
                message_id=evt.message_id, trace_id=evt.trace_id)

        if not self.cfg.enabled:
            log("info", "disabled_skip", group_id=evt.group_id, trace_id=evt.trace_id)
            return False
        if evt.group_id not in self.cfg.target_groups:
            log("info", "group_not_target_skip", group_id=evt.group_id,
                configured_groups=list(self.cfg.target_groups.keys()), trace_id=evt.trace_id)
            return False
        if self.cfg.ignore_self_messages and evt.self_id and evt.user_id == evt.self_id:
            log("info", "self_message_skip", group_id=evt.group_id, user_id=evt.user_id, trace_id=evt.trace_id)
            return False
        if self.cfg.allowed_user_ids and evt.user_id not in self.cfg.allowed_user_ids:
            log("info", "sender_not_allowed_skip", group_id=evt.group_id, user_id=evt.user_id,
                allowed_user_ids=self.cfg.allowed_user_ids, trace_id=evt.trace_id)
            return False
        if self.cfg.ignored_user_ids and evt.user_id in self.cfg.ignored_user_ids:
            log("info", "sender_ignored_skip", group_id=evt.group_id, user_id=evt.user_id, trace_id=evt.trace_id)
            return False
        stripped = evt.text.strip()
        for p in self.cfg.ignore_prefixes:
            if p and stripped.startswith(p):
                log("info", "ignore_prefix_skip", group_id=evt.group_id, prefix=p, text=stripped[:120], trace_id=evt.trace_id)
                return False
        if self.cfg.require_keyword:
            if not any(k and k in stripped for k in self.cfg.trigger_keywords):
                log("info", "keyword_skip", group_id=evt.group_id, text=stripped[:120], trace_id=evt.trace_id)
                return False
        # If keywords are configured but not required, remove keyword from prompt only logically; still reply to all.
        last = self.last_reply_at.get(evt.group_id, 0.0)
        if now - last < self.cfg.min_seconds_between_replies_per_group:
            log("info", "cooldown_skip", group_id=evt.group_id, seconds=round(now - last, 3), trace_id=evt.trace_id)
            return False
        self.last_reply_at[evt.group_id] = now
        return True

    def should_queue_media_analysis(self, evt: OneBotEvent) -> bool:
        if not evt.media_types:
            return False
        has_image = any(x == "image" for x in evt.media_types)
        has_record = any(x == "record" for x in evt.media_types)
        return (has_image and self.cfg.vision_ocr.auto_analyze) or (has_record and self.cfg.asr.auto_transcribe)

    def _worker_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                evt = self.events.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.handle_event(evt)
            except Exception as e:
                self.recent_errors.append({"time": now_ts(), "error": str(e), "group_id": evt.group_id, "trace_id": evt.trace_id})
                log("error", "handle_event_exception", error=str(e), traceback=traceback.format_exc(), trace_id=evt.trace_id, group_id=evt.group_id)
            finally:
                self.events.task_done()

    def _media_worker_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                evt = self.media_events.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.analyze_event_media(evt)
            except Exception as exc:
                self.recent_errors.append({"time": now_ts(), "error": str(exc), "group_id": evt.group_id, "trace_id": evt.trace_id})
                log("error", "media_ocr_worker_error", group_id=evt.group_id, trace_id=evt.trace_id,
                    error=str(exc), traceback=traceback.format_exc())
            finally:
                self.media_events.task_done()

    def local_image_data_url(self, value: str) -> str:
        value = str(value or "").strip()
        if not value:
            raise ValueError("图片路径为空")
        if value.startswith(("http://", "https://", "data:image/")):
            return value
        if value.startswith("file://"):
            value = urllib.parse.unquote(urllib.parse.urlparse(value).path)
        path = Path(value).expanduser()
        if not path.exists() or not path.is_file():
            raise ValueError(f"图片文件不存在：{value}")
        mime = mimetypes.guess_type(str(path))[0] or "image/jpeg"
        if not mime.startswith("image/"):
            raise ValueError(f"不是图片文件：{value}")
        raw = path.read_bytes()
        if len(raw) > 10 * 1024 * 1024:
            raise ValueError(f"图片超过 10MB：{len(raw)} bytes")
        return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")

    def vision_api_key(self) -> str:
        return os.getenv(self.cfg.vision_ocr.api_key_env, "") or os.getenv("AI_REPLY_API_KEY", "")

    def analyze_image_with_vision(self, image_value: str, group_name: str = "", sender: str = "") -> Dict[str, Any]:
        v = self.cfg.vision_ocr
        if not v.enabled:
            raise RuntimeError("OCR 模型未启用")
        if not v.base_url or not v.model:
            raise RuntimeError("OCR 模型配置不完整")
        key = self.vision_api_key()
        if not key and not v.base_url.startswith(("http://127.0.0.1", "http://localhost")):
            raise RuntimeError("OCR API Key 未配置")
        image_url = self.local_image_data_url(image_value)
        prompt = (
            v.prompt.strip()
            + "\n\n请严格返回 JSON，不要 Markdown："
            + '{"summary":"一句话描述图片","ocr_text":"图片中可见文字，没有则为空","tags":["物体/场景/人物/动物/表情等标签"],"keywords":["便于日后搜索的关键词"]}'
            + f"\n群：{group_name}\n发送者：{sender}"
        )
        payload = {
            "model": v.model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            }],
            "temperature": 0,
            "max_tokens": 1000,
        }
        req = urllib.request.Request(v.base_url.rstrip("/") + "/chat/completions",
                                     data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        # 部分第三方中转站会拦截 Python-urllib 默认 UA；和 Web 后台测试接口保持一致。
        req.add_header("User-Agent", "openai-python/1.99.0")
        if key:
            req.add_header("Authorization", "Bearer " + key)
        started = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=v.timeout_seconds) as resp:
                body = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", "replace")
            raise RuntimeError(f"OCR HTTP {e.code}: {err_body[:800]}")
        latency_ms = round((time.monotonic() - started) * 1000)
        obj = json.loads(body)
        content = str(obj["choices"][0]["message"]["content"]).strip()
        parsed: Dict[str, Any]
        try:
            cleaned = content.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            parsed = json.loads(cleaned)
        except Exception:
            parsed = {"summary": content[:500], "ocr_text": "", "tags": [], "keywords": []}
        tags = [str(x).strip() for x in parsed.get("tags", []) if str(x).strip()][:20] if isinstance(parsed.get("tags"), list) else []
        keywords = [str(x).strip() for x in parsed.get("keywords", []) if str(x).strip()][:30] if isinstance(parsed.get("keywords"), list) else []
        return {
            "summary": str(parsed.get("summary") or "")[:1200],
            "ocr_text": str(parsed.get("ocr_text") or "")[:4000],
            "tags": tags,
            "keywords": keywords,
            "latency_ms": latency_ms,
            "raw": content[:2000],
        }

    def asr_api_key(self) -> str:
        return os.getenv(self.cfg.asr.api_key_env, "") or os.getenv("AI_REPLY_API_KEY", "")

    def convert_silk_to_wav(self, path: Path) -> Path:
        path = Path(path).expanduser()
        if path.suffix.lower() != ".silk":
            return path
        VOICE_CACHE_DIR = DEFAULT_HOME / "voice_cache"
        VOICE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        sig = hashlib.sha1(f"{path}:{path.stat().st_mtime_ns}:{path.stat().st_size}".encode("utf-8", "ignore")).hexdigest()[:16]
        out = VOICE_CACHE_DIR / f"{path.stem[:60]}-{sig}.wav"
        if out.exists() and out.stat().st_size > 44:
            return out
        binary = DEFAULT_HOME / "bin" / "silk_to_wav"
        if not binary.exists():
            raise RuntimeError(f"缺少 silk_to_wav 转码工具：{binary}")
        proc = subprocess.run([str(binary), str(path), str(out)], text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=30)
        if proc.returncode or not out.exists() or out.stat().st_size <= 44:
            raise RuntimeError("SILK 转 WAV 失败：" + proc.stdout[-1200:])
        return out

    def local_audio_path(self, value: str) -> Path:
        value = str(value or "").strip()
        if not value:
            raise ValueError("语音路径为空")
        if value.startswith("file://"):
            value = urllib.parse.unquote(urllib.parse.urlparse(value).path)
        if value.startswith(("http://", "https://", "data:")):
            raise ValueError("ASR 自动识别需要本地语音原始文件")
        path = Path(value).expanduser()
        if not path.exists() or not path.is_file():
            raise ValueError(f"语音文件不存在：{value}")
        if path.stat().st_size <= 0:
            raise ValueError("语音文件为空")
        if path.stat().st_size > 25 * 1024 * 1024:
            raise ValueError(f"语音超过 25MB：{path.stat().st_size} bytes")
        return self.convert_silk_to_wav(path)

    def multipart_post(self, url: str, fields: Dict[str, str], file_field: str, file_path: Path,
                       timeout: int, api_key: str = "") -> Dict[str, Any]:
        boundary = "----WeChatSecondASR" + hashlib.sha1(str(time.time_ns()).encode()).hexdigest()
        chunks: List[bytes] = []
        for name, value in fields.items():
            if value is None or str(value) == "":
                continue
            chunks.append(f"--{boundary}\r\n".encode())
            chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
            chunks.append(str(value).encode("utf-8"))
            chunks.append(b"\r\n")
        mime = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        raw = file_path.read_bytes()
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(f'Content-Disposition: form-data; name="{file_field}"; filename="{file_path.name}"\r\n'.encode())
        chunks.append(f"Content-Type: {mime}\r\n\r\n".encode())
        chunks.append(raw)
        chunks.append(b"\r\n")
        chunks.append(f"--{boundary}--\r\n".encode())
        req = urllib.request.Request(url, data=b"".join(chunks), method="POST")
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", "openai-python/1.99.0")
        if api_key:
            req.add_header("Authorization", "Bearer " + api_key)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", "replace")
            raise RuntimeError(f"ASR HTTP {e.code}: {err_body[:800]}")
        try:
            return json.loads(body)
        except Exception:
            return {"text": body.strip(), "raw": body[:2000]}

    def transcribe_audio_with_asr(self, audio_value: str) -> Dict[str, Any]:
        a = self.cfg.asr
        if not a.enabled:
            raise RuntimeError("ASR 模型未启用")
        if not a.base_url or not a.model:
            raise RuntimeError("ASR 模型配置不完整")
        key = self.asr_api_key()
        if not key and not a.base_url.startswith(("http://127.0.0.1", "http://localhost")):
            raise RuntimeError("ASR API Key 未配置")
        path = self.local_audio_path(audio_value)
        fields = {"model": a.model, "language": a.language, "prompt": a.prompt, "response_format": "json"}
        started = time.monotonic()
        obj = self.multipart_post(a.base_url.rstrip("/") + "/audio/transcriptions", fields, "file", path, a.timeout_seconds, key)
        latency_ms = round((time.monotonic() - started) * 1000)
        text = str(obj.get("text") or obj.get("transcript") or obj.get("result") or "").strip()
        if not text:
            try:
                text = str(obj["choices"][0]["message"]["content"]).strip()
            except Exception:
                pass
        if not text:
            raise RuntimeError("ASR 响应未包含转写文本")
        return {"text": text[:4000], "latency_ms": latency_ms, "raw": obj}

    def enqueue_asr_transcript_reply(self, evt: OneBotEvent, transcript: str, media_id: int) -> None:
        transcript = str(transcript or "").strip()
        if not transcript:
            return
        raw = dict(evt.raw)
        raw.update({"asr_voice_transcript": True, "voice_transcript_text": transcript, "voice_media_id": media_id, "voice_message_id": evt.message_id})
        event_id_src = f"{evt.event_id}|asr|{media_id}|{transcript}"
        voice_evt = OneBotEvent(
            event_id=hashlib.sha1(event_id_src.encode("utf-8", "ignore")).hexdigest(),
            group_id=evt.group_id,
            group_name=evt.group_name,
            self_id=evt.self_id,
            user_id=evt.user_id,
            sender_name=evt.sender_name,
            text=transcript,
            raw_message=transcript,
            message_id=evt.message_id,
            timestamp=evt.timestamp,
            raw=raw,
            trace_id=evt.trace_id + "-asr",
            has_text=True,
            media_types=[],
        )
        self.persist_incoming(voice_evt)
        if not self.should_reply(voice_evt):
            return
        try:
            self.events.put_nowait(voice_evt)
            log("info", "voice_asr_transcript_queued", group_id=evt.group_id, media_id=media_id,
                message_id=evt.message_id, trace_id=voice_evt.trace_id, text=transcript[:240])
        except queue.Full:
            log("error", "queue_full", group_id=evt.group_id, message_id=evt.message_id, trace_id=voice_evt.trace_id)

    def analyze_event_media(self, evt: OneBotEvent) -> None:
        image_items = [x for x in self.store.media_by_event(evt.event_id) if x.get("media_type") == "image"]
        if image_items and not self.cfg.vision_ocr.enabled:
            log("info", "media_ocr_skip_disabled", group_id=evt.group_id, trace_id=evt.trace_id)
        if self.cfg.vision_ocr.enabled:
            for item in image_items:
                media_id = int(item["id"])
                try:
                    self.store.mark_media_status(media_id, "ocr_running")
                    image_value = str(item.get("file") or item.get("url") or "")
                    result = self.analyze_image_with_vision(image_value, evt.group_name, evt.sender_name or evt.user_id)
                    self.store.save_media_annotation(
                        media_id,
                        ocr_text=result["ocr_text"],
                        image_summary=result["summary"],
                        status="ocr_done",
                        tags=result["tags"],
                        keywords=result["keywords"],
                    )
                    log("info", "media_ocr_done", group_id=evt.group_id, media_id=media_id,
                        latency_ms=result["latency_ms"], summary=result["summary"][:200], tags=result["tags"], trace_id=evt.trace_id)
                except Exception as exc:
                    self.store.mark_media_status(media_id, "ocr_failed", str(exc)[:1000])
                    log("error", "media_ocr_failed", group_id=evt.group_id, media_id=media_id,
                        error=str(exc), trace_id=evt.trace_id)

        record_items = [x for x in self.store.media_by_event(evt.event_id) if x.get("media_type") == "record"]
        if record_items and not self.cfg.asr.enabled:
            log("info", "voice_asr_skip_disabled", group_id=evt.group_id, trace_id=evt.trace_id)
        if self.cfg.asr.enabled:
            for item in record_items:
                media_id = int(item["id"])
                if str(item.get("ocr_text") or "").strip() and str(item.get("status") or "") == "transcribed":
                    continue
                try:
                    self.store.mark_media_status(media_id, "asr_running")
                    audio_value = str(item.get("file") or item.get("url") or "")
                    result = self.transcribe_audio_with_asr(audio_value)
                    transcript = result["text"]
                    self.store.save_media_annotation(
                        media_id,
                        ocr_text=transcript,
                        image_summary=f"ASR语音转文字：{transcript[:240]}",
                        status="transcribed",
                        tags=["语音泡", "ASR转文字"],
                        keywords=[transcript, "语音", "ASR", evt.sender_name or evt.user_id],
                        error="",
                    )
                    log("info", "voice_asr_done", group_id=evt.group_id, media_id=media_id,
                        latency_ms=result["latency_ms"], transcript=transcript[:240], trace_id=evt.trace_id)
                    self.enqueue_asr_transcript_reply(evt, transcript, media_id)
                except Exception as exc:
                    self.store.mark_media_status(media_id, "asr_failed", str(exc)[:1000])
                    log("error", "voice_asr_failed", group_id=evt.group_id, media_id=media_id,
                        error=str(exc), trace_id=evt.trace_id)

    def tool_allowed(self, name: str) -> bool:
        return self.cfg.tools.enabled and name in set(self.cfg.tools.allowed)

    def post_web_admin(self, path: str, payload: Dict[str, Any], timeout: int = 40) -> Dict[str, Any]:
        """Call the local web-admin API for media/voice tool actions."""
        base = os.getenv("WEB_ADMIN_API", "http://127.0.0.1:8765").rstrip("/")
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(base + path, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace") if exc.fp else ""
            try:
                obj = json.loads(body)
                detail = obj.get("error") or body[:500]
            except Exception:
                detail = body[:500]
            raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
        obj = json.loads(body)
        if isinstance(obj, dict) and obj.get("ok") is False:
            raise RuntimeError(str(obj.get("error") or body[:500]))
        return obj.get("data", obj) if isinstance(obj, dict) else {"raw": obj}

    def clean_voice_pack_query(self, text: str) -> str:
        q = str(text or "").strip()
        q = re.sub(r"^/发语音", "", q).strip()
        q = re.sub(r"^(?:给我|给你|帮我|麻烦|可以|能不能|能否|请)\s*(?=(?:来|发|整|搞|播放|语音|声音|音频))", "", q)
        q = re.sub(r"^(?:你\s*)?(?:发个|发条|发(?:给)?)\s*", "", q)
        q = re.sub(r"^(?:一下|一个|一条|来个|来条|发个|发条)\s*(?=(?:语音|声音|音频|语音包))", "", q)
        q = re.sub(r"^(?:整|搞|找|匹配|回复|回应|用|拿|播放|发送|来)\s*", "", q)
        q = re.sub(r"(语音包|语音|声音|音频|素材|内容|出来|逗逗哥们)", " ", q)
        q = re.sub(r"[，。！？、,.!?~～：:；;（）()【】\\[\\]\"'“”‘’]+", " ", q)
        q = re.sub(r"\s+", " ", q).strip()
        compact = q.replace(" ", "")
        if compact in {"句", "一句", "一段", "段", "个", "条", "再", "再句", "再一句", "随便", "给我", "给你"}:
            return ""
        return q

    def generic_voice_pack_request(self, text: str) -> bool:
        raw = str(text or "").strip()
        cleaned = self.clean_voice_pack_query(raw)
        if cleaned:
            return False
        return bool(re.search(r"(语音|语音包|声音|音频|来句|来一句|再来|整句|搞句)", raw))

    def voice_pack_intent_words(self, query: str) -> List[str]:
        q = str(query or "")
        groups = [
            (("招呼", "问好", "你好", "哈喽"), ["你好", "哈喽"]),
            (("hello", "hi"), ["hello", "hi", "你好"]),
            (("早安", "早上好", "上午好"), ["早上好", "早安"]),
            (("晚安", "晚上好"), ["晚安", "晚上好"]),
            (("别急", "稳", "慢慢", "不要急"), ["别急", "稳"]),
            (("道歉", "抱歉", "不好意思", "对不起"), ["不好意思", "对不起", "抱歉"]),
            (("谢谢", "感谢"), ["谢谢", "感谢"]),
            (("笑话", "搞笑", "好笑", "逗逗", "哈哈"), ["哈哈", "搞笑", "笑话"]),
            (("不要", "拒绝", "不行", "别", "滚"), ["不要", "不行", "滚"]),
            (("牛", "厉害", "可以", "强"), ["牛", "厉害", "可以"]),
        ]
        words: List[str] = []
        low = q.lower()
        for keys, vals in groups:
            if any(k.lower() in low for k in keys):
                words.extend(vals)
        return words

    def score_voice_item(self, item: Dict[str, Any], query: str) -> int:
        q = self.clean_voice_pack_query(query).lower()
        title = str(item.get("title") or item.get("text") or "").lower()
        hay = " ".join(str(item.get(k) or "") for k in ("title", "text", "category", "pack_name")).lower()
        score = 0
        if q and q in hay:
            score += 120 + min(len(q), 30)
        if title and q and title in q:
            score += 100 + min(len(title), 30)
        for word in self.voice_pack_intent_words(query):
            if word.lower() in hay:
                score += 90
        # 中文短语按字重叠兜底；英文/数字按词重叠。
        q_chars = {c for c in q if "\u4e00" <= c <= "\u9fff"}
        h_chars = {c for c in hay if "\u4e00" <= c <= "\u9fff"}
        score += len(q_chars & h_chars) * 8
        q_words = {w for w in re.findall(r"[a-z0-9]+", q) if len(w) >= 2}
        h_words = {w for w in re.findall(r"[a-z0-9]+", hay) if len(w) >= 2}
        score += len(q_words & h_words) * 12
        return score

    def voice_pack_candidates(self, query: str, limit: int = 8) -> List[Dict[str, Any]]:
        cleaned = self.clean_voice_pack_query(query)
        if self.generic_voice_pack_request(query):
            cleaned = "你好"
        # 后台和 AI 共用完整索引排序，避免只查最近 300 条导致大语音库漏检。
        candidates = self.store.search_voice_items(cleaned or query, limit=max(1, limit))
        if not candidates and cleaned and cleaned != query:
            candidates = self.store.search_voice_items(query, limit=max(1, limit))
        return candidates

    def select_voice_pack_item(self, query: str) -> Dict[str, Any]:
        raw_query = re.sub(r"^/发语音", "", str(query or "")).strip()
        if raw_query:
            exact_rows = self.store.voice_items(query=raw_query, limit=30)
            exact_rows = [
                row for row in exact_rows
                if str(row.get("title") or row.get("text") or "").strip().lower() == raw_query.lower()
            ]
            if exact_rows:
                exact_rows.sort(key=lambda r: (int(r.get("usage_count") or 0), -int(r.get("id") or 0)))
                return exact_rows[0]
        if self.generic_voice_pack_request(query):
            for exact in ("你好", "你好你好你好", "Hello", "可以"):
                rows = self.store.voice_items(query=exact, limit=20)
                exact_rows = [r for r in rows if str(r.get("title") or r.get("text") or "").lower() == exact.lower()]
                if exact_rows:
                    exact_rows.sort(key=lambda r: (int(r.get("usage_count") or 0), -int(r.get("id") or 0)))
                    return exact_rows[0]
                if rows:
                    rows.sort(key=lambda r: (int(r.get("usage_count") or 0), -int(r.get("id") or 0)))
                    return rows[0]
        candidates = self.voice_pack_candidates(query, limit=1)
        if candidates:
            return candidates[0]
        return {}

    def send_voice_pack_tool(self, evt: OneBotEvent, query: str, quiet: bool = False) -> str:
        if not self.tool_allowed("send_voice_pack"):
            return "语音包发送工具未启用。"
        requested = (query or evt.text or "").strip()
        q = "你好" if self.generic_voice_pack_request(requested) else (self.clean_voice_pack_query(requested) or requested)
        item = self.select_voice_pack_item(q)
        if not item:
            return f"没有匹配到“{q[:80]}”对应的语音内容，请换一个更接近文件名的说法。"
        try:
            self.post_web_admin("/api/voicepacks/send", {"group_id": evt.group_id, "id": item["id"]}, timeout=90)
            log("info", "voice_pack_sent", group_id=evt.group_id, voice_id=item["id"],
                title=item.get("title"), query=q, trace_id=evt.trace_id)
            if quiet:
                return "__NO_TEXT_REPLY__"
            return f"已发送语音包：{item.get('title') or q}"
        except Exception as exc:
            log("error", "voice_pack_send_failed", group_id=evt.group_id, voice_id=item.get("id"),
                error=str(exc), trace_id=evt.trace_id)
            detail = str(exc)
            recoverable_media_error = any(marker in detail for marker in (
                "媒体上传通道未初始化",
                "真实图片/文件上传",
                "文件传输助手",
                "upload voice failed",
                "access violation",
            ))
            if recoverable_media_error:
                try:
                    self.post_web_admin("/api/onebot/media-repair", {}, timeout=30)
                    time.sleep(1.5)
                    self.post_web_admin("/api/voicepacks/send", {"group_id": evt.group_id, "id": item["id"]}, timeout=90)
                    log("info", "voice_pack_sent_after_media_repair", group_id=evt.group_id, voice_id=item["id"],
                        title=item.get("title"), query=q, trace_id=evt.trace_id)
                    if quiet:
                        return "__NO_TEXT_REPLY__"
                    return f"已发送语音包：{item.get('title') or q}"
                except Exception as retry_exc:
                    log("error", "voice_pack_send_retry_failed", group_id=evt.group_id, voice_id=item.get("id"),
                        error=str(retry_exc), trace_id=evt.trace_id)
                    if quiet:
                        return "__NO_TEXT_REPLY__"
                    return "语音通道已触发后台自修复；本次语音发送仍未完成。"
            return "语音通道暂时不可用，已降级为文字回复。"

    def send_face_pack_tool(self, evt: OneBotEvent, query: str, quiet: bool = False) -> str:
        if not self.tool_allowed("send_face_pack"):
            return "表情包发送工具未启用。"
        q = (query or evt.text or "").strip()
        q = re.sub(r"^/发表情", "", q).strip()
        rows = self.store.media(evt.group_id, "image", limit=1, query=q or "表情包")
        if not rows:
            rows = self.store.media(evt.group_id, "image", limit=1)
        if not rows:
            return "当前群还没有可用的表情/图片素材。"
        item = rows[0]
        file_value = str(item.get("file") or item.get("url") or "")
        if not file_value:
            return "选中的表情素材没有本地文件，无法发送。"
        try:
            self.post_web_admin("/api/faces/send", {"group_id": evt.group_id, "id": item["id"]}, timeout=90)
            log("info", "face_pack_sent", group_id=evt.group_id, media_id=item["id"],
                summary=str(item.get("image_summary") or "")[:120], trace_id=evt.trace_id)
            if quiet:
                return "__NO_TEXT_REPLY__"
            desc = item.get("image_summary") or item.get("ocr_text") or f"#{item.get('id')}"
            return f"已发送表情素材：{str(desc)[:60]}"
        except Exception as exc:
            log("error", "face_pack_send_failed", group_id=evt.group_id, media_id=item.get("id"),
                error=str(exc), trace_id=evt.trace_id)
            return f"表情发送失败：{str(exc)[:160]}"

    def command_reply(self, evt: OneBotEvent) -> Optional[str]:
        text = evt.text.strip()
        if not text.startswith("/"):
            return None
        if text == "/状态" and self.tool_allowed("get_status"):
            return (
                f"状态：AI={'启用' if self.cfg.enabled else '停用'}，dry_run={self.cfg.dry_run}，"
                f"队列={self.events.qsize()}，授权群={len(self.cfg.target_groups)}，"
                f"当前模型={self.cfg.ai.model}，渠道={self.cfg.ai.active_channel_id}"
            )
        if text == "/日志" and self.tool_allowed("get_recent_logs"):
            if not self.recent_errors:
                return "最近没有记录到错误。"
            return "最近错误：\n" + "\n".join(f"- {x['time']} {x['error'][:120]}" for x in list(self.recent_errors)[-6:])
        if text == "/群列表" and self.tool_allowed("list_groups"):
            return "已授权群：\n" + "\n".join(f"- {name}（{gid}）" for gid, name in self.cfg.target_groups.items())
        if text == "/重置记忆":
            self.histories.pop(evt.group_id, None)
            try:
                self.store.save_group_memory(evt.group_id, "", [])
            except Exception:
                pass
            self.save_memory()
            return "已清空当前群的上下文记忆。"
        if text.startswith("/查记录"):
            query = text.removeprefix("/查记录").strip()
            rows = self.store.search_messages(query=query, group_id=evt.group_id, limit=8)
            if not rows:
                return "当前群没有查到相关聊天记录。"
            lines = []
            for r in reversed(rows):
                who = r.get("sender_name") or r.get("user_id") or r.get("direction")
                lines.append(f"- {r.get('created_at','')} {who}: {str(r.get('text') or r.get('raw_message') or '')[:120]}")
            return "查询到的最近记录：\n" + "\n".join(lines)
        if text.startswith("/发语音"):
            return self.send_voice_pack_tool(evt, text.removeprefix("/发语音").strip(), quiet=True)
        if text.startswith("/发表情"):
            return self.send_face_pack_tool(evt, text.removeprefix("/发表情").strip(), quiet=True)
        return None

    def build_messages(self, evt: OneBotEvent) -> List[Dict[str, str]]:
        hist = self.histories[evt.group_id]
        personality_rule = (
            "机器人性格（最高优先级，必须严格遵守）：\n" + self.cfg.personality.strip()
            if self.cfg.personality.strip() else ""
        )
        system_content = self.cfg.ai.system_prompt
        if personality_rule:
            system_content += "\n\n" + personality_rule
        messages: List[Dict[str, str]] = [{"role": "system", "content": system_content}]
        if hist:
            ctx = []
            for role, content in hist:
                ctx.append(f"{role}: {content}")
            messages.append({"role": "system", "content": "最近群聊上下文：\n" + "\n".join(ctx)})
        try:
            memory = self.store.group_memory(evt.group_id)
            if memory.get("summary"):
                messages.append({"role": "system", "content": "当前群长期记忆摘要：\n" + str(memory["summary"])[:2000]})
            recent = self.store.recent_context(evt.group_id, min(12, self.cfg.memory.max_turns if self.cfg.memory.enabled else 8))
            if recent:
                lines = []
                for r in recent:
                    who = r.get("sender_name") or r.get("user_id") or r.get("direction")
                    txt = str(r.get("text") or r.get("raw_message") or "").replace("\n", " ")[:240]
                    if txt:
                        lines.append(f"{who}: {txt}")
                if lines:
                    messages.append({"role": "system", "content": "数据库最近消息（用于连续对话，不要逐字复述）：\n" + "\n".join(lines[-12:])})
            image_words = ("图", "图片", "照片", "截图", "表情", "画面", "看见", "刚才", "之前", "那张", "这张", "哈士奇", "狗", "猫", "识别", "内容")
            need_images = any(w in evt.text for w in image_words)
            image_rows = self.store.media(evt.group_id, "image", limit=12, query=evt.text if need_images else "")
            if not image_rows and need_images:
                image_rows = self.store.media(evt.group_id, "image", limit=12)
            if image_rows:
                lines = []
                for x in image_rows[:12]:
                    tags = []
                    keywords = []
                    try:
                        tags = json.loads(x.get("tags_json") or "[]")
                    except Exception:
                        pass
                    try:
                        keywords = json.loads(x.get("keywords_json") or "[]")
                    except Exception:
                        pass
                    who = x.get("sender_name") or x.get("user_id") or "群成员"
                    summary = str(x.get("image_summary") or "").replace("\n", " ")[:220]
                    ocr = str(x.get("ocr_text") or "").replace("\n", " ")[:160]
                    label = "、".join([str(t) for t in (tags + keywords)[:10] if str(t).strip()])
                    if summary or ocr or label:
                        lines.append(f"- #{x.get('id')} {x.get('created_at')} {who}: 摘要={summary or '无'} OCR={ocr or '无'} 标签={label or '无'}")
                if lines:
                    messages.append({"role": "system", "content": "当前群已解析图片记忆（回答图片相关问题时优先参考，可提到图片编号）：\n" + "\n".join(lines)})
            voice_words = ("语音", "语音泡", "刚才说", "说了什么", "讲了什么", "听到", "转文字", "声音", "音频")
            need_voice = any(w in evt.text for w in voice_words)
            record_rows = self.store.media(evt.group_id, "record", limit=10, query=evt.text if need_voice else "")
            if not record_rows and need_voice:
                record_rows = self.store.media(evt.group_id, "record", limit=10)
            if record_rows:
                lines = []
                for x in record_rows[:10]:
                    transcript = str(x.get("ocr_text") or "").replace("\n", " ")[:260]
                    summary = str(x.get("image_summary") or "").replace("\n", " ")[:180]
                    who = x.get("sender_name") or x.get("user_id") or "群成员"
                    if transcript or summary:
                        lines.append(f"- record#{x.get('id')} {x.get('created_at')} {who}: 转文字={transcript or '无'} 摘要={summary or '无'}")
                if lines:
                    messages.append({"role": "system", "content": "当前群语音泡记忆（回答语音相关问题时优先参考）：\n" + "\n".join(lines)})
            voice_pack_rows = self.voice_pack_candidates(evt.text, limit=8)
            if voice_pack_rows:
                lines = []
                for v in voice_pack_rows[:8]:
                    lines.append(f"- voice#{v.get('id')} [{v.get('category')}/{v.get('pack_name')}]: 内容=\"{v.get('title') or v.get('text')}\"")
                messages.append({"role": "system", "content": "可用语音包素材：每条语音的“名称/标题”就是这条语音真实说出的内容，可直接当作回复内容理解和选择。默认使用文字回复；只有最新消息明确要求语音/语音包，或某条语音内容与问题高度匹配且可以直接作答时才使用语音。需要使用时只能输出：/发语音 <语音内容>，不要输出[语音包]、[语音]或任何解释；系统会发送对应语音且不再发送文字。\n" + "\n".join(lines)})
            face_words = ("表情", "表情包", "梗图", "动图", "斗图", "搞笑")
            if any(w in evt.text for w in face_words):
                face_rows = self.store.media(evt.group_id, "image", limit=6, query=evt.text)
                if not face_rows:
                    face_rows = self.store.media(evt.group_id, "image", limit=6, query="表情包")
                if face_rows:
                    lines = []
                    for f in face_rows[:6]:
                        lines.append(f"- face#{f.get('id')}: {str(f.get('image_summary') or f.get('ocr_text') or f.get('raw_message') or '')[:120]}")
                    messages.append({"role": "system", "content": "可用表情包/图片素材（如果用户明确要求发表情，后台可用 /发表情 关键词 调用）：\n" + "\n".join(lines)})
        except Exception as exc:
            log("warning", "memory_context_error", group_id=evt.group_id, trace_id=evt.trace_id, error=str(exc))
        sender = evt.sender_name or evt.user_id or "群成员"
        user_content = f"群：{evt.group_name}({evt.group_id})\n发言人：{sender}\n最新消息：{evt.text}"
        messages.append({"role": "user", "content": user_content})
        return messages

    def handle_event(self, evt: OneBotEvent) -> None:
        log("info", "reply_job_start", group_id=evt.group_id, group_name=evt.group_name,
            message_id=evt.message_id, text=evt.text[:300], trace_id=evt.trace_id)
        command = self.command_reply(evt)
        if command == "__NO_TEXT_REPLY__":
            hist = self.histories[evt.group_id]
            hist.append((evt.sender_name or evt.user_id or "群成员", evt.text))
            hist.append(("AI", "[已发送媒体，无文字回复]"))
            self.save_memory()
            return
        is_asr_transcript = bool(isinstance(evt.raw, dict) and evt.raw.get("asr_voice_transcript"))
        if not is_asr_transcript and command is None and re.search(r"(发|来|整|搞).{0,8}(语音|语音包|声音|音频)", evt.text):
            result = self.send_voice_pack_tool(evt, evt.text, quiet=True)
            if result == "__NO_TEXT_REPLY__":
                hist = self.histories[evt.group_id]
                hist.append((evt.sender_name or evt.user_id or "群成员", evt.text))
                hist.append(("AI", "[已按语音名称匹配并发送语音包]"))
                self.save_memory()
                return
            if result.startswith("语音包发送失败"):
                self.send_group_msg(evt.group_id, result, evt.trace_id)
                hist = self.histories[evt.group_id]
                hist.append((evt.sender_name or evt.user_id or "群成员", evt.text))
                hist.append(("AI", result))
                self.save_memory()
                return
        reply = command if command is not None else self.generate_reply(evt)
        if not reply:
            return
        reply = self.clean_reply(reply)
        if not reply:
            return
        voice_query = self.extract_voice_marker(reply)
        if command is None and voice_query is not None:
            result = self.send_voice_pack_tool(evt, voice_query, quiet=True)
            if result == "__NO_TEXT_REPLY__":
                hist = self.histories[evt.group_id]
                hist.append((evt.sender_name or evt.user_id or "群成员", evt.text))
                hist.append(("AI", "[模型选择语音包回复，无文字回复]"))
                self.save_memory()
                return
            reply = result
        if self.cfg.reply_prefix and not reply.startswith(self.cfg.reply_prefix):
            reply_to_send = self.cfg.reply_prefix + reply
        else:
            reply_to_send = reply
        if self.cfg.dry_run:
            log("info", "dry_run_reply", group_id=evt.group_id, reply=reply_to_send, trace_id=evt.trace_id)
        else:
            if self.cfg.send_delay_seconds > 0:
                time.sleep(self.cfg.send_delay_seconds)
            self.send_group_msg(evt.group_id, reply_to_send, evt.trace_id)
            # 明确请求时再附加语音/表情，不做随机发送，避免群里刷屏。
            if not is_asr_transcript and command is None and re.search(r"(发|来|整|搞).{0,4}(表情|表情包|梗图|动图)", evt.text):
                self.send_face_pack_tool(evt, evt.text, quiet=True)
        hist = self.histories[evt.group_id]
        hist.append((evt.sender_name or evt.user_id or "群成员", evt.text))
        hist.append(("AI", reply_to_send))
        self.save_memory()

    def get_api_key(self, channel: AIChannel) -> str:
        key = os.getenv(channel.api_key_env, "")
        if channel.id == self.cfg.ai.active_channel_id:
            return key or os.getenv("AI_REPLY_API_KEY", "")
        return key

    def channel_order(self) -> List[AIChannel]:
        enabled = [x for x in self.cfg.ai.channels if x.enabled]
        enabled.sort(key=lambda x: (x.id != self.cfg.ai.active_channel_id, x.priority, x.name))
        if not self.cfg.ai.auto_failover:
            return enabled[:1]
        now = time.time()
        available = [x for x in enabled if self.channel_unavailable_until.get(x.id, 0) <= now]
        return available or enabled

    def generate_reply(self, evt: OneBotEvent) -> str:
        channels = self.channel_order()
        if not channels:
            log("error", "no_enabled_ai_channels", group_id=evt.group_id, trace_id=evt.trace_id)
            return ""
        failures: List[str] = []
        for index, channel in enumerate(channels):
            reply, error = self.request_channel(channel, evt)
            if reply:
                self.channel_unavailable_until.pop(channel.id, None)
                if index:
                    log("warning", "channel_failover", group_id=evt.group_id,
                        channel_id=channel.id, channel_name=channel.name, failed_channels=failures, trace_id=evt.trace_id)
                log("info", "channel_success", group_id=evt.group_id, channel_id=channel.id,
                    channel_name=channel.name, model=channel.model, trace_id=evt.trace_id)
                return reply
            failures.append(channel.id)
            self.channel_unavailable_until[channel.id] = time.time() + self.cfg.ai.failure_cooldown_seconds
            log("error", "channel_failed", group_id=evt.group_id, channel_id=channel.id,
                channel_name=channel.name, error=error, trace_id=evt.trace_id)
            self.recent_errors.append({"time": now_ts(), "error": error, "group_id": evt.group_id, "trace_id": evt.trace_id})
        log("error", "all_channels_failed", group_id=evt.group_id, channels=failures, trace_id=evt.trace_id)
        return ""

    def request_channel(self, channel: AIChannel, evt: OneBotEvent) -> Tuple[str, str]:
        if channel.provider != "openai_compatible":
            return "", f"unsupported provider: {channel.provider}"
        api_key = self.get_api_key(channel)
        if not api_key and not channel.base_url.startswith(("http://127.0.0.1", "http://localhost")):
            return "", f"missing API key: {channel.api_key_env}"
        url = channel.base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": channel.model,
            "messages": self.build_messages(evt),
            "temperature": self.cfg.ai.temperature,
            "max_tokens": self.cfg.ai.max_tokens,
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", "Bearer " + api_key)
        # Some OpenAI-compatible relays reject urllib's default fingerprint.
        req.add_header("User-Agent", "openai-python/1.99.0")
        req.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=channel.timeout_seconds) as resp:
                body = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace") if e.fp else ""
            return "", f"HTTP {e.code}: {body[:500]}"
        except Exception as e:
            return "", str(e)
        try:
            obj = json.loads(body)
            reply = obj["choices"][0]["message"]["content"]
            log("info", "ai_reply_generated", group_id=evt.group_id, chars=len(reply),
                model=channel.model, channel_id=channel.id, trace_id=evt.trace_id)
            return str(reply), ""
        except Exception as e:
            return "", f"response parse error: {e}; body={body[:500]}"

    def extract_voice_marker(self, reply: str) -> Optional[str]:
        """Normalize both current and legacy model voice commands."""
        r = str(reply or "").strip()
        patterns = (
            r"^/发语音\s*(.*)$",
            r"^\[语音包\]\s*(.*)$",
            r"^\[语音\]\s*(.*)$",
            r"^(?:语音包|语音回复)\s*[:：]\s*(.*)$",
        )
        for pattern in patterns:
            match = re.match(pattern, r, flags=re.S | re.I)
            if match:
                query = match.group(1).strip().strip("\"'“”‘’ ")
                query = re.sub(r"[。！？!！]+$", "", query).strip()
                log("debug", "voice_marker_detected", marker=r[:20], query=query[:160])
                return query
        return None

    def clean_reply(self, reply: str) -> str:
        r = reply.strip()
        # Remove surrounding quotes occasionally produced by models.
        if len(r) >= 2 and ((r[0] == r[-1] == '"') or (r[0] == r[-1] == "'")):
            r = r[1:-1].strip()
        if len(r) > self.cfg.max_reply_chars:
            r = r[: self.cfg.max_reply_chars].rstrip() + "…"
        return r

    def send_group_msg(self, group_id: str, text: str, trace_id: str = "") -> None:
        url = self.cfg.onebot_api.rstrip("/") + "/send_group_msg"
        payload = {
            "group_id": group_id,
            "message": [{"type": "text", "data": {"text": text}}],
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        delays = [0, 0, 1.2]
        last_error = ""
        for attempt, delay in enumerate(delays, start=1):
            if delay:
                time.sleep(delay)
            req = urllib.request.Request(url, data=data, method="POST")
            req.add_header("Content-Type", "application/json")
            started = time.monotonic()
            log("info", "send_group_start", group_id=group_id, attempt=attempt, trace_id=trace_id, text=text[:300])
            try:
                with urllib.request.urlopen(req, timeout=25) as resp:
                    body = resp.read().decode("utf-8", "replace")
                    status = resp.status
                latency_ms = round((time.monotonic() - started) * 1000)
                ok = status < 300
                try:
                    parsed = json.loads(body)
                    ok = ok and parsed.get("status") in {"ok", "success"} and parsed.get("retcode", 0) in {0, None}
                except ValueError:
                    pass
                if ok:
                    log("info", "send_group_done", group_id=group_id, status=status, latency_ms=latency_ms,
                        body=body[:1000], text=text[:300], attempt=attempt, trace_id=trace_id)
                    try:
                        self.store.add_message({
                            "event_id": f"out|{trace_id}|{attempt}|{time.time_ns()}",
                            "trace_id": trace_id,
                            "direction": "outgoing",
                            "group_id": group_id,
                            "group_name": self.cfg.target_groups.get(group_id, group_id),
                            "user_id": "AI",
                            "sender_name": "AI",
                            "message_id": "",
                            "event_time": int(time.time()),
                            "text": text,
                            "raw_message": text,
                            "segments": payload["message"],
                            "raw": {"onebot_response": body[:1000]},
                            "source": "ai_reply",
                            "selected": True,
                        })
                    except Exception as exc:
                        log("warning", "outgoing_persist_error", group_id=group_id, trace_id=trace_id, error=str(exc))
                    return
                last_error = f"OneBot 返回失败: {body[:800]}"
                log("error", "send_group_error", group_id=group_id, error=last_error, attempt=attempt, latency_ms=latency_ms, trace_id=trace_id)
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace") if e.fp else ""
                last_error = f"HTTP {e.code}: {body[:500]}"
                log("error", "send_group_http_error", group_id=group_id, status=e.code, body=body[:1000], attempt=attempt, trace_id=trace_id)
            except Exception as e:
                last_error = str(e)
                log("error", "send_group_error", group_id=group_id, error=str(e), attempt=attempt, trace_id=trace_id)
        self.recent_errors.append({"time": now_ts(), "error": last_error, "group_id": group_id, "trace_id": trace_id})


SERVICE: Optional[AIReplyService] = None
CONFIG: Optional[AppConfig] = None


class Handler(BaseHTTPRequestHandler):
    server_version = "WeChatSecondAIReply/1.0"

    def _send_json(self, code: int, obj: Dict[str, Any]) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        # Use structured log instead of stderr noise.
        log("debug", "http_access", client=self.client_address[0], message=fmt % args)

    def do_GET(self) -> None:
        if self.path in {"/", "/health", "/status"}:
            assert CONFIG is not None
            assert SERVICE is not None
            self._send_json(200, {
                "status": "ok",
                "enabled": CONFIG.enabled,
                "dry_run": CONFIG.dry_run,
                "target_groups": CONFIG.target_groups,
                "queue_size": SERVICE.events.qsize(),
                "active_channel_id": CONFIG.ai.active_channel_id,
                "enabled_channels": [x.id for x in CONFIG.ai.channels if x.enabled],
                "memory": {"enabled": CONFIG.memory.enabled, "max_turns": CONFIG.memory.max_turns},
                "tools": {"enabled": CONFIG.tools.enabled, "allowed": CONFIG.tools.allowed},
            })
        else:
            self._send_json(404, {"status": "not_found"})

    def do_POST(self) -> None:
        if self.path != "/onebot":
            self._send_json(404, {"status": "not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            raw = json.loads(body.decode("utf-8", "replace"))
        except Exception as e:
            log("error", "bad_request", error=str(e))
            self._send_json(400, {"status": "bad_request", "error": str(e)})
            return
        assert SERVICE is not None
        queued, reason = SERVICE.enqueue_raw(raw, signature=self.headers.get("X-Signature", ""))
        self._send_json(200, {"status": "ok", "queued": queued, "reason": reason})


def write_pid() -> None:
    ensure_dirs()
    PID_PATH.write_text(str(os.getpid()), encoding="utf-8")


def remove_pid() -> None:
    try:
        if PID_PATH.exists() and PID_PATH.read_text().strip() == str(os.getpid()):
            PID_PATH.unlink()
    except Exception:
        pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--check", action="store_true", help="load config and exit")
    args = parser.parse_args()

    cfg_path = Path(args.config).expanduser().resolve()
    cfg = AppConfig.from_file(cfg_path)
    if args.check:
        print(json.dumps({
            "config": str(cfg_path),
            "listen": f"{cfg.listen_host}:{cfg.listen_port}",
            "onebot_api": cfg.onebot_api,
            "target_groups": cfg.target_groups,
            "dry_run": cfg.dry_run,
            "ai_base_url": cfg.ai.base_url,
            "ai_model": cfg.ai.model,
            "channels": [{"id": x.id, "name": x.name, "model": x.model, "enabled": x.enabled,
                          "api_key_present": bool(os.getenv(x.api_key_env, ""))} for x in cfg.ai.channels],
            "memory": {"enabled": cfg.memory.enabled, "max_turns": cfg.memory.max_turns},
            "tools": {"enabled": cfg.tools.enabled, "allowed": cfg.tools.allowed},
        }, ensure_ascii=False, indent=2))
        return 0

    ensure_dirs()
    write_pid()

    global CONFIG, SERVICE
    CONFIG = cfg
    SERVICE = AIReplyService(cfg)
    SERVICE.start()

    def _shutdown(signum: int, frame: Any) -> None:
        log("info", "shutdown_signal", signal=signum)
        if SERVICE:
            SERVICE.stop_event.set()
        remove_pid()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    server = ThreadingHTTPServer((cfg.listen_host, cfg.listen_port), Handler)
    log("info", "ai_reply_server_started", listen=f"{cfg.listen_host}:{cfg.listen_port}",
        config=str(cfg_path), target_groups=cfg.target_groups, dry_run=cfg.dry_run,
        onebot_api=cfg.onebot_api, ai_base_url=cfg.ai.base_url, ai_model=cfg.ai.model,
        active_channel_id=cfg.ai.active_channel_id, enabled_channels=[x.id for x in cfg.ai.channels if x.enabled])
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        remove_pid()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
