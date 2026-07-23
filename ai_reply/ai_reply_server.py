#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OneBot -> AI -> WeChat group reply bridge for the installed WeChat only.

- Listens on 127.0.0.1:36060/onebot (the send_url configured for wechat_chatter OneBot).
- Filters target chatrooms, calls an OpenAI-compatible chat completion API, then sends reply
  through the local OneBot HTTP API at 127.0.0.1:58080/send_group_msg.
"""
from __future__ import annotations

import argparse
import base64
import collections
import concurrent.futures
import hashlib
import hmac
import html
import json
import mimetypes
import os
import queue
import random
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
from types import SimpleNamespace
from typing import Any, Deque, Dict, List, Optional, Tuple

DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "config" / "ai_reply_config.json"
DEFAULT_HOME = Path.home() / "Library" / "Application Support" / "WeChatAgent"
LOG_PATH = DEFAULT_HOME / "logs" / "ai-reply.log"
PID_PATH = DEFAULT_HOME / "ai-reply.pid"
SAFETY_STATE_PATH = DEFAULT_HOME / "safety-state.json"
MEMORY_PATH = DEFAULT_HOME / "ai-group-memory.json"
EVENTS_PATH = DEFAULT_HOME / "logs" / "brain-events.jsonl"

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from memory_store import MemoryStore, is_readable_member_name  # noqa: E402
from brain_engine import (BrainConfig, FINAL_STATES, OpportunityScorer, ReplyScheduler, ReplyTask,
                          TaskRegistry, extract_explicit_media_kind, extract_image_generation_prompt,
                          media_suppression)  # noqa: E402
from embedding_service import EmbeddingConfig, EmbeddingService  # noqa: E402
from durable_sync import DurableSyncService  # noqa: E402
from hermes_automation import HermesAutomationService  # noqa: E402
from graphiti_bridge import GraphitiBridge  # noqa: E402
from group_admin import GroupAdminService, is_admin_command  # noqa: E402


def now_ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def ensure_dirs() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    PID_PATH.parent.mkdir(parents=True, exist_ok=True)


_log_lock = threading.Lock()
_event_lock = threading.Lock()


def log(level: str, msg: str, **fields: Any) -> None:
    ensure_dirs()
    rec = {"time": now_ts(), "level": level, "msg": msg}
    rec.update(fields)
    line = json.dumps(rec, ensure_ascii=False)
    with _log_lock:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        print(line, flush=True)


def emit_brain_event(event: Dict[str, Any]) -> None:
    ensure_dirs()
    line = json.dumps(event, ensure_ascii=False)
    with _event_lock:
        with EVENTS_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


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
class RouterConfig:
    enabled: bool = True
    model: str = "grok-chat-fast"
    base_url: str = ""
    api_key_env: str = ""
    timeout_seconds: int = 2
    max_tokens: int = 320


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
    retrieval_deadline_ms: int = 250
    context_budget_chars: int = 8000
    prompt_budget_chars: int = 24000


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
class ImageGenerationConfig:
    enabled: bool = False
    base_url: str = ""
    api_key_env: str = "AI_REPLY_IMAGE_GENERATION_API_KEY"
    model: str = ""
    size: str = "1024x1024"
    quality: str = "standard"
    timeout_seconds: int = 180
    response_format: str = "b64_json"


@dataclass
class MediaReplyConfig:
    automatic_enabled: bool = True
    voice_probability: float = 0.15
    face_probability: float = 0.20
    voice_min_fit: float = 55.0
    face_min_fit: float = 45.0
    min_candidate_confidence: float = 0.65
    global_face_assets: bool = True
    auto_media_replaces_text: bool = True
    group_overrides: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def from_raw(cls, raw: Dict[str, Any]) -> "MediaReplyConfig":
        value = raw.get("media_reply") if isinstance(raw.get("media_reply"), dict) else {}
        legacy_min_fit = float(value.get("min_fit", 70))
        return cls(
            automatic_enabled=bool(value.get("automatic_enabled", True)),
            voice_probability=max(0.0, min(1.0, float(value.get("voice_probability", 0.15)))),
            face_probability=max(0.0, min(1.0, float(value.get("face_probability", 0.20)))),
            voice_min_fit=max(0.0, min(100.0, float(value.get("voice_min_fit", legacy_min_fit if "min_fit" in value else 55)))),
            face_min_fit=max(0.0, min(100.0, float(value.get("face_min_fit", legacy_min_fit if "min_fit" in value else 45)))),
            min_candidate_confidence=max(0.0, min(1.0, float(value.get("min_candidate_confidence", 0.65)))),
            global_face_assets=bool(value.get("global_face_assets", True)),
            auto_media_replaces_text=bool(value.get("auto_media_replaces_text", True)),
            group_overrides={str(k): dict(v) for k, v in (value.get("group_overrides") or {}).items() if isinstance(v, dict)},
        )

    def for_group(self, group_id: str) -> Dict[str, Any]:
        result = {
            "automatic_enabled": self.automatic_enabled,
            "voice_probability": self.voice_probability,
            "face_probability": self.face_probability,
            "voice_min_fit": self.voice_min_fit,
            "face_min_fit": self.face_min_fit,
            "min_candidate_confidence": self.min_candidate_confidence,
            "global_face_assets": self.global_face_assets,
            "auto_media_replaces_text": self.auto_media_replaces_text,
        }
        override = dict(self.group_overrides.get(str(group_id), {}))
        if "min_fit" in override:
            override.setdefault("voice_min_fit", override["min_fit"])
            override.setdefault("face_min_fit", override["min_fit"])
            override.pop("min_fit", None)
        result.update(override)
        return result


@dataclass
class PokeReplyConfig:
    enabled: bool = False
    text_enabled: bool = True
    image_enabled: bool = False
    texts: List[str] = field(default_factory=lambda: ["拍我干嘛～", "在呢，别拍了。"])
    face_ids: List[int] = field(default_factory=list)
    # Stable WeChat identities that represent the bot even when OneBot was
    # attached to another saved account after an automatic login.  This is an
    # explicit allowlist: arbitrary group members are never inferred as bot
    # targets from their nickname, aliases or historical messages.
    bot_target_ids: List[str] = field(default_factory=list)
    cooldown_seconds: int = 8

    @classmethod
    def from_raw(cls, raw: Dict[str, Any]) -> "PokeReplyConfig":
        value = raw.get("poke_reply") if isinstance(raw.get("poke_reply"), dict) else {}
        source_texts = value.get("texts") if "texts" in value else ["拍我干嘛～", "在呢，别拍了。"]
        texts = [str(x).strip()[:200] for x in (source_texts or []) if str(x).strip()]
        face_ids = list(dict.fromkeys(int(x) for x in (value.get("face_ids") or []) if str(x).isdigit() and int(x) > 0))
        bot_target_ids = list(dict.fromkeys(
            str(x).strip()[:128] for x in (value.get("bot_target_ids") or []) if str(x).strip()
        ))
        return cls(enabled=bool(value.get("enabled", False)), text_enabled=bool(value.get("text_enabled", True)),
                   image_enabled=bool(value.get("image_enabled", False)), texts=texts,
                   face_ids=face_ids[:100], bot_target_ids=bot_target_ids[:20],
                   cooldown_seconds=max(0, min(300, int(value.get("cooldown_seconds", 8)))))


@dataclass
class ReplyDecision:
    text: str = ""
    medium: str = "text"
    voice_fit: float = 0.0
    face_fit: float = 0.0
    media_query: str = ""
    intent: str = ""
    reason: str = ""


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
    ignored_group_members: Dict[str, List[str]] = field(default_factory=dict)
    group_reply_enabled: Dict[str, bool] = field(default_factory=dict)
    group_personalities: Dict[str, Dict[str, Any]] = field(default_factory=dict)
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
    router: RouterConfig = field(default_factory=RouterConfig)
    vision_ocr: VisionOCRConfig = field(default_factory=VisionOCRConfig)
    asr: ASRConfig = field(default_factory=ASRConfig)
    image_generation: ImageGenerationConfig = field(default_factory=ImageGenerationConfig)
    media_reply: MediaReplyConfig = field(default_factory=MediaReplyConfig)
    poke_reply: PokeReplyConfig = field(default_factory=PokeReplyConfig)
    brain: BrainConfig = field(default_factory=BrainConfig)
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)

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
        router_raw = raw.get("router", {}) or {}
        tools_raw = raw.get("tools", {}) or {}
        vision_raw = raw.get("vision_ocr", {}) or {}
        asr_raw = raw.get("asr", {}) or {}
        image_gen_raw = raw.get("image_generation", {}) or {}
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
            ignored_group_members={
                str(group_id): sorted({str(user_id) for user_id in user_ids if str(user_id).strip()})
                for group_id, user_ids in (
                    raw.get("ignored_group_members", {}) if isinstance(raw.get("ignored_group_members"), dict) else {}
                ).items()
                if str(group_id).endswith("@chatroom") and isinstance(user_ids, list)
            },
            group_reply_enabled={
                str(group_id): bool(enabled)
                for group_id, enabled in (
                    raw.get("group_reply_enabled", {}) if isinstance(raw.get("group_reply_enabled"), dict) else {}
                ).items()
                if str(group_id).endswith("@chatroom")
            },
            group_personalities={
                str(group_id): (
                    {"enabled": True, "prompt": str(value)}
                    if isinstance(value, str) else
                    {"enabled": bool(value.get("enabled", True)), "prompt": str(value.get("prompt") or "")}
                )
                for group_id, value in (
                    raw.get("group_personalities", {}) if isinstance(raw.get("group_personalities"), dict) else {}
                ).items()
                if str(group_id).endswith("@chatroom") and isinstance(value, (str, dict))
            },
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
                retrieval_deadline_ms=max(50, min(2000, int(memory_raw.get("retrieval_deadline_ms", 250)))),
                context_budget_chars=max(2000, min(24000, int(memory_raw.get("context_budget_chars", 8000)))),
                prompt_budget_chars=max(8000, min(64000, int(memory_raw.get("prompt_budget_chars", 24000)))),
            ),
            tools=ToolsConfig(
                enabled=bool(tools_raw.get("enabled", True)),
                allowed=[str(x) for x in tools_raw.get("allowed", ["get_status", "get_recent_logs", "list_groups", "test_model_channel", "send_probe", "search_messages", "get_group_memory", "vector_search", "list_personas", "list_media", "send_voice_pack", "send_face_pack"])],
            ),
            safety=safety,
            ai=ai,
            router=RouterConfig(
                enabled=bool(router_raw.get("enabled", True)),
                model=str(router_raw.get("model") or "grok-chat-fast"),
                base_url=str(router_raw.get("base_url") or ai.base_url).rstrip("/"),
                api_key_env=str(router_raw.get("api_key_env") or ai.api_key_env),
                timeout_seconds=max(1, min(5, int(router_raw.get("timeout_seconds", 2)))),
                max_tokens=max(128, min(600, int(router_raw.get("max_tokens", 320)))),
            ),
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
            image_generation=ImageGenerationConfig(
                enabled=bool(image_gen_raw.get("enabled", False)),
                base_url=str(image_gen_raw.get("base_url") or ai.base_url).rstrip("/"),
                api_key_env=str(image_gen_raw.get("api_key_env") or "AI_REPLY_IMAGE_GENERATION_API_KEY"),
                model=str(image_gen_raw.get("model") or ""),
                size=str(image_gen_raw.get("size") or "1024x1024"),
                quality=str(image_gen_raw.get("quality") or "standard"),
                timeout_seconds=max(10, min(600, int(image_gen_raw.get("timeout_seconds", 180)))),
                response_format=str(image_gen_raw.get("response_format") or "b64_json"),
            ),
            media_reply=MediaReplyConfig.from_raw(raw),
            poke_reply=PokeReplyConfig.from_raw(raw),
            brain=BrainConfig.from_raw(raw),
            embedding=EmbeddingConfig.from_raw(raw),
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
        self.media_events: "queue.Queue[OneBotEvent]" = queue.Queue(maxsize=300)
        self.culture_events: "queue.Queue[OneBotEvent]" = queue.Queue(maxsize=100)
        self.stop_event = threading.Event()
        self.seen: Dict[str, float] = {}
        self.last_reply_at: Dict[str, float] = {}
        self.channel_unavailable_until: Dict[str, float] = {}
        self.state_lock = threading.RLock()
        self.memory_file_lock = threading.RLock()
        self.channel_state_lock = threading.RLock()
        self.router_state: Dict[str, Any] = {
            "enabled": cfg.router.enabled,
            "configured": bool(cfg.router.base_url and cfg.router.model),
            "healthy": None,
            "model": cfg.router.model,
            "last_checked_at": "",
            "last_error": "",
        }
        self.culture_state_lock = threading.RLock()
        self.culture_pending: set[str] = set()
        self.culture_message_counts: Dict[str, int] = collections.defaultdict(int)
        self.culture_last_run: Dict[str, float] = collections.defaultdict(float)
        self.poke_last_reply_at: Dict[str, float] = collections.defaultdict(float)
        self.media_analysis_events: Dict[int, threading.Event] = {}
        self.media_analysis_events_lock = threading.RLock()
        self.history_locks: Dict[str, threading.RLock] = collections.defaultdict(threading.RLock)
        self.send_locks: Dict[str, threading.RLock] = collections.defaultdict(threading.RLock)
        self.model_semaphore = threading.BoundedSemaphore(cfg.brain.model_concurrency)
        self.media_send_semaphore = threading.BoundedSemaphore(1)
        self.histories: Dict[str, Deque[Tuple[str, str]]] = collections.defaultdict(
            lambda: collections.deque(maxlen=max(2, self.cfg.memory.max_turns if self.cfg.memory.enabled else self.cfg.max_context_messages))
        )
        self.recent_errors: Deque[Dict[str, Any]] = collections.deque(maxlen=30)
        self.store = MemoryStore()
        self.durable_sync = DurableSyncService(self.store)
        self.hermes = HermesAutomationService(
            self.store,
            lambda group_id, text, trace_id: self.send_group_msg(group_id, text, trace_id),
        )
        self.graphiti = GraphitiBridge()
        self.memory_route_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="realtime-memory"
        )
        try:
            self.store.rebuild_face_assets()
        except Exception as exc:
            log("warning", "face_index_bootstrap_failed", error=str(exc))
        self.load_memory()
        self.task_registry = TaskRegistry(self.store, emit_brain_event)
        self.scorer = OpportunityScorer(cfg.brain)
        self.embedding_service = EmbeddingService(self.store, cfg.embedding, emit_brain_event)
        self.scheduler = ReplyScheduler(cfg.brain, self.task_registry)
        self.group_admin_service = GroupAdminService(
            self.store,
            DEFAULT_CONFIG,
            reload_callback=self._reload_admin_config,
            runtime_callback=self._admin_runtime,
        )
        self.media_worker = threading.Thread(target=self._media_worker_loop, name="ai-media-worker", daemon=True)
        self.culture_worker = threading.Thread(target=self._culture_worker_loop, name="culture-learner", daemon=True)

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
        with self.memory_file_lock:
            ensure_dirs()
            data = {"updated_at": now_ts(), "histories": {gid: list(rows)[-self.cfg.memory.max_turns:] for gid, rows in self.histories.items()}}
            tmp = MEMORY_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, MEMORY_PATH)

    def start(self) -> None:
        self.scheduler.start()
        self.durable_sync.start()
        self.hermes.start()
        self.graphiti.start()
        self.embedding_service.start()
        self.media_worker.start()
        self.culture_worker.start()
        self.recover_pending_media_analysis()

    def stop(self) -> None:
        self.stop_event.set()
        self.scheduler.stop()
        self.durable_sync.stop()
        self.hermes.stop()
        self.graphiti.stop()
        self.memory_route_executor.shutdown(wait=False, cancel_futures=True)
        self.embedding_service.stop()

    def reload_config(self, cfg: AppConfig) -> Dict[str, Any]:
        """Apply mutable configuration without replacing the running process."""
        with self.state_lock:
            old_brain = self.cfg.brain
            self.cfg = cfg
            self._update_router_state({
                "enabled": cfg.router.enabled,
                "configured": bool(cfg.router.base_url and cfg.router.model),
                "model": cfg.router.model,
            })
            self.scorer.cfg = cfg.brain
            self.scheduler.reconfigure(cfg.brain)
            self.embedding_service.cfg = cfg.embedding
            if old_brain.model_concurrency != cfg.brain.model_concurrency:
                self.model_semaphore = threading.BoundedSemaphore(cfg.brain.model_concurrency)
        emit_brain_event({"type": "service_status", "time": time.time(), "event": "config_reloaded",
                          "brain": self.brain_status(), "pid": os.getpid()})
        return {"pid": os.getpid(), "brain": self.brain_status()}

    def _reload_admin_config(self) -> Dict[str, Any]:
        try:
            result = self.reload_config(AppConfig.from_file(DEFAULT_CONFIG))
            return {"applied": True, **result}
        except Exception as exc:
            return {"applied": False, "error": str(exc)}

    def _update_router_state(self, values: Dict[str, Any]) -> None:
        """Keep router telemetry safe for lightweight test/service instances."""
        state = getattr(self, "router_state", None)
        if not isinstance(state, dict):
            cfg = getattr(getattr(self, "cfg", None), "router", None)
            state = {
                "enabled": bool(getattr(cfg, "enabled", False)),
                "configured": bool(getattr(cfg, "base_url", "") and getattr(cfg, "model", "")),
                "healthy": None,
                "model": str(getattr(cfg, "model", "") or ""),
                "last_checked_at": "",
                "last_error": "",
            }
            self.router_state = state
        state.update(values)

    def _admin_runtime(self, group_id: str) -> Dict[str, Any]:
        tasks = self.store.reply_tasks(group_id, 20)
        active = [item for item in tasks if item.get("state") not in FINAL_STATES]
        return {
            "active_tasks": len(active),
            "tasks": [
                f"{item.get('state_label') or item.get('state')} · {str(item.get('question') or '')[:70]}"
                for item in active[:8]
            ],
            "errors": [
                f"{item.get('created_at') or item.get('time') or ''} · {str(item.get('error') or '')[:100]}"
                for item in list(self.recent_errors)[-8:] if not item.get("group_id") or item.get("group_id") == group_id
            ],
        }

    def brain_status(self) -> Dict[str, Any]:
        scheduler = self.scheduler.snapshot()
        tasks = self.task_registry.snapshot(200)
        return {
            "scheduler": scheduler,
            "tasks": tasks,
            "embedding": self.embedding_service.snapshot(),
            "durable": self.durable_sync.snapshot(),
            "hermes": self.hermes.snapshot(),
            "graphiti": self.graphiti.snapshot(),
            "router": dict(getattr(self, "router_state", {})),
            "media_queue": self.media_events.qsize(),
            "culture_queue": self.culture_events.qsize(),
            "media_channel_waiting": max(0, sum(
                1 for item in tasks.get("items", []) if item.get("state") == "waiting_media_channel"
            )),
            "reply_strategy": {
                "mode": self.cfg.brain.mode,
                "threshold": self.cfg.brain.threshold,
                "scoring_mode": self.cfg.brain.scoring_mode,
                "rerank_candidates": self.cfg.brain.rerank_candidates,
                "mute_duration_seconds": self.cfg.brain.mute_duration_seconds,
                "mention_user_on_reply": self.cfg.brain.mention_user_on_reply,
                "factor_weights": self.cfg.brain.factor_weights,
                "modifiers": self.cfg.brain.modifiers,
            },
            "active_group_mutes": self.store.active_group_reply_mutes(),
            "retrieval": {
                "vector_limit": self.cfg.embedding.vector_limit, "fts_limit": self.cfg.embedding.fts_limit,
                "fusion_limit": self.cfg.embedding.fusion_limit, "adaptive_rerank": self.cfg.embedding.adaptive_rerank,
            },
            "media_reply": self.cfg.media_reply.for_group(""),
        }

    def enqueue_raw(self, raw: Dict[str, Any], signature: str = "") -> Tuple[bool, str]:
        poke = self.parse_poke_event(raw)
        if poke:
            if self.group_is_muted(poke.group_id, poke.trace_id, "poke"):
                return False, "group_muted"
            if not self.cfg.poke_reply.enabled:
                return False, "poke_reply_disabled"
            # Poke replies are a dedicated global feature.  The ordinary AI
            # target-group ACL must not silently disable them in other groups;
            # parse_poke_event has already verified that this is a group event
            # and that a configured bot identity is the member being patted.
            now = time.time()
            with self.state_lock:
                if now - self.poke_last_reply_at[poke.group_id] < self.cfg.poke_reply.cooldown_seconds:
                    return False, "poke_reply_cooldown"
                self.poke_last_reply_at[poke.group_id] = now
            threading.Thread(target=self.handle_poke_reply, args=(poke,), name="poke-reply", daemon=True).start()
            return True, "poke_reply_queued"
        evt, reason = self.parse_event(raw)
        if not evt:
            return False, reason
        if (not raw.get("voice_transcript") and is_admin_command(evt.text)
                and str(raw.get("post_type") or "") == "message"
                and str(raw.get("message_type") or "") == "group"):
            result = self.group_admin_service.handle(evt)
            if result and result.get("handled"):
                emit_brain_event({
                    "type": "admin_command", "event": "handled", "time": time.time(),
                    "group_id": evt.group_id, "user_id": evt.user_id, "command": evt.text[:300],
                    "authorized": bool(result.get("authorized")), "duplicate": bool(result.get("duplicate")),
                    "audit_id": result.get("audit_id"), "error": result.get("error", ""),
                    "trace_id": evt.trace_id,
                })
                card = str(result.get("card") or "")
                if card:
                    threading.Thread(
                        target=self.send_admin_card, args=(evt, card),
                        name="group-admin-receipt", daemon=True,
                    ).start()
                return True, "admin_command_duplicate" if result.get("duplicate") else "admin_command_handled"
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
            if self.is_mute_command(evt.text):
                self.activate_group_mute(evt)
                return False, "group_muted"
            if self.group_is_muted(evt.group_id, evt.trace_id, "voice_transcript"):
                return False, "group_muted"
            if not self.should_reply(evt):
                return False, "voice_transcript_indexed"
            task = self.scheduler.submit(evt, self.handle_brain_task)
            log("info", "voice_transcript_queued", group_id=evt.group_id,
                message_id=evt.message_id, trace_id=evt.trace_id, task_id=task.task_id, text=transcript[:240])
            return True, "voice_transcript_queued"
        self.persist_incoming(evt)
        if self.should_queue_media_analysis(evt):
            try:
                for item in self.store.media_by_event(evt.event_id):
                    media_type = str(item.get("media_type") or "")
                    queued_status = "ocr_queued" if media_type == "image" else "asr_queued"
                    self.store.claim_media_status(int(item["id"]), ["indexed", "ocr_failed", "asr_failed"], queued_status)
                self.media_events.put_nowait(evt)
                log("info", "media_analysis_queued", group_id=evt.group_id, media_types=evt.media_types,
                    message_id=evt.message_id, trace_id=evt.trace_id)
            except queue.Full:
                log("error", "media_analysis_queue_full", group_id=evt.group_id, message_id=evt.message_id, trace_id=evt.trace_id)
        if self.is_mute_command(evt.text):
            self.activate_group_mute(evt)
            return False, "group_muted"
        if self.group_is_muted(evt.group_id, evt.trace_id, "message"):
            return False, "group_muted"
        if not evt.has_text:
            return False, "media_only_indexed"
        if not self.should_reply(evt):
            return False, "ignored"
        task = self.scheduler.submit(evt, self.handle_brain_task)
        return True, "queued:" + task.task_id

    @staticmethod
    def is_mute_command(text: str) -> bool:
        normalized = re.sub(r"[\s\u2005\u00a0，。！？!?、~～]+", "", str(text or ""))
        return normalized == "闭嘴"

    def activate_group_mute(self, evt: OneBotEvent) -> Dict[str, Any]:
        result = self.store.set_group_reply_mute(
            evt.group_id, self.cfg.brain.mute_duration_seconds, evt.user_id, evt.message_id
        )
        log("info", "group_reply_muted", group_id=evt.group_id, group_name=evt.group_name,
            duration_seconds=self.cfg.brain.mute_duration_seconds, muted_until=result.get("muted_until"),
            user_id=evt.user_id, message_id=evt.message_id, trace_id=evt.trace_id)
        emit_brain_event({"type": "service_status", "event": "group_reply_muted", "group_id": evt.group_id,
                          "duration_seconds": self.cfg.brain.mute_duration_seconds,
                          "muted_until": result.get("muted_until"), "time": time.time()})
        return result

    def group_is_muted(self, group_id: str, trace_id: str = "", source: str = "") -> bool:
        if not getattr(self, "store", None) or not hasattr(self.store, "group_reply_mute"):
            return False
        mute = self.store.group_reply_mute(group_id)
        if not mute.get("active"):
            return False
        log("info", "group_reply_mute_skip", group_id=group_id, source=source,
            remaining_seconds=mute.get("remaining_seconds"), trace_id=trace_id)
        return True

    def parse_poke_event(self, raw: Dict[str, Any]) -> Optional[OneBotEvent]:
        post_type = str(raw.get("post_type") or "")
        notice_type = str(raw.get("notice_type") or raw.get("event_type") or "").lower()
        sub_type = str(raw.get("sub_type") or raw.get("type") or "").lower()
        raw_message = str(raw.get("raw_message") or raw.get("content") or "")
        if not raw_message:
            for segment in raw.get("message") or []:
                if isinstance(segment, dict) and str(segment.get("type") or "") == "sys":
                    data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
                    raw_message = str(data.get("text") or "")
                    if raw_message:
                        break
        structured = post_type == "notice" and (notice_type in {"notify", "poke", "pat"} or sub_type in {"poke", "pat"})
        xml_pat = bool(re.search(r"<(?:sysmsg[^>]+type=[\"']pat[\"']|pat\b|patmsg\b)", raw_message, re.I))
        if not structured and not xml_pat:
            return None

        def pat_xml_value(tag: str) -> str:
            match = re.search(
                rf"<{tag}>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</{tag}>",
                raw_message,
                re.I | re.S,
            )
            return str(match.group(1) if match else "").strip()

        group_id = str(raw.get("group_id") or raw.get("from_id") or "")
        self_id = str(raw.get("self_id") or "")
        target_id = str(raw.get("target_id") or raw.get("receiver_id") or pat_xml_value("pattedusername"))
        # A pat event must positively identify either the currently attached
        # OneBot account or one of the explicitly configured bot identities.
        # The latter keeps the bot responsive when WeChat's saved-account
        # auto-login attaches OneBot to another local account, without
        # reintroducing the old "pat any member and the bot replies" bug.
        accepted_target_ids = {self_id, *self.cfg.poke_reply.bot_target_ids}
        accepted_target_ids.discard("")
        if not group_id.endswith("@chatroom") or not self_id or target_id not in accepted_target_ids:
            if group_id.endswith("@chatroom") and target_id:
                log("info", "poke_target_rejected", group_id=group_id, self_id=self_id,
                    target_id=target_id, configured_target_count=len(self.cfg.poke_reply.bot_target_ids))
            return None
        if target_id != self_id:
            log("warning", "poke_bot_identity_fallback", group_id=group_id, self_id=self_id,
                target_id=target_id, reason="onebot_account_differs_from_configured_bot_identity")
        user_id = str(raw.get("operator_id") or raw.get("user_id") or raw.get("sender_id") or "")
        if not user_id or user_id.endswith("@chatroom"):
            user_id = pat_xml_value("fromusername")
        if not user_id or user_id.endswith("@chatroom"):
            return None
        sender = raw.get("sender") if isinstance(raw.get("sender"), dict) else {}
        sender_name = self.store.resolve_member_name(group_id, user_id, str(sender.get("card") or sender.get("nickname") or ""))
        seed = f"poke|{group_id}|{user_id}|{raw.get('time')}|{raw.get('message_id')}"
        return OneBotEvent(event_id=hashlib.sha1(seed.encode()).hexdigest(), group_id=group_id,
                           group_name=self.cfg.target_groups.get(group_id, group_id), self_id=self_id,
                           user_id=user_id, sender_name=sender_name, text="[拍一拍机器人]",
                           raw_message=raw_message, message_id=str(raw.get("message_id") or ""),
                           timestamp=int(raw.get("time") or time.time()), raw=raw,
                           trace_id="poke-" + hashlib.sha1((seed + str(time.time_ns())).encode()).hexdigest()[:12])

    def handle_poke_reply(self, evt: OneBotEvent) -> None:
        started = time.monotonic()
        if self.group_is_muted(evt.group_id, evt.trace_id, "poke_worker"):
            return
        config = self.cfg.poke_reply
        media_options = ["text"] if config.text_enabled and config.texts else []
        if config.image_enabled and config.face_ids:
            media_options.append("face")
        if not media_options:
            log("warning", "poke_reply_no_content", group_id=evt.group_id, trace_id=evt.trace_id)
            return
        medium = random.choice(media_options)
        try:
            if medium == "face":
                face_ids = list(dict.fromkeys(config.face_ids))
                random.shuffle(face_ids)
                candidates = []
                for face_id in face_ids:
                    item = self.store.face_asset(face_id)
                    if item and int(item.get("enabled", 0)):
                        candidates.append(item)
                if not candidates:
                    raise RuntimeError("拍一拍没有可用的已选表情")
                # A native upload can be accepted and still never call CDN
                # completion (observed with malformed/unsupported GIF assets).
                # Keep such assets out of the instant-reply pool after their
                # failures catch up with successes; users can still test them
                # manually or replace/re-encode the file.
                reliable = [
                    item for item in candidates
                    if int(item.get("failure_count") or 0) < max(1, int(item.get("success_count") or 0))
                ]
                if reliable:
                    candidates = reliable
                # One poke is exactly one upload attempt. Cascading through all
                # selected GIFs turns a transient failure into 20+ seconds of
                # native uploads and can destabilize the WeChat process.
                item = random.choice(candidates)
                self.send_poke_face_fast(evt, item)
            else:
                self.send_group_msg(evt.group_id, random.choice(config.texts), evt.trace_id, evt)
            log("info", "poke_reply_sent", group_id=evt.group_id, user_id=evt.user_id,
                medium=medium, asset_id=int(item.get("id") or 0) if medium == "face" else 0,
                asset_file=str(item.get("file") or "")[-180:] if medium == "face" else "", fast_path=True,
                elapsed_ms=round((time.monotonic() - started) * 1000), trace_id=evt.trace_id)
        except Exception as exc:
            log("error", "poke_reply_failed", group_id=evt.group_id, medium=medium,
                fast_path=True, elapsed_ms=round((time.monotonic() - started) * 1000),
                error=str(exc), trace_id=evt.trace_id)

    def send_poke_face_fast(self, evt: OneBotEvent, item: Dict[str, Any]) -> None:
        """Send a configured poke image without entering normal reply/tool/task auditing."""
        file_value = str(item.get("file") or item.get("url") or "")
        if not file_value:
            raise RuntimeError("拍一拍表情没有本地文件")
        # Native type=8 emoticons neither upload nor use the shared media
        # channel. Waiting on that semaphore made poke replies stall behind an
        # unrelated voice/image upload.
        result = self.post_web_admin("/api/faces/send", {
            "group_id": evt.group_id,
            "face_id": item["id"],
            "trace_id": evt.trace_id,
            "fast_path": True,
        }, timeout=4)
        if str(result.get("state") or "") not in {"sent", "timeout_confirmed"}:
            raise RuntimeError(str(result.get("error") or "表情发送未确认"))

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
                durable_sync = getattr(self, "durable_sync", None)
                if durable_sync is not None:
                    durable_sync.notify()
                log("debug", "message_persisted", group_id=evt.group_id, trace_id=evt.trace_id, message_id=evt.message_id)
                self.schedule_culture_learning(evt)
        except Exception as exc:
            log("error", "message_persist_error", group_id=evt.group_id, trace_id=evt.trace_id, error=str(exc))

    @staticmethod
    def quoted_message_metadata(raw: Dict[str, Any]) -> Dict[str, str]:
        """Extract WeChat's embedded refermsg metadata from a quote message."""
        sources = [str(raw.get("raw_message") or "")]
        for segment in raw.get("message") or []:
            if isinstance(segment, dict) and str(segment.get("type") or "") == "text":
                data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
                sources.append(str(data.get("text") or ""))
        source = next((value for value in sources if "<refermsg" in value), "")
        if not source:
            return {}
        block_match = re.search(r"<refermsg\b[^>]*>(.*?)</refermsg>", source, re.I | re.S)
        if not block_match:
            return {}
        block = block_match.group(1)

        def tag(name: str, value: str = block) -> str:
            match = re.search(rf"<{name}\b[^>]*>(.*?)</{name}>", value, re.I | re.S)
            return html.unescape(match.group(1)).strip() if match else ""

        content = tag("content")
        image_md5 = ""
        md5_match = re.search(r"\bmd5=[\"']([0-9a-fA-F]{16,64})[\"']", content)
        if md5_match:
            image_md5 = md5_match.group(1).lower()
        title_match = re.search(r"<appmsg\b[^>]*>.*?<title\b[^>]*>(.*?)</title>", source, re.I | re.S)
        return {
            "type": tag("type"),
            "message_id": tag("svrid"),
            "group_id": tag("fromusr"),
            "user_id": tag("chatusr"),
            "sender_name": tag("displayname"),
            "created_at": tag("createtime"),
            "content": content,
            "md5": image_md5,
            "title": html.unescape(title_match.group(1)).strip() if title_match else "",
        }

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

        quoted = self.quoted_message_metadata(raw)
        if quoted:
            raw["_quoted_message"] = quoted
            segments = raw.get("message") if isinstance(raw.get("message"), list) else []
            if quoted.get("message_id") and not any(
                isinstance(segment, dict) and segment.get("type") == "reply" for segment in segments
            ):
                segments.append({"type": "reply", "data": {"id": quoted["message_id"], "source": "wechat_refermsg"}})
                raw["message"] = segments

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
        text = (quoted.get("title") or "".join(text_parts)).strip()
        has_text = bool(text)
        if not text and media_types:
            text = " ".join(f"[{x}]" for x in media_types)
        if not text:
            return None, "no_text"

        sender = raw.get("sender") or {}
        raw_message = str(raw.get("raw_message") or "")
        user_id = str(raw.get("user_id") or sender.get("user_id") or "").strip()
        # Some group protobuf events carry the chatroom id as user_id when the
        # sender uses a legacy/non-wxid username. The raw prefix still contains
        # the real account id (for example "saarjoye:\n...").
        if not user_id or user_id.endswith("@chatroom"):
            prefix = raw_message.split(":", 1)[0].strip() if ":" in raw_message else ""
            if re.fullmatch(r"[A-Za-z0-9_.@-]{2,128}", prefix) and not prefix.endswith("@chatroom"):
                user_id = prefix
        sender_name = self.store.resolve_member_name(
            group_id,
            user_id,
            str(sender.get("nickname") or sender.get("card") or ""),
        )
        message_id = str(raw.get("message_id") or "")
        event_id_src = f"{group_id}|{message_id}|{raw.get('time')}|{text}|{','.join(media_types)}"
        event_id = hashlib.sha1(event_id_src.encode("utf-8", "ignore")).hexdigest()
        trace_id = "wx-" + hashlib.sha1((event_id_src + "|" + str(time.time_ns())).encode("utf-8", "ignore")).hexdigest()[:12]
        return OneBotEvent(
            event_id=event_id,
            group_id=group_id,
            group_name=self.cfg.target_groups.get(group_id, group_id),
            self_id=str(raw.get("self_id") or ""),
            user_id=user_id,
            sender_name=sender_name,
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
        with self.state_lock:
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
        if self.group_is_muted(evt.group_id, evt.trace_id, "should_reply"):
            return False
        if evt.group_id not in self.cfg.target_groups:
            log("info", "group_not_target_skip", group_id=evt.group_id,
                configured_groups=list(self.cfg.target_groups.keys()), trace_id=evt.trace_id)
            return False
        if not getattr(self.cfg, "group_reply_enabled", {}).get(evt.group_id, True):
            log("info", "group_reply_disabled_skip", group_id=evt.group_id, trace_id=evt.trace_id)
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
        group_ignored = self.cfg.ignored_group_members.get(evt.group_id, [])
        if evt.user_id in group_ignored:
            log("info", "group_member_blacklisted_skip", group_id=evt.group_id,
                group_name=evt.group_name, user_id=evt.user_id, sender=evt.sender_name,
                message_id=evt.message_id, trace_id=evt.trace_id)
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
        return True

    def should_queue_media_analysis(self, evt: OneBotEvent) -> bool:
        if not evt.media_types:
            return False
        has_image = any(x == "image" for x in evt.media_types)
        has_record = any(x == "record" for x in evt.media_types)
        return (has_image and self.cfg.vision_ocr.auto_analyze) or (has_record and self.cfg.asr.auto_transcribe)

    def schedule_culture_learning(self, evt: OneBotEvent) -> None:
        """Extract permanent aliases, relationships and memes every 30 messages/15 minutes."""
        with self.culture_state_lock:
            self.culture_message_counts[evt.group_id] += 1
            due_count = self.culture_message_counts[evt.group_id] >= 30
            due_time = time.time() - self.culture_last_run[evt.group_id] >= 900
            if (not due_count and not due_time) or evt.group_id in self.culture_pending:
                return
            try:
                self.culture_events.put_nowait(evt)
            except queue.Full:
                return
            self.culture_pending.add(evt.group_id)
            self.culture_message_counts[evt.group_id] = 0

    def _culture_worker_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                evt = self.culture_events.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.learn_group_culture(evt)
            except Exception as exc:
                log("error", "culture_learning_failed", group_id=evt.group_id, error=str(exc), trace_id=evt.trace_id)
            finally:
                with self.culture_state_lock:
                    self.culture_pending.discard(evt.group_id)
                    self.culture_last_run[evt.group_id] = time.time()
                self.culture_events.task_done()

    def learn_group_culture(self, evt: OneBotEvent) -> None:
        rows = self.store.recent_context(evt.group_id, 100)
        if len(rows) < 3:
            return
        transcript = "\n".join(
            f"[{row.get('message_id')}] {row.get('user_id')}({row.get('sender_name')}): "
            f"{str(row.get('text') or row.get('raw_message') or '')[:400]}"
            for row in rows
        )
        prompt = (
            "从微信群记录中提取可永久保存的群文化。只输出JSON，结构为"
            '{"aliases":[{"user_id":"","alias":"","confidence":0-1,"evidence":[]}],'
            '"relations":[{"from_user_id":"","to_user_id":"","relation":"","confidence":0-1,"evidence":[]}],'
            '"memes":[{"name":"","meaning":"","triggers":[],"confidence":0-1,"evidence":[],"related_media":[]}]}。'
            "置信度仅用于排序，所有有文本证据的结果都保留；不要根据否定反馈删除或停用旧记录。\n" + transcript
        )
        reply, model = self.request_messages([
            {"role": "system", "content": "你是群聊历史整理器，保留外号、关系、群梗与经典说法。"},
            {"role": "user", "content": prompt},
        ], 1200, 0.0, evt)
        if not reply:
            return
        cleaned = re.sub(r"^```(?:json)?|```$", "", reply.strip(), flags=re.I).strip()
        start, end = cleaned.find("{"), cleaned.rfind("}")
        obj = json.loads(cleaned[start:end + 1])
        counts = {"aliases": 0, "relations": 0, "memes": 0}
        for item in obj.get("aliases") or []:
            if item.get("user_id") and item.get("alias"):
                self.store.upsert_alias(evt.group_id, str(item["user_id"]), str(item["alias"]),
                                        float(item.get("confidence") or 0), list(item.get("evidence") or []))
                counts["aliases"] += 1
        for item in obj.get("relations") or []:
            if item.get("from_user_id") and item.get("to_user_id") and item.get("relation"):
                self.store.upsert_relation(evt.group_id, str(item["from_user_id"]), str(item["to_user_id"]),
                                           str(item["relation"]), float(item.get("confidence") or 0),
                                           list(item.get("evidence") or []))
                counts["relations"] += 1
        for item in obj.get("memes") or []:
            if item.get("name"):
                self.store.upsert_meme(evt.group_id, str(item["name"]), str(item.get("meaning") or ""),
                                       list(item.get("triggers") or []), list(item.get("evidence") or []),
                                       list(item.get("related_media") or []), float(item.get("confidence") or 0))
                counts["memes"] += 1
        emit_brain_event({"type": "model_task", "time": time.time(), "event": "culture_learned",
                          "group_id": evt.group_id, "model": model, "counts": counts})

    def handle_brain_task(self, task: ReplyTask, evt: OneBotEvent) -> None:
        registry = self.task_registry
        brain_cfg = self.cfg.brain.for_group(evt.group_id)
        scorer = getattr(self, "scorer", None) if brain_cfg is self.cfg.brain else None
        scorer = scorer or OpportunityScorer(brain_cfg)
        if self.group_is_muted(evt.group_id, evt.trace_id, "task_start"):
            registry.update(task, "skipped", result="group_muted", details={"mute_gate": True})
            return
        pipeline_started = time.monotonic()
        command_text = re.sub(r"^@\S+\s*", "", str(evt.text or "").strip()).strip()
        explicit_command = next((name for name in ("/发语音", "/发表情", "/生图")
                                 if command_text.startswith(name)), "")
        if not explicit_command and extract_image_generation_prompt(command_text):
            explicit_command = "/生图"
        if explicit_command:
            evt.raw["_brain_task_id"] = task.task_id
            state = "generating_image" if explicit_command == "/生图" else (
                "selecting_voice" if explicit_command == "/发语音" else "selecting_face"
            )
            registry.update(task, state, medium="image" if state == "generating_image" else "")
            self.handle_event(evt)
            if task.state not in FINAL_STATES:
                registry.update(task, "completed", result=task.result or "media_sent")
            return
        registry.update(task, "scoring", threshold=brain_cfg.threshold)
        recent = self.store.recent_context(evt.group_id, 30)
        last_outgoing = next((x for x in reversed(recent) if x.get("direction") == "outgoing"), None)
        last_bot_reply = None
        recent_tasks = self.store.reply_tasks(evt.group_id, 100)
        latest_completed = next((x for x in recent_tasks if x.get("state") == "completed"), None)
        if last_outgoing:
            last_bot_reply = {"user_id": str((latest_completed or {}).get("user_id") or ""),
                              "event_time": last_outgoing.get("event_time")}

        prefilter_started = time.monotonic()
        prefilter = scorer.local_score(evt, recent, {"items": [], "culture": {}}, last_bot_reply)
        prefilter_ms = (time.monotonic() - prefilter_started) * 1000
        prefilter_threshold = min(20.0, float(brain_cfg.threshold))
        if not prefilter["mandatory"] and float(prefilter["pre_score"]) < prefilter_threshold:
            registry.update(task, "skipped", score=float(prefilter["pre_score"]), details={
                "local": prefilter, "reason": "local_prefilter", "prefilter_threshold": prefilter_threshold,
                "timings_ms": {"prefilter": round(prefilter_ms, 1), "pre_generation_total": round((time.monotonic() - pipeline_started) * 1000, 1)},
            })
            return

        registry.update(task, "retrieving_memory")
        retrieval_started = time.monotonic()
        retrieval_deadline_ms = getattr(
            getattr(self.cfg, "memory", None), "retrieval_deadline_ms", 250
        )
        graph_future = None
        central_future = None
        if getattr(self, "graphiti", None) and getattr(self, "memory_route_executor", None):
            graph_future = self.memory_route_executor.submit(
                self.graphiti.search, evt.text, evt.group_id, 8,
                max(50, retrieval_deadline_ms - 30),
            )
        if (getattr(self, "durable_sync", None)
                and getattr(self, "memory_route_executor", None)
                and self.durable_sync.cfg.enabled):
            central_future = self.memory_route_executor.submit(
                self.durable_sync.search_messages, evt.text, evt.group_id, 8,
            )
        memory = self.embedding_service.search(
            evt.text, evt.group_id, 12,
            stage_callback=lambda state: registry.update(task, state),
            rerank_candidates=brain_cfg.rerank_candidates,
            context_messages=recent[-4:],
            sender_name=evt.sender_name,
            deadline_ms=retrieval_deadline_ms,
        )
        graph_items: List[Dict[str, Any]] = []
        remaining = retrieval_deadline_ms / 1000.0 - (time.monotonic() - retrieval_started)
        if graph_future is not None and remaining > 0:
            try:
                graph_items = graph_future.result(timeout=remaining)
            except Exception:
                graph_future.cancel()
        if graph_items:
            memory = {
                **memory,
                "items": [*(memory.get("items") or []), *graph_items][:12],
                "graph_count": len(graph_items),
            }
        central_items: List[Dict[str, Any]] = []
        remaining = retrieval_deadline_ms / 1000.0 - (time.monotonic() - retrieval_started)
        if central_future is not None and remaining > 0:
            try:
                for item in central_future.result(timeout=remaining):
                    central_items.append({
                        **item,
                        "source": "postgres_pgroonga",
                        "route_sources": ["postgres_pgroonga"],
                        "object_type": "message",
                        "object_id": str(item.get("event_id") or ""),
                    })
            except Exception:
                central_future.cancel()
        if central_items:
            known = {self.memory_item_id(item) for item in memory.get("items") or []}
            fresh = [item for item in central_items if self.memory_item_id(item) not in known]
            memory = {
                **memory,
                "items": [*(memory.get("items") or []), *fresh][:12],
                "central_count": len(fresh),
            }
        task.details = {**task.details, "memory_count": len(memory.get("items") or []),
                        "embedding_error": memory.get("error", "")}
        scoring_started = time.monotonic()
        local = scorer.local_score(evt, recent, memory, last_bot_reply)
        if brain_cfg.scoring_mode == "model_deep":
            factors, score_reason, score_model = self.score_reply_opportunity(evt, recent, memory, local)
        else:
            factors, score_reason = scorer.local_factors(evt, recent, memory, local, last_bot_reply)
            score_model = "local-fast"
        score = scorer.final_score(factors, local.get("reasons") or [])
        scoring_ms = (time.monotonic() - scoring_started) * 1000
        timings = {"prefilter": round(prefilter_ms, 1), **(memory.get("timings_ms") or {}),
                   "scoring": round(scoring_ms, 1),
                   "pre_generation_total": round((time.monotonic() - pipeline_started) * 1000, 1)}
        task.details = {"local": local, "factors": factors, "score_reason": score_reason,
                        "scoring_mode": brain_cfg.scoring_mode, "timings_ms": timings,
                        "recalled_count": memory.get("recalled_count", 0),
                        "reranked_count": memory.get("reranked_count", 0),
                        "route_counts": memory.get("route_counts", {}),
                        "pinned_count": memory.get("pinned_count", 0),
                        "expanded_second_batch": memory.get("expanded_second_batch", False),
                        "rerank_skipped_reason": memory.get("rerank_skipped_reason", ""),
                        "rerank_cache_hits": memory.get("rerank_cache_hits", 0),
                        "memory": [{k: v for k, v in x.items() if k != "vector_blob"} for x in (memory.get("items") or [])[:12]]}
        registry.update(task, "scoring", score=score, threshold=brain_cfg.threshold, model=score_model, details=task.details)
        mandatory_trigger = bool(local.get("mandatory"))
        if score < brain_cfg.threshold and not mandatory_trigger:
            task.details = {
                **task.details,
                "threshold_gate": "below_threshold",
                "below_threshold": True,
                "trigger_was_mandatory": False,
            }
            registry.update(task, "skipped", result="score_below_threshold", details=task.details)
            log("info", "score_below_threshold_skip", group_id=evt.group_id,
                user_id=evt.user_id, sender=evt.sender_name, score=score,
                threshold=brain_cfg.threshold, mandatory=bool(local.get("mandatory")),
                alias_hit=str(local.get("alias_hit") or ""), task_id=task.task_id,
                message_id=evt.message_id, trace_id=evt.trace_id)
            return

        if mandatory_trigger:
            task.details = {
                **task.details,
                "threshold_gate": "mandatory_bypass",
                "below_threshold": score < brain_cfg.threshold,
                "trigger_was_mandatory": True,
                "mandatory_reason": (
                    "at_self" if local.get("at_self") else
                    "reply" if local.get("reply_id") else
                    "bot_alias" if local.get("alias_hit") else
                    "explicit_media"
                ),
            }
            registry.update(task, "scoring", score=score, threshold=brain_cfg.threshold,
                            model=score_model, details=task.details)
            log("info", "mandatory_trigger_bypass_threshold", group_id=evt.group_id,
                user_id=evt.user_id, sender=evt.sender_name, score=score,
                threshold=brain_cfg.threshold, reason=task.details["mandatory_reason"],
                alias_hit=str(local.get("alias_hit") or ""), task_id=task.task_id,
                message_id=evt.message_id, trace_id=evt.trace_id)

        routing_started = time.monotonic()
        route = self.fast_route(evt, memory, recent, mandatory_trigger)
        routing_ms = round((time.monotonic() - routing_started) * 1000, 1)
        timings["routing"] = routing_ms
        task.details = {**task.details, "timings_ms": timings, "fast_route": route}
        if not mandatory_trigger and route.get("available") and not route.get("reply_required", True):
            self.task_registry.update(task, "skipped", result="fast_router_declined", details=task.details)
            return
        selected_ids = {str(x) for x in route.get("memory_ids") or [] if str(x)}
        if selected_ids:
            selected = [
                item for item in memory.get("items") or []
                if self.memory_item_id(item) in selected_ids
            ]
            if selected:
                memory = {**memory, "items": selected[:12]}
        if route.get("automation_required"):
            result = self.hermes.submit(evt, route)
            message = str(result.get("message") or "自动化任务未能接收。")
            try:
                self.send_group_msg(evt.group_id, message, evt.trace_id, evt)
            except Exception as exc:
                task.details = {**task.details, "automation": result, "automation_ack_error": str(exc)}
                self.task_registry.update(task, "failed", result="automation_ack_failed", details=task.details)
                return
            task.details = {**task.details, "automation": result}
            self.task_registry.update(
                task, "completed" if result.get("accepted") else "failed",
                result="automation_queued" if result.get("accepted") else "automation_rejected",
                details=task.details,
            )
            return
        evt.raw["_brain_memory"] = memory
        evt.raw["_brain_score"] = {"score": score, "factors": factors, "reason": score_reason}
        evt.raw["_fast_route"] = route
        evt.raw["_brain_task_id"] = task.task_id
        registry.update(task, "generating")
        self.handle_event(evt)
        if task.state not in FINAL_STATES:
            registry.update(task, "completed", result=task.result or "sent")

    def score_reply_opportunity(self, evt: OneBotEvent, recent: List[Dict[str, Any]],
                                memory: Dict[str, Any], local: Dict[str, Any]) -> Tuple[Dict[str, float], str, str]:
        defaults = {key: float(local.get("pre_score") or 50) for key in self.cfg.brain.factor_weights}
        context = []
        for row in recent[-12:]:
            context.append(f"{row.get('sender_name') or row.get('user_id')}: {str(row.get('text') or row.get('raw_message') or '')[:240]}")
        recalled = [str(x.get("text") or x.get("raw_message") or "")[:300] for x in (memory.get("items") or [])[:8]]
        culture = memory.get("culture") or {}
        prompt = (
            "你是微信群社交机会评分器。只输出一个JSON对象，不要Markdown。评分均为0到100。"
            "不要因为消息指向其他成员、机器人刚说过话、机器人消息占比、严肃语境、素材或梗近期使用过而扣分。"
            "字段必须是 involvement,continuity,memory,value,humor,emotion,timing,reason。\n"
            "involvement评估提到机器人、承接其观点及对成员/话题熟悉度；continuity评估追问、未结束问题、语义承接和引用；"
            "memory评估外号、关系、群梗、经典原话、旧图片语音和历史事件；value评估新观点、补充、纠错和有价值反应；"
            "humor评估反差、包袱、回怼、翻旧梗、新梗空间和熟人关系；emotion只判断怎样表达符合气氛，不直接扣接话分；"
            "timing评估消息是否说完、上下文是否完整及是否值得等待。\n"
            f"当前消息：{evt.sender_name}: {evt.text}\n最近对话：\n" + "\n".join(context) +
            "\n相关永久记忆：\n" + "\n".join(recalled) +
            "\n外号/关系/群梗：" + json.dumps(culture, ensure_ascii=False)[:2500]
        )
        reply, model = self.request_messages([
            {"role": "system", "content": "你只负责评估是否值得像群内熟人一样接话。"},
            {"role": "user", "content": prompt},
        ], max_tokens=320, temperature=0.0, evt=evt)
        if not reply:
            return defaults, "model_score_unavailable", model
        try:
            cleaned = re.sub(r"^```(?:json)?|```$", "", reply.strip(), flags=re.I).strip()
            start, end = cleaned.find("{"), cleaned.rfind("}")
            obj = json.loads(cleaned[start:end + 1])
            factors = {key: max(0.0, min(100.0, float(obj.get(key, defaults[key])))) for key in defaults}
            return factors, str(obj.get("reason") or ""), model
        except Exception:
            return defaults, "model_score_parse_fallback", model

    @staticmethod
    def memory_item_id(item: Dict[str, Any]) -> str:
        return ":".join((
            str(item.get("object_type") or item.get("source") or "message"),
            str(item.get("object_id") or item.get("event_id") or item.get("id") or ""),
        ))

    def fast_route(self, evt: OneBotEvent, memory: Dict[str, Any],
                   recent: List[Dict[str, Any]], mandatory: bool) -> Dict[str, Any]:
        text = str(evt.text or "")
        automation_terms = (
            "github", "git ", "提交代码", "推送代码", "更新项目", "跑测试",
            "运行测试", "构建", "部署", "重启服务", "恢复服务", "服务状态",
            "健康检查", "定时任务", "定时", "提醒", "闹钟", "分钟后",
            "小时后", "每天", "每周", "每月", "倒计时", "监控",
        )
        capability_terms = (
            "天气", "气温", "温度", "降雨", "空气质量", "台风",
            "实时", "最新消息", "最新新闻", "今天新闻", "热搜",
            "股价", "股票价格", "汇率", "金价", "油价", "比赛结果",
            "查一下", "查询一下", "搜一下", "搜索一下", "帮我查",
            "网上查", "联网查", "浏览网页", "打开网页", "官网", "网址", "链接",
        )
        lower_text = text.lower()
        local_automation = any(term in lower_text for term in automation_terms)
        local_capability = any(term in lower_text for term in capability_terms)
        local_hermes = local_automation or local_capability
        high_terms = ("强制推送", "force push", "删除", "密钥", "生产部署", "回滚")
        write_terms = (
            "提交", "推送", "更新", "测试", "构建", "部署", "重启",
            "恢复", "创建", "暂停", "执行", "提醒", "闹钟", "定时",
        )
        local_risk = (
            "high" if any(term in text.lower() for term in high_terms)
            else "write" if any(term in text.lower() for term in write_terms)
            else "read"
        )
        fallback = {
            "available": False,
            "reply_required": bool(mandatory),
            "memory_ids": [],
            "automation_required": local_hermes,
            "automation_intent": text[:500] if local_hermes else "",
            "risk_level": local_risk if local_automation else "read",
            "hermes_mode": "automation" if local_automation else "answer",
            "model": "",
            "reason": "local_fallback",
        }
        cfg = getattr(self.cfg, "router", None)
        if cfg is None:
            self._update_router_state({"enabled": False, "configured": False, "healthy": None})
            return fallback
        if not cfg.enabled or not cfg.base_url or not cfg.model:
            self._update_router_state({
                "enabled": cfg.enabled, "configured": bool(cfg.base_url and cfg.model),
                "healthy": None, "model": cfg.model, "last_checked_at": now_ts(),
                "last_error": "disabled_or_incomplete",
            })
            return fallback
        candidates = []
        for item in (memory.get("items") or [])[:18]:
            candidates.append({
                "id": self.memory_item_id(item),
                "text": str(item.get("text") or item.get("raw_message") or item.get("searchable_text") or "")[:280],
                "source": list(item.get("route_sources") or []),
            })
        recent_lines = [
            f"{row.get('sender_name') or row.get('user_id')}: "
            f"{str(row.get('text') or row.get('raw_message') or '')[:180]}"
            for row in recent[-6:]
        ]
        prompt = (
            "你是微信群机器人的高速路由器。只输出一个JSON对象，不要Markdown。"
            "字段必须为 reply_required(bool), memory_ids(string数组，最多8个), "
            "automation_required(bool), automation_intent(string), "
            "risk_level(read|write|high), reason(string)。"
            "明确@机器人、引用机器人、叫机器人名字或直接提问时 reply_required 必须为 true。"
            "需要实时天气、新闻、价格、比赛、联网搜索、浏览网页、读取外部资源、调用工具，"
            "或者操作代码、GitHub、测试、部署、监控、定时任务时 automation_required 必须为 true。"
            "纯聊天和可直接可靠回答的常识才为 false。"
            "memory_ids只能从候选ID中选择，不相关记忆不要选。\n"
            f"强制触发={mandatory}\n当前消息={evt.sender_name}: {evt.text[:800]}\n"
            "最近对话=\n" + "\n".join(recent_lines) +
            "\n候选记忆=" + json.dumps(candidates, ensure_ascii=False)
        )
        channel = AIChannel(
            id="fast-router", name="fast-router", provider="openai_compatible",
            base_url=cfg.base_url, api_key_env=cfg.api_key_env, model=cfg.model,
            timeout_seconds=cfg.timeout_seconds, enabled=True, priority=-1,
        )
        reply, error = self.request_channel_messages(
            channel,
            [{"role": "system", "content": "只做高速路由并输出严格JSON。"},
             {"role": "user", "content": prompt}],
            cfg.max_tokens, 0.0, evt,
        )
        if not reply:
            self._update_router_state({
                "enabled": True, "configured": True, "healthy": False,
                "model": cfg.model, "last_checked_at": now_ts(), "last_error": error[:500],
            })
            return {**fallback, "reason": f"router_unavailable:{error[:160]}"}
        try:
            clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", reply.strip(), flags=re.I | re.S)
            parsed = json.loads(clean)
            allowed_ids = {item["id"] for item in candidates}
            chosen = [str(x) for x in parsed.get("memory_ids") or [] if str(x) in allowed_ids][:8]
            result = {
                "available": True,
                "reply_required": bool(parsed.get("reply_required", mandatory)) or bool(mandatory),
                "memory_ids": chosen,
                "automation_required": bool(parsed.get("automation_required", False)),
                "automation_intent": str(parsed.get("automation_intent") or "")[:500],
                "risk_level": str(parsed.get("risk_level") or "read")
                    if str(parsed.get("risk_level") or "read") in {"read", "write", "high"} else "high",
                "hermes_mode": (
                    "automation" if str(parsed.get("risk_level") or "read") in {"write", "high"}
                    else "answer"
                ),
                "model": cfg.model,
                "reason": str(parsed.get("reason") or "")[:300],
            }
            self._update_router_state({
                "enabled": True, "configured": True, "healthy": True,
                "model": cfg.model, "last_checked_at": now_ts(), "last_error": "",
            })
            return result
        except Exception as exc:
            self._update_router_state({
                "enabled": True, "configured": True, "healthy": False,
                "model": cfg.model, "last_checked_at": now_ts(), "last_error": str(exc)[:500],
            })
            return {**fallback, "reason": f"router_parse_fallback:{exc}"}

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

    def media_analysis_event(self, media_id: int) -> threading.Event:
        with self.media_analysis_events_lock:
            event = self.media_analysis_events.get(int(media_id))
            if event is None:
                event = threading.Event()
                self.media_analysis_events[int(media_id)] = event
            return event

    def recover_pending_media_analysis(self) -> int:
        """Requeue media left unfinished by a process restart or log reconciliation."""
        recovered = 0
        kinds: List[str] = []
        if self.cfg.vision_ocr.enabled and self.cfg.vision_ocr.auto_analyze:
            kinds.append("image")
        if self.cfg.asr.enabled and self.cfg.asr.auto_transcribe:
            kinds.append("record")
        for media_type in kinds:
            for item in self.store.pending_media_analysis(media_type, 100):
                try:
                    raw = json.loads(str(item.get("raw_json") or "{}"))
                    evt, _ = self.parse_event(raw)
                    if not evt:
                        continue
                    # Log recovery can normalize timestamps or message text after
                    # the callback row was first stored. Recomputing the hash then
                    # points at a different event and the worker sees no media.
                    # The database event_id is the canonical join key.
                    evt.event_id = str(item.get("event_id") or evt.event_id)
                    evt.group_id = str(item.get("group_id") or evt.group_id)
                    evt.message_id = str(item.get("message_id") or evt.message_id)
                    evt.trace_id = str(item.get("trace_id") or evt.trace_id)
                    desired = "ocr_queued" if media_type == "image" else "asr_queued"
                    self.store.mark_media_status(int(item["id"]), desired)
                    self.media_events.put_nowait(evt)
                    recovered += 1
                except (ValueError, TypeError, queue.Full):
                    break
        if recovered:
            log("info", "media_analysis_recovered", recovered=recovered)
        return recovered

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
        boundary = "----WeChatAgentASR" + hashlib.sha1(str(time.time_ns()).encode()).hexdigest()
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
            task = self.scheduler.submit(voice_evt, self.handle_brain_task)
            log("info", "voice_asr_transcript_queued", group_id=evt.group_id, media_id=media_id,
                message_id=evt.message_id, trace_id=voice_evt.trace_id, task_id=task.task_id, text=transcript[:240])
        except Exception as exc:
            log("error", "voice_asr_queue_failed", group_id=evt.group_id, message_id=evt.message_id,
                trace_id=voice_evt.trace_id, error=str(exc))

    def analyze_event_media(self, evt: OneBotEvent) -> None:
        image_items = [x for x in self.store.media_by_event(evt.event_id) if x.get("media_type") == "image"]
        if image_items and not self.cfg.vision_ocr.enabled:
            log("info", "media_ocr_skip_disabled", group_id=evt.group_id, trace_id=evt.trace_id)
        if self.cfg.vision_ocr.enabled:
            for item in image_items:
                media_id = int(item["id"])
                signal = self.media_analysis_event(media_id)
                if not self.store.claim_media_status(
                    media_id, ["indexed", "ocr_queued", "ocr_failed"], "ocr_running"
                ):
                    continue
                signal.clear()
                try:
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
                finally:
                    signal.set()

        record_items = [x for x in self.store.media_by_event(evt.event_id) if x.get("media_type") == "record"]
        if record_items and not self.cfg.asr.enabled:
            log("info", "voice_asr_skip_disabled", group_id=evt.group_id, trace_id=evt.trace_id)
        if self.cfg.asr.enabled:
            for item in record_items:
                media_id = int(item["id"])
                if str(item.get("ocr_text") or "").strip() and str(item.get("status") or "") == "transcribed":
                    continue
                if not self.store.claim_media_status(
                    media_id, ["indexed", "asr_queued", "asr_failed", "waiting_transcript"], "asr_running"
                ):
                    continue
                try:
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

    def select_voice_pack_item(self, query: str, evt: Optional[OneBotEvent] = None) -> Dict[str, Any]:
        raw_query = re.sub(r"^/发语音", "", str(query or "")).strip()
        if raw_query:
            exact_rows = self.store.voice_items(query=raw_query, limit=30)
            exact_rows = [
                row for row in exact_rows
                if str(row.get("title") or row.get("text") or "").strip().lower() == raw_query.lower()
            ]
            if exact_rows:
                exact_rows.sort(key=lambda r: (int(r.get("usage_count") or 0), -int(r.get("id") or 0)))
                return {**exact_rows[0], "match_score": 1200, "match_reason": "语音标题完整命中"}
        if self.generic_voice_pack_request(query):
            for exact in ("你好", "你好你好你好", "Hello", "可以"):
                rows = self.store.voice_items(query=exact, limit=20)
                exact_rows = [r for r in rows if str(r.get("title") or r.get("text") or "").lower() == exact.lower()]
                if exact_rows:
                    exact_rows.sort(key=lambda r: (int(r.get("usage_count") or 0), -int(r.get("id") or 0)))
                    return {**exact_rows[0], "match_score": 1200, "match_reason": "通用语音完整命中"}
                if rows:
                    rows.sort(key=lambda r: (int(r.get("usage_count") or 0), -int(r.get("id") or 0)))
                    return {**rows[0], "match_score": max(75, float(rows[0].get("match_score") or 0)),
                            "match_reason": rows[0].get("match_reason") or "通用语音匹配"}
        candidates = self.voice_pack_candidates(query, limit=8)
        memory = evt.raw.get("_brain_memory") if evt and isinstance(evt.raw, dict) else {}
        vector_candidates = [
            row for row in (memory.get("asset_candidates") or []) if isinstance(memory, dict)
            if row.get("object_type") == "voice_pack"
        ]
        merged: Dict[int, Dict[str, Any]] = {}
        for row in candidates:
            item_id = int(row.get("id") or 0)
            if item_id:
                merged[item_id] = row
        for row in vector_candidates:
            item_id = int(row.get("id") or 0)
            if not item_id:
                continue
            vector_item = {**row, "match_reason": "向量语义匹配"}
            current = merged.get(item_id)
            if not current or self.voice_candidate_confidence(vector_item) > self.voice_candidate_confidence(current):
                merged[item_id] = vector_item
            elif current:
                current["vector_score"] = max(float(current.get("vector_score") or 0), float(row.get("vector_score") or 0))
        ranked = sorted(merged.values(), key=self.voice_candidate_confidence, reverse=True)
        return ranked[0] if ranked else {}

    @staticmethod
    def voice_candidate_confidence(item: Dict[str, Any]) -> float:
        if not item:
            return 0.0
        lexical = float(item.get("match_score") or 0)
        vector = max(0.0, float(item.get("vector_score") or 0))
        if lexical >= 1200:
            return 1.0
        if lexical >= 900:
            return 0.94
        if lexical >= 280:
            return 0.80
        if lexical >= 150:
            return 0.72
        if lexical >= 75:
            return 0.65
        return min(0.9, vector) if vector else 0.0

    def send_voice_pack_tool(self, evt: OneBotEvent, query: str, quiet: bool = False,
                             selected_item: Optional[Dict[str, Any]] = None) -> str:
        if not self.tool_allowed("send_voice_pack"):
            return "__MEDIA_FAILED__" if quiet else "语音包发送工具未启用。"
        requested = (query or evt.text or "").strip()
        q = "你好" if self.generic_voice_pack_request(requested) else (self.clean_voice_pack_query(requested) or requested)
        item = selected_item or self.select_voice_pack_item(q, evt)
        if not item:
            return "__MEDIA_FAILED__" if quiet else f"没有匹配到“{q[:80]}”对应的语音内容，请换一个更接近文件名的说法。"
        task = self._task_for_event(evt)
        if task:
            self.task_registry.update(task, "selecting_voice", medium="voice")
            self.task_registry.update(task, "waiting_media_channel")
        try:
            with self.media_send_semaphore:
                if task:
                    self.task_registry.update(task, "sending", medium="voice")
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
                    if task:
                        self.task_registry.update(task, "waiting_media_channel", error="首次上传失败，正在自修复")
                    with self.media_send_semaphore:
                        self.post_web_admin("/api/onebot/media-repair", {}, timeout=30)
                        time.sleep(1.5)
                        if task:
                            self.task_registry.update(task, "sending", medium="voice", error="")
                        self.post_web_admin("/api/voicepacks/send", {"group_id": evt.group_id, "id": item["id"]}, timeout=90)
                    log("info", "voice_pack_sent_after_media_repair", group_id=evt.group_id, voice_id=item["id"],
                        title=item.get("title"), query=q, trace_id=evt.trace_id)
                    if quiet:
                        return "__NO_TEXT_REPLY__"
                    return f"已发送语音包：{item.get('title') or q}"
                except Exception as retry_exc:
                    log("error", "voice_pack_send_retry_failed", group_id=evt.group_id, voice_id=item.get("id"),
                        error=str(retry_exc), trace_id=evt.trace_id)
                    return "__MEDIA_FAILED__" if quiet else "语音通道自修复后仍未发送成功。"
            return "__MEDIA_FAILED__" if quiet else "语音通道暂时不可用。"

    def select_face_pack_item(self, evt: OneBotEvent, query: str) -> Dict[str, Any]:
        q = re.sub(r"^/发表情", "", str(query or "")).strip() or "搞笑"
        rows = self.store.search_face_assets(
            q, evt.group_id, limit=8, global_shared=self.cfg.media_reply.global_face_assets
        )
        memory = evt.raw.get("_brain_memory") if isinstance(evt.raw, dict) else {}
        candidates: Dict[int, Dict[str, Any]] = {
            int(row.get("id") or 0): row for row in rows if int(row.get("id") or 0)
        }
        for row in (memory.get("asset_candidates") or []) if isinstance(memory, dict) else []:
            if row.get("object_type") != "face_asset":
                continue
            if not bool(row.get("enabled", 1)):
                continue
            score = float(row.get("vector_score") or 0)
            item_id = int(row.get("id") or 0)
            if not item_id:
                continue
            available = getattr(self.store, "face_asset_available", None)
            if available and not available(item_id, evt.group_id, self.cfg.media_reply.global_face_assets):
                continue
            vector_item = {**row, "match_score": score, "match_reason": "向量语义匹配"}
            current = candidates.get(item_id)
            if not current or score > float(current.get("match_score") or current.get("vector_score") or 0):
                candidates[item_id] = vector_item
            elif current:
                current["vector_score"] = max(float(current.get("vector_score") or 0), score)
        ranked = sorted(
            candidates.values(),
            key=lambda row: float(row.get("match_score") or row.get("vector_score") or 0),
            reverse=True,
        )
        return ranked[0] if ranked else {}

    def send_face_pack_tool(self, evt: OneBotEvent, query: str, quiet: bool = False,
                            selected_item: Optional[Dict[str, Any]] = None) -> str:
        if not self.tool_allowed("send_face_pack"):
            return "__MEDIA_FAILED__" if quiet else "表情包发送工具未启用。"
        q = (query or evt.text or "").strip()
        q = re.sub(r"^/发表情", "", q).strip()
        item = selected_item or self.select_face_pack_item(evt, q)
        if not item:
            log("warning", "face_pack_no_match", group_id=evt.group_id, query=q, trace_id=evt.trace_id)
            return "__MEDIA_FAILED__" if quiet else "没有找到语义足够匹配的表情素材。"
        file_value = str(item.get("file") or item.get("url") or "")
        if not file_value:
            return "__MEDIA_FAILED__" if quiet else "选中的表情素材没有本地文件，无法发送。"
        task = self._task_for_event(evt)
        if task:
            self.task_registry.update(task, "selecting_face", medium="face")
            self.task_registry.update(task, "waiting_media_channel")
        try:
            with self.media_send_semaphore:
                if task:
                    self.task_registry.update(task, "sending", medium="face")
                result = self.post_web_admin("/api/faces/send", {
                    "group_id": evt.group_id, "face_id": item["id"], "trace_id": evt.trace_id,
                    "query": q, "reason": item.get("match_reason", ""),
                }, timeout=22)
                if str(result.get("state") or "") not in {"sent", "timeout_confirmed"}:
                    raise RuntimeError(str(result.get("error") or "表情发送未确认"))
            log("info", "face_pack_sent", group_id=evt.group_id, face_id=item["id"],
                media_id=item.get("canonical_media_id"), send_state=result.get("state"),
                summary=str(item.get("image_summary") or "")[:120], trace_id=evt.trace_id)
            if quiet:
                return "__NO_TEXT_REPLY__"
            desc = item.get("image_summary") or item.get("ocr_text") or f"#{item.get('id')}"
            return f"已发送表情素材：{str(desc)[:60]}"
        except Exception as exc:
            log("error", "face_pack_send_failed", group_id=evt.group_id, face_id=item.get("id"),
                error=str(exc), trace_id=evt.trace_id)
            return "__MEDIA_FAILED__" if quiet else f"表情发送失败：{str(exc)[:160]}"

    def command_reply(self, evt: OneBotEvent) -> Optional[str]:
        text = evt.text.strip()
        text = re.sub(r"^@\S+\s*", "", text).strip()
        if not text.startswith("/"):
            return None
        if text == "/状态" and self.tool_allowed("get_status"):
            scheduler = self.scheduler.snapshot()
            return (
                f"状态：AI={'启用' if self.cfg.enabled else '停用'}，dry_run={self.cfg.dry_run}，"
                f"队列={scheduler['queued']}，活动线程={scheduler['active_threads']}，授权群={len(self.cfg.target_groups)}，"
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
        if text.startswith("/生图"):
            return self.send_generated_image_tool(evt, text.removeprefix("/生图").strip(), quiet=True)
        return None

    @staticmethod
    def is_latest_image_query(text: str) -> bool:
        value = re.sub(r"\s+", "", str(text or ""))
        latest = any(word in value for word in ("刚发", "刚才发", "上一张", "那张", "这张", "最新"))
        image = any(word in value for word in ("图", "图片", "照片", "截图", "画面", "表情"))
        question = any(word in value for word in ("什么", "啥", "内容", "识别", "看到", "看看", "说说"))
        return image and (latest or question) and (latest or "这" in value)

    def prepare_latest_image_context(self, evt: OneBotEvent) -> Dict[str, Any]:
        if not self.is_latest_image_query(evt.text):
            return {}
        quoted = evt.raw.get("_quoted_message") if isinstance(evt.raw, dict) else {}
        quoted = quoted if isinstance(quoted, dict) else {}
        is_quoted_image = str(quoted.get("type") or "") == "3"
        if is_quoted_image:
            latest = self.store.referenced_image(
                evt.group_id,
                str(quoted.get("message_id") or ""),
                str(quoted.get("md5") or ""),
            )
            if latest:
                latest["_context_source"] = "quoted_image"
                latest["_quoted_message_id"] = str(quoted.get("message_id") or "")
                log("info", "quoted_image_exact_match", group_id=evt.group_id,
                    quoted_message_id=quoted.get("message_id"), quoted_md5=quoted.get("md5"),
                    media_id=latest.get("id"), image_summary=str(latest.get("image_summary") or "")[:160],
                    trace_id=evt.trace_id)
            else:
                missing = {
                    "id": 0,
                    "group_id": evt.group_id,
                    "media_type": "image",
                    "status": "referenced_image_not_indexed",
                    "image_summary": "",
                    "ocr_text": "",
                    "tags_json": "[]",
                    "keywords_json": "[]",
                    "_context_source": "quoted_image_missing",
                    "_quoted_message_id": str(quoted.get("message_id") or ""),
                }
                evt.raw["_latest_image_context"] = missing
                log("warning", "quoted_image_not_found", group_id=evt.group_id,
                    quoted_message_id=quoted.get("message_id"), quoted_md5=quoted.get("md5"),
                    trace_id=evt.trace_id)
                return missing
        else:
            latest = self.store.latest_image_before(evt.group_id, evt.timestamp, evt.user_id)
        if not latest:
            return {}
        media_id = int(latest["id"])
        status = str(latest.get("status") or "")
        if status in {"indexed", "ocr_queued", "ocr_running", "ocr_failed"} and self.cfg.vision_ocr.enabled:
            # OCR is a background concern. The former implementation performed
            # analysis here or waited up to 25 seconds for another worker,
            # making a simple group reply depend on a slow vision endpoint.
            log("info", "latest_image_nonblocking_context", group_id=evt.group_id,
                media_id=media_id, status=status, trace_id=evt.trace_id)
        evt.raw["_latest_image_context"] = latest
        return latest

    @staticmethod
    def _context_priority(content: str) -> int:
        rules = (
            ("数据库最近消息", 100), ("最近群聊上下文", 98),
            ("当前群长期记忆摘要", 96), ("当前发言人在本群", 94),
            ("永久群文化记忆", 90), ("当前群已解析图片", 82),
            ("用户当前明确引用", 86), ("当前群语音泡记忆", 78),
            ("可用收藏表情", 55), ("可用语音包素材", 55),
        )
        return next((score for marker, score in rules if marker in content), 60)

    def apply_context_budget(self, messages: List[Dict[str, str]]) -> List[Dict[str, str]]:
        """Keep high-value memory while bounding model prefill latency."""
        if len(messages) <= 3:
            return messages
        first = messages[0]
        user = messages[-1]
        middle = messages[1:-1]
        control = [
            item for item in middle
            if "你的最终输出必须是单个 JSON 对象" in str(item.get("content") or "")
        ]
        contexts = [item for item in middle if item not in control]
        ranked = sorted(
            enumerate(contexts),
            key=lambda pair: (-self._context_priority(str(pair[1].get("content") or "")), pair[0]),
        )
        selected: List[tuple[int, Dict[str, str]]] = []
        remaining = self.cfg.memory.context_budget_chars
        for index, item in ranked:
            content = str(item.get("content") or "")
            if remaining <= 0:
                break
            if len(content) > remaining:
                if remaining < 240:
                    continue
                item = {**item, "content": content[:remaining] + "\n[记忆已按实时预算截断]"}
                content = str(item["content"])
            selected.append((index, item))
            remaining -= len(content)
        selected.sort(key=lambda pair: pair[0])
        bounded = [first, *[item for _, item in selected], *control, user]
        total = sum(len(str(item.get("content") or "")) for item in bounded)
        if total > self.cfg.memory.prompt_budget_chars:
            overflow = total - self.cfg.memory.prompt_budget_chars
            for item in bounded[1:-2]:
                content = str(item.get("content") or "")
                removable = max(0, len(content) - 240)
                cut = min(removable, overflow)
                if cut:
                    item["content"] = content[:len(content) - cut] + "\n[上下文已压缩]"
                    overflow -= cut
                if overflow <= 0:
                    break
        return bounded

    def build_messages(self, evt: OneBotEvent) -> List[Dict[str, str]]:
        with self.history_locks[evt.group_id]:
            hist = list(self.histories[evt.group_id])
        group_personality = self.cfg.group_personalities.get(evt.group_id) or {}
        active_personality = (
            str(group_personality.get("prompt") or "").strip()
            if group_personality.get("enabled", False) else ""
        )
        personality_text = active_personality or self.cfg.personality.strip()
        personality_rule = (
            "机器人性格（最高优先级，必须严格遵守；当前群独立配置）：\n" + personality_text
            if personality_text else ""
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
            persona = self.store.effective_persona_context(evt.group_id, evt.user_id, 12) if evt.user_id else {}
            if persona:
                claim_lines = []
                for claim in persona.get("claims") or []:
                    evidence = claim.get("evidence") or []
                    proof = ""
                    if evidence:
                        first = evidence[0]
                        proof = f"（证据 {first.get('time','')} message_id={first.get('message_id') or first.get('event_id','')}：{str(first.get('text') or '')[:120]}）"
                    claim_lines.append(f"- [{claim.get('category')}] {claim.get('value')}{proof}")
                messages.append({
                    "role": "system",
                    "content": (
                        "当前发言人在本群的永久用户画像（严格限定当前群；人工内容优先；只在自然相关时使用，不要向群友解释画像来源）：\n"
                        f"姓名/群昵称：{persona.get('name') or evt.sender_name or '群成员'}\n"
                        f"外号：{'、'.join(str(x) for x in persona.get('aliases') or []) or '无'}\n"
                        f"摘要：{persona.get('summary') or '无'}\n"
                        f"人工及永久事实：{json.dumps(persona.get('facts') or [], ensure_ascii=False)}\n"
                        f"兴趣/标签：{json.dumps(persona.get('tags') or [], ensure_ascii=False)}\n"
                        "有原话证据的画像结论：\n" + ("\n".join(claim_lines) if claim_lines else "无")
                    )[:5000],
                })
            latest_image = self.prepare_latest_image_context(evt)
            image_words = ("图", "图片", "照片", "截图", "表情", "画面", "看见", "刚才", "之前", "那张", "这张", "哈士奇", "狗", "猫", "识别", "内容")
            need_images = any(w in evt.text for w in image_words)
            if latest_image:
                image_rows = [latest_image]
            else:
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
                    quoted_exact = str(latest_image.get("_context_source") or "") == "quoted_image" if latest_image else False
                    heading = (
                        "用户当前明确引用的历史图片（这是唯一可信的目标图，禁止改用最近图片或其他语义相似图片）：\n"
                        if quoted_exact else
                        "当前群已解析图片记忆（回答图片相关问题时优先参考，可提到图片编号）：\n"
                    )
                    messages.append({"role": "system", "content": heading + "\n".join(lines)})
                elif latest_image:
                    target_label = "明确引用的历史图片" if str(latest_image.get("_context_source") or "").startswith("quoted_image") else "刚刚发送的最新图片"
                    messages.append({
                        "role": "system",
                        "content": (
                            f"用户问的是{target_label} #{latest_image.get('id')}，"
                            f"当前状态为 {latest_image.get('status')}。禁止把更早的其他图片当成这张图来回答；"
                            "若仍无解析结果，只能明确说这张被引用图片尚未入库或仍在解析。"
                        ),
                    })
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
            brain_memory = evt.raw.get("_brain_memory") if isinstance(evt.raw, dict) else None
            vector_assets = (brain_memory.get("asset_candidates") or []) if isinstance(brain_memory, dict) else []
            voice_pack_rows = self.voice_pack_candidates(evt.text, limit=8)
            known_voice_ids = {int(row.get("id") or 0) for row in voice_pack_rows}
            for asset in vector_assets:
                if asset.get("object_type") == "voice_pack" and int(asset.get("id") or 0) not in known_voice_ids:
                    voice_pack_rows.append(asset)
                    known_voice_ids.add(int(asset.get("id") or 0))
            if voice_pack_rows:
                lines = []
                for v in voice_pack_rows[:8]:
                    lines.append(f"- voice#{v.get('id')} [{v.get('category')}/{v.get('pack_name')}]: 内容=\"{v.get('title') or v.get('text')}\"")
                messages.append({"role": "system", "content": "可用语音包素材：标题是语音真实说出的内容。根据完整语境评估语音是否比文字更自然，不要输出任何斜杠命令。\n" + "\n".join(lines[:8])})
            face_rows = self.store.search_face_assets(
                evt.text, evt.group_id, limit=8, global_shared=self.cfg.media_reply.global_face_assets
            )
            known_face_ids = {int(row.get("id") or 0) for row in face_rows}
            for asset in vector_assets:
                if asset.get("object_type") == "face_asset" and int(asset.get("id") or 0) not in known_face_ids:
                    face_rows.append(asset)
                    known_face_ids.add(int(asset.get("id") or 0))
            if face_rows:
                lines = []
                for face in face_rows[:8]:
                    desc = str(face.get("searchable_text") or face.get("image_summary") or face.get("ocr_text") or "")[:180]
                    lines.append(f"- face#{face.get('id')}: {desc}")
                messages.append({"role": "system", "content": "可用收藏表情：根据完整对话判断表情是否能自然接话。只能根据下列 OCR、摘要、标签和关键词理解素材，不要输出任何斜杠命令。\n" + "\n".join(lines)})
            if isinstance(brain_memory, dict):
                remembered = []
                for item in (brain_memory.get("items") or [])[:12]:
                    text = str(item.get("text") or item.get("raw_message") or "").replace("\n", " ")[:360]
                    if text:
                        remembered.append(f"- [{item.get('object_type') or item.get('source')}] {text}")
                culture = brain_memory.get("culture") or {}
                if remembered or any(culture.get(key) for key in ("aliases", "memes", "relations")):
                    messages.append({
                        "role": "system",
                        "content": (
                            "永久群文化记忆：只能在与当前语境相关时自然使用；允许大胆嘴贫、翻旧梗和使用外号，"
                            "不要解释记忆来源，不要编造未提供的历史。\n" + "\n".join(remembered) +
                            "\n人物外号/关系/群梗：" + json.dumps(culture, ensure_ascii=False)[:3500]
                        ),
                    })
        except Exception as exc:
            log("warning", "memory_context_error", group_id=evt.group_id, trace_id=evt.trace_id, error=str(exc))
        sender = evt.sender_name or evt.user_id or "群成员"
        media_settings = self.cfg.media_reply.for_group(evt.group_id)
        voice_threshold = int(float(media_settings.get("voice_min_fit", 55)))
        face_threshold = int(float(media_settings.get("face_min_fit", 45)))
        messages.append({
            "role": "system",
            "content": (
                "你的最终输出必须是单个 JSON 对象，不要 Markdown，不要斜杠命令。字段为 "
                '{"text":"适合直接发到群里的文字回复","medium":"text|voice|face",'
                '"voice_fit":0,"face_fit":0,"media_query":"用于匹配素材的简短语义描述",'
                '"intent":"当前情绪或接话意图","reason":"一句话理由"}。'
                "voice_fit 和 face_fit 均为 0-100，评估的是对当前群聊语境的自然程度，"
                "不要因为文字也能回复就压低媒介分。搞笑、惊讶、夸赞、安慰、短确认、熟人调侃等"
                "反应型场景可给 60-90；需要长步骤、代码或精确事实的回答应较低。"
                f"当前后台门槛为语音 {voice_threshold}、表情 {face_threshold}。"
                "medium 必须表示你真正首选的最终媒介；若选 voice 或 face，请让对应 fit 与这个选择一致。"
                "text 始终提供可用的文字备选，系统会在后台继续检查素材置信度和概率。"
            ),
        })
        user_content = f"群：{evt.group_name}({evt.group_id})\n发言人：{sender}\n最新消息：{evt.text}"
        messages.append({"role": "user", "content": user_content})
        return self.apply_context_budget(messages)

    def _task_for_event(self, evt: OneBotEvent) -> Optional[ReplyTask]:
        task_id = str(evt.raw.get("_brain_task_id") or "") if isinstance(evt.raw, dict) else ""
        with self.task_registry.lock:
            return self.task_registry.tasks.get(task_id)

    def _record_history(self, evt: OneBotEvent, ai_text: str) -> None:
        with self.history_locks[evt.group_id]:
            hist = self.histories[evt.group_id]
            hist.append((evt.sender_name or evt.user_id or "群成员", evt.text))
            hist.append(("AI", ai_text))
        self.save_memory()

    @staticmethod
    def reply_needs_hermes(text: str) -> bool:
        """Detect a model capability failure before that failure is sent to the group."""
        value = re.sub(r"\s+", "", str(text or "")).lower()
        if not value:
            return False
        direct_markers = (
            "拿不到实时数据", "没有实时数据", "无法获取实时", "不能获取实时",
            "无法联网", "不能联网", "无法浏览网页", "不能浏览网页",
            "无法访问互联网", "不能访问互联网", "无法搜索网络", "不能搜索网络",
            "无法确认最新", "无法得知最新", "做不了定时", "不能定时",
            "无法定时", "做不了提醒", "不能提醒", "无法提醒",
        )
        if any(marker in value for marker in direct_markers):
            return True
        limitation = any(marker in value for marker in ("无法", "不能", "没法", "不具备"))
        capability = any(marker in value for marker in (
            "获取", "查询", "搜索", "访问", "联网", "浏览", "打开", "读取",
            "执行", "确认", "实时", "最新", "外部数据", "工具",
            "定时", "提醒", "闹钟",
        ))
        return limitation and capability

    def handle_event(self, evt: OneBotEvent) -> None:
        log("info", "reply_job_start", group_id=evt.group_id, group_name=evt.group_name,
            message_id=evt.message_id, text=evt.text[:300], trace_id=evt.trace_id)
        if self.group_is_muted(evt.group_id, evt.trace_id, "handle_event"):
            task = self._task_for_event(evt)
            if task:
                self.task_registry.update(task, "skipped", result="group_muted", details={**task.details, "mute_gate": True})
            return
        command = self.command_reply(evt)
        if command == "__NO_TEXT_REPLY__":
            self._record_history(evt, "[已发送媒体，无文字回复]")
            return
        if command == "__MEDIA_FAILED__":
            task = self._task_for_event(evt)
            if task:
                self.task_registry.update(task, "failed", error="显式媒介命令未发送成功")
            self._record_history(evt, "[媒介命令发送失败，未发送文字]")
            return
        is_asr_transcript = bool(isinstance(evt.raw, dict) and evt.raw.get("asr_voice_transcript"))
        if not is_asr_transcript and command is None:
            image_prompt = self.extract_image_generation_prompt(evt.text)
            if image_prompt:
                result = self.send_generated_image_tool(evt, image_prompt, quiet=True)
                if result == "__NO_TEXT_REPLY__":
                    self._record_history(evt, f"[已生成并发送图片：{image_prompt[:120]}]")
                    return
                task = self._task_for_event(evt)
                if task:
                    self.task_registry.update(task, "failed", error="生图或图片发送失败")
                self._record_history(evt, "[生图失败，未发送图片]")
                return
        explicit_media_kind = extract_explicit_media_kind(evt.text)
        if not is_asr_transcript and command is None and explicit_media_kind == "voice":
            result = self.send_voice_pack_tool(evt, evt.text, quiet=True)
            if result == "__NO_TEXT_REPLY__":
                self._record_history(evt, "[已按语音名称匹配并发送语音包]")
                return
            if result == "__MEDIA_FAILED__":
                task = self._task_for_event(evt)
                if task:
                    self.task_registry.update(task, "failed", error="显式语音请求未发送成功")
                self._record_history(evt, "[显式语音请求失败，未发送文字]")
                return
        if not is_asr_transcript and command is None and explicit_media_kind == "face":
            result = self.send_face_pack_tool(evt, evt.text, quiet=True)
            if result == "__NO_TEXT_REPLY__":
                self._record_history(evt, "[已按语义匹配并发送表情]")
                return
            task = self._task_for_event(evt)
            if task:
                self.task_registry.update(task, "failed", error="显式表情请求未发送成功")
            self._record_history(evt, "[显式表情请求失败，未发送文字]")
            return

        raw_reply = command if command is not None else self.generate_reply(evt)
        if not raw_reply:
            task = self._task_for_event(evt)
            if task:
                self.task_registry.update(task, "failed", error="模型没有生成回复")
            return
        if self.group_is_muted(evt.group_id, evt.trace_id, "after_generation"):
            task = self._task_for_event(evt)
            if task:
                self.task_registry.update(task, "skipped", result="group_muted", details={**task.details, "mute_gate": True})
            return
        decision = ReplyDecision(text=str(raw_reply), medium="text") if command is not None else self.parse_reply_decision(raw_reply)
        task = self._task_for_event(evt)
        if command is None and self.reply_needs_hermes(decision.text):
            route = {
                "automation_required": True,
                "automation_intent": evt.text[:500],
                "risk_level": "read",
                "hermes_mode": "answer",
                "reason": "model_capability_fallback",
            }
            result = self.hermes.submit(evt, route)
            message = str(result.get("message") or "工具调用未能接收。")
            if not self.cfg.dry_run:
                self.send_group_msg(evt.group_id, message, evt.trace_id, evt)
            self._record_history(evt, message)
            if task:
                self.task_registry.update(
                    task, "completed" if result.get("accepted") else "failed",
                    result="hermes_capability_queued" if result.get("accepted") else "hermes_capability_rejected",
                    details={**(task.details or {}), "hermes_capability": result},
                )
            return
        legacy_marker = decision.reason == "legacy_marker"
        selected_medium = "text"
        selected_item: Dict[str, Any] = {}
        media_details: Dict[str, Any] = {}
        if command is None and not is_asr_transcript:
            if legacy_marker:
                selected_medium = decision.medium
                selected_item = (
                    self.select_voice_pack_item(decision.media_query, evt) if selected_medium == "voice"
                    else self.select_face_pack_item(evt, decision.media_query)
                )
                media_details = {"legacy_marker_intercepted": True, "selected_medium": selected_medium,
                                 "selected_asset_id": selected_item.get("id")}
            else:
                selected_medium, selected_item, media_details = self.choose_auto_medium(evt, decision)
            if task:
                details = {**(task.details or {}), "media_decision": media_details}
                self.task_registry.update(task, details=details)
            if selected_medium in {"voice", "face"} and selected_item:
                if self.cfg.dry_run:
                    log("info", "dry_run_media_reply", group_id=evt.group_id, medium=selected_medium,
                        asset_id=selected_item.get("id"), trace_id=evt.trace_id)
                    self._record_history(evt, f"[演练选择{selected_medium}#{selected_item.get('id')}]")
                    if task:
                        self.task_registry.update(task, "completed", medium=selected_medium, result="dry_run")
                    return
                result = (
                    self.send_voice_pack_tool(evt, decision.media_query, quiet=True, selected_item=selected_item)
                    if selected_medium == "voice"
                    else self.send_face_pack_tool(evt, decision.media_query, quiet=True, selected_item=selected_item)
                )
                if result == "__NO_TEXT_REPLY__":
                    self._record_history(evt, f"[自动选择{selected_medium}回复，无文字回复]")
                    if task:
                        self.task_registry.update(task, "completed", medium=selected_medium,
                                                  result=f"asset:{selected_item.get('id')}")
                    return
                if legacy_marker and not decision.text:
                    if task:
                        self.task_registry.update(task, "failed", error="模型媒介 marker 已拦截，但媒介发送失败")
                    return

        reply = self.clean_reply(decision.text)
        if not reply:
            if task:
                self.task_registry.update(task, "failed", error="没有可用的文字备选")
            return
        if self.cfg.reply_prefix and not reply.startswith(self.cfg.reply_prefix):
            reply_to_send = self.cfg.reply_prefix + reply
        else:
            reply_to_send = reply
        if self.cfg.dry_run:
            log("info", "dry_run_reply", group_id=evt.group_id, reply=reply_to_send, trace_id=evt.trace_id)
        else:
            if self.cfg.send_delay_seconds > 0:
                time.sleep(self.cfg.send_delay_seconds)
            if task:
                self.task_registry.update(task, "sending", medium="text")
            self.send_group_msg(evt.group_id, reply_to_send, evt.trace_id, evt)
        self._record_history(evt, reply_to_send)
        if task:
            self.task_registry.update(task, "completed", medium=task.medium or "text", result=reply_to_send[:500])

    def get_api_key(self, channel: AIChannel) -> str:
        key = os.getenv(channel.api_key_env, "")
        if channel.id == self.cfg.ai.active_channel_id:
            return key or os.getenv("AI_REPLY_API_KEY", "")
        return key

    @staticmethod
    def extract_image_generation_prompt(text: str) -> str:
        return extract_image_generation_prompt(text)

    def image_generation_api_key(self) -> str:
        cfg = self.cfg.image_generation
        return os.getenv(cfg.api_key_env, "") or os.getenv("AI_REPLY_API_KEY", "")

    def generate_image(self, prompt: str) -> Dict[str, Any]:
        cfg = self.cfg.image_generation
        prompt = str(prompt or "").strip()
        if not cfg.enabled:
            raise RuntimeError("生图功能未启用")
        if not prompt:
            raise ValueError("生图描述不能为空")
        if not cfg.base_url or not cfg.model:
            raise RuntimeError("生图渠道配置不完整")
        key = self.image_generation_api_key()
        if not key and not cfg.base_url.startswith(("http://127.0.0.1", "http://localhost")):
            raise RuntimeError("生图 API Key 未配置")
        payload: Dict[str, Any] = {
            "model": cfg.model, "prompt": prompt, "n": 1, "size": cfg.size,
        }
        if cfg.quality:
            payload["quality"] = cfg.quality
        if cfg.response_format in {"url", "b64_json"}:
            payload["response_format"] = cfg.response_format
        started = time.monotonic()
        raw_body = ""
        request_attempts = 0
        for attempt in range(2):
            request_attempts = attempt + 1
            req = urllib.request.Request(
                cfg.base_url.rstrip("/") + "/images/generations",
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), method="POST",
                headers={"Content-Type": "application/json"},
            )
            if key:
                req.add_header("Authorization", "Bearer " + key)
            try:
                with urllib.request.urlopen(req, timeout=cfg.timeout_seconds) as resp:
                    raw_body = resp.read().decode("utf-8", "replace")
                break
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace") if exc.fp else ""
                cooling = exc.code == 429 and any(
                    marker in detail for marker in ("upstream_cooling", "upstream_model_cooling")
                )
                if cooling and attempt == 0:
                    retry_raw = str(exc.headers.get("Retry-After") or "1").strip()
                    try:
                        retry_after = float(retry_raw)
                    except ValueError:
                        retry_after = 1.0
                    retry_after = max(1.0, min(60.0, retry_after))
                    log("warning", "image_generation_cooling_retry", model=cfg.model,
                        retry_after_seconds=retry_after, attempt=request_attempts)
                    time.sleep(retry_after)
                    continue
                raise RuntimeError(f"生图 HTTP {exc.code}: {detail[:800]}") from exc
        parsed = json.loads(raw_body)
        items = parsed.get("data") if isinstance(parsed, dict) else None
        item = items[0] if isinstance(items, list) and items and isinstance(items[0], dict) else {}
        image_bytes = b""
        source = ""
        if item.get("b64_json"):
            image_bytes = base64.b64decode(str(item["b64_json"]), validate=True)
            source = "b64_json"
        elif item.get("url"):
            source = str(item["url"])
            with urllib.request.urlopen(urllib.request.Request(source, headers={"User-Agent": "WeChatAgent/0.0.4"}), timeout=min(60, cfg.timeout_seconds)) as resp:
                image_bytes = resp.read(25 * 1024 * 1024 + 1)
        if not image_bytes or len(image_bytes) > 25 * 1024 * 1024:
            raise RuntimeError("生图响应未包含可用图片，或图片超过 25MB")
        output_dir = DEFAULT_HOME / "generated_images"
        output_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(image_bytes).hexdigest()
        suffix = ".jpg"
        mime_type = "image/jpeg"
        if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            suffix, mime_type = ".png", "image/png"
        elif image_bytes.startswith((b"GIF87a", b"GIF89a")):
            suffix, mime_type = ".gif", "image/gif"
        elif image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
            suffix, mime_type = ".webp", "image/webp"
        elif not image_bytes.startswith(b"\xff\xd8\xff"):
            raise RuntimeError("生图响应不是受支持的 JPEG、PNG、GIF 或 WebP 图片")
        path = output_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{digest[:12]}{suffix}"
        path.write_bytes(image_bytes)
        return {
            "file": str(path), "size_bytes": len(image_bytes), "sha256": digest,
            "mime_type": mime_type,
            "model": cfg.model, "prompt": prompt, "revised_prompt": str(item.get("revised_prompt") or ""),
            "source": source, "latency_ms": round((time.monotonic() - started) * 1000),
            "request_attempts": request_attempts,
        }

    def send_generated_image_tool(self, evt: OneBotEvent, prompt: str, quiet: bool = False) -> str:
        task = self._task_for_event(evt)
        try:
            if task:
                self.task_registry.update(task, "generating_image", medium="image")
            with self.model_semaphore:
                generated = self.generate_image(prompt)
            if task:
                self.task_registry.update(task, "waiting_media_channel", medium="image", model=generated["model"])
            with self.media_send_semaphore:
                if task:
                    self.task_registry.update(task, "sending", medium="image")
                sent = self.post_web_admin("/api/messages/send", {
                    "group_id": evt.group_id, "type": "image", "file": generated["file"],
                    "_send_timeout": 120, "trace_id": evt.trace_id,
                }, timeout=130)
            log("info", "generated_image_sent", group_id=evt.group_id, model=generated["model"],
                file=generated["file"], latency_ms=generated["latency_ms"],
                send_latency_ms=sent.get("latency_ms"), trace_id=evt.trace_id)
            if task:
                details = {**(task.details or {}), "image_generation": generated}
                self.task_registry.update(task, "completed", medium="image", model=generated["model"],
                                          result=generated["file"], details=details)
            return "__NO_TEXT_REPLY__" if quiet else f"已生成图片：{generated['file']}"
        except Exception as exc:
            log("error", "generated_image_failed", group_id=evt.group_id, prompt=prompt[:200],
                error=str(exc), trace_id=evt.trace_id)
            if task:
                self.task_registry.update(task, "failed", medium="image", error=str(exc)[:500])
            return "__MEDIA_FAILED__" if quiet else "生图失败，请在后台检查生图渠道。"

    def channel_order(self) -> List[AIChannel]:
        enabled = [x for x in self.cfg.ai.channels if x.enabled]
        enabled.sort(key=lambda x: (x.id != self.cfg.ai.active_channel_id, x.priority, x.name))
        if not self.cfg.ai.auto_failover:
            return enabled[:1]
        now = time.time()
        with self.channel_state_lock:
            available = [x for x in enabled if self.channel_unavailable_until.get(x.id, 0) <= now]
        return available or enabled

    def request_messages(self, messages: List[Dict[str, str]], max_tokens: int,
                         temperature: float, evt: OneBotEvent) -> Tuple[str, str]:
        failures: List[str] = []
        for channel in self.channel_order():
            with self.model_semaphore:
                reply, error = self.request_channel_messages(channel, messages, max_tokens, temperature, evt)
            if reply:
                with self.channel_state_lock:
                    self.channel_unavailable_until.pop(channel.id, None)
                return reply, channel.model
            failures.append(f"{channel.id}:{error}")
            with self.channel_state_lock:
                self.channel_unavailable_until[channel.id] = time.time() + self.cfg.ai.failure_cooldown_seconds
        log("error", "all_channels_failed", group_id=evt.group_id, channels=failures, trace_id=evt.trace_id)
        return "", ""

    def extract_persona_claims(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        messages = raw.get("messages") if isinstance(raw.get("messages"), list) else []
        if not messages:
            return {"claims": [], "model": "", "reason": "empty_batch"}
        # Reserve two slots momentarily, then release one. This job therefore
        # never consumes the final slot needed by realtime group replies.
        if not self.model_semaphore.acquire(blocking=False):
            raise RuntimeError("realtime_model_capacity_busy")
        if not self.model_semaphore.acquire(blocking=False):
            self.model_semaphore.release()
            raise RuntimeError("last_model_slot_reserved_for_replies")
        self.model_semaphore.release()
        evt = OneBotEvent(
            event_id=f"persona-{raw.get('job_id')}", group_id=str(raw.get("group_id") or ""), group_name="画像分析",
            self_id="", user_id=str(raw.get("user_id") or ""), sender_name="", text="", raw_message="",
            message_id="", timestamp=int(time.time()), raw={}, trace_id=f"persona-{raw.get('job_id')}",
        )
        compact = [{"event_id": str(x.get("event_id") or ""), "message_id": str(x.get("message_id") or ""),
                    "time": str(x.get("time") or ""), "text": str(x.get("text") or "")[:500]}
                   for x in messages[:100] if isinstance(x, dict) and str(x.get("text") or "").strip()]
        prompt = (
            "分析同一微信群成员的这一批历史发言，提取可长期使用的用户画像结论。只输出 JSON 对象，格式为 "
            '{"claims":[{"category":"fact|interest|habit|style|role|topic|quote","value":"简洁结论",'
            '"confidence":0.0,"evidence_ids":["必须来自输入的 event_id 或 message_id"]}]}。'
            "禁止性格雷达、禁止无证据推断、禁止把单次随口发言夸大为永久事实；每条结论至少引用一个输入 ID。\n消息：\n" +
            json.dumps(compact, ensure_ascii=False)
        )
        failures = []
        try:
            for channel in self.channel_order():
                reply, error = self.request_channel_messages(channel, [{"role": "user", "content": prompt}], 1600, 0, evt)
                if not reply:
                    failures.append(f"{channel.id}:{error}")
                    continue
                clean = reply.strip()
                if clean.startswith("```"):
                    clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", clean, flags=re.I | re.S)
                try:
                    parsed = json.loads(clean)
                    claims = parsed.get("claims") if isinstance(parsed, dict) and isinstance(parsed.get("claims"), list) else []
                    return {"claims": claims, "model": channel.model, "reason": "ok"}
                except ValueError as exc:
                    failures.append(f"{channel.id}:invalid_json:{exc}")
            raise RuntimeError("; ".join(failures)[:1000] or "persona_model_unavailable")
        finally:
            self.model_semaphore.release()

    def generate_reply(self, evt: OneBotEvent) -> str:
        started = time.monotonic()
        reply, model = self.request_messages(
            self.build_messages(evt), self.cfg.ai.max_tokens, self.cfg.ai.temperature, evt
        )
        generation_ms = round((time.monotonic() - started) * 1000, 1)
        task = self._task_for_event(evt)
        if task:
            timings = dict((task.details or {}).get("timings_ms") or {})
            timings["generation"] = generation_ms
            timings["estimated_total"] = round(float(timings.get("pre_generation_total") or 0) + generation_ms, 1)
            details = {**(task.details or {}), "timings_ms": timings}
            self.task_registry.update(task, model=model or task.model, details=details)
        if reply:
            log("info", "channel_success", group_id=evt.group_id, model=model, trace_id=evt.trace_id)
        return reply

    def request_channel(self, channel: AIChannel, evt: OneBotEvent) -> Tuple[str, str]:
        return self.request_channel_messages(
            channel, self.build_messages(evt), self.cfg.ai.max_tokens, self.cfg.ai.temperature, evt
        )

    def request_channel_messages(self, channel: AIChannel, messages: List[Dict[str, str]],
                                 max_tokens: int, temperature: float,
                                 evt: OneBotEvent) -> Tuple[str, str]:
        if channel.provider != "openai_compatible":
            return "", f"unsupported provider: {channel.provider}"
        api_key = self.get_api_key(channel)
        if not api_key and not channel.base_url.startswith(("http://127.0.0.1", "http://localhost")):
            return "", f"missing API key: {channel.api_key_env}"
        url = channel.base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": channel.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", "Bearer " + api_key)
        # Some OpenAI-compatible relays reject urllib's default fingerprint.
        req.add_header("User-Agent", "openai-python/1.99.0")
        req.add_header("Accept", "application/json")
        request_started = time.monotonic()
        first_byte_ms = 0.0
        try:
            with urllib.request.urlopen(req, timeout=channel.timeout_seconds) as resp:
                first_byte_ms = (time.monotonic() - request_started) * 1000
                body = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace") if e.fp else ""
            return "", f"HTTP {e.code}: {body[:500]}"
        except Exception as e:
            return "", str(e)
        try:
            obj = json.loads(body)
            reply = obj["choices"][0]["message"]["content"]
            total_ms = (time.monotonic() - request_started) * 1000
            log("info", "ai_reply_generated", group_id=evt.group_id, chars=len(reply),
                model=channel.model, channel_id=channel.id, trace_id=evt.trace_id,
                first_byte_ms=round(first_byte_ms, 1), total_ms=round(total_ms, 1))
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

    def extract_face_marker(self, reply: str) -> Optional[str]:
        """Intercept legacy face commands before they can reach text sending."""
        value = str(reply or "").strip()
        patterns = (
            r"^(?:@\S+\s*)?/发表情\s*(.*)$",
            r"^\[表情(?:包)?\]\s*(.*)$",
            r"^(?:表情包|表情回复)\s*[:：]\s*(.*)$",
        )
        for pattern in patterns:
            match = re.match(pattern, value, flags=re.S | re.I)
            if match:
                query = match.group(1).strip().strip("\"'“”‘’ ")
                query = re.sub(r"[.。！？!！]+$", "", query).strip()
                log("debug", "face_marker_detected", marker=value[:30], query=query[:160])
                return query
        return None

    def parse_reply_decision(self, raw_reply: str) -> ReplyDecision:
        raw = str(raw_reply or "").strip()
        voice_query = self.extract_voice_marker(raw)
        if voice_query is not None:
            return ReplyDecision(medium="voice", voice_fit=100, media_query=voice_query,
                                 intent="legacy_marker", reason="legacy_marker")
        face_query = self.extract_face_marker(raw)
        if face_query is not None:
            return ReplyDecision(medium="face", face_fit=100, media_query=face_query,
                                 intent="legacy_marker", reason="legacy_marker")
        cleaned = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.I).strip()
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start >= 0 and end > start:
            try:
                obj = json.loads(cleaned[start:end + 1])
                medium = str(obj.get("medium") or "text").lower()
                if medium not in {"text", "voice", "face"}:
                    medium = "text"
                return ReplyDecision(
                    text=str(obj.get("text") or "").strip(), medium=medium,
                    voice_fit=max(0.0, min(100.0, float(obj.get("voice_fit") or 0))),
                    face_fit=max(0.0, min(100.0, float(obj.get("face_fit") or 0))),
                    media_query=str(obj.get("media_query") or "").strip(),
                    intent=str(obj.get("intent") or "").strip(), reason=str(obj.get("reason") or "").strip(),
                )
            except (ValueError, TypeError, json.JSONDecodeError):
                pass
        return ReplyDecision(text=raw, medium="text")

    @staticmethod
    def _probability_pass(evt: OneBotEvent, medium: str, probability: float) -> bool:
        probability = max(0.0, min(1.0, float(probability)))
        if probability <= 0:
            return False
        if probability >= 1:
            return True
        seed = f"{evt.trace_id}|{evt.message_id}|{medium}".encode("utf-8", "ignore")
        value = int.from_bytes(hashlib.sha256(seed).digest()[:8], "big") / float(2 ** 64 - 1)
        return value < probability

    @staticmethod
    def _probability_value(evt: OneBotEvent, medium: str) -> float:
        seed = f"{evt.trace_id}|{evt.message_id}|{medium}".encode("utf-8", "ignore")
        return int.from_bytes(hashlib.sha256(seed).digest()[:8], "big") / float(2 ** 64 - 1)

    def choose_auto_medium(self, evt: OneBotEvent, decision: ReplyDecision) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
        settings = self.cfg.media_reply.for_group(evt.group_id)
        task = self._task_for_event(evt)
        if task:
            self.task_registry.update(task, "media_deciding")
        requested_medium = str(getattr(decision, "medium", "text") or "text").lower()
        if requested_medium not in {"text", "voice", "face"}:
            requested_medium = "text"
        details: Dict[str, Any] = {
            "voice_fit": decision.voice_fit, "face_fit": decision.face_fit,
            "media_query": decision.media_query, "intent": decision.intent,
            "requested_medium": requested_medium,
            "automatic_enabled": bool(settings.get("automatic_enabled", True)),
        }
        if not settings.get("automatic_enabled", True):
            return "text", {}, details
        query = decision.media_query or decision.intent or evt.text
        voice_min_fit = float(settings.get("voice_min_fit", 55))
        face_min_fit = float(settings.get("face_min_fit", 45))
        min_confidence = float(settings.get("min_candidate_confidence", 0.65))
        voice_probability = float(settings.get("voice_probability", 0.15))
        face_probability = float(settings.get("face_probability", 0.20))
        # `medium` is the model's explicit final-medium preference.  Previously
        # it was parsed and then discarded, so even a deliberate `face` answer
        # could be blocked by a slightly under-calibrated fit score.  Preserve
        # the raw score for diagnostics and lift only the selected medium to its
        # configured gate; candidate confidence and probability still apply.
        voice_effective_fit = max(float(decision.voice_fit), voice_min_fit) if requested_medium == "voice" else float(decision.voice_fit)
        face_effective_fit = max(float(decision.face_fit), face_min_fit) if requested_medium == "face" else float(decision.face_fit)
        details.update({
            "voice_min_fit": voice_min_fit, "face_min_fit": face_min_fit,
            "voice_effective_fit": voice_effective_fit, "face_effective_fit": face_effective_fit,
            "min_candidate_confidence": min_confidence,
            "voice_probability": voice_probability, "face_probability": face_probability,
        })
        suppressed = media_suppression(evt.text)
        details["user_suppressed_media"] = sorted(suppressed)
        # The model-selected medium is the final answer type, not a loose hint.
        # Do not let high UI probabilities replace a useful text answer with
        # unrelated voice/face material.
        if requested_medium == "text":
            details.update({
                "voice_gate": "model_selected_text",
                "face_gate": "model_selected_text",
                "selected_medium": "text",
            })
            return "text", {}, details
        if requested_medium in suppressed:
            details.update({
                f"{requested_medium}_gate": "user_suppressed",
                "selected_medium": "text",
            })
            return "text", {}, details
        options: List[Tuple[float, str, Dict[str, Any], float]] = []
        if voice_effective_fit >= voice_min_fit:
            voice = self.select_voice_pack_item(query, evt)
            voice_confidence = self.voice_candidate_confidence(voice)
            details["voice_candidate"] = {"id": voice.get("id"), "confidence": voice_confidence,
                                            "title": voice.get("title"), "reason": voice.get("match_reason", "")}
            voice_draw = self._probability_value(evt, "voice")
            details["voice_sample"] = {"draw": round(voice_draw, 4), "probability": voice_probability,
                                         "passed": voice_draw < voice_probability}
            if voice_confidence < min_confidence:
                details["voice_gate"] = "candidate_below_threshold"
            elif voice_draw >= voice_probability:
                details["voice_gate"] = "probability_miss"
            else:
                options.append((voice_effective_fit / 100.0 * voice_confidence, "voice", voice, voice_confidence))
        else:
            details["voice_gate"] = "fit_below_threshold"
        if face_effective_fit >= face_min_fit:
            face = self.select_face_pack_item(evt, query)
            face_confidence = float(face.get("match_score") or face.get("vector_score") or 0)
            details["face_candidate"] = {"id": face.get("id"), "confidence": face_confidence,
                                           "summary": face.get("image_summary"), "reason": face.get("match_reason", "")}
            face_draw = self._probability_value(evt, "face")
            details["face_sample"] = {"draw": round(face_draw, 4), "probability": face_probability,
                                        "passed": face_draw < face_probability}
            if face_confidence < min_confidence:
                details["face_gate"] = "candidate_below_threshold"
            elif face_draw >= face_probability:
                details["face_gate"] = "probability_miss"
            else:
                options.append((face_effective_fit / 100.0 * face_confidence, "face", face, face_confidence))
        else:
            details["face_gate"] = "fit_below_threshold"
        if not options:
            details["selected_medium"] = "text"
            return "text", {}, details
        options.sort(key=lambda item: item[0], reverse=True)
        score, medium, item, confidence = options[0]
        details.update({"selected_medium": medium, "selected_asset_id": item.get("id"),
                        "selected_utility": round(score, 4), "selected_confidence": confidence})
        return medium, item, details

    def clean_reply(self, reply: str) -> str:
        r = reply.strip()
        # Remove surrounding quotes occasionally produced by models.
        if len(r) >= 2 and ((r[0] == r[-1] == '"') or (r[0] == r[-1] == "'")):
            r = r[1:-1].strip()
        if len(r) > self.cfg.max_reply_chars:
            r = r[: self.cfg.max_reply_chars].rstrip() + "…"
        return r

    def send_group_msg(self, group_id: str, text: str, trace_id: str = "",
                       evt: Optional[OneBotEvent] = None) -> None:
        if self.group_is_muted(group_id, trace_id, "text_send"):
            raise RuntimeError("当前群处于“闭嘴”静默期")
        with self.send_locks[group_id]:
            self._send_group_msg_locked(group_id, text, trace_id, evt)

    def send_admin_card(self, evt: OneBotEvent, text: str) -> None:
        """Send management receipts as quote-bound text, bypassing ordinary mute/media gates."""
        try:
            with self.send_locks[evt.group_id]:
                self._send_group_msg_locked(evt.group_id, text, evt.trace_id, evt, quote_message=True)
            log("info", "admin_command_receipt_sent", group_id=evt.group_id, user_id=evt.user_id,
                message_id=evt.message_id, trace_id=evt.trace_id)
        except Exception as exc:
            log("error", "admin_command_receipt_failed", group_id=evt.group_id, user_id=evt.user_id,
                message_id=evt.message_id, error=str(exc), trace_id=evt.trace_id)

    def _send_group_msg_locked(self, group_id: str, text: str, trace_id: str,
                               evt: Optional[OneBotEvent], quote_message: bool = False) -> None:
        url = self.cfg.onebot_api.rstrip("/") + "/send_group_msg"
        message: List[Dict[str, Any]] = []
        if quote_message and evt and evt.message_id:
            message.append({"type": "reply", "data": {"id": evt.message_id}})
        mention_enabled = self.cfg.brain.for_group(group_id).mention_user_on_reply
        if (mention_enabled and not quote_message and evt and evt.user_id and evt.user_id != evt.self_id
                and not evt.user_id.endswith("@chatroom")):
            display_name = self.store.resolve_member_name(evt.group_id, evt.user_id, evt.sender_name)
            if is_readable_member_name(display_name, evt.user_id):
                message.append({"type": "at", "data": {
                    "qq": evt.user_id,
                    "user_id": evt.user_id,
                    "name": display_name,
                }})
                message.append({"type": "text", "data": {"text": " "}})
            else:
                log("warning", "mention_skipped_unresolved_name", group_id=evt.group_id,
                    user_id=evt.user_id, trace_id=trace_id)
        message.append({"type": "text", "data": {"text": text}})
        payload = {
            "group_id": group_id,
            "message": message,
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
                parsed: Any = {}
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
                            "message_id": str((parsed.get("data") or {}).get("message_id") or parsed.get("message_id") or "") if isinstance(parsed, dict) else "",
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
        raise RuntimeError(last_error or "OneBot send failed")


SERVICE: Optional[AIReplyService] = None
CONFIG: Optional[AppConfig] = None


class Handler(BaseHTTPRequestHandler):
    server_version = "WeChatAgentAIReply/1.0"

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
                "queue_size": SERVICE.scheduler.snapshot()["queued"],
                "active_channel_id": CONFIG.ai.active_channel_id,
                "enabled_channels": [x.id for x in CONFIG.ai.channels if x.enabled],
                "memory": {"enabled": CONFIG.memory.enabled, "max_turns": CONFIG.memory.max_turns},
                "tools": {"enabled": CONFIG.tools.enabled, "allowed": CONFIG.tools.allowed},
                "brain": SERVICE.brain_status(),
            })
        else:
            self._send_json(404, {"status": "not_found"})

    def do_POST(self) -> None:
        if self.path in {"/automation/submit", "/automation/approve", "/automation/stop"}:
            try:
                length = int(self.headers.get("Content-Length", "0"))
                raw = json.loads(self.rfile.read(length).decode("utf-8", "replace")) if length else {}
                assert SERVICE is not None
                if self.path == "/automation/submit":
                    group_id = str(raw.get("group_id") or "").strip()
                    intent = str(raw.get("intent") or "").strip()
                    risk_level = str(raw.get("risk_level") or "write")
                    if not group_id.endswith("@chatroom") or not intent:
                        raise ValueError("需要有效群 ID 和任务内容")
                    if risk_level not in {"read", "write", "high"}:
                        raise ValueError("risk_level 必须是 read、write 或 high")
                    result = SERVICE.hermes.submit_manual(group_id, intent, risk_level)
                elif self.path == "/automation/approve":
                    result = SERVICE.hermes.approve(str(raw.get("run_id") or ""))
                else:
                    result = SERVICE.hermes.stop_run(str(raw.get("run_id") or ""))
                self._send_json(200, {"status": "ok", "data": result})
            except Exception as exc:
                self._send_json(400, {"status": "failed", "error": str(exc)})
            return
        if self.path == "/router/test":
            try:
                assert SERVICE is not None
                event_id = f"router-test-{time.time_ns()}"
                event = SimpleNamespace(
                    group_id="web-admin@chatroom", user_id="web-admin",
                    sender_name="后台检查", text="检查快速路由是否可用",
                    trace_id=event_id, message_id=event_id,
                )
                result = SERVICE.fast_route(event, {"items": []}, [], True)
                self._send_json(200, {
                    "status": "ok",
                    "data": {"result": result, "runtime": dict(SERVICE.router_state)},
                })
            except Exception as exc:
                self._send_json(400, {"status": "failed", "error": str(exc)})
            return
        if self.path in {"/embedding/pause", "/embedding/resume"}:
            assert SERVICE is not None
            paused = self.path.endswith("/pause")
            SERVICE.embedding_service.set_paused(paused)
            self._send_json(200, {"status": "ok", "data": SERVICE.embedding_service.snapshot()})
            return
        if self.path == "/embedding/test":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                raw = json.loads(self.rfile.read(length).decode("utf-8", "replace")) if length else {}
                assert SERVICE is not None
                started = time.monotonic()
                result = SERVICE.embedding_service.search(str(raw.get("query") or ""),
                                                          str(raw.get("group_id") or ""), 12)
                result["latency_ms"] = round((time.monotonic() - started) * 1000)
                self._send_json(200, {"status": "ok", "data": result})
            except Exception as exc:
                self._send_json(400, {"status": "failed", "error": str(exc)})
            return
        if self.path == "/persona/extract":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                raw = json.loads(self.rfile.read(length).decode("utf-8", "replace")) if length else {}
                assert SERVICE is not None
                self._send_json(200, {"status": "ok", "data": SERVICE.extract_persona_claims(raw)})
            except Exception as exc:
                self._send_json(409, {"status": "busy", "error": str(exc)})
            return
        if self.path == "/image/generate":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                raw = json.loads(self.rfile.read(length).decode("utf-8", "replace")) if length else {}
                assert SERVICE is not None
                self._send_json(200, {"status": "ok", "data": SERVICE.generate_image(str(raw.get("prompt") or ""))})
            except Exception as exc:
                self._send_json(400, {"status": "failed", "error": str(exc)})
            return
        if self.path == "/config/reload":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                raw = json.loads(self.rfile.read(length).decode("utf-8", "replace")) if length else {}
                cfg_path = Path(str(raw.get("config") or DEFAULT_CONFIG)).expanduser().resolve()
                new_cfg = AppConfig.from_file(cfg_path)
                assert SERVICE is not None
                global CONFIG
                CONFIG = new_cfg
                self._send_json(200, {"status": "ok", "data": SERVICE.reload_config(new_cfg)})
            except Exception as exc:
                self._send_json(400, {"status": "failed", "error": str(exc)})
            return
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
            SERVICE.stop()
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
        if SERVICE:
            SERVICE.stop()
        remove_pid()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
