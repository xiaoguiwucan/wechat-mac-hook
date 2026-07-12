#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OneBot -> AI -> WeChat group reply bridge for the second WeChat only.

- Listens on 127.0.0.1:36060/onebot (the send_url configured for wechat_chatter OneBot).
- Filters target chatrooms, calls an OpenAI-compatible chat completion API, then sends reply
  through the local OneBot HTTP API at 127.0.0.1:58080/send_group_msg.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import hmac
import json
import os
import queue
import signal
import sys
import threading
import time
import traceback
import urllib.error
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
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    ai: AIConfig = field(default_factory=AIConfig)

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
            safety=safety,
            ai=ai,
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


class AIReplyService:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.events: "queue.Queue[OneBotEvent]" = queue.Queue(maxsize=200)
        self.stop_event = threading.Event()
        self.seen: Dict[str, float] = {}
        self.last_reply_at: Dict[str, float] = {}
        self.channel_unavailable_until: Dict[str, float] = {}
        self.histories: Dict[str, Deque[Tuple[str, str]]] = collections.defaultdict(
            lambda: collections.deque(maxlen=max(2, self.cfg.max_context_messages))
        )
        self.worker = threading.Thread(target=self._worker_loop, name="ai-reply-worker", daemon=True)

    def start(self) -> None:
        self.worker.start()

    def enqueue_raw(self, raw: Dict[str, Any], signature: str = "") -> Tuple[bool, str]:
        evt, reason = self.parse_event(raw)
        if not evt:
            return False, reason
        if not self.should_reply(evt):
            return False, "ignored"
        try:
            self.events.put_nowait(evt)
            return True, "queued"
        except queue.Full:
            log("error", "queue_full", group_id=evt.group_id, message_id=evt.message_id)
            return False, "queue_full"

    def parse_event(self, raw: Dict[str, Any]) -> Tuple[Optional[OneBotEvent], str]:
        if raw.get("post_type") != "message":
            return None, "not_message"
        if raw.get("message_type") != "group":
            return None, "not_group"
        group_id = str(raw.get("group_id") or "")
        if not group_id:
            return None, "no_group_id"

        text_parts: List[str] = []
        for m in raw.get("message") or []:
            if isinstance(m, dict) and m.get("type") == "text":
                data = m.get("data") or {}
                text_parts.append(str(data.get("text") or ""))
        text = "".join(text_parts).strip()
        if not text:
            return None, "no_text"

        sender = raw.get("sender") or {}
        message_id = str(raw.get("message_id") or "")
        raw_message = str(raw.get("raw_message") or "")
        event_id_src = f"{group_id}|{message_id}|{raw.get('time')}|{text}"
        event_id = hashlib.sha1(event_id_src.encode("utf-8", "ignore")).hexdigest()
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
        ), "ok"

    def should_reply(self, evt: OneBotEvent) -> bool:
        now = time.time()
        # Compact dedupe cache.
        if len(self.seen) > 1000:
            cutoff = now - 3600
            self.seen = {k: v for k, v in self.seen.items() if v >= cutoff}
        if evt.event_id in self.seen:
            log("debug", "duplicate_event", group_id=evt.group_id, message_id=evt.message_id)
            return False
        self.seen[evt.event_id] = now

        if self.cfg.log_all_group_messages:
            log("info", "group_message_seen", group_id=evt.group_id, group_name=evt.group_name,
                sender=evt.sender_name, user_id=evt.user_id, text=evt.text[:300], raw_message=evt.raw_message[:300])

        if not self.cfg.enabled:
            log("info", "disabled_skip", group_id=evt.group_id)
            return False
        if evt.group_id not in self.cfg.target_groups:
            log("info", "group_not_target_skip", group_id=evt.group_id,
                configured_groups=list(self.cfg.target_groups.keys()))
            return False
        if self.cfg.ignore_self_messages and evt.self_id and evt.user_id == evt.self_id:
            log("info", "self_message_skip", group_id=evt.group_id, user_id=evt.user_id)
            return False
        if self.cfg.allowed_user_ids and evt.user_id not in self.cfg.allowed_user_ids:
            log("info", "sender_not_allowed_skip", group_id=evt.group_id, user_id=evt.user_id,
                allowed_user_ids=self.cfg.allowed_user_ids)
            return False
        if self.cfg.ignored_user_ids and evt.user_id in self.cfg.ignored_user_ids:
            log("info", "sender_ignored_skip", group_id=evt.group_id, user_id=evt.user_id)
            return False
        stripped = evt.text.strip()
        for p in self.cfg.ignore_prefixes:
            if p and stripped.startswith(p):
                log("info", "ignore_prefix_skip", group_id=evt.group_id, prefix=p, text=stripped[:120])
                return False
        if self.cfg.require_keyword:
            if not any(k and k in stripped for k in self.cfg.trigger_keywords):
                log("info", "keyword_skip", group_id=evt.group_id, text=stripped[:120])
                return False
        # If keywords are configured but not required, remove keyword from prompt only logically; still reply to all.
        last = self.last_reply_at.get(evt.group_id, 0.0)
        if now - last < self.cfg.min_seconds_between_replies_per_group:
            log("info", "cooldown_skip", group_id=evt.group_id, seconds=round(now - last, 3))
            return False
        self.last_reply_at[evt.group_id] = now
        return True

    def _worker_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                evt = self.events.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.handle_event(evt)
            except Exception as e:
                log("error", "handle_event_exception", error=str(e), traceback=traceback.format_exc())
            finally:
                self.events.task_done()

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
        sender = evt.sender_name or evt.user_id or "群成员"
        user_content = f"群：{evt.group_name}({evt.group_id})\n发言人：{sender}\n最新消息：{evt.text}"
        messages.append({"role": "user", "content": user_content})
        return messages

    def handle_event(self, evt: OneBotEvent) -> None:
        log("info", "reply_job_start", group_id=evt.group_id, group_name=evt.group_name,
            message_id=evt.message_id, text=evt.text[:300])
        reply = self.generate_reply(evt)
        if not reply:
            return
        reply = self.clean_reply(reply)
        if not reply:
            return
        if self.cfg.reply_prefix and not reply.startswith(self.cfg.reply_prefix):
            reply_to_send = self.cfg.reply_prefix + reply
        else:
            reply_to_send = reply
        if self.cfg.dry_run:
            log("info", "dry_run_reply", group_id=evt.group_id, reply=reply_to_send)
        else:
            if self.cfg.send_delay_seconds > 0:
                time.sleep(self.cfg.send_delay_seconds)
            self.send_group_msg(evt.group_id, reply_to_send)
        hist = self.histories[evt.group_id]
        hist.append((evt.sender_name or evt.user_id or "群成员", evt.text))
        hist.append(("AI", reply_to_send))

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
            log("error", "no_enabled_ai_channels")
            return ""
        failures: List[str] = []
        for index, channel in enumerate(channels):
            reply, error = self.request_channel(channel, evt)
            if reply:
                self.channel_unavailable_until.pop(channel.id, None)
                if index:
                    log("warning", "channel_failover", group_id=evt.group_id,
                        channel_id=channel.id, channel_name=channel.name, failed_channels=failures)
                log("info", "channel_success", group_id=evt.group_id, channel_id=channel.id,
                    channel_name=channel.name, model=channel.model)
                return reply
            failures.append(channel.id)
            self.channel_unavailable_until[channel.id] = time.time() + self.cfg.ai.failure_cooldown_seconds
            log("error", "channel_failed", group_id=evt.group_id, channel_id=channel.id,
                channel_name=channel.name, error=error)
        log("error", "all_channels_failed", group_id=evt.group_id, channels=failures)
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
                model=channel.model, channel_id=channel.id)
            return str(reply), ""
        except Exception as e:
            return "", f"response parse error: {e}; body={body[:500]}"

    def clean_reply(self, reply: str) -> str:
        r = reply.strip()
        # Remove surrounding quotes occasionally produced by models.
        if len(r) >= 2 and ((r[0] == r[-1] == '"') or (r[0] == r[-1] == "'")):
            r = r[1:-1].strip()
        if len(r) > self.cfg.max_reply_chars:
            r = r[: self.cfg.max_reply_chars].rstrip() + "…"
        return r

    def send_group_msg(self, group_id: str, text: str) -> None:
        url = self.cfg.onebot_api.rstrip("/") + "/send_group_msg"
        payload = {
            "group_id": group_id,
            "message": [{"type": "text", "data": {"text": text}}],
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                body = resp.read().decode("utf-8", "replace")
                status = resp.status
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace") if e.fp else ""
            log("error", "send_group_http_error", group_id=group_id, status=e.code, body=body[:1000])
            return
        except Exception as e:
            log("error", "send_group_error", group_id=group_id, error=str(e))
            return
        log("info", "send_group_done", group_id=group_id, status=status, body=body[:1000], text=text[:300])


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
