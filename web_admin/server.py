#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Local web control plane for the isolated second WeChat instance."""
from __future__ import annotations

import argparse
import base64
import collections
import hashlib
import json
import mimetypes
import os
import re
import shutil
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from memory_store import MemoryStore  # noqa: E402
from brain_engine import BrainConfig, OpportunityScorer  # noqa: E402

STATIC = Path(__file__).resolve().parent / "static"
CONFIG_PATH = ROOT / "config" / "ai_reply_config.json"
ENV_PATH = ROOT / "config" / "ai_reply.env"
SECOND_HOME = Path.home() / "Library" / "Application Support" / "WeChatSecond"
LOG_DIR = SECOND_HOME / "logs"
VOICE_PACK_DIR = SECOND_HOME / "voice_packs"
VOICE_CACHE_DIR = SECOND_HOME / "voice_cache"
VOICE_EXTENSIONS = {".silk", ".wav", ".mp3", ".m4a", ".amr"}
VOICE_ARCHIVE_EXTENSIONS = {".zip", ".zip1"}
BIN_DIR = SECOND_HOME / "bin"
WECHAT2_APP = Path.home() / "Applications" / "WeChat2.app"
WECHAT2_EXE = WECHAT2_APP / "Contents" / "MacOS" / "WeChat"
PID_FILES = {
    "onebot": SECOND_HOME / "onebot-wechat2.pid",
    "ai": SECOND_HOME / "ai-reply.pid",
}
LOG_FILES = {
    "ai": LOG_DIR / "ai-reply.log",
    "onebot": LOG_DIR / "onebot-wechat2.log",
}
BRAIN_EVENTS_FILE = LOG_DIR / "brain-events.jsonl"
MEMORY = MemoryStore()

ACTION_SCRIPTS = {
    "launch_wechat2": "launch_wechat2_4_1_11_53.sh",
    "start_onebot": "start_onebot_wechat2.sh",
    "stop_onebot": "stop_onebot_wechat2.sh",
    "start_ai": "start_ai_reply.sh",
    "stop_ai": "stop_ai_reply.sh",
    "stop_backend": "stop_backend_wechat2.sh",
}

PROVIDER_PRESETS = {
    "deepseek": {"base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
    "openai": {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
    "openrouter": {"base_url": "https://openrouter.ai/api/v1", "model": "openai/gpt-4o-mini"},
    "compatible": {"base_url": "", "model": ""},
    "local": {"base_url": "http://127.0.0.1:1234/v1", "model": "local-model"},
}

CHANNEL_HEALTH: Dict[str, Dict[str, Any]] = {}
CHANNEL_HEALTH_LOCK = threading.Lock()
ONEBOT_MONITOR_STATE: Dict[str, Any] = {"checked_at": "", "port_open": False, "last_action": "", "last_error": ""}
ONEBOT_MONITOR_LOCK = threading.Lock()
MEDIA_REPAIR_STATE: Dict[str, Any] = {"last_attempt": 0.0, "last_success": 0.0, "last_error": "", "running": False}
MEDIA_REPAIR_LOCK = threading.Lock()
FACE_SEND_LOCK = threading.Lock()
MEDIA_PAYLOAD_CACHE_LOCK = threading.Lock()
MEDIA_PAYLOAD_CACHE: "collections.OrderedDict[str, Tuple[str, Dict[str, Any]]]" = collections.OrderedDict()
CONFIG_WRITE_LOCK = threading.Lock()
PERSONA_EVENT_LOCK = threading.Lock()
PERSONA_WORKER_STOP = threading.Event()

# 1x1 透明 PNG。用于发到“文件传输助手(filehelper)”暖起 UploadMedia
# 通道；不污染任何群聊，也不需要用户手动点 UI。
TINY_PLACEHOLDER_PNG = (
    "base64://"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAFgwJ/lD3X9wAAAABJRU5ErkJggg=="
)


def config_revision() -> str:
    digest = hashlib.sha256()
    for path in (CONFIG_PATH, ENV_PATH):
        try:
            digest.update(path.read_bytes())
        except OSError:
            pass
    return digest.hexdigest()[:16]


def ai_json(path: str, method: str = "GET", data: Optional[Dict[str, Any]] = None,
            timeout: int = 5) -> Dict[str, Any]:
    payload = None if data is None else json.dumps(data, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request("http://127.0.0.1:36060" + path, data=payload, method=method)
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def emit_persona_event(job: Dict[str, Any], event: str = "progress") -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"type": "persona_analysis", "event": event, "time": time.time(), "job": job}
    with PERSONA_EVENT_LOCK:
        with BRAIN_EVENTS_FILE.open("a", encoding="utf-8") as file:
            file.write(json.dumps(payload, ensure_ascii=False) + "\n")


def persona_worker_loop() -> None:
    """Run resumable 100-message persona batches below interactive work."""
    last_due_check = 0.0
    while not PERSONA_WORKER_STOP.wait(0.35):
        job: Optional[Dict[str, Any]] = None
        try:
            jobs = MEMORY.persona_jobs(limit=20)
            job = next((item for item in jobs if item.get("status") in {"running", "queued"}), None)
            if not job:
                if time.monotonic() - last_due_check >= 60:
                    six_hours_ago = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 6 * 3600))
                    due = MEMORY.queue_due_persona_analysis(six_hours_ago)
                    last_due_check = time.monotonic()
                    if due.get("queued"):
                        emit_persona_event(due, "auto_queued")
                PERSONA_WORKER_STOP.wait(1.0)
                continue
            batch = MEMORY.persona_job_batch_payload(int(job["id"]), 100)
            model_claims = 0
            model_name = ""
            if batch.get("messages"):
                try:
                    extraction = (ai_json("/persona/extract", "POST", batch, 120).get("data") or {})
                    model_name = str(extraction.get("model") or "")
                    model_claims = MEMORY.add_persona_model_claims(batch["group_id"], batch["user_id"], extraction.get("claims") or [], batch["messages"])
                except urllib.error.HTTPError as exc:
                    if exc.code == 409:
                        emit_persona_event({**job, "model_waiting": True}, "waiting_for_model_slot")
                        PERSONA_WORKER_STOP.wait(1.0)
                        continue
                except Exception:
                    # AI service may be offline during administration. Local metrics and
                    # resumable deterministic evidence still progress and can be rebuilt later.
                    pass
            updated = MEMORY.process_persona_job_batch(int(job["id"]), 100)
            updated["model_claims"] = model_claims
            updated["model"] = model_name
            emit_persona_event(updated, "completed" if updated.get("status") == "completed" else "progress")
            # Yield to realtime reply/embedding HTTP requests between every batch.
            PERSONA_WORKER_STOP.wait(0.2)
        except Exception as exc:
            try:
                if job:
                    with MEMORY.connect() as db:
                        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
                        db.execute("UPDATE persona_analysis_jobs SET status='failed',error=?,updated_at=? WHERE id=?", (str(exc)[:1000], stamp, int(job["id"])))
                        db.execute("UPDATE personas SET analysis_status='failed',analysis_error=?,updated_at=? WHERE group_id=? AND user_id=?", (str(exc)[:1000], stamp, job["group_id"], job["user_id"]))
                    emit_persona_event({**job, "status": "failed", "error": str(exc)}, "failed")
            except Exception:
                pass
            PERSONA_WORKER_STOP.wait(1.0)


def brain_config_payload() -> Dict[str, Any]:
    raw = json_read(CONFIG_PATH, {})
    strategy = raw.get("reply_strategy") if isinstance(raw.get("reply_strategy"), dict) else {}
    embedding = raw.get("embedding") if isinstance(raw.get("embedding"), dict) else {}
    retrieval = raw.get("retrieval") if isinstance(raw.get("retrieval"), dict) else {}
    media_reply = raw.get("media_reply") if isinstance(raw.get("media_reply"), dict) else {}
    ignored_group_members = raw.get("ignored_group_members") if isinstance(raw.get("ignored_group_members"), dict) else {}
    return {"reply_strategy": strategy, "embedding": embedding, "retrieval": retrieval,
            "media_reply": media_reply, "ignored_group_members": ignored_group_members}


def save_brain_config(data: Dict[str, Any]) -> Dict[str, Any]:
    current = json_read(CONFIG_PATH, {})
    if isinstance(data.get("reply_strategy"), dict):
        incoming = dict(data["reply_strategy"])
        mode = str(incoming.get("mode") or "veteran")
        presets = {"reserved": 78.0, "natural": 65.0, "veteran": 52.0}
        threshold = float(incoming.get("threshold", presets.get(mode, 52.0)))
        incoming["mode"] = mode
        incoming["threshold"] = max(0.0, min(100.0, threshold))
        incoming["scoring_mode"] = str(incoming.get("scoring_mode") or "local_fast")
        if incoming["scoring_mode"] not in {"local_fast", "model_deep"}:
            incoming["scoring_mode"] = "local_fast"
        incoming["rerank_candidates"] = max(4, min(24, int(incoming.get("rerank_candidates", 12))))
        incoming["global_workers"] = max(1, min(16, int(incoming.get("global_workers", 8))))
        incoming["per_group_workers"] = max(1, min(6, int(incoming.get("per_group_workers", 3))))
        incoming["model_concurrency"] = max(1, min(16, int(incoming.get("model_concurrency", 6))))
        current["reply_strategy"] = incoming
    if isinstance(data.get("embedding"), dict):
        incoming_embedding = dict(data["embedding"])
        incoming_embedding["dimensions"] = 4096
        incoming_embedding["batch_size"] = max(1, min(64, int(incoming_embedding.get("batch_size", 32))))
        current["embedding"] = incoming_embedding
    if isinstance(data.get("retrieval"), dict):
        incoming_retrieval = dict(data["retrieval"])
        limits = {
            "vector_limit": (12, 200, 60), "fts_limit": (8, 100, 30), "person_limit": (4, 50, 12),
            "meme_limit": (4, 50, 12), "time_limit": (4, 100, 20), "media_limit": (4, 100, 16),
            "fusion_limit": (12, 100, 60), "rerank_cache_seconds": (0, 3600, 600),
        }
        for key, (low, high, default) in limits.items():
            incoming_retrieval[key] = max(low, min(high, int(incoming_retrieval.get(key, default))))
        incoming_retrieval["adaptive_rerank"] = bool(incoming_retrieval.get("adaptive_rerank", True))
        current["retrieval"] = incoming_retrieval
    if isinstance(data.get("media_reply"), dict):
        incoming_media = dict(data["media_reply"])
        incoming_media["automatic_enabled"] = bool(incoming_media.get("automatic_enabled", True))
        incoming_media["voice_probability"] = max(0.0, min(1.0, float(incoming_media.get("voice_probability", 0.15))))
        incoming_media["face_probability"] = max(0.0, min(1.0, float(incoming_media.get("face_probability", 0.20))))
        legacy_min_fit = float(incoming_media.get("min_fit", 70))
        incoming_media["voice_min_fit"] = max(0.0, min(100.0, float(incoming_media.get("voice_min_fit", legacy_min_fit if "min_fit" in incoming_media else 55))))
        incoming_media["face_min_fit"] = max(0.0, min(100.0, float(incoming_media.get("face_min_fit", legacy_min_fit if "min_fit" in incoming_media else 45))))
        incoming_media.pop("min_fit", None)
        incoming_media["min_candidate_confidence"] = max(0.0, min(1.0, float(incoming_media.get("min_candidate_confidence", 0.65))))
        incoming_media["global_face_assets"] = bool(incoming_media.get("global_face_assets", True))
        incoming_media["auto_media_replaces_text"] = True
        incoming_media["group_overrides"] = incoming_media.get("group_overrides") if isinstance(incoming_media.get("group_overrides"), dict) else {}
        current["media_reply"] = incoming_media
    atomic_write(CONFIG_PATH, json.dumps(current, ensure_ascii=False, indent=2) + "\n")
    hot_reload: Dict[str, Any] = {"applied": False}
    try:
        response = ai_json("/config/reload", "POST", {"config": str(CONFIG_PATH)}, 15)
        hot_reload = {"applied": response.get("status") == "ok", "response": response}
    except Exception as exc:
        hot_reload = {"applied": False, "error": str(exc)}
    return {**brain_config_payload(), "hot_reload": hot_reload, "revision": config_revision()}


def brain_tasks(data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    data = data or {}
    rows = MEMORY.reply_tasks(str(data.get("group_id") or ""), int(data.get("limit") or 200),
                              bool(data.get("active_only", False)))
    now = time.time()
    for row in rows:
        row["elapsed_seconds"] = round(max(0.0, (row.get("completed_at") or now) - (row.get("started_at") or row.get("queued_at") or now)), 2)
    active = [row for row in rows if row.get("state") not in {"completed", "skipped", "failed", "cancelled"}]
    runtime: Dict[str, Any] = {}
    try:
        runtime = ai_json("/status", timeout=3).get("brain") or {}
    except Exception:
        pass
    positions = ((runtime.get("scheduler") or {}).get("queue_positions") or {})
    for row in rows:
        row["queue_position"] = positions.get(row.get("task_id"), 0)
    return {
        "items": rows,
        "active": len(active),
        "queued": sum(row.get("state") == "queued" for row in rows),
        "completed_recent": sum(row.get("state") == "completed" and now - float(row.get("completed_at") or 0) <= 60 for row in rows),
        "runtime": runtime,
    }


def brain_preview(data: Dict[str, Any]) -> Dict[str, Any]:
    cfg = BrainConfig.from_raw(json_read(CONFIG_PATH, {}))
    scorer = OpportunityScorer(cfg)
    group_id = str(data.get("group_id") or "").strip()
    cutoff = int(time.time()) - 86400
    sql = "SELECT * FROM messages WHERE direction='incoming' AND event_time>=?"
    params: list[Any] = [cutoff]
    if group_id:
        sql += " AND group_id=?"
        params.append(group_id)
    sql += " ORDER BY event_time ASC"
    with MEMORY.connect() as db:
        rows = [dict(row) for row in db.execute(sql, params).fetchall()]
    recent_by_group: Dict[str, list[Dict[str, Any]]] = {}
    previews = []
    for row in rows:
        gid = str(row.get("group_id") or "")
        recent = recent_by_group.setdefault(gid, [])
        value = str(row.get("text") or row.get("raw_message") or "").strip()
        evt = type("PreviewEvent", (), {
            "text": value, "user_id": str(row.get("user_id") or ""), "self_id": "",
            "timestamp": int(row.get("event_time") or cutoff), "raw": {"message": []},
        })()
        local = scorer.local_score(evt, recent, {"items": [], "culture": MEMORY.culture_context(gid, value, 8)}, None)
        predicted = bool(float(local["pre_score"]) >= cfg.threshold)
        previews.append({"event_id": row.get("event_id"), "group_id": gid,
                         "sender_name": row.get("sender_name"), "text": value[:500],
                         "event_time": row.get("event_time"), "estimated_score": local["pre_score"],
                         "threshold": cfg.threshold, "predicted_reply": predicted,
                         "mandatory": local["mandatory"], "signals": local["reasons"]})
        recent.append(row)
        if len(recent) > 30:
            del recent[:-30]
    predicted_rows = [row for row in previews if row["predicted_reply"]]
    return {"hours": 24, "evaluated": len(previews), "predicted": len(predicted_rows),
            "threshold": cfg.threshold, "items": predicted_rows[-1000:]}


def embedding_control(action: str) -> Dict[str, Any]:
    if action == "start":
        queued = MEMORY.enqueue_all_embeddings()
        return {"action": action, "queued": queued, "stats": MEMORY.stats()}
    # Pause/resume must change the live worker. Expose dedicated AI endpoints when
    # available; queued work remains durable if AI is temporarily offline.
    response = ai_json("/embedding/" + action, "POST", {}, 10)
    return {"action": action, "response": response, "stats": MEMORY.stats()}


def json_read(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def atomic_write(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def parse_env(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:]
        try:
            parts = shlex.split(line)
        except ValueError:
            continue
        for part in parts:
            if "=" in part:
                key, value = part.split("=", 1)
                values[key] = value
    return values


def shell_value(value: Any) -> str:
    return shlex.quote(str(value))


def read_config() -> Dict[str, Any]:
    cfg = json_read(CONFIG_PATH, {})
    env = parse_env(ENV_PATH)
    ai = cfg.setdefault("ai", {})
    raw_channels = ai.get("channels", []) or []
    channels = []
    for index, item in enumerate(raw_channels):
        if not isinstance(item, dict):
            continue
        channel_id = str(item.get("id") or f"channel-{index + 1}")
        key_env = str(item.get("api_key_env") or f"AI_REPLY_CHANNEL_{index + 1}_API_KEY")
        channels.append({
            "id": channel_id,
            "name": str(item.get("name") or channel_id),
            "provider": str(item.get("provider") or "compatible"),
            "base_url": str(item.get("base_url") or ""),
            "api_key": env.get(key_env, ""),
            "api_key_env": key_env,
            "model": str(item.get("model") or ""),
            "timeout_seconds": max(3, int(item.get("timeout_seconds", 30))),
            "enabled": bool(item.get("enabled", True)),
            "priority": int(item.get("priority", index)),
        })
    if not channels:
        channels.append({
            "id": "primary",
            "name": "默认渠道",
            "provider": env.get("AI_REPLY_PROVIDER", ai.get("provider", "compatible")),
            "base_url": env.get("AI_REPLY_BASE_URL", ai.get("base_url", "https://api.deepseek.com/v1")),
            "api_key": env.get("AI_REPLY_API_KEY", ""),
            "api_key_env": "AI_REPLY_API_KEY",
            "model": env.get("AI_REPLY_MODEL", ai.get("model", "deepseek-chat")),
            "timeout_seconds": int(env.get("AI_REPLY_TIMEOUT_SECONDS", ai.get("timeout_seconds", 30))),
            "enabled": True,
            "priority": 0,
        })
    active_channel_id = str(ai.get("active_channel_id") or channels[0]["id"])
    active = next((x for x in channels if x["id"] == active_channel_id), channels[0])
    vision_raw = cfg.get("vision_ocr", {}) if isinstance(cfg.get("vision_ocr"), dict) else {}
    vision_key_env = str(vision_raw.get("api_key_env") or "AI_REPLY_VISION_OCR_API_KEY")
    asr_raw = cfg.get("asr", {}) if isinstance(cfg.get("asr"), dict) else {}
    asr_key_env = str(asr_raw.get("api_key_env") or "AI_REPLY_ASR_API_KEY")
    return {
        "revision": config_revision(),
        "provider": active["provider"],
        "base_url": active["base_url"],
        "api_key": active["api_key"],
        "model": active["model"],
        "channels": channels,
        "active_channel_id": active["id"],
        "auto_failover": bool(ai.get("auto_failover", True)),
        "auto_health_check": bool(ai.get("auto_health_check", True)),
        "health_check_interval_seconds": max(30, int(ai.get("health_check_interval_seconds", 300))),
        "temperature": float(env.get("AI_REPLY_TEMPERATURE", ai.get("temperature", 0.3))),
        "max_tokens": int(env.get("AI_REPLY_MAX_TOKENS", ai.get("max_tokens", 600))),
        "timeout_seconds": active["timeout_seconds"],
        "system_prompt": ai.get("system_prompt", "你是微信群值班助手。用中文简洁回复。"),
        "personality": str(cfg.get("personality", "专业、克制、直接。回答简洁，不说空话。")),
        "enabled": bool(cfg.get("enabled", True)),
        "dry_run": str(env.get("AI_REPLY_DRY_RUN", cfg.get("dry_run", False))).lower() in {"1", "true", "yes"},
        "require_keyword": str(env.get("AI_REPLY_REQUIRE_KEYWORD", cfg.get("require_keyword", False))).lower() in {"1", "true", "yes"},
        "ignore_self_messages": str(env.get("AI_REPLY_IGNORE_SELF", cfg.get("ignore_self_messages", False))).lower() in {"1", "true", "yes"},
        "reply_prefix": cfg.get("reply_prefix", "AI："),
        "trigger_keywords": cfg.get("trigger_keywords", []),
        "cooldown": float(cfg.get("min_seconds_between_replies_per_group", 2)),
        "max_reply_chars": int(cfg.get("max_reply_chars", 600)),
        "max_context_messages": int(cfg.get("max_context_messages", 8)),
        "onebot_api": env.get("AI_REPLY_ONEBOT_API", cfg.get("onebot_api", "http://127.0.0.1:58080")),
        "target_groups": cfg.get("target_groups", []),
        "group_aliases": cfg.get("group_aliases", {}) if isinstance(cfg.get("group_aliases", {}), dict) else {},
        "memory": cfg.get("memory", {"enabled": True, "max_turns": 12, "summary_enabled": True}),
        "tools": cfg.get("tools", {"enabled": True, "allowed": ["get_status", "get_recent_logs", "list_groups", "test_model_channel", "send_probe", "search_messages", "get_group_memory", "vector_search", "list_personas", "list_media"]}),
        "onebot_monitor": cfg.get("onebot_monitor", {"enabled": True, "auto_recover": True}),
        "media_auto_repair": cfg.get("media_auto_repair", {
            "enabled": True,
            "filehelper_id": "filehelper",
            "cooldown_seconds": 45,
            "poll_seconds": 8,
        }) if isinstance(cfg.get("media_auto_repair", {}), dict) else {
            "enabled": True,
            "filehelper_id": "filehelper",
            "cooldown_seconds": 45,
            "poll_seconds": 8,
        },
        "vision_ocr": {
            "enabled": bool(vision_raw.get("enabled", False)),
            "base_url": str(vision_raw.get("base_url") or active["base_url"]),
            "api_key": env.get(vision_key_env, ""),
            "api_key_env": vision_key_env,
            "model": str(vision_raw.get("model") or active["model"]),
            "timeout_seconds": max(3, int(vision_raw.get("timeout_seconds", 60))),
            "prompt": str(vision_raw.get("prompt") or "请对这张图片进行OCR识别，提取所有可见文字，并用中文给出一句简短图片摘要。"),
            "auto_analyze": bool(vision_raw.get("auto_analyze", False)),
        },
        "asr": {
            "enabled": bool(asr_raw.get("enabled", False)),
            "base_url": str(asr_raw.get("base_url") or active["base_url"]),
            "api_key": env.get(asr_key_env, ""),
            "api_key_env": asr_key_env,
            "model": str(asr_raw.get("model") or ""),
            "timeout_seconds": max(3, int(asr_raw.get("timeout_seconds", 90))),
            "language": str(asr_raw.get("language") or "zh"),
            "prompt": str(asr_raw.get("prompt") or ""),
            "auto_transcribe": bool(asr_raw.get("auto_transcribe", True)),
        },
        "presets": PROVIDER_PRESETS,
    }


def save_config(data: Dict[str, Any]) -> Dict[str, Any]:
    client_revision = str(data.get("revision", ""))
    if not client_revision or client_revision != config_revision():
        raise ValueError("配置已在后台更新，请刷新页面后重新修改")
    current = json_read(CONFIG_PATH, {})
    ai = current.setdefault("ai", {})
    groups = data.get("target_groups", [])
    clean_groups = []
    for item in groups:
        gid = str(item.get("id", "")).strip() if isinstance(item, dict) else ""
        if gid and gid.endswith("@chatroom"):
            clean_groups.append({"id": gid, "name": str(item.get("name") or gid).strip()})
    if not clean_groups:
        raise ValueError("至少保留一个有效的群 ID（以 @chatroom 结尾）")
    raw_channels = data.get("channels")
    if not isinstance(raw_channels, list) or not raw_channels:
        raw_channels = [{
            "id": "primary", "name": "默认渠道", "provider": data.get("provider", "compatible"),
            "base_url": data.get("base_url", ""), "api_key": data.get("api_key", ""),
            "model": data.get("model", ""), "timeout_seconds": data.get("timeout_seconds", 30),
            "enabled": True, "priority": 0,
        }]
    clean_channels = []
    channel_keys: Dict[str, str] = {}
    seen_ids = set()
    for index, item in enumerate(raw_channels):
        if not isinstance(item, dict):
            continue
        channel_id = re.sub(r"[^a-zA-Z0-9_-]", "-", str(item.get("id") or f"channel-{index + 1}"))[:48].strip("-")
        if not channel_id or channel_id in seen_ids:
            raise ValueError("渠道 ID 无效或重复")
        seen_ids.add(channel_id)
        base_url = str(item.get("base_url", "")).strip().rstrip("/")
        model = str(item.get("model", "")).strip()
        if not base_url.startswith(("http://", "https://")):
            raise ValueError(f"渠道“{item.get('name') or channel_id}”的 API 地址无效")
        if not model:
            raise ValueError(f"渠道“{item.get('name') or channel_id}”的模型不能为空")
        key_env = "AI_REPLY_CHANNEL_" + re.sub(r"[^A-Z0-9]", "_", channel_id.upper()) + "_API_KEY"
        clean_channels.append({
            "id": channel_id,
            "name": str(item.get("name") or channel_id).strip(),
            "provider": "openai_compatible",
            "base_url": base_url,
            "api_key_env": key_env,
            "model": model,
            "timeout_seconds": max(3, min(300, int(item.get("timeout_seconds", 30)))),
            "enabled": bool(item.get("enabled", True)),
            "priority": index,
        })
        channel_keys[key_env] = str(item.get("api_key", ""))
    if not clean_channels or not any(x["enabled"] for x in clean_channels):
        raise ValueError("至少保留一个启用的模型渠道")
    active_channel_id = str(data.get("active_channel_id") or clean_channels[0]["id"])
    active = next((x for x in clean_channels if x["id"] == active_channel_id and x["enabled"]), None)
    if active is None:
        active = next(x for x in clean_channels if x["enabled"])
        active_channel_id = active["id"]

    ai.update({
        "provider": "openai_compatible",
        "base_url": active["base_url"],
        "api_key_env": active["api_key_env"],
        "model": active["model"],
        "timeout_seconds": active["timeout_seconds"],
        "channels": clean_channels,
        "active_channel_id": active_channel_id,
        "auto_failover": bool(data.get("auto_failover", True)),
        "auto_health_check": bool(data.get("auto_health_check", True)),
        "health_check_interval_seconds": max(30, min(86400, int(data.get("health_check_interval_seconds", 300)))),
        "failure_cooldown_seconds": 60,
        "temperature": max(0.0, min(2.0, float(data.get("temperature", 0.3)))),
        "max_tokens": max(1, int(data.get("max_tokens", 600))),
        "system_prompt": str(data.get("system_prompt", "")).strip(),
    })
    current.update({
        "enabled": bool(data.get("enabled", True)),
        "dry_run": bool(data.get("dry_run", False)),
        "require_keyword": bool(data.get("require_keyword", False)),
        "ignore_self_messages": bool(data.get("ignore_self_messages", False)),
        "reply_prefix": str(data.get("reply_prefix", "AI：")),
        "trigger_keywords": [str(x).strip() for x in data.get("trigger_keywords", []) if str(x).strip()],
        "min_seconds_between_replies_per_group": max(0.0, float(data.get("cooldown", 2))),
        "max_reply_chars": max(1, int(data.get("max_reply_chars", 600))),
        "max_context_messages": max(1, int(data.get("max_context_messages", 8))),
        "onebot_api": str(data.get("onebot_api", "http://127.0.0.1:58080")).rstrip("/"),
        "target_groups": clean_groups,
        "personality": str(data.get("personality", "")).strip(),
        "group_aliases": {
            **{str(k): str(v).strip() for k, v in (current.get("group_aliases") or {}).items() if str(k).endswith("@chatroom") and str(v).strip()},
            **{str(x["id"]): str(x.get("name") or x["id"]).strip() for x in clean_groups},
        },
        "memory": data.get("memory") if isinstance(data.get("memory"), dict) else current.get("memory", {"enabled": True, "max_turns": 12, "summary_enabled": True}),
        "tools": data.get("tools") if isinstance(data.get("tools"), dict) else current.get("tools", {"enabled": True, "allowed": ["get_status", "get_recent_logs", "list_groups", "test_model_channel", "send_probe", "search_messages", "get_group_memory", "vector_search", "list_personas", "list_media"]}),
        "onebot_monitor": data.get("onebot_monitor") if isinstance(data.get("onebot_monitor"), dict) else current.get("onebot_monitor", {"enabled": True, "auto_recover": True}),
        "media_auto_repair": data.get("media_auto_repair") if isinstance(data.get("media_auto_repair"), dict) else current.get("media_auto_repair", {"enabled": True, "filehelper_id": "filehelper", "cooldown_seconds": 45, "poll_seconds": 8}),
    })
    vision_in = data.get("vision_ocr") if isinstance(data.get("vision_ocr"), dict) else current.get("vision_ocr", {})
    vision_base = str(vision_in.get("base_url") or active["base_url"]).strip().rstrip("/")
    if vision_base and not vision_base.startswith(("http://", "https://")):
        raise ValueError("OCR 模型 API 地址无效")
    current["vision_ocr"] = {
        "enabled": bool(vision_in.get("enabled", False)),
        "base_url": vision_base or active["base_url"],
        "api_key_env": "AI_REPLY_VISION_OCR_API_KEY",
        "model": str(vision_in.get("model") or active["model"]).strip(),
        "timeout_seconds": max(3, min(300, int(vision_in.get("timeout_seconds", 60)))),
        "prompt": str(vision_in.get("prompt") or "请对这张图片进行OCR识别，提取所有可见文字，并用中文给出一句简短图片摘要。").strip(),
        "auto_analyze": bool(vision_in.get("auto_analyze", False)),
    }
    if not current["vision_ocr"]["model"]:
        current["vision_ocr"]["model"] = active["model"]

    asr_in = data.get("asr") if isinstance(data.get("asr"), dict) else current.get("asr", {})
    asr_base = str(asr_in.get("base_url") or active["base_url"]).strip().rstrip("/")
    if asr_base and not asr_base.startswith(("http://", "https://")):
        raise ValueError("ASR 模型 API 地址无效")
    current["asr"] = {
        "enabled": bool(asr_in.get("enabled", False)),
        "base_url": asr_base or active["base_url"],
        "api_key_env": "AI_REPLY_ASR_API_KEY",
        "model": str(asr_in.get("model") or "").strip(),
        "timeout_seconds": max(3, min(300, int(asr_in.get("timeout_seconds", 90)))),
        "language": str(asr_in.get("language") or "zh").strip(),
        "prompt": str(asr_in.get("prompt") or "").strip(),
        "auto_transcribe": bool(asr_in.get("auto_transcribe", True)),
    }
    atomic_write(CONFIG_PATH, json.dumps(current, ensure_ascii=False, indent=2) + "\n")
    env_values = {
        "AI_REPLY_PROVIDER": ai["provider"],
        "AI_REPLY_API_KEY": channel_keys.get(active["api_key_env"], ""),
        "AI_REPLY_BASE_URL": active["base_url"],
        "AI_REPLY_MODEL": active["model"],
        "AI_REPLY_TEMPERATURE": ai["temperature"],
        "AI_REPLY_MAX_TOKENS": ai["max_tokens"],
        "AI_REPLY_TIMEOUT_SECONDS": active["timeout_seconds"],
        "AI_REPLY_ONEBOT_API": current["onebot_api"],
        "AI_REPLY_DRY_RUN": str(current["dry_run"]).lower(),
        "AI_REPLY_REQUIRE_KEYWORD": str(current["require_keyword"]).lower(),
        "AI_REPLY_IGNORE_SELF": str(current["ignore_self_messages"]).lower(),
    }
    env_values.update(channel_keys)
    env_values["AI_REPLY_VISION_OCR_API_KEY"] = str(vision_in.get("api_key", ""))
    env_values["AI_REPLY_ASR_API_KEY"] = str(asr_in.get("api_key", ""))
    lines = ["# 由第二微信 Web 管理后台生成。"] + [f"export {k}={shell_value(v)}" for k, v in env_values.items()]
    atomic_write(ENV_PATH, "\n".join(lines) + "\n")
    return read_config()


def pid_command(pid: int) -> str:
    if pid <= 0:
        return ""
    try:
        return subprocess.check_output(["/bin/ps", "-p", str(pid), "-o", "command="], text=True).strip()
    except subprocess.SubprocessError:
        return ""


def pid_from_file(path: Path, marker: str) -> Tuple[int, str]:
    try:
        pid = int(path.read_text().strip())
    except (OSError, ValueError):
        return 0, ""
    cmd = pid_command(pid)
    return (pid, cmd) if marker in cmd else (0, "")


def find_wechat2() -> Tuple[int, str]:
    script = ROOT / "scripts" / "find_wechat2_pid.sh"
    try:
        output = subprocess.check_output([str(script)], text=True, timeout=5).strip().splitlines()
        pid = int(output[0]) if output else 0
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0, ""
    cmd = pid_command(pid)
    return (pid, cmd) if str(WECHAT2_EXE) in cmd else (0, "")


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.35):
            return True
    except OSError:
        return False


def process_item(pid: int, cmd: str, port: Optional[int] = None) -> Dict[str, Any]:
    uptime = ""
    if pid:
        try:
            uptime = subprocess.check_output(["/bin/ps", "-p", str(pid), "-o", "etime="], text=True).strip()
        except subprocess.SubprocessError:
            pass
    return {"running": bool(pid), "pid": pid or None, "uptime": uptime, "port": port, "port_open": port_open(port) if port else None}


def onebot_target_pid(command: str) -> int:
    match = re.search(r"(?:^|\s)-wechat_pid=(\d+)(?:\s|$)", str(command or ""))
    return int(match.group(1)) if match else 0


def upload_x0_cache_info(wechat_pid: int) -> Dict[str, Any]:
    cache_file = SECOND_HOME / "state" / "upload_x0.json"
    try:
        raw = json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {"present": False, "valid": False, "path": str(cache_file)}
    cached_pid = int(raw.get("pid") or 0)
    x0 = str(raw.get("x0") or "").strip()
    pointer_valid = bool(re.fullmatch(r"0x[0-9a-fA-F]+", x0))
    valid = bool(wechat_pid and cached_pid == int(wechat_pid) and pointer_valid)
    stale = bool(cached_pid and wechat_pid and cached_pid != int(wechat_pid))
    if stale:
        # An X0 is an address in the WeChat process. Keeping it after a crash
        # only creates a misleading “ready” state and can crash the next send.
        try:
            cache_file.unlink()
        except OSError:
            pass
    return {
        "present": True,
        "valid": valid,
        "stale": stale,
        "pid": cached_pid,
        "current_pid": int(wechat_pid or 0),
        "x0": x0 if valid else "",
        "updated_at": str(raw.get("updated_at") or ""),
        "path": str(cache_file),
    }


def cached_upload_x0_ready(wechat_pid: int) -> bool:
    return bool(upload_x0_cache_info(wechat_pid).get("valid"))


def status() -> Dict[str, Any]:
    wp, wc = find_wechat2()
    op, oc = pid_from_file(PID_FILES["onebot"], "tools/onebot/onebot/onebot")
    ap, ac = pid_from_file(PID_FILES["ai"], "ai_reply_server.py")
    onebot_head = ""
    onebot_tail = ""
    try:
        with LOG_FILES["onebot"].open("r", encoding="utf-8", errors="replace") as f:
            onebot_head = f.read(64_000)
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 128_000))
            onebot_tail = f.read()
    except OSError:
        pass
    onebot_log = onebot_head + onebot_tail
    attached_pid = onebot_target_pid(oc)
    attached_current_wechat = bool(wp and attached_pid == wp)
    hook_ready = attached_current_wechat and "Dynamic Text Message Setup Complete" in onebot_log and "Cannot locate runtime base" not in onebot_log
    send_ready = hook_ready and "捕获到 StartTask" in onebot_tail
    cache_info = upload_x0_cache_info(wp)
    # The OneBot log is truncated on every attach, so a capture in this
    # process is valid for the current WeChat PID. Old cache files are checked
    # separately and are never accepted for a new process.
    recent_capture = "捕获到真实 UploadMedia" in onebot_tail or "使用缓存 UploadMedia X0" in onebot_tail
    media_upload_ready = hook_ready and (recent_capture or bool(cache_info.get("valid")))
    cfg = read_config()
    with ONEBOT_MONITOR_LOCK:
        monitor_state = dict(ONEBOT_MONITOR_STATE)
    with MEDIA_REPAIR_LOCK:
        media_repair_state = dict(MEDIA_REPAIR_STATE)
    return {
        "wechat2": process_item(wp, wc),
        "onebot": {
            **process_item(op, oc, 58080),
            "hook_ready": hook_ready,
            "send_ready": send_ready,
            "attached_wechat_pid": attached_pid or None,
            "attached_current_wechat": attached_current_wechat,
            "media_upload_ready": media_upload_ready,
            "media_upload": {
                "ready": media_upload_ready,
                "current_wechat_pid": wp or 0,
                "cache": cache_info,
                "needs_real_upload": not media_upload_ready,
                "message": "已捕获当前进程上传通道" if media_upload_ready else "后台会自动向文件传输助手发送极小占位图唤醒媒体通道",
                "auto_repair": media_repair_state,
            },
        },
        "ai": {**process_item(ap, ac, 36060), "configured": any(
            x.get("enabled") and (x.get("api_key") or str(x.get("base_url", "")).startswith(("http://127.0.0.1", "http://localhost")))
            for x in cfg.get("channels", [])
        )},
        "isolation": {"app": str(WECHAT2_APP), "bundle_id": "com.tencent.xinWeChat.instance2", "main_wechat_touched": False},
        "onebot_monitor": monitor_state,
        "time": time.strftime("%H:%M:%S"),
    }


def request_json(url: str, method: str = "GET", payload: Any = None, headers: Optional[Dict[str, str]] = None, timeout: int = 30) -> Tuple[int, Any, str]:
    body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    req_headers = {
        "Accept": "application/json",
        "User-Agent": "openai-python/1.99.0",
        **(headers or {}),
    }
    if body is not None:
        req_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, method=method, headers=req_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(raw), raw
            except ValueError:
                return resp.status, raw, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = raw
        return e.code, parsed, raw


def api_credentials(data: Dict[str, Any]) -> Tuple[str, str, str, int]:
    cfg = read_config()
    base = str(data.get("base_url") or cfg["base_url"]).rstrip("/")
    key = str(data.get("api_key") if "api_key" in data else cfg["api_key"])
    model = str(data.get("model") or cfg["model"])
    timeout = int(data.get("timeout_seconds") or cfg["timeout_seconds"])
    if not base.startswith(("http://", "https://")):
        raise ValueError("API 地址无效")
    if not key and not base.startswith(("http://127.0.0.1", "http://localhost")):
        raise ValueError("请先填写 API Key")
    return base, key, model, timeout


def auth_headers(key: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {key}"} if key else {}


def fetch_models(data: Dict[str, Any]) -> Dict[str, Any]:
    base, key, _, timeout = api_credentials(data)
    code, parsed, raw = request_json(base + "/models", headers=auth_headers(key), timeout=timeout)
    if code >= 300:
        raise RuntimeError(f"模型接口 HTTP {code}: {raw[:500]}")
    items = parsed.get("data", []) if isinstance(parsed, dict) else []
    models = sorted({str(x.get("id")) for x in items if isinstance(x, dict) and x.get("id")})
    return {"models": models, "count": len(models)}


def test_ai(data: Dict[str, Any]) -> Dict[str, Any]:
    base, key, model, timeout = api_credentials(data)
    prompt = str(data.get("prompt") or "只回复：连接成功")
    started = time.monotonic()
    code, parsed, raw = request_json(base + "/chat/completions", "POST", {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 100,
    }, auth_headers(key), timeout)
    elapsed = round((time.monotonic() - started) * 1000)
    if code >= 300:
        raise RuntimeError(f"AI 接口 HTTP {code}: {raw[:800]}")
    try:
        reply = parsed["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"响应格式不兼容: {raw[:800]}")
    return {"reply": str(reply), "model": model, "latency_ms": elapsed}


def image_input_for_vision(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        raise ValueError("需要图片 URL 或本地图片路径")
    if value.startswith(("http://", "https://", "data:image/")):
        return value
    if value.startswith("file://"):
        value = urllib.parse.unquote(urllib.parse.urlparse(value).path)
    path = Path(value).expanduser()
    if not path.exists() or not path.is_file():
        raise ValueError(f"图片文件不存在：{value}")
    mime = mimetypes.guess_type(str(path))[0] or "image/png"
    if not mime.startswith("image/"):
        raise ValueError("OCR 测试只接受图片文件")
    raw = path.read_bytes()
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError("图片超过 8MB，请先压缩")
    return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")


def vision_credentials(data: Dict[str, Any]) -> Tuple[str, str, str, int, str]:
    cfg = read_config()
    v = cfg.get("vision_ocr", {}) or {}
    base = str(data.get("base_url") or v.get("base_url") or cfg["base_url"]).rstrip("/")
    key = str(data.get("api_key") if "api_key" in data else v.get("api_key") or cfg["api_key"])
    model = str(data.get("model") or v.get("model") or cfg["model"])
    timeout = int(data.get("timeout_seconds") or v.get("timeout_seconds") or 60)
    prompt = str(data.get("prompt") or v.get("prompt") or "请OCR识别图片文字并给出简短摘要。")
    if not base.startswith(("http://", "https://")):
        raise ValueError("OCR API 地址无效")
    if not key and not base.startswith(("http://127.0.0.1", "http://localhost")):
        raise ValueError("请先填写 OCR API Key")
    if not model:
        raise ValueError("请先填写 OCR 模型 ID")
    return base, key, model, timeout, prompt


def test_vision_ocr(data: Dict[str, Any]) -> Dict[str, Any]:
    base, key, model, timeout, prompt = vision_credentials(data)
    image_url = image_input_for_vision(str(data.get("image") or data.get("file") or data.get("url") or ""))
    started = time.monotonic()
    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": image_url}},
            ],
        }],
        "temperature": 0,
        "max_tokens": int(data.get("max_tokens") or 800),
    }
    code, parsed, raw = request_json(base + "/chat/completions", "POST", payload, auth_headers(key), timeout)
    elapsed = round((time.monotonic() - started) * 1000)
    if code >= 300:
        raise RuntimeError(f"OCR 接口 HTTP {code}: {raw[:1000]}")
    try:
        reply = parsed["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"OCR 响应格式不兼容: {raw[:1000]}")
    return {"reply": str(reply), "model": model, "latency_ms": elapsed}


def asr_credentials(data: Dict[str, Any]) -> Tuple[str, str, str, int, str, str]:
    cfg = read_config()
    a = cfg.get("asr", {}) or {}
    base = str(data.get("base_url") or a.get("base_url") or cfg["base_url"]).rstrip("/")
    key = str(data.get("api_key") if "api_key" in data else a.get("api_key") or cfg["api_key"])
    model = str(data.get("model") or a.get("model") or "")
    timeout = int(data.get("timeout_seconds") or a.get("timeout_seconds") or 90)
    language = str(data.get("language") or a.get("language") or "zh")
    prompt = str(data.get("prompt") if "prompt" in data else a.get("prompt") or "")
    if not base.startswith(("http://", "https://")):
        raise ValueError("ASR API 地址无效")
    if not key and not base.startswith(("http://127.0.0.1", "http://localhost")):
        raise ValueError("请先填写 ASR API Key")
    if not model:
        raise ValueError("请先填写 ASR 模型 ID")
    return base, key, model, timeout, language, prompt


def audio_file_for_asr(value: str) -> Tuple[Path, Optional[Path]]:
    value = str(value or "").strip()
    if not value:
        raise ValueError("请提供测试语音路径 / URL")
    tmp_dir: Optional[Path] = None
    if value.startswith("data:"):
        header, _, b64 = value.partition(",")
        if not b64:
            raise ValueError("data URL 无效")
        tmp_dir = Path(tempfile.mkdtemp(prefix="asr_data_"))
        ext = ".wav" if "wav" in header else ".mp3" if "mpeg" in header or "mp3" in header else ".audio"
        path = tmp_dir / ("input" + ext)
        path.write_bytes(base64.b64decode(b64))
        return path, tmp_dir
    if value.startswith(("http://", "https://")):
        raw, meta = fetch_binary_url(value, 25 * 1024 * 1024)
        suffix = Path(urllib.parse.urlparse(value).path).suffix or ".audio"
        tmp_dir = Path(tempfile.mkdtemp(prefix="asr_url_"))
        path = tmp_dir / ("input" + suffix[:12])
        path.write_bytes(raw)
        return path, tmp_dir
    if value.startswith("file://"):
        value = urllib.parse.unquote(urllib.parse.urlparse(value).path)
    path = Path(value).expanduser()
    if not path.exists() or not path.is_file():
        raise ValueError(f"语音文件不存在：{value}")
    if path.stat().st_size <= 0:
        raise ValueError("语音文件为空")
    if path.stat().st_size > 25 * 1024 * 1024:
        raise ValueError(f"语音超过 25MB：{path.stat().st_size} bytes")
    if path.suffix.lower() == ".silk":
        path = convert_silk_to_wav(path)
    return path, None


def multipart_request_json(url: str, fields: Dict[str, str], file_field: str, file_path: Path, key: str, timeout: int) -> Tuple[int, Any, str]:
    boundary = "----WeChatSecondASR" + hashlib.sha1(str(time.time_ns()).encode()).hexdigest()
    chunks = []
    for name, value in fields.items():
        if value is None or str(value) == "":
            continue
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        chunks.append(str(value).encode("utf-8"))
        chunks.append(b"\r\n")
    mime = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
    chunks.append(f"--{boundary}\r\n".encode())
    chunks.append(f'Content-Disposition: form-data; name="{file_field}"; filename="{file_path.name}"\r\n'.encode())
    chunks.append(f"Content-Type: {mime}\r\n\r\n".encode())
    chunks.append(file_path.read_bytes())
    chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Accept": "application/json",
        "User-Agent": "openai-python/1.99.0",
        **auth_headers(key),
    }
    req = urllib.request.Request(url, data=b"".join(chunks), method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(raw), raw
            except ValueError:
                return resp.status, raw, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = raw
        return e.code, parsed, raw


def extract_asr_text(parsed: Any, raw: str) -> str:
    if isinstance(parsed, dict):
        text = str(parsed.get("text") or parsed.get("transcript") or parsed.get("result") or "").strip()
        if text:
            return text
        try:
            return str(parsed["choices"][0]["message"]["content"]).strip()
        except Exception:
            return ""
    if isinstance(parsed, str):
        return parsed.strip()
    return str(raw or "").strip()


def test_asr(data: Dict[str, Any]) -> Dict[str, Any]:
    base, key, model, timeout, language, prompt = asr_credentials(data)
    path, tmp_dir = audio_file_for_asr(str(data.get("audio") or data.get("file") or data.get("url") or ""))
    started = time.monotonic()
    try:
        code, parsed, raw = multipart_request_json(
            base + "/audio/transcriptions",
            {"model": model, "language": language, "prompt": prompt, "response_format": "json"},
            "file",
            path,
            key,
            timeout,
        )
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)
    elapsed = round((time.monotonic() - started) * 1000)
    if code >= 300:
        raise RuntimeError(f"ASR 接口 HTTP {code}: {raw[:1000]}")
    text = extract_asr_text(parsed, raw)
    if not text:
        raise RuntimeError(f"ASR 响应格式不兼容: {raw[:1000]}")
    return {"text": text, "model": model, "latency_ms": elapsed}


def parse_vision_json(text: str) -> Dict[str, Any]:
    raw = str(text or "").strip()
    cleaned = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        obj = json.loads(cleaned)
    except Exception:
        obj = {"summary": raw[:500], "ocr_text": "", "tags": [], "keywords": []}
    tags = [str(x).strip() for x in obj.get("tags", []) if str(x).strip()][:20] if isinstance(obj.get("tags"), list) else []
    keywords = [str(x).strip() for x in obj.get("keywords", []) if str(x).strip()][:30] if isinstance(obj.get("keywords"), list) else []
    return {
        "summary": str(obj.get("summary") or obj.get("image_summary") or "")[:1200],
        "ocr_text": str(obj.get("ocr_text") or obj.get("text") or "")[:4000],
        "tags": tags,
        "keywords": keywords,
        "raw": raw[:2000],
    }


def media_value_to_path(value: str) -> Optional[Path]:
    value = str(value or "").strip()
    if not value or value.startswith(("http://", "https://", "data:")):
        return None
    if value.startswith("file://"):
        value = urllib.parse.unquote(urllib.parse.urlparse(value).path)
    path = Path(value).expanduser()
    return path if path.exists() and path.is_file() else None


def image_data_url(value: str, max_bytes: int = 10 * 1024 * 1024) -> str:
    if str(value or "").startswith("data:image/"):
        return str(value)
    path = media_value_to_path(value)
    if not path:
        return ""
    mime = mimetypes.guess_type(str(path))[0] or "image/jpeg"
    if not mime.startswith("image/"):
        return ""
    raw = path.read_bytes()
    if len(raw) > max_bytes:
        return ""
    return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")


def audio_data_url(value: str, max_bytes: int = 12 * 1024 * 1024) -> str:
    path = media_value_to_path(value)
    if not path:
        return ""
    if path.suffix.lower() == ".silk":
        try:
            path = convert_silk_to_wav(path)
        except Exception:
            return ""
    mime = mimetypes.guess_type(str(path))[0] or ""
    if path.suffix.lower() == ".mp3":
        mime = "audio/mpeg"
    elif path.suffix.lower() in {".wav", ".wave"}:
        mime = "audio/wav"
    if not mime.startswith("audio/"):
        return ""
    raw = path.read_bytes()
    if len(raw) > max_bytes:
        return ""
    return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")


def channel_health_snapshot() -> Dict[str, Any]:
    cfg = read_config()
    with CHANNEL_HEALTH_LOCK:
        cache = {key: dict(value) for key, value in CHANNEL_HEALTH.items()}
    channels = []
    for channel in cfg["channels"]:
        health = cache.get(channel["id"], {})
        channels.append({
            "id": channel["id"],
            "name": channel["name"],
            "enabled": channel["enabled"],
            "active": channel["id"] == cfg["active_channel_id"],
            "status": health.get("status", "unknown"),
            "latency_ms": health.get("latency_ms"),
            "checked_at": health.get("checked_at"),
            "error": health.get("error", ""),
        })
    return {
        "channels": channels,
        "auto_health_check": cfg["auto_health_check"],
        "interval_seconds": cfg["health_check_interval_seconds"],
    }


def test_channel(channel_id: str) -> Dict[str, Any]:
    cfg = read_config()
    channel = next((x for x in cfg["channels"] if x["id"] == channel_id), None)
    if channel is None:
        raise ValueError("渠道不存在")
    checked_at = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        result = test_ai({
            "base_url": channel["base_url"],
            "api_key": channel["api_key"],
            "model": channel["model"],
            "timeout_seconds": channel["timeout_seconds"],
            "prompt": "只回复OK",
        })
        health = {"status": "healthy", "latency_ms": result["latency_ms"], "checked_at": checked_at, "error": ""}
    except Exception as exc:
        health = {"status": "unhealthy", "latency_ms": None, "checked_at": checked_at, "error": str(exc)[:800]}
    with CHANNEL_HEALTH_LOCK:
        CHANNEL_HEALTH[channel_id] = health
    return {"id": channel_id, "name": channel["name"], **health}


def test_all_channels() -> Dict[str, Any]:
    cfg = read_config()
    results = [test_channel(x["id"]) for x in cfg["channels"] if x["enabled"]]
    return {"results": results, "healthy": sum(x["status"] == "healthy" for x in results), "total": len(results)}


def discover_groups() -> Dict[str, Any]:
    cfg = read_config()
    aliases = {str(k): str(v) for k, v in (cfg.get("group_aliases") or {}).items() if str(k).endswith("@chatroom") and str(v).strip()}
    selected = {str(x["id"]): str(x.get("name") or aliases.get(str(x["id"])) or x["id"]) for x in cfg.get("target_groups", []) if isinstance(x, dict) and x.get("id")}
    groups: Dict[str, Dict[str, Any]] = {
        gid: {"id": gid, "name": aliases.get(gid, name), "selected": True, "last_seen": "", "source": "alias" if aliases.get(gid) else "config", "confidence": 95 if aliases.get(gid) else 85}
        for gid, name in selected.items()
    }

    def display_name(group_id: str, candidate: str = "") -> str:
        candidate = str(candidate or "").strip()
        if aliases.get(group_id):
            return aliases[group_id]
        if group_id in selected:
            return selected[group_id]
        if candidate and candidate != group_id:
            return candidate
        return group_id

    def remember(group_id: str, name: str = "", last_seen: str = "", source: str = "event", preview: str = "") -> None:
        if not re.fullmatch(r"\d{6,}@chatroom", group_id):
            return
        item = groups.setdefault(group_id, {"id": group_id, "name": display_name(group_id, name), "selected": False, "last_seen": "", "source": source, "preview": "", "confidence": 40})
        item["name"] = display_name(group_id, name)
        if aliases.get(group_id):
            item["source"] = "alias"
            item["confidence"] = 95
        elif group_id in selected:
            item["source"] = "config"
            item["confidence"] = max(int(item.get("confidence") or 0), 85)
        elif name and name != group_id:
            item["source"] = source
            item["confidence"] = max(int(item.get("confidence") or 0), 70)
        if last_seen:
            item["last_seen"] = last_seen
        if preview:
            item["preview"] = preview[:80]
        if group_id in selected:
            item["selected"] = True

    try:
        file = LOG_FILES["ai"]
        with file.open("rb") as f:
            f.seek(max(0, file.stat().st_size - 2_000_000))
            for raw in f.read().decode("utf-8", "replace").splitlines():
                try:
                    rec = json.loads(raw)
                except ValueError:
                    continue
                if rec.get("msg") != "group_message_seen" or not rec.get("raw_message"):
                    continue
                group_id = str(rec.get("group_id") or "")
                if group_id.endswith("@chatroom"):
                    remember(group_id, str(rec.get("group_name") or ""), str(rec.get("time") or ""), "ai_event", str(rec.get("text") or ""))
    except OSError:
        pass

    chatroom_pattern = re.compile(r"(\d{6,}@chatroom)")
    for file in (LOG_FILES["onebot"], ROOT / "tools" / "onebot" / "onebot" / "log" / "macos.log"):
        try:
            with file.open("rb") as f:
                f.seek(max(0, file.stat().st_size - 3_000_000))
                content = f.read().decode("utf-8", "replace")
            for group_id in chatroom_pattern.findall(content):
                remember(group_id, source="onebot_event")
        except OSError:
            pass

    result = sorted(groups.values(), key=lambda x: (not x["selected"], x["name"] == x["id"], x["name"].lower()))
    return {"groups": result, "count": len(result), "selected_count": sum(x["selected"] for x in result)}


def group_members_catalog(group_id: str) -> Dict[str, Any]:
    group_id = str(group_id or "").strip()
    if not group_id.endswith("@chatroom"):
        raise ValueError("请选择有效的微信群")
    cfg = json_read(CONFIG_PATH, {})
    ignored = set(str(x) for x in (cfg.get("ignored_group_members", {}) or {}).get(group_id, []))
    merged: Dict[str, Dict[str, Any]] = {}
    source_counts: Dict[str, int] = collections.defaultdict(int)

    def readable_name(value: Any, user_id: str) -> str:
        name = str(value or "").strip()
        lower = name.lower()
        if name and name != user_id and not lower.startswith("wxid_") and not lower.endswith("@chatroom"):
            return name
        return ""

    def remember(item: Dict[str, Any], source: str) -> None:
        user_id = str(item.get("user_id") or item.get("id") or "").strip()
        if not user_id or user_id.endswith("@chatroom"):
            return
        nickname = readable_name(item.get("nickname"), user_id)
        card = readable_name(item.get("card"), user_id)
        display_name = readable_name(item.get("display_name") or item.get("name"), user_id)
        name = card or display_name or nickname
        old = merged.get(user_id)
        if old:
            old["nickname"] = old.get("nickname") or nickname
            old["card"] = old.get("card") or card
            if name and (not old.get("name") or old.get("name") == "群友"):
                old["name"] = name
            old["message_count"] = max(int(old.get("message_count") or 0), int(item.get("message_count") or 0))
            old["last_seen"] = max(str(old.get("last_seen") or ""), str(item.get("last_seen") or ""))
            if source not in old["sources"]:
                old["sources"].append(source)
        else:
            merged[user_id] = {
                "user_id": user_id, "name": name or "群友", "nickname": nickname, "card": card,
                "message_count": int(item.get("message_count") or 0),
                "last_seen": str(item.get("last_seen") or ""), "sources": [source],
                "ignored": user_id in ignored,
            }
        source_counts[source] += 1

    onebot_error = ""
    onebot_complete = False
    onebot_self_id = ""
    onebot_api = str(cfg.get("onebot_api") or "http://127.0.0.1:58080").rstrip("/")
    try:
        code, payload, _ = request_json(
            onebot_api + "/get_group_member_list?" + urllib.parse.urlencode({"group_id": group_id}),
            timeout=5,
        )
        if code == 200 and isinstance(payload, dict):
            onebot_self_id = str(payload.get("self_id") or "")
            onebot_rows = payload.get("data") if isinstance(payload.get("data"), list) else payload.get("members", [])
            for item in onebot_rows:
                if isinstance(item, dict):
                    remember(item, "onebot_live")
            onebot_complete = bool(payload.get("complete", False))
        else:
            onebot_error = f"OneBot HTTP {code}"
    except Exception as exc:
        onebot_error = str(exc)

    if not onebot_self_id:
        try:
            health_code, health, _ = request_json(onebot_api + "/health", timeout=3)
            if health_code == 200 and isinstance(health, dict):
                onebot_self_id = str(health.get("self_id") or "")
        except Exception:
            pass
    if not onebot_self_id:
        try:
            with MEMORY.connect() as db:
                row = db.execute(
                    """SELECT json_extract(raw_json,'$.self_id') AS self_id,COUNT(*) AS total
                       FROM messages WHERE group_id=?
                         AND COALESCE(json_extract(raw_json,'$.self_id'),'')!=''
                         AND (COALESCE(json_extract(raw_json,'$.msgsource'),'')!=''
                              OR (message_id!='' AND message_id NOT GLOB '*[^0-9]*'))
                       GROUP BY self_id ORDER BY total DESC LIMIT 1""",
                    (group_id,),
                ).fetchone()
                if row:
                    onebot_self_id = str(row["self_id"] or "")
        except Exception:
            pass
    genuine_memory_ids: set[str] = set()
    if onebot_self_id:
        try:
            with MEMORY.connect() as db:
                genuine_memory_ids = {
                    str(row["user_id"] or "") for row in db.execute(
                        """SELECT DISTINCT user_id FROM messages
                           WHERE group_id=? AND json_extract(raw_json,'$.self_id')=?
                             AND (COALESCE(json_extract(raw_json,'$.msgsource'),'')!=''
                                  OR (message_id!='' AND message_id NOT GLOB '*[^0-9]*'))""",
                        (group_id, onebot_self_id),
                    ).fetchall() if str(row["user_id"] or "")
                }
        except Exception:
            genuine_memory_ids = set()
    for item in MEMORY.members(group_id, 1000):
        user_id = str(item.get("user_id") or "")
        if user_id in genuine_memory_ids or user_id in ignored:
            remember(item, "permanent_memory")
    for user_id in ignored:
        if user_id not in merged:
            remember({"user_id": user_id}, "saved_blacklist")

    # A readable name learned in another group is still better than exposing a wxid.
    for item in merged.values():
        if item["name"] == "群友":
            item["name"] = MEMORY.resolve_member_name(group_id, item["user_id"], "")
    rows = sorted(merged.values(), key=lambda x: (
        not x["ignored"], x["name"] == "群友", -int(x.get("message_count") or 0), x["name"].lower(), x["user_id"]
    ))
    return {
        "group_id": group_id, "items": rows, "count": len(rows), "ignored_ids": sorted(ignored),
        "source_counts": dict(source_counts), "onebot_complete": onebot_complete,
        "onebot_error": onebot_error,
    }


def save_group_member_blacklist(data: Dict[str, Any]) -> Dict[str, Any]:
    group_id = str(data.get("group_id") or "").strip()
    if not group_id.endswith("@chatroom"):
        raise ValueError("请选择有效的微信群")
    raw_ids = data.get("user_ids")
    if not isinstance(raw_ids, list):
        raise ValueError("user_ids 必须是数组")
    user_ids = sorted({str(x).strip() for x in raw_ids if str(x).strip() and not str(x).endswith("@chatroom")})
    if len(user_ids) > 5000:
        raise ValueError("单群黑名单不能超过 5000 人")
    with CONFIG_WRITE_LOCK:
        current = json_read(CONFIG_PATH, {})
        mapping = current.get("ignored_group_members") if isinstance(current.get("ignored_group_members"), dict) else {}
        mapping = {str(k): list(v) for k, v in mapping.items() if str(k).endswith("@chatroom") and isinstance(v, list)}
        if user_ids:
            mapping[group_id] = user_ids
        else:
            mapping.pop(group_id, None)
        current["ignored_group_members"] = mapping
        atomic_write(CONFIG_PATH, json.dumps(current, ensure_ascii=False, indent=2) + "\n")
    hot_reload: Dict[str, Any] = {"applied": False}
    try:
        response = ai_json("/config/reload", "POST", {"config": str(CONFIG_PATH)}, 15)
        hot_reload = {"applied": response.get("status") == "ok", "response": response}
    except Exception as exc:
        hot_reload = {"applied": False, "error": str(exc)}
    return {"group_id": group_id, "ignored_ids": user_ids, "count": len(user_ids),
            "hot_reload": hot_reload, "revision": config_revision()}


def save_group_aliases(data: Dict[str, Any]) -> Dict[str, Any]:
    aliases = data.get("aliases")
    if not isinstance(aliases, dict):
        raise ValueError("aliases 必须是对象")
    current = json_read(CONFIG_PATH, {})
    clean = {str(k): str(v).strip() for k, v in (current.get("group_aliases") or {}).items()
             if str(k).endswith("@chatroom") and str(v).strip()}
    for key, value in aliases.items():
        gid = str(key).strip()
        name = str(value).strip()
        if not gid.endswith("@chatroom"):
            continue
        if name:
            clean[gid] = name[:80]
        else:
            clean.pop(gid, None)
    current["group_aliases"] = clean
    atomic_write(CONFIG_PATH, json.dumps(current, ensure_ascii=False, indent=2) + "\n")
    return discover_groups()


def sync_ui_groups() -> Dict[str, Any]:
    """Read visible second-WeChat UI labels through macOS Accessibility only."""
    script = r'''
tell application "System Events"
  set procNames to name of every process whose bundle identifier is "com.tencent.xinWeChat.instance2"
  if (count of procNames) is 0 then error "第二微信副本未运行"
  set procName to item 1 of procNames
  tell process procName
    set out to {}
    repeat with w in windows
      try
        set end of out to "WINDOW:" & (name of w as text)
        set labels to name of every UI element of w
        repeat with itemName in labels
          if itemName is not missing value and (itemName as text) is not "" then set end of out to itemName as text
        end repeat
      end try
    end repeat
    return out
  end tell
end tell
'''
    try:
        proc = subprocess.run(["/usr/bin/osascript", "-e", script], text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=8)
    except subprocess.TimeoutExpired:
        raise RuntimeError("读取第二微信 UI 超时，请确认第二微信窗口已打开")
    raw = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode:
        if "不允许辅助访问" in raw or "not allowed assistive access" in raw or "-25211" in raw:
            raise RuntimeError("需要开启辅助功能权限：系统设置 → 隐私与安全性 → 辅助功能，允许 Terminal/Codex/osascript 后重试")
        raise RuntimeError(raw.strip() or "读取第二微信 UI 失败")
    names = []
    seen = set()
    for part in re.split(r", |\r?\n", raw):
        name = part.strip()
        if not name or name in seen or name.startswith("WINDOW:") or name.endswith("@chatroom"):
            continue
        if len(name) > 80:
            continue
        seen.add(name)
        names.append(name)
    return {"ok": True, "names": names[:120], "count": len(names), "note": "已只读扫描第二微信窗口文本；如需绑定到群 ID，请在群聊策略中手动保存别名。"}


def visible_wechat2_texts() -> List[str]:
    data = sync_ui_groups()
    return [str(x).strip() for x in data.get("names", []) if str(x).strip()]


def sync_record_transcripts(data: Dict[str, Any]) -> Dict[str, Any]:
    group_id = str(data.get("group_id") or "").strip()
    manual_text = str(data.get("text") or data.get("transcript") or "").strip()
    texts = [manual_text] if manual_text else visible_wechat2_texts()
    noise = {
        "值班群", "图片/语音图库", "发送", "表情", "文件", "截图", "聊天信息", "语音输入",
        "更多", "搜索", "聊天记录", "置顶", "免打扰"
    }
    candidates = []
    for text in texts:
        t = text.strip()
        if t in noise or t.endswith("@chatroom") or len(t) < 2 or len(t) > 120:
            continue
        if re.fullmatch(r"\d+\"|\d{1,2}:\d{2}|\\d+", t):
            continue
        if re.search(r"[\u4e00-\u9fff]", t):
            candidates.append(t)
    rows = MEMORY.media(group_id, "record", limit=10) if group_id else MEMORY.media("", "record", limit=10)
    updated = []
    for row in rows:
        if row.get("ocr_text"):
            continue
        chosen = ""
        # 优先选择含“语音/测试/现在”等明显转写文本；否则用最后一条中文候选。
        for c in candidates:
            if any(k in c for k in ("语音", "测试", "现在", "收到", "回答")):
                chosen = c
        if not chosen and candidates:
            chosen = candidates[-1]
        if not chosen:
            break
        saved = MEMORY.save_media_annotation(
            int(row["id"]),
            chosen,
            f"微信客户端自动转文字：{chosen}",
            "transcribed",
            ["语音泡", "自动转文字"],
            [chosen, "语音", "转文字"],
            "",
        )
        updated.append({"id": saved.get("id"), "text": chosen})
        break
    return {"updated": updated, "candidates": candidates[:20], "count": len(updated), "mode": "manual" if manual_text else "auto_ui"}


def onebot_send_target(target_id: str, message: Any, timeout: int = 25) -> Dict[str, Any]:
    cfg = read_config()
    target_id = str(target_id or "").strip()
    if not target_id:
        raise ValueError("发送目标不能为空")
    is_group = target_id.endswith("@chatroom")
    endpoint = "/send_group_msg" if is_group else "/send_private_msg"
    id_key = "group_id" if is_group else "user_id"
    started = time.monotonic()
    code, parsed, raw = request_json(cfg["onebot_api"].rstrip("/") + endpoint, "POST", {
        id_key: target_id,
        "message": message,
    }, timeout=timeout)
    elapsed = round((time.monotonic() - started) * 1000)
    ok = code < 300 and isinstance(parsed, dict) and parsed.get("status") in {"ok", "success"} and parsed.get("retcode", 0) in {0, None}
    return {"ok": ok, "http_status": code, "latency_ms": elapsed, "response": parsed, "raw": raw[:2000], "target_id": target_id, "endpoint": endpoint}


def onebot_send_message(group_id: str, message: Any, timeout: int = 25) -> Dict[str, Any]:
    return onebot_send_target(group_id, message, timeout=timeout)


def fetch_binary_url(url: str, max_bytes: int) -> Tuple[bytes, Dict[str, Any]]:
    req = urllib.request.Request(url, headers={"User-Agent": "wechat-mac-hook/0.0.1"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        length = resp.headers.get("Content-Length")
        if length and int(length) > max_bytes:
            raise ValueError(f"远程文件过大：{int(length)} bytes，当前上限 {max_bytes} bytes")
        raw = resp.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise ValueError(f"远程文件超过大小上限：{max_bytes} bytes")
        meta = {
            "source": url,
            "size": len(raw),
            "content_type": resp.headers.get("Content-Type", ""),
            "transport": "downloaded_url_base64",
        }
        return raw, meta


def onebot_file_payload(value: str, kind: str, max_bytes: int = 50 * 1024 * 1024) -> Tuple[str, Dict[str, Any]]:
    """Convert local path / URL / data URL into the base64 payload required by current OneBot media sender."""
    value = str(value or "").strip()
    if not value:
        raise ValueError(f"{kind} 消息需要 file/url")
    if value.startswith("base64://"):
        raw = value[len("base64://"):]
        return value, {"source": "base64://…", "size": len(raw) * 3 // 4, "transport": "base64_passthrough"}
    if value.startswith("data:"):
        return value, {"source": "data:…", "size": 0, "transport": "data_url_passthrough"}
    if value.startswith(("http://", "https://")):
        raw, meta = fetch_binary_url(value, max_bytes)
    else:
        if value.startswith("file://"):
            value = urllib.parse.urlparse(value).path
        path = Path(value).expanduser()
        if not path.exists() or not path.is_file():
            raise ValueError(f"媒体文件不存在：{value}")
        size = path.stat().st_size
        if size > max_bytes:
            raise ValueError(f"媒体文件过大：{size} bytes，当前上限 {max_bytes} bytes")
        cache_key = f"{path.resolve()}|{size}|{path.stat().st_mtime_ns}|{kind}"
        with MEDIA_PAYLOAD_CACHE_LOCK:
            cached = MEDIA_PAYLOAD_CACHE.get(cache_key)
            if cached:
                MEDIA_PAYLOAD_CACHE.move_to_end(cache_key)
                return cached[0], dict(cached[1])
        raw = path.read_bytes()
        meta = {
            "source": str(path),
            "size": len(raw),
            "content_type": mimetypes.guess_type(str(path))[0] or "",
            "transport": "local_file_base64",
        }
    meta["sha256"] = hashlib.sha256(raw).hexdigest()[:16]
    meta["kind"] = kind
    payload = "base64://" + base64.b64encode(raw).decode("ascii")
    if "cache_key" in locals():
        with MEDIA_PAYLOAD_CACHE_LOCK:
            MEDIA_PAYLOAD_CACHE[cache_key] = (payload, dict(meta))
            while len(MEDIA_PAYLOAD_CACHE) > 32:
                MEDIA_PAYLOAD_CACHE.popitem(last=False)
    return payload, meta


def go_bin() -> Path:
    bundled = ROOT / "tools" / "runtime" / "go125" / "bin" / "go"
    if bundled.exists():
        return bundled
    found = shutil.which("go")
    if found:
        return Path(found)
    raise RuntimeError("缺少 Go 运行时，无法构建 silk_to_wav 工具")


def ensure_silk_to_wav() -> Path:
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    binary = BIN_DIR / "silk_to_wav"
    src = ROOT / "tools" / "silk_to_wav.go"
    if binary.exists() and binary.stat().st_mtime >= src.stat().st_mtime:
        return binary
    mod_dir = ROOT / "external" / "wechat_chatter" / "onebot"
    proc = subprocess.run(
        [str(go_bin()), "build", "-o", str(binary), str(src)],
        cwd=str(mod_dir),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=90,
    )
    if proc.returncode:
        raise RuntimeError("构建 silk_to_wav 失败：" + proc.stdout[-2000:])
    return binary


def convert_silk_to_wav(path: Path) -> Path:
    path = Path(path).expanduser()
    if not path.exists():
        raise ValueError(f"silk 文件不存在：{path}")
    VOICE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    sig = hashlib.sha1(f"{path}:{path.stat().st_mtime_ns}:{path.stat().st_size}".encode("utf-8", "ignore")).hexdigest()[:16]
    out = VOICE_CACHE_DIR / f"{path.stem[:60]}-{sig}.wav"
    if out.exists() and out.stat().st_size > 44:
        return out
    binary = ensure_silk_to_wav()
    proc = subprocess.run([str(binary), str(path), str(out)], text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, timeout=30)
    if proc.returncode or not out.exists():
        raise RuntimeError("SILK 转 WAV 失败：" + proc.stdout[-1200:])
    return out


def clean_voice_title(name: str) -> str:
    title = Path(name).stem
    title = re.sub(r"[_-]?音频(?:[_-]?\d+)?$", "", title, flags=re.I)
    # PDD 等语音包常把分段编号追加到文件名末尾；它不是实际文案。
    title = re.sub(r"p\d+\s*[_-]?\s*\d*$", "", title, flags=re.I)
    title = re.sub(r"[_\s]+", " ", title).strip(" ._-")
    return title or Path(name).stem


def normalize_existing_voice_titles() -> int:
    """Apply the same filename rule to entries imported by older versions."""
    changed = 0
    with MEMORY.connect() as db:
        rows = db.execute("SELECT id, title, text FROM voice_items").fetchall()
        for row in rows:
            title = clean_voice_title(str(row["title"] or ""))
            text = clean_voice_title(str(row["text"] or "")) if str(row["text"] or "").strip() else title
            if title and (title != row["title"] or text != row["text"]):
                db.execute("UPDATE voice_items SET title=?, text=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (title, text, int(row["id"])))
                changed += 1
    return changed


def safe_voice_name(name: str) -> str:
    name = re.sub(r"[\x00/\\:]+", "_", name).strip()
    return name[:120] or "voice"


def _voice_files(path: Path) -> List[Path]:
    return sorted(
        (f for f in path.rglob("*") if f.is_file() and f.suffix.lower() in VOICE_EXTENSIONS),
        key=lambda f: str(f).lower(),
    )


def _iter_archive_files(path: Path, category: str) -> Iterable[Tuple[Path, str, str]]:
    """Yield files from one archive and remove its temporary extraction directory."""
    suffix = path.suffix.lower()
    tmp_root = Path(tempfile.mkdtemp(prefix="voice_zip1_")) if suffix == ".zip1" else None
    tmp_zip = path
    if tmp_root:
        tmp_zip = tmp_root / (path.stem + ".zip")
        shutil.copy2(path, tmp_zip)
    extract_dir = Path(tempfile.mkdtemp(prefix="voice_pack_"))
    try:
        with zipfile.ZipFile(tmp_zip) as zf:
            base = extract_dir.resolve()
            for info in zf.infolist():
                target = (extract_dir / info.filename).resolve()
                if target != base and base not in target.parents:
                    raise ValueError(f"压缩包包含非法路径：{info.filename}")
                zf.extract(info, extract_dir)
        for f in _voice_files(extract_dir):
            yield f, category, path.stem.strip() or path.name
    finally:
        shutil.rmtree(extract_dir, ignore_errors=True)
        if tmp_root:
            shutil.rmtree(tmp_root, ignore_errors=True)


def iter_voice_source_files(path: Path) -> Iterable[Tuple[Path, str, str]]:
    """Yield (file_path, category, pack_name) using the import rules shown in UI."""
    path = Path(path).expanduser()
    if not path.exists():
        raise ValueError(f"路径不存在：{path}")
    if path.is_file() and path.suffix.lower() in VOICE_ARCHIVE_EXTENSIONS:
        yield from _iter_archive_files(path, path.parent.name.strip() or "默认")
        return
    if path.is_file() and path.suffix.lower() in VOICE_EXTENSIONS:
        pack_name = path.parent.name.strip() or "默认"
        yield path, path.parent.parent.name.strip() or pack_name, pack_name
        return
    if not path.is_dir():
        raise ValueError(f"不支持的语音包格式：{path.name}")

    category = path.name.strip() or path.parent.name.strip() or "默认"
    direct_files = sorted(
        (f for f in path.iterdir() if f.is_file() and f.suffix.lower() in VOICE_EXTENSIONS),
        key=lambda f: str(f).lower(),
    )
    if direct_files:
        for f in direct_files:
            yield f, category, path.name.strip() or category
        return

    children = sorted(path.iterdir(), key=lambda f: f.name.lower())
    source_children = [
        child for child in children
        if (child.is_file() and child.suffix.lower() in VOICE_ARCHIVE_EXTENSIONS)
        or (child.is_dir() and _voice_files(child))
    ]
    # 单个嵌套目录通常只是压缩包解开后的包装目录，仍归入当前语音包。
    if len(source_children) == 1 and source_children[0].is_dir():
        pack_name = path.name.strip() or category
        for f in _voice_files(source_children[0]):
            yield f, category, pack_name
        return

    for child in source_children:
        if child.is_file():
            yield from _iter_archive_files(child, category)
        else:
            pack_name = child.name.strip() or category
            for f in _voice_files(child):
                yield f, category, pack_name


def _voice_import_paths(data: Dict[str, Any]) -> List[str]:
    raw_paths = data.get("paths")
    if not isinstance(raw_paths, list):
        one = str(data.get("path") or "").strip()
        raw_paths = [one] if one else []
    return [str(x).strip() for x in raw_paths if str(x).strip()]


def _voice_import_target(data: Dict[str, Any]) -> Dict[str, Any]:
    target_pack_id = int(data.get("target_pack_id") or 0)
    if target_pack_id <= 0:
        return {}
    pack = MEMORY.voice_pack(target_pack_id)
    if not pack:
        raise ValueError("要追加的语音包不存在，请刷新列表后重试")
    return pack


def voicepack_import_plan(data: Dict[str, Any]) -> Dict[str, Any]:
    raw_paths = _voice_import_paths(data)
    if not raw_paths:
        raise ValueError("请填写语音包总目录、zip/zip1 或单个 silk 文件路径")
    grouped: Dict[str, Dict[str, Any]] = {}
    errors: List[Dict[str, str]] = []
    target_pack = _voice_import_target(data)
    for raw_path in raw_paths:
        try:
            for src, category, pack_name in iter_voice_source_files(Path(raw_path)):
                category = str(target_pack.get("category") or data.get("category") or category or "默认").strip()
                pack_name = str(target_pack.get("name") or pack_name or category).strip()
                key = f"{category}/{pack_name}"
                group = grouped.setdefault(key, {"category": category, "pack_name": pack_name, "count": 0, "samples": []})
                group["count"] += 1
                if len(group["samples"]) < 4:
                    group["samples"].append(clean_voice_title(src.name))
        except Exception as exc:
            errors.append({"path": str(raw_path), "error": str(exc)})
    groups = sorted(grouped.values(), key=lambda x: (x["category"].lower(), x["pack_name"].lower()))
    return {"sources": raw_paths, "groups": groups, "total": sum(x["count"] for x in groups),
            "errors": errors, "target_pack": target_pack}


def import_voice_paths(data: Dict[str, Any]) -> Dict[str, Any]:
    raw_paths = _voice_import_paths(data)
    if not raw_paths:
        raise ValueError("请填写语音包总目录、zip/zip1 或单个 silk 文件路径")
    VOICE_PACK_DIR.mkdir(parents=True, exist_ok=True)
    imported = 0
    skipped = 0
    packs: Dict[str, Dict[str, Any]] = {}
    samples = []
    errors = []
    target_pack = _voice_import_target(data)
    existing_digests = {
        hashlib.sha1(f.read_bytes()).hexdigest()
        for f in VOICE_PACK_DIR.rglob("*")
        if f.is_file() and f.suffix.lower() in VOICE_EXTENSIONS
    }
    for raw_path in raw_paths:
        try:
            source_path = Path(str(raw_path)).expanduser()
            for src, category, pack_name in iter_voice_source_files(source_path):
                category = str(target_pack.get("category") or data.get("category") or category or pack_name or "默认").strip()
                pack_name = str(target_pack.get("name") or pack_name or category).strip()
                pack_id = int(target_pack.get("id") or 0) or MEMORY.upsert_voice_pack(pack_name, category, str(source_path))
                pack_key = f"{category}/{pack_name}"
                pack_row = packs.setdefault(pack_key, {
                    "category": category,
                    "pack_name": pack_name,
                    "pack_id": pack_id,
                    "imported": 0,
                    "skipped": 0,
                })
                ext = src.suffix.lower()
                title = clean_voice_title(src.name)
                # 使用音频内容做指纹，避免 zip/zip1 每次解压到随机临时目录后重复入库。
                source_digest = hashlib.sha1(src.read_bytes()).hexdigest()
                if source_digest in existing_digests:
                    skipped += 1
                    pack_row["skipped"] += 1
                    continue
                digest = source_digest[:12]
                dest_dir = VOICE_PACK_DIR / safe_voice_name(category) / safe_voice_name(pack_name)
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest = dest_dir / f"{safe_voice_name(title)}-{digest}{ext}"
                if not dest.exists():
                    shutil.copy2(src, dest)
                existing_digests.add(source_digest)
                duration_ms = 0
                if ext == ".silk":
                    try:
                        wav = convert_silk_to_wav(dest)
                        duration_ms = max(0, int((wav.stat().st_size - 44) * 1000 / (16000 * 2)))
                    except Exception:
                        duration_ms = 0
                ok = MEMORY.add_voice_item(pack_id, category, title, title, str(dest), ext.removeprefix("."), dest.stat().st_size, duration_ms, [category, pack_name])
                if ok:
                    imported += 1
                    pack_row["imported"] += 1
                    if len(samples) < 12:
                        samples.append({"title": title, "category": category, "file": str(dest), "duration_ms": duration_ms})
                else:
                    skipped += 1
                    pack_row["skipped"] += 1
        except Exception as exc:
            errors.append({"path": str(raw_path), "error": str(exc)})
    return {
        "imported": imported,
        "skipped": skipped,
        "packs": len(packs),
        "pack_summary": list(packs.values()),
        "samples": samples,
        "errors": errors,
        "target_pack": target_pack,
    }


def _delete_managed_voice_files(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    root = VOICE_PACK_DIR.resolve()
    deleted = 0
    errors: List[Dict[str, str]] = []
    for item in items:
        path = media_value_to_path(str(item.get("file") or ""))
        if not path:
            continue
        try:
            resolved = path.resolve()
            if resolved != root and root not in resolved.parents:
                continue
            if resolved.exists() and resolved.is_file():
                resolved.unlink()
                deleted += 1
            for cached in VOICE_CACHE_DIR.glob(f"{resolved.stem[:60]}-*.wav"):
                cached.unlink(missing_ok=True)
            parent = resolved.parent
            while parent != root and root in parent.parents:
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent
        except Exception as exc:
            errors.append({"file": str(path), "error": str(exc)})
    return {"files_deleted": deleted, "file_errors": errors}


def voicepack_delete_item(data: Dict[str, Any]) -> Dict[str, Any]:
    item_id = int(data.get("id") or data.get("item_id") or 0)
    if item_id <= 0:
        raise ValueError("需要语音条目 ID")
    result = MEMORY.delete_voice_item(item_id)
    if not result.get("deleted"):
        raise ValueError("语音条目不存在或已经删除")
    cleanup = _delete_managed_voice_files(result.get("items") or []) if data.get("delete_file", True) else {
        "files_deleted": 0, "file_errors": []}
    return {**result, **cleanup}


def voicepack_delete_pack(data: Dict[str, Any]) -> Dict[str, Any]:
    pack_id = int(data.get("pack_id") or data.get("id") or 0)
    if pack_id <= 0:
        raise ValueError("需要语音包 ID")
    result = MEMORY.delete_voice_pack(pack_id)
    if not result.get("pack"):
        raise ValueError("语音包不存在或已经删除")
    cleanup = _delete_managed_voice_files(result.get("items") or []) if data.get("delete_files", True) else {
        "files_deleted": 0, "file_errors": []}
    return {**result, **cleanup}


def voicepacks_list(data: Dict[str, Any]) -> Dict[str, Any]:
    category = str(data.get("category") or "").strip()
    pack_id = int(data.get("pack_id") or 0)
    query = str(data.get("query") or "").strip()
    limit = int(data.get("limit") or 240)
    all_rows = MEMORY.search_voice_items(query, category, pack_id, 50000) if query else MEMORY.voice_items(
        category=category, pack_id=pack_id, limit=50000
    )
    matched_count = len(all_rows)
    rows = all_rows[:max(1, min(50000, limit))]
    for row in rows:
        try:
            row["tags"] = json.loads(row.get("tags_json") or "[]")
        except Exception:
            row["tags"] = []
    packs = MEMORY.voice_packs()
    categories: Dict[str, Dict[str, Any]] = {}
    for pack in packs:
        name = str(pack.get("category") or "未分类")
        entry = categories.setdefault(name, {"name": name, "pack_count": 0, "item_count": 0})
        entry["pack_count"] += 1
        entry["item_count"] += int(pack.get("item_count") or 0)
    category_rows = sorted(categories.values(), key=lambda x: str(x["name"]).lower())
    for index, row in enumerate(packs, 1):
        row["sequence"] = index
    for index, row in enumerate(rows, 1):
        row["list_index"] = index
    total_items = sum(int(pack.get("item_count") or 0) for pack in packs)
    return {
        "packs": packs,
        "items": rows,
        "count": len(rows),
        "matched_count": matched_count,
        "stats": {"total_packs": len(packs), "total_items": total_items, "categories": category_rows},
        "filters": {"category": category, "pack_id": pack_id, "query": query},
    }


def voicepack_recommend(data: Dict[str, Any]) -> Dict[str, Any]:
    query = str(data.get("query") or "").strip()
    if not query:
        raise ValueError("请输入想表达的内容或回复意图")
    category = str(data.get("category") or "").strip()
    pack_id = int(data.get("pack_id") or 0)
    limit = max(1, min(12, int(data.get("limit") or 8)))
    candidates = MEMORY.search_voice_items(query, category, pack_id, limit)
    for row in candidates:
        try:
            row["tags"] = json.loads(row.get("tags_json") or "[]")
        except Exception:
            row["tags"] = []
    return {"query": query, "candidates": candidates, "count": len(candidates)}


def voicepack_audio(item_id: int) -> Tuple[bytes, str, str]:
    item = MEMORY.voice_item(int(item_id))
    if not item:
        raise ValueError("语音条目不存在")
    path = media_value_to_path(str(item.get("file") or ""))
    if not path:
        raise ValueError("语音文件不存在")
    if path.suffix.lower() == ".silk":
        path = convert_silk_to_wav(path)
    mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    if path.suffix.lower() in {".wav", ".wave"}:
        mime = "audio/wav"
    elif path.suffix.lower() == ".mp3":
        mime = "audio/mpeg"
    return path.read_bytes(), mime, path.name


def voicepack_send(data: Dict[str, Any]) -> Dict[str, Any]:
    item_id = int(data.get("id") or data.get("item_id") or 0)
    group_id = str(data.get("group_id") or "").strip()
    if item_id <= 0:
        raise ValueError("需要语音条目 ID")
    if not group_id.endswith("@chatroom"):
        raise ValueError("需要目标群")
    item = MEMORY.voice_item(item_id)
    if not item:
        raise ValueError("语音条目不存在")
    try:
        result = send_message({"group_id": group_id, "type": "record", "file": item["file"]})
    except Exception as exc:
        if "媒体上传通道未初始化" not in str(exc):
            raise
        ensure_media_channel_ready("voicepack_send")
        result = send_message({"group_id": group_id, "type": "record", "file": item["file"]})
    event_id = f"voicepack|{group_id}|{item_id}|{time.time_ns()}"
    title = str(item.get("text") or item.get("title") or "")
    try:
        inserted = MEMORY.add_message({
            "event_id": event_id,
            "trace_id": "voicepack-" + uuid.uuid4().hex[:10],
            "direction": "outgoing",
            "group_id": group_id,
            "group_name": read_config().get("group_aliases", {}).get(group_id, group_id),
            "user_id": "AI",
            "sender_name": "AI语音包",
            "message_id": "",
            "event_time": int(time.time()),
            # 这是数据库里的发送记录，不把内部标记写回上下文，避免模型照抄
            # “[语音包]”并把它当成普通文字发到群里。
            "text": f"语音内容：{title}",
            "raw_message": f"语音内容：{title}",
            "segments": [{"type": "record", "data": {"file": str(item["file"]), "text": title}}],
            "raw": {"voicepack_item": item, "send": result},
            "source": "voicepack_send",
            "selected": True,
        })
        if inserted:
            for media in MEMORY.media_by_event(event_id):
                MEMORY.save_media_annotation(
                    int(media["id"]),
                    title,
                    f"语音包发送：{title}",
                    "transcribed",
                    ["语音包", str(item.get("category") or "")],
                    [title, "语音", str(item.get("pack_name") or "")],
                    "",
                )
    except Exception as exc:
        print(json.dumps({"level": "warning", "msg": "voicepack_memory_persist_failed", "error": str(exc), "group_id": group_id, "item_id": item_id}, ensure_ascii=False))
    MEMORY.mark_voice_used(item_id)
    return {"item": MEMORY.voice_item(item_id), "send": result}


def face_md5(row: Dict[str, Any]) -> str:
    raw = str(row.get("meta_json") or "") + "\n" + str(row.get("raw_message") or "")
    m = re.search(r'md5=\\"([0-9a-fA-F]{16,64})\\"|md5="([0-9a-fA-F]{16,64})"|md5=\\\\?"([0-9a-fA-F]{16,64})', raw)
    if m:
        return next(x for x in m.groups() if x)
    path = media_value_to_path(str(row.get("file") or row.get("url") or ""))
    if path:
        try:
            return hashlib.sha1(path.read_bytes()).hexdigest()
        except Exception:
            pass
    return hashlib.sha1(str(row.get("file") or row.get("id") or "").encode("utf-8", "ignore")).hexdigest()


def is_face_row(row: Dict[str, Any]) -> bool:
    meta = str(row.get("meta_json") or "")
    raw = str(row.get("raw_message") or "")
    return '"type": "face"' in meta or "'type': 'face'" in meta or "<emoji" in raw or "<emoji" in meta


def faces_list(data: Dict[str, Any]) -> Dict[str, Any]:
    group_id = str(data.get("group_id") or "").strip()
    query = str(data.get("query") or "").strip()
    global_shared = bool(data.get("global_shared", True))
    if not MEMORY.face_asset_count():
        MEMORY.rebuild_face_assets()
    rows = MEMORY.search_face_assets(query, group_id, 500, global_shared=global_shared, include_disabled=True)
    out = []
    for row in rows:
        try:
            row["tags"] = json.loads(row.get("tags_json") or "[]")
        except Exception:
            row["tags"] = []
        try:
            row["keywords"] = json.loads(row.get("keywords_json") or "[]")
        except Exception:
            row["keywords"] = []
        for source, target in (("aliases_json", "aliases"), ("emotions_json", "emotions"),
                               ("intents_json", "intents"), ("actions_json", "actions"),
                               ("subjects_json", "subjects")):
            try:
                row[target] = json.loads(row.get(source) or "[]")
            except Exception:
                row[target] = []
        row["data_url"] = image_data_url(str(row.get("file") or row.get("url") or ""))
        out.append(row)
    return {"items": out, "count": len(out)}


def face_send(data: Dict[str, Any]) -> Dict[str, Any]:
    face_id = int(data.get("face_id") or 0)
    media_id = int(data.get("id") or data.get("media_id") or 0)
    group_id = str(data.get("group_id") or "").strip()
    if face_id <= 0 and media_id <= 0:
        raise ValueError("需要表情包 ID")
    if not group_id.endswith("@chatroom"):
        raise ValueError("需要目标群")
    item = MEMORY.face_asset(face_id) if face_id > 0 else {}
    if not item and media_id > 0:
        item = MEMORY.sync_face_asset(media_id) or MEMORY.media_detail(media_id)
    if not item:
        raise ValueError("表情包不存在")
    file_value = str(item.get("file") or item.get("url") or "")
    if not file_value:
        raise ValueError("表情包没有本地文件")
    path = media_value_to_path(file_value)
    if not path or not path.exists() or not path.is_file():
        raise ValueError("表情包本地文件不存在")
    if path.stat().st_size <= 0 or path.stat().st_size > 25 * 1024 * 1024:
        raise ValueError(f"表情包文件大小异常：{path.stat().st_size} bytes")
    if not media_channel_ready():
        repair = ensure_media_channel_ready("face_send_preflight")
        if not repair.get("ready") and not media_channel_ready():
            raise RuntimeError("表情发送前 UploadMedia 通道未就绪")
    trace_id = str(data.get("trace_id") or ("face-" + uuid.uuid4().hex[:12]))
    started = time.monotonic()
    state = "failed"
    result: Dict[str, Any] = {}
    with FACE_SEND_LOCK:
        offsets = {str(log_path): log_path.stat().st_size for log_path in LOG_FILES.values() if log_path.exists()}
        try:
            result = send_message({
                "group_id": group_id, "type": "image", "file": file_value,
                "_send_timeout": 15, "_retry_media": False, "_force_media_send": True,
            })
            state = "sent"
        except (TimeoutError, socket.timeout, urllib.error.URLError) as exc:
            deadline = time.monotonic() + 5
            confirmed = False
            while time.monotonic() < deadline and not confirmed:
                for log_path in LOG_FILES.values():
                    if not log_path.exists():
                        continue
                    try:
                        with log_path.open("rb") as handle:
                            handle.seek(min(offsets.get(str(log_path), 0), log_path.stat().st_size))
                            tail = handle.read().decode("utf-8", "replace")
                        if "buf2resp: 收到响应, msgType=img" in tail or "发送图片消息成功" in tail:
                            confirmed = True
                            break
                    except OSError:
                        continue
                if not confirmed:
                    time.sleep(0.25)
            if confirmed:
                state = "timeout_confirmed"
                result = {"ok": True, "latency_ms": round((time.monotonic() - started) * 1000), "timeout": str(exc)}
            else:
                MEMORY.mark_face_used(int(item.get("id") or face_id), False)
                raise RuntimeError("表情发送 15 秒超时，5 秒内未收到 OneBot 回调确认") from exc
        except Exception:
            MEMORY.mark_face_used(int(item.get("id") or face_id), False)
            raise
    MEMORY.mark_face_used(int(item.get("id") or face_id), True)
    return {
        "state": state, "trace_id": trace_id, "query": str(data.get("query") or ""),
        "reason": str(data.get("reason") or ""), "elapsed_ms": round((time.monotonic() - started) * 1000),
        "item": item, "send": result,
    }


def face_update(data: Dict[str, Any]) -> Dict[str, Any]:
    face_id = int(data.get("face_id") or data.get("id") or 0)
    if face_id <= 0:
        raise ValueError("需要表情资产 ID")
    values = {key: data[key] for key in (
        "ocr_text", "image_summary", "tags_json", "keywords_json", "aliases_json",
        "emotions_json", "intents_json", "actions_json", "subjects_json", "enabled",
    ) if key in data}
    item = MEMORY.update_face_asset(face_id, values)
    if not item:
        raise ValueError("表情资产不存在")
    group_id = str(data.get("group_id") or "").strip()
    if group_id and "group_enabled" in data:
        MEMORY.set_face_group_enabled(face_id, group_id, bool(data.get("group_enabled")))
    return item


def face_reindex(data: Dict[str, Any]) -> Dict[str, Any]:
    return MEMORY.rebuild_face_assets()


def media_reply_test(data: Dict[str, Any]) -> Dict[str, Any]:
    group_id = str(data.get("group_id") or "").strip()
    query = str(data.get("query") or "").strip()
    if not query:
        raise ValueError("请输入需要测试的群聊上下文")
    faces = MEMORY.search_face_assets(query, group_id, 8, global_shared=True)
    voices = MEMORY.search_voice_items(query, limit=8)
    return {
        "query": query, "group_id": group_id,
        "faces": [{"id": row.get("id"), "summary": row.get("image_summary"), "ocr_text": row.get("ocr_text"),
                   "score": row.get("match_score"), "reason": row.get("match_reason")} for row in faces],
        "voices": [{"id": row.get("id"), "title": row.get("title"), "score": row.get("match_score"),
                    "reason": row.get("match_reason")} for row in voices],
    }


def sanitize_message_segments(message: Any) -> Any:
    if not isinstance(message, list):
        return message
    out = []
    for seg in message:
        item = json.loads(json.dumps(seg, ensure_ascii=False))
        data = item.get("data") if isinstance(item, dict) else None
        if isinstance(data, dict) and isinstance(data.get("file"), str) and (
            data["file"].startswith("base64://") or data["file"].startswith("data:")
        ):
            raw = data["file"]
            data["file"] = raw[:32] + f"…({len(raw)} chars)"
        out.append(item)
    return out


def build_message_segments(data: Dict[str, Any]) -> Tuple[str, Any, Dict[str, Any]]:
    group_id = str(data.get("group_id", "")).strip()
    user_id = str(data.get("user_id") or data.get("target_id") or "").strip()
    target_id = group_id or user_id
    if not target_id:
        raise ValueError("需要有效 group_id 或 user_id")
    is_group = target_id.endswith("@chatroom")
    kind = str(data.get("type") or "text").strip().lower()
    text = str(data.get("text") or "")
    if kind == "text":
        if not text:
            raise ValueError("文本不能为空")
        return target_id, [{"type": "text", "data": {"text": text}}], {}
    if kind == "at":
        if not is_group:
            raise ValueError("@ 消息只能发送到群")
        user_id = str(data.get("user_id") or data.get("qq") or "").strip()
        if not user_id:
            raise ValueError("@ 消息需要 user_id")
        return target_id, [{"type": "at", "data": {"qq": user_id}}, {"type": "text", "data": {"text": " " + text}}], {}
    if kind == "reply":
        if not is_group:
            raise ValueError("引用回复只能发送到群")
        message_id = str(data.get("message_id") or "").strip()
        if not message_id:
            raise ValueError("引用回复需要 message_id")
        return target_id, [{"type": "reply", "data": {"id": message_id}}, {"type": "text", "data": {"text": text}}], {}
    if kind in {"image", "file", "video", "record"}:
        file_value = str(data.get("file") or data.get("url") or "").strip()
        if not file_value:
            raise ValueError(f"{kind} 消息需要 file/url")
        payload, meta = onebot_file_payload(file_value, kind)
        return target_id, [{"type": kind, "data": {"file": payload}}], {"media": meta}
    raise ValueError("消息类型仅支持 text/at/reply/image/file/video/record")


def send_message(data: Dict[str, Any]) -> Dict[str, Any]:
    target_id, message, meta = build_message_segments(data)
    kind = str(data.get("type") or "text").strip().lower()
    if kind in {"image", "video", "record"} and not bool(data.get("_force_media_send")) and not status()["onebot"].get("media_upload_ready"):
        repair = ensure_media_channel_ready(f"send_{kind}")
        if not repair.get("ready") and not media_channel_ready():
            raise RuntimeError("媒体上传通道未初始化：已自动向文件传输助手发送极小占位图尝试修复，但通道仍未确认就绪。")
    # GIF/大图/语音需要先 UploadMedia 再 send，实际可能超过 25s。
    # 等待时间过短会造成“微信已发出但后台显示 timeout”的假失败。
    send_timeout = max(3, min(120, int(data.get("_send_timeout") or (90 if kind in {"image", "video", "record"} else 25))))
    result = onebot_send_target(target_id, message, timeout=send_timeout)
    retry_media = bool(data.get("_retry_media", True))
    if not result["ok"] and retry_media and kind in {"image", "video", "record"} and not bool(data.get("_force_media_send")):
        try:
            warm_media_channel(f"retry_after_send_failure:{kind}", force=True)
        except Exception:
            pass
        result = onebot_send_target(target_id, message, timeout=send_timeout)
    if not result["ok"]:
        raise RuntimeError(f"OneBot 发送失败: {result['raw']}")
    key = "group_id" if target_id.endswith("@chatroom") else "user_id"
    return {key: target_id, "target_id": target_id, "message": sanitize_message_segments(message), **meta, **result}


def media_auto_repair_config() -> Dict[str, Any]:
    cfg = read_config().get("media_auto_repair", {})
    if not isinstance(cfg, dict):
        cfg = {}
    return {
        "enabled": bool(cfg.get("enabled", True)),
        "filehelper_id": str(cfg.get("filehelper_id") or "filehelper").strip() or "filehelper",
        "cooldown_seconds": max(5, int(cfg.get("cooldown_seconds", 45))),
        "poll_seconds": max(1, min(30, int(cfg.get("poll_seconds", 8)))),
    }


def media_channel_ready() -> bool:
    try:
        return bool(status()["onebot"].get("media_upload_ready"))
    except Exception:
        return False


def warm_media_channel(reason: str = "manual", force: bool = False) -> Dict[str, Any]:
    """Send a tiny private image to filehelper to wake/verify UploadMedia.

    This is intentionally private-only by default: the repair probe never goes
    to a chatroom, so normal groups remain clean while the hook gets the same
    UploadMedia path required by image/video/voice sends.
    """
    cfg = media_auto_repair_config()
    now = time.time()
    if not cfg["enabled"] and not force:
        return {"started": False, "skipped": "disabled", "ready": media_channel_ready()}
    with MEDIA_REPAIR_LOCK:
        if MEDIA_REPAIR_STATE.get("running"):
            return {"started": False, "skipped": "already_running", **MEDIA_REPAIR_STATE}
        if not force and now - float(MEDIA_REPAIR_STATE.get("last_attempt") or 0) < cfg["cooldown_seconds"]:
            return {"started": False, "skipped": "cooldown", **MEDIA_REPAIR_STATE}
        MEDIA_REPAIR_STATE.update({"running": True, "last_attempt": now, "last_error": ""})
    try:
        before = status()
        if before["onebot"].get("media_upload_ready"):
            with MEDIA_REPAIR_LOCK:
                MEDIA_REPAIR_STATE.update({"running": False, "last_success": now, "last_error": ""})
            return {"started": False, "skipped": "already_ready", "ready": True}
        if not before["onebot"].get("send_ready"):
            raise RuntimeError("文本发送 Hook 尚未就绪，无法自动暖起媒体通道")
        target = cfg["filehelper_id"]
        result = send_message({
            "user_id": target,
            "type": "image",
            "file": TINY_PLACEHOLDER_PNG,
            "_force_media_send": True,
        })
        deadline = time.time() + cfg["poll_seconds"]
        ready = False
        last_status: Dict[str, Any] = {}
        while time.time() < deadline:
            time.sleep(0.5)
            last_status = status()
            if last_status["onebot"].get("media_upload_ready"):
                ready = True
                break
        if not ready:
            ready = media_channel_ready()
        with MEDIA_REPAIR_LOCK:
            MEDIA_REPAIR_STATE.update({
                "running": False,
                "last_success": time.time() if ready else float(MEDIA_REPAIR_STATE.get("last_success") or 0),
                "last_error": "" if ready else "占位图已发送但媒体通道状态仍未确认",
                "last_reason": reason,
                "last_target": target,
            })
        return {"started": True, "ready": ready, "target": target, "send": result, "status": last_status.get("onebot", {})}
    except Exception as exc:
        with MEDIA_REPAIR_LOCK:
            MEDIA_REPAIR_STATE.update({"running": False, "last_error": str(exc)[:500], "last_reason": reason})
        raise


def ensure_media_channel_ready(reason: str = "send_media") -> Dict[str, Any]:
    if media_channel_ready():
        return {"ready": True, "skipped": "already_ready"}
    return warm_media_channel(reason=reason, force=True)


def send_probe(data: Dict[str, Any]) -> Dict[str, Any]:
    group_id = str(data.get("group_id", "")).strip()
    cfg = read_config()
    allowed = {str(x.get("id")) for x in cfg.get("target_groups", []) if isinstance(x, dict)}
    if group_id not in allowed:
        raise ValueError("发送探针只允许选择已授权群")
    trace_id = "probe-" + uuid.uuid4().hex[:12]
    text = str(data.get("text") or f"链路诊断探针 {trace_id}").strip()
    result = onebot_send_message(group_id, [{"type": "text", "data": {"text": text}}])
    return {"trace_id": trace_id, "group_id": group_id, **result}


def recover_onebot(data: Dict[str, Any]) -> Dict[str, Any]:
    outputs = []
    for script_name in (ACTION_SCRIPTS["stop_onebot"], ACTION_SCRIPTS["start_onebot"]):
        path = ROOT / "scripts" / script_name
        proc = subprocess.run([str(path)], cwd=str(ROOT), text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=90)
        outputs.append(f"$ {script_name}\n{proc.stdout.strip()}")
        if proc.returncode:
            raise RuntimeError("\n\n".join(outputs))
    time.sleep(1.5)
    st = status()
    if data.get("restart_wechat2") and not st["onebot"].get("send_ready"):
        env = os.environ.copy()
        env["WECHAT2_RESTART"] = "1"
        path = ROOT / "scripts" / ACTION_SCRIPTS["launch_wechat2"]
        proc = subprocess.run([str(path)], cwd=str(ROOT), env=env, text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=90)
        outputs.append(f"$ WECHAT2_RESTART=1 {ACTION_SCRIPTS['launch_wechat2']}\n{proc.stdout.strip()}")
        if proc.returncode:
            raise RuntimeError("\n\n".join(outputs))
        st = status()
    return {"output": "\n\n".join(outputs), "status": st, "scope": str(WECHAT2_APP)}


TRACE_EVENTS = {
    "group_message_seen", "reply_job_start", "ai_reply_generated", "send_group_start",
    "send_group_done", "send_group_error", "send_group_http_error", "group_not_target_skip",
    "channel_failed", "channel_success", "all_channels_failed", "dry_run_reply",
}


def recent_traces(limit: int = 80) -> Dict[str, Any]:
    records: list[Dict[str, Any]] = []
    try:
        with LOG_FILES["ai"].open("rb") as f:
            f.seek(max(0, f.seek(0, os.SEEK_END) - 3_000_000))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        msg = str(rec.get("msg") or "")
        if msg not in TRACE_EVENTS and not rec.get("trace_id"):
            continue
        records.append(rec)
    records = records[-limit:]
    latest = {
        "received_at": "",
        "ai_generated_at": "",
        "send_latency_ms": None,
        "send_result": "暂无发送",
        "onebot_receive_status": "online" if port_open(36060) else "offline",
        "onebot_send_status": "online" if port_open(58080) else "offline",
        "trace_id": "",
    }
    with ONEBOT_MONITOR_LOCK:
        latest["monitor"] = dict(ONEBOT_MONITOR_STATE)
    for rec in records:
        if rec.get("msg") == "group_message_seen":
            latest["received_at"] = rec.get("time", "")
            latest["trace_id"] = rec.get("trace_id", latest["trace_id"])
        elif rec.get("msg") == "ai_reply_generated":
            latest["ai_generated_at"] = rec.get("time", "")
            latest["trace_id"] = rec.get("trace_id", latest["trace_id"])
        elif rec.get("msg") == "send_group_done":
            latest["send_result"] = "成功"
            latest["send_latency_ms"] = rec.get("latency_ms")
            latest["trace_id"] = rec.get("trace_id", latest["trace_id"])
        elif rec.get("msg") in {"send_group_error", "send_group_http_error"}:
            latest["send_result"] = "失败：" + str(rec.get("error") or rec.get("body") or "")[:180]
            latest["trace_id"] = rec.get("trace_id", latest["trace_id"])
    return {"diagnostic": latest, "events": records}


def memory_stats() -> Dict[str, Any]:
    stats = MEMORY.stats()
    stats["note"] = "当前数据库保存 OneBot/AI 管道已观察到的消息；不读取或解密微信私有数据库。"
    return stats


def memory_search(data: Dict[str, Any]) -> Dict[str, Any]:
    rows = MEMORY.search_messages(
        query=str(data.get("query") or "").strip(),
        group_id=str(data.get("group_id") or "").strip(),
        user_id=str(data.get("user_id") or "").strip(),
        limit=int(data.get("limit") or 50),
    )
    for row in rows:
        for key in ("raw_json", "segments_json", "media_json"):
            value = row.get(key)
            if isinstance(value, str) and len(value) > 600:
                row[key] = value[:600] + "..."
    return {"items": rows, "count": len(rows)}


def memory_members(data: Dict[str, Any]) -> Dict[str, Any]:
    rows = MEMORY.members(str(data.get("group_id") or "").strip(), int(data.get("limit") or 200))
    return {"items": rows, "count": len(rows)}


def memory_personas(data: Dict[str, Any]) -> Dict[str, Any]:
    rows = MEMORY.personas(str(data.get("group_id") or "").strip(), int(data.get("limit") or 200))
    return {"items": rows, "count": len(rows)}


def memory_persona_save(data: Dict[str, Any]) -> Dict[str, Any]:
    user_id = str(data.get("user_id") or "").strip()
    group_id = str(data.get("group_id") or "").strip()
    if not user_id or not group_id:
        raise ValueError("需要 user_id 和 group_id")
    tags = data.get("tags")
    facts = data.get("facts")
    if not isinstance(tags, list):
        tags = [x.strip() for x in str(data.get("tags") or "").split(",") if x.strip()]
    if not isinstance(facts, list):
        facts = []
    return MEMORY.save_persona(user_id, group_id, str(data.get("summary") or "").strip(), tags, facts)


def personas_list_api(data: Dict[str, Any]) -> Dict[str, Any]:
    group_id = str(data.get("group_id") or "").strip()
    if not group_id:
        raise ValueError("需要选择群")
    return MEMORY.persona_list(group_id, str(data.get("query") or "").strip(),
                               str(data.get("status") or "").strip(), int(data.get("page") or 1),
                               int(data.get("page_size") or 100))


def personas_detail_api(data: Dict[str, Any]) -> Dict[str, Any]:
    group_id = str(data.get("group_id") or "").strip()
    user_id = str(data.get("user_id") or "").strip()
    if not group_id or not user_id:
        raise ValueError("需要 group_id 和 user_id")
    detail = MEMORY.persona_detail(group_id, user_id)
    if not detail:
        raise ValueError("当前群中没有该成员")
    return detail


def personas_analyze_api(data: Dict[str, Any]) -> Dict[str, Any]:
    action = str(data.get("action") or "start")
    if action in {"pause", "resume", "retry", "cancel"}:
        job_id = int(data.get("job_id") or 0)
        if not job_id:
            raise ValueError("需要 job_id")
        status = {"pause": "paused", "resume": "queued", "retry": "queued", "cancel": "cancelled"}[action]
        result = MEMORY.set_persona_job_status(job_id, status)
        emit_persona_event(result, action)
        return result
    result = MEMORY.queue_persona_analysis(
        str(data.get("group_id") or "").strip(), str(data.get("user_id") or "").strip(),
        str(data.get("mode") or "full"),
    )
    emit_persona_event({"group_id": data.get("group_id"), **result}, "queued")
    return result


def personas_jobs_api(data: Dict[str, Any]) -> Dict[str, Any]:
    rows = MEMORY.persona_jobs(str(data.get("group_id") or "").strip(), str(data.get("status") or "").strip(), int(data.get("limit") or 200))
    return {"items": rows, "count": len(rows)}


def memory_vector_search(data: Dict[str, Any]) -> Dict[str, Any]:
    query = str(data.get("query") or "").strip()
    if not query:
        raise ValueError("请输入语义检索内容")
    rows = MEMORY.vector_search(query, str(data.get("group_id") or "").strip(), int(data.get("limit") or 20))
    return {"items": rows, "count": len(rows), "query": query}


def iter_onebot_logged_messages(max_bytes: int = 3_000_000) -> Iterable[Dict[str, Any]]:
    """Yield OneBot message JSON objects from both plain and structured log formats."""
    files = [LOG_FILES["onebot"], ROOT / "tools" / "onebot" / "onebot" / "log" / "macos.log"]
    decoder = json.JSONDecoder()
    seen = set()
    for file in files:
        if not file.exists():
            continue
        try:
            with file.open("rb") as f:
                f.seek(max(0, file.stat().st_size - max_bytes))
                lines = f.read().decode("utf-8", "replace").splitlines()
        except OSError:
            continue
        for line in lines:
            candidates: list[str] = []
            # Older plain text: ... 发送数据 msg={...}
            if "发送数据 msg=" in line:
                candidates.append(line[line.index("msg=") + 4:])
            # Current zap/json format: {"msg":"{...}", "message":"... 发送数据"}
            try:
                rec = json.loads(line)
                msg_text = str(rec.get("msg") or "") if isinstance(rec, dict) else ""
                event_text = str(rec.get("message") or "") if isinstance(rec, dict) else ""
                if msg_text.startswith("{") and ("发送数据" in event_text or "post_type" in msg_text):
                    candidates.append(msg_text)
            except Exception:
                pass
            for encoded in candidates:
                try:
                    payload_text, _ = decoder.raw_decode(encoded)
                    raw = json.loads(payload_text) if isinstance(payload_text, str) else payload_text
                except Exception:
                    continue
                if not isinstance(raw, dict):
                    continue
                if raw.get("post_type") != "message" or raw.get("message_type") != "group":
                    continue
                group_id = str(raw.get("group_id") or "")
                message_id = str(raw.get("message_id") or "")
                key = (group_id, message_id, str(raw.get("time") or ""))
                if key in seen:
                    continue
                seen.add(key)
                yield raw


def ingest_onebot_record_failures() -> int:
    """Import/annotate record placeholders from OneBot logs when audio bytes are empty.

    Current stable OneBot can emit the group record callback first, log ``保存音频失败``
    immediately after, and only then print the CDN ``下载文件`` line.  The old parser only
    handled the reverse order, which left blank duplicate records in the gallery.
    This routine is order-independent and annotates the real record row when possible.
    """
    files = [LOG_FILES["onebot"], ROOT / "tools" / "onebot" / "onebot" / "log" / "macos.log"]
    all_lines: list[str] = []
    for file in files:
        if not file.exists():
            continue
        try:
            with file.open("rb") as f:
                f.seek(max(0, file.stat().st_size - 1_800_000))
                all_lines.extend(f.read().decode("utf-8", "replace").splitlines())
        except OSError:
            continue
    if not all_lines:
        return 0

    download_re = re.compile(r"下载文件 .*?file_id=(?P<file_id>\d+@chatroom_(?P<sec>\d+)_[^\s]+).*?media_len=(?P<len>\d+)")
    failure_re = re.compile(r"(保存音频失败|JSON 序列化失败).*?(empty audio payload)?")
    downloads: Dict[str, Dict[str, Any]] = {}
    pending_errors: list[str] = []
    inserted = 0

    def annotate_existing(file_id: str, group_id: str, media_len: int, err: str) -> bool:
        # Prefer the actual OneBot callback row near the same second, then fall back to
        # the synthetic log row.  This keeps the UI to one useful voice item.
        sec_match = re.search(r"@chatroom_(\d+)_", file_id)
        sec = int(sec_match.group(1)) if sec_match else 0
        rows = MEMORY.media(group_id, "record", limit=30)
        best = None
        # First prefer the real OneBot callback row by timestamp/message id; synthetic
        # onebot-record-failed rows are only fallback rows.
        for row in rows:
            try:
                evt = int(row.get("event_time") or 0)
                evt_sec = evt // 1000 if evt > 10_000_000_000 else evt
            except Exception:
                evt_sec = 0
            raw = str(row.get("raw_message") or "") + "\n" + str(row.get("meta_json") or "")
            is_synthetic = str(row.get("event_id") or "").startswith("onebot-record-failed-") or file_id in raw
            if sec and abs(evt_sec - sec) <= 2 and not row.get("ocr_text") and not is_synthetic:
                best = row
                break
        if not best:
            for row in rows:
                raw = str(row.get("raw_message") or "") + "\n" + str(row.get("meta_json") or "")
                if file_id in raw:
                    best = row
                    break
        if not best:
            return False
        transcript = str(best.get("ocr_text") or "").strip()
        if transcript:
            # The automatic WeChat transcript is authoritative.  A missing original
            # Silk byte stream only affects playback and must not overwrite success.
            MEMORY.save_media_annotation(
                int(best["id"]), transcript, f"微信客户端自动转文字：{transcript}", "transcribed",
                ["语音泡", "自动转文字"], [transcript, "语音", "转文字", group_id], "",
            )
            return True
        summary = f"语音泡已抓取，等待微信客户端自动转文字。原始 Silk 音频未导出（{err or 'empty audio payload'}）；file_id={file_id}，media_len={media_len}"
        MEMORY.save_media_annotation(
            int(best["id"]),
            "",
            summary,
            "waiting_transcript",
            ["语音泡", "待转文字", "record"],
            [file_id, "语音", "转文字", group_id],
            "",
        )
        return True

    for line in all_lines:
        fm = failure_re.search(line)
        if fm:
            pending_errors.append("保存音频失败" if "保存音频失败" in line else "JSON 序列化失败")
        m = download_re.search(line)
        if not m:
            continue
        file_id = m.group("file_id")
        if file_id in downloads:
            continue
        group_id = file_id.split("_", 1)[0]
        media_len = int(m.group("len") or 0)
        err = pending_errors.pop(0) if pending_errors else "empty audio payload"
        downloads[file_id] = {"group_id": group_id, "media_len": media_len, "err": err}
        if annotate_existing(file_id, group_id, media_len, err):
            continue
        # The AI callback now persists every record event.  Do not manufacture a
        # second gallery item from a diagnostic log line when the callback is late;
        # it would compete with the real message and hide a later transcript.
        continue
    return inserted


def ingest_onebot_missed_media_events() -> int:
    """Replay recent OneBot media callbacks into memory, including record placeholders."""
    inserted = 0
    for raw in iter_onebot_logged_messages():
        group_id = str(raw.get("group_id") or "")
        if not group_id.endswith("@chatroom"):
            continue
        segments = raw.get("message") if isinstance(raw.get("message"), list) else []
        media_segments = []
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            typ = str(seg.get("type") or "")
            if typ not in {"image", "face", "file", "video", "record"}:
                continue
            media_segments.append(seg)
        if not media_segments:
            continue
        message_id = str(raw.get("message_id") or "")
        media_types = ",".join(str(x.get("type") or "") for x in media_segments)
        event_id_src = f"onebot-log-media|{group_id}|{message_id}|{raw.get('time')}|{media_types}"
        event_id = hashlib.sha1(event_id_src.encode("utf-8", "ignore")).hexdigest()
        sender = raw.get("sender") if isinstance(raw.get("sender"), dict) else {}
        ok = MEMORY.add_message({
            "event_id": event_id,
            "trace_id": "onebot-log-media-" + hashlib.sha1(event_id_src.encode("utf-8", "ignore")).hexdigest()[:10],
            "direction": "incoming",
            "group_id": group_id,
            "group_name": group_id,
            "user_id": str(raw.get("user_id") or sender.get("user_id") or ""),
            "sender_name": str(sender.get("nickname") or sender.get("card") or raw.get("user_id") or ""),
            "message_id": message_id,
            "event_time": int(raw.get("time") or time.time()),
            "text": " ".join(f"[{'image' if str(x.get('type')) == 'face' else x.get('type')}]" for x in media_segments),
            "raw_message": str(raw.get("raw_message") or raw.get("show_content") or ""),
            "segments": segments,
            "raw": raw,
            "source": "onebot_log_media_replay",
        })
        if not ok:
            continue
        inserted += 1
        for row in MEMORY.media_by_event(event_id):
            typ = str(row.get("media_type") or "")
            try:
                meta = json.loads(row.get("meta_json") or "{}")
            except Exception:
                meta = {}
            if typ == "image" and meta.get("type") == "face":
                MEMORY.save_media_annotation(
                    int(row["id"]),
                    "",
                    "微信表情/动图已抓取入库，可放大查看；如有文字或画面内容，可点击重新解析。",
                    "indexed",
                    ["表情", "动图", "gif"],
                    ["微信表情", "动图", "gif", group_id],
                    "",
                )
            elif typ == "record" and not str(row.get("file") or "").strip():
                xml_text = ""
                if isinstance(meta.get("data"), dict):
                    xml_text = str(meta["data"].get("text") or "")
                MEMORY.save_media_annotation(
                    int(row["id"]),
                    "",
                    "语音泡已入库，但 OneBot 未给出原始音频字节，等待补抓本地文件或下载链路。",
                    "waiting_audio",
                    ["语音泡", "待音频", "record"],
                    [message_id, group_id, "语音", "ASR", xml_text[:300]],
                    "empty audio payload",
                )
    return inserted

def memory_media(data: Dict[str, Any]) -> Dict[str, Any]:
    ingest_onebot_record_failures()
    ingest_onebot_missed_media_events()
    # A missing raw Silk payload does not mean the voice bubble failed to enter memory.
    MEMORY.normalize_voice_placeholders(str(data.get("group_id") or "").strip())
    MEMORY.deduplicate_media_items(
        str(data.get("group_id") or "").strip(),
        str(data.get("media_type") or "").strip(),
    )
    rows = MEMORY.media(
        str(data.get("group_id") or "").strip(),
        str(data.get("media_type") or "").strip(),
        int(data.get("limit") or 100),
        str(data.get("status") or "").strip(),
        str(data.get("query") or "").strip(),
    )
    for row in rows:
        try:
            row["tags"] = json.loads(row.get("tags_json") or "[]")
        except Exception:
            row["tags"] = []
        try:
            row["keywords"] = json.loads(row.get("keywords_json") or "[]")
        except Exception:
            row["keywords"] = []
        if row.get("media_type") == "image":
            row["data_url"] = image_data_url(str(row.get("file") or row.get("url") or ""))
        elif row.get("media_type") == "record":
            row["data_url"] = ""
            row["audio_url"] = audio_data_url(str(row.get("file") or row.get("url") or ""))
        else:
            row["data_url"] = ""
            row["audio_url"] = ""
    return {"items": rows, "count": len(rows)}


def memory_media_analyze(data: Dict[str, Any]) -> Dict[str, Any]:
    media_id = int(data.get("id") or data.get("media_id") or 0)
    if media_id <= 0:
        raise ValueError("需要媒体 ID")
    return MEMORY.analyze_media_metadata(media_id)


def memory_media_annotate(data: Dict[str, Any]) -> Dict[str, Any]:
    media_id = int(data.get("id") or data.get("media_id") or 0)
    if media_id <= 0:
        raise ValueError("需要媒体 ID")
    tags = data.get("tags") or []
    keywords = data.get("keywords") or []
    if isinstance(tags, str):
        tags = [x.strip() for x in re.split(r"[,，\\s]+", tags) if x.strip()]
    if isinstance(keywords, str):
        keywords = [x.strip() for x in re.split(r"[,，\\s]+", keywords) if x.strip()]
    return MEMORY.save_media_annotation(
        media_id,
        str(data.get("ocr_text") or ""),
        str(data.get("image_summary") or data.get("summary") or ""),
        str(data.get("status") or "annotated"),
        [str(x) for x in tags][:20] if isinstance(tags, list) else [],
        [str(x) for x in keywords][:30] if isinstance(keywords, list) else [],
    )


def memory_media_asr(data: Dict[str, Any]) -> Dict[str, Any]:
    media_id = int(data.get("id") or data.get("media_id") or 0)
    if media_id <= 0:
        raise ValueError("需要媒体 ID")
    item = MEMORY.media_detail(media_id)
    if not item:
        raise ValueError("媒体不存在")
    if item.get("media_type") != "record":
        raise ValueError("只有语音 record 支持 ASR 转写")
    audio_value = str(item.get("file") or item.get("url") or "")
    if not audio_value:
        raise ValueError("语音没有可用原始文件")
    MEMORY.mark_media_status(media_id, "asr_running")
    try:
        payload = {"audio": audio_value}
        if isinstance(data.get("asr"), dict):
            payload.update(data["asr"])
        r = test_asr(payload)
        text = str(r.get("text") or "").strip()
        row = MEMORY.save_media_annotation(
            media_id,
            ocr_text=text,
            image_summary=f"ASR语音转文字：{text[:240]}",
            status="transcribed",
            tags=["语音泡", "ASR转文字"],
            keywords=[text, "语音", "ASR", str(item.get("sender_name") or item.get("user_id") or "")],
            error="",
        )
        row["latency_ms"] = r.get("latency_ms")
        row["text"] = text
        return row
    except Exception as exc:
        MEMORY.mark_media_status(media_id, "asr_failed", str(exc)[:1000])
        raise


def memory_media_ocr(data: Dict[str, Any]) -> Dict[str, Any]:
    media_id = int(data.get("id") or data.get("media_id") or 0)
    if media_id <= 0:
        raise ValueError("需要媒体 ID")
    item = MEMORY.media_detail(media_id)
    if not item:
        raise ValueError("媒体不存在")
    if item.get("media_type") != "image":
        raise ValueError("只有图片支持 OCR / 视觉解析")
    image_value = str(item.get("file") or item.get("url") or "")
    prompt = str(data.get("prompt") or read_config().get("vision_ocr", {}).get("prompt") or "")
    prompt = (
        prompt
        + "\n\n请严格返回 JSON，不要 Markdown："
        + '{"summary":"一句话描述图片","ocr_text":"图片中可见文字，没有则为空","tags":["物体/场景/人物/动物/表情等标签"],"keywords":["便于日后搜索的关键词"]}'
    )
    MEMORY.mark_media_status(media_id, "ocr_running")
    try:
        result = test_vision_ocr({"image": image_value, "prompt": prompt})
        parsed = parse_vision_json(result["reply"])
        row = MEMORY.save_media_annotation(
            media_id,
            parsed["ocr_text"],
            parsed["summary"],
            "ocr_done",
            parsed["tags"],
            parsed["keywords"],
        )
        row["latency_ms"] = result.get("latency_ms")
        row["model"] = result.get("model")
        row["parsed"] = parsed
        if row.get("media_type") == "image":
            row["data_url"] = image_data_url(str(row.get("file") or row.get("url") or ""))
        return row
    except Exception as exc:
        MEMORY.mark_media_status(media_id, "ocr_failed", str(exc)[:1000])
        raise


def memory_rebuild(data: Dict[str, Any]) -> Dict[str, Any]:
    scope = str(data.get("scope") or "all")
    result: Dict[str, Any] = {}
    if scope in {"all", "indexes", "vectors", "media"}:
        result["indexes"] = MEMORY.rebuild_indexes()
    if scope in {"all", "personas"}:
        result["personas"] = MEMORY.rebuild_personas(str(data.get("group_id") or "").strip())
    return result


def memory_export(data: Dict[str, Any]) -> Dict[str, Any]:
    return MEMORY.export_json(str(data.get("group_id") or "").strip(), int(data.get("limit") or 5000))


def memory_group_get(group_id: str) -> Dict[str, Any]:
    if not group_id.endswith("@chatroom"):
        raise ValueError("需要有效群 ID")
    return MEMORY.group_memory(group_id)


def memory_group_save(data: Dict[str, Any]) -> Dict[str, Any]:
    group_id = str(data.get("group_id") or "").strip()
    if not group_id.endswith("@chatroom"):
        raise ValueError("需要有效群 ID")
    facts = data.get("facts")
    if not isinstance(facts, list):
        facts = []
    return MEMORY.save_group_memory(group_id, str(data.get("summary") or "").strip(), facts)


def memory_import(data: Dict[str, Any]) -> Dict[str, Any]:
    """Import user-provided messages into the same long-term memory schema."""
    items = data.get("items")
    if not isinstance(items, list):
        raise ValueError("items 必须是数组")
    inserted = 0
    for item in items[:5000]:
        if not isinstance(item, dict):
            continue
        payload = {
            "event_id": str(item.get("event_id") or f"import|{item.get('group_id','')}|{item.get('message_id','')}|{time.time_ns()}"),
            "trace_id": str(item.get("trace_id") or "import"),
            "direction": str(item.get("direction") or "import"),
            "group_id": str(item.get("group_id") or ""),
            "group_name": str(item.get("group_name") or item.get("group_id") or ""),
            "user_id": str(item.get("user_id") or ""),
            "sender_name": str(item.get("sender_name") or item.get("user_id") or ""),
            "message_id": str(item.get("message_id") or ""),
            "event_time": int(item.get("event_time") or time.time()),
            "text": str(item.get("text") or item.get("raw_message") or ""),
            "raw_message": str(item.get("raw_message") or item.get("text") or ""),
            "segments": item.get("segments") if isinstance(item.get("segments"), list) else [],
            "raw": item,
            "source": "user_import",
        }
        if MEMORY.add_message(payload):
            inserted += 1
    return {"inserted": inserted, "received": len(items), "stats": MEMORY.stats()}


def health_check_loop() -> None:
    time.sleep(2)
    while True:
        try:
            cfg = read_config()
            if cfg["auto_health_check"]:
                test_all_channels()
            delay = cfg["health_check_interval_seconds"]
        except Exception:
            delay = 60
        time.sleep(max(30, delay))


def onebot_monitor_loop() -> None:
    time.sleep(3)
    while True:
        try:
            raw = json_read(CONFIG_PATH, {})
            monitor = raw.get("onebot_monitor", {}) if isinstance(raw.get("onebot_monitor"), dict) else {}
            enabled = bool(monitor.get("enabled", True))
            auto_recover = bool(monitor.get("auto_recover", True))
            wp, _ = find_wechat2()
            op, oc = pid_from_file(PID_FILES["onebot"], "tools/onebot/onebot/onebot")
            opened = port_open(58080)
            target_mismatch = bool(wp and onebot_target_pid(oc) != wp)
            action = ""
            error = ""
            if enabled and auto_recover and (not opened or target_mismatch):
                try:
                    recover_onebot({"restart_wechat2": False})
                    action = "restarted_onebot" if not target_mismatch else "reattached_current_wechat"
                    opened = port_open(58080)
                except Exception as exc:
                    error = str(exc)[:500]
            if enabled and auto_recover and opened:
                try:
                    st = status()
                    if st["onebot"].get("send_ready") and not st["onebot"].get("media_upload_ready"):
                        repair = warm_media_channel("monitor_self_check", force=False)
                        if repair.get("started"):
                            action = "media_channel_warmed"
                        elif repair.get("skipped"):
                            action = action or f"media_warm_skipped:{repair.get('skipped')}"
                except Exception as exc:
                    error = str(exc)[:500]
            with ONEBOT_MONITOR_LOCK:
                ONEBOT_MONITOR_STATE.update({
                    "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "enabled": enabled,
                    "auto_recover": auto_recover,
                    "port_open": opened,
                    "last_action": action or ONEBOT_MONITOR_STATE.get("last_action", ""),
                    "last_error": error,
                })
        except Exception as exc:
            with ONEBOT_MONITOR_LOCK:
                ONEBOT_MONITOR_STATE.update({"checked_at": time.strftime("%Y-%m-%d %H:%M:%S"), "last_error": str(exc)[:500]})
        time.sleep(15)


def test_onebot(data: Dict[str, Any]) -> Dict[str, Any]:
    cfg = read_config()
    current = status()
    if not current["onebot"]["send_ready"]:
        raise RuntimeError("发送 Hook 尚未捕获 StartTask；OneBot 已在线，但发送通道还未就绪")
    group_id = str(data.get("group_id", "")).strip()
    text = str(data.get("text", "")).strip()
    if not group_id.endswith("@chatroom") or not text:
        raise ValueError("需要有效群 ID 和测试消息")
    result = onebot_send_message(group_id, [{"type": "text", "data": {"text": text}}], timeout=20)
    if not result["ok"]:
        raise RuntimeError(f"OneBot 发送失败: {result['raw']}")
    return {"response": result["response"], "group_id": group_id, "latency_ms": result["latency_ms"]}


def test_callback(data: Dict[str, Any]) -> Dict[str, Any]:
    current = status()
    if not current["onebot"]["send_ready"]:
        raise RuntimeError("完整链路不可测试：OneBot 发送 Hook 尚未就绪")
    group_id = str(data.get("group_id", "")).strip()
    text = str(data.get("text", "")).strip()
    if not group_id.endswith("@chatroom") or not text:
        raise ValueError("需要有效群 ID 和测试消息")
    event = {
        "post_type": "message", "message_type": "group", "group_id": group_id,
        "self_id": "web-admin-test", "user_id": "web-admin-test-user",
        "sender": {"user_id": "web-admin-test-user", "nickname": "后台测试"},
        "time": int(time.time()), "message_id": f"web-{time.time_ns()}",
        "message": [{"type": "text", "data": {"text": text}}],
        "raw_message": "后台测试:\n" + text,
    }
    try:
        log_start = LOG_FILES["ai"].stat().st_size
    except OSError:
        log_start = 0
    code, parsed, raw = request_json("http://127.0.0.1:36060/onebot", "POST", event, timeout=10)
    if code >= 300 or not isinstance(parsed, dict) or not parsed.get("queued"):
        raise RuntimeError(f"回调未进入队列: {raw[:800]}")
    deadline = time.monotonic() + 55
    generated = False
    while time.monotonic() < deadline:
        try:
            with LOG_FILES["ai"].open("r", encoding="utf-8", errors="replace") as f:
                f.seek(log_start)
                records = [json.loads(line) for line in f if line.strip()]
        except (OSError, ValueError):
            records = []
        for rec in records:
            if str(rec.get("group_id", "")) != group_id:
                continue
            if rec.get("msg") == "ai_reply_generated":
                generated = True
            if rec.get("msg") == "send_group_done":
                body = str(rec.get("body", ""))
                if '"status":"ok"' in body.replace(" ", ""):
                    return {"queued": True, "generated": generated, "sent": True, "note": "AI 生成与 OneBot 群发送均成功"}
                raise RuntimeError(f"OneBot 返回非成功结果: {body[:500]}")
            if rec.get("msg") in {"missing_api_key", "ai_http_error", "ai_request_error", "ai_parse_error", "send_group_http_error", "send_group_error"}:
                raise RuntimeError(f"完整链路失败于 {rec.get('msg')}: {json.dumps(rec, ensure_ascii=False)[:700]}")
        time.sleep(0.4)
    raise RuntimeError("完整链路超时：AI 或 OneBot 未在 55 秒内返回最终结果")


def run_action(action: str) -> Dict[str, Any]:
    if action == "restart_ai":
        scripts = [ACTION_SCRIPTS["stop_ai"], ACTION_SCRIPTS["start_ai"]]
    elif action == "start_all":
        scripts = [ACTION_SCRIPTS["launch_wechat2"], ACTION_SCRIPTS["start_onebot"], ACTION_SCRIPTS["start_ai"]]
    else:
        if action not in ACTION_SCRIPTS:
            raise ValueError("未知操作")
        scripts = [ACTION_SCRIPTS[action]]
    output = []
    for name in scripts:
        path = ROOT / "scripts" / name
        proc = subprocess.run([str(path)], cwd=str(ROOT), text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=90)
        output.append(f"$ {name}\n{proc.stdout.strip()}")
        if proc.returncode:
            raise RuntimeError("\n\n".join(output))
    return {"output": "\n\n".join(output), "status": status()}


def normalize_log(source: str, raw: str) -> Dict[str, Any]:
    clean = re.sub(r"\x1b\[[0-9;]*m", "", raw.rstrip("\r\n"))
    level = "info"
    message = clean
    timestamp = ""
    if source == "ai":
        try:
            obj = json.loads(clean)
            level = str(obj.pop("level", "info")).lower()
            timestamp = str(obj.pop("time", ""))
            event = str(obj.pop("msg", "event"))
            trace_id = str(obj.get("trace_id") or "")
            group_id = str(obj.get("group_id") or "")
            details = " ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in obj.items())
            message = event + ("  " + details if details else "")
            return {"source": source, "level": level, "time": timestamp, "message": message,
                    "event": event, "trace_id": trace_id, "group_id": group_id}
        except ValueError:
            pass
    else:
        match = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s*\|\s*(DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL)", clean, re.I)
        if match:
            timestamp, level = match.group(1), match.group(2).lower().replace("warn", "warning")
    return {"source": source, "level": level, "time": timestamp, "message": message}


class Handler(BaseHTTPRequestHandler):
    server_version = "WeChatSecondAdmin/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def json_response(self, code: int, data: Any) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def bytes_response(self, code: int, raw: bytes, content_type: str, filename: str = "") -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "private, max-age=3600")
        if filename:
            self.send_header("Content-Disposition", f'inline; filename="{urllib.parse.quote(filename)}"')
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 2_000_000:
            raise ValueError("请求过大")
        return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}

    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/status":
            return self.json_response(200, {"ok": True, "data": status()})
        if path == "/api/config":
            return self.json_response(200, {"ok": True, "data": read_config()})
        if path == "/api/channels/health":
            return self.json_response(200, {"ok": True, "data": channel_health_snapshot()})
        if path == "/api/groups/discover":
            return self.json_response(200, {"ok": True, "data": discover_groups()})
        if path == "/api/groups/members":
            try:
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                group_id = str((qs.get("group_id") or [""])[0])
                return self.json_response(200, {"ok": True, "data": group_members_catalog(group_id)})
            except Exception as exc:
                return self.json_response(400, {"ok": False, "error": str(exc)})
        if path == "/api/traces/recent":
            return self.json_response(200, {"ok": True, "data": recent_traces()})
        if path == "/api/memory/stats":
            return self.json_response(200, {"ok": True, "data": memory_stats()})
        if path in {"/api/personas/list", "/api/personas/detail", "/api/personas/jobs"}:
            try:
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                data = {key: values[0] for key, values in qs.items() if values}
                handler = {"/api/personas/list": personas_list_api, "/api/personas/detail": personas_detail_api,
                           "/api/personas/jobs": personas_jobs_api}[path]
                return self.json_response(200, {"ok": True, "data": handler(data)})
            except Exception as exc:
                return self.json_response(400, {"ok": False, "error": str(exc)})
        if path == "/api/brain/config":
            return self.json_response(200, {"ok": True, "data": brain_config_payload()})
        if path == "/api/brain/tasks":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            return self.json_response(200, {"ok": True, "data": brain_tasks({
                "group_id": (qs.get("group_id") or [""])[0],
                "limit": int((qs.get("limit") or ["200"])[0]),
                "active_only": (qs.get("active_only") or ["0"])[0] in {"1", "true"},
            })})
        if path == "/api/brain/culture":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            group_id = str((qs.get("group_id") or [""])[0])
            query = str((qs.get("query") or [""])[0])
            return self.json_response(200, {"ok": True, "data": MEMORY.culture_context(group_id, query, 200)})
        if path == "/api/embedding/status":
            runtime = {}
            try:
                runtime = (ai_json("/status", timeout=3).get("brain") or {}).get("embedding") or {}
            except Exception:
                pass
            return self.json_response(200, {"ok": True, "data": {**runtime, "stats": MEMORY.stats()}})
        if path == "/api/voicepacks":
            return self.json_response(200, {"ok": True, "data": voicepacks_list({})})
        if path == "/api/voicepacks/audio":
            try:
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                raw, mime, name = voicepack_audio(int((qs.get("id") or ["0"])[0]))
                return self.bytes_response(200, raw, mime, name)
            except Exception as exc:
                return self.json_response(400, {"ok": False, "error": str(exc)})
        if path == "/api/faces":
            return self.json_response(200, {"ok": True, "data": faces_list({})})
        if path in {"/api/logs/stream", "/api/events/stream"}:
            return self.stream_events(logs_only=path == "/api/logs/stream")
        if path == "/" or path.startswith("/static/"):
            file = STATIC / ("index.html" if path == "/" else path.removeprefix("/static/"))
            return self.serve_file(file)
        self.send_error(404)

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        try:
            data = self.read_json()
            if path == "/api/config":
                ai_was_running = bool(pid_from_file(PID_FILES["ai"], "ai_reply_server.py")[0])
                result = save_config(data)
                if ai_was_running:
                    run_action("restart_ai")
            elif path == "/api/brain/config":
                result = save_brain_config(data)
            elif path == "/api/personas/analyze":
                result = personas_analyze_api(data)
            elif path == "/api/personas/save":
                result = memory_persona_save(data)
            elif path == "/api/groups/ignored-members":
                result = save_group_member_blacklist(data)
            elif path == "/api/brain/tasks":
                result = brain_tasks(data)
            elif path == "/api/brain/preview":
                result = brain_preview(data)
            elif path == "/api/embedding/backfill/start":
                result = embedding_control("start")
            elif path == "/api/embedding/backfill/pause":
                result = embedding_control("pause")
            elif path == "/api/embedding/backfill/resume":
                result = embedding_control("resume")
            elif path == "/api/embedding/test":
                result = ai_json("/embedding/test", "POST", {
                    "query": str(data.get("query") or "群里大家最近在聊什么"),
                    "group_id": str(data.get("group_id") or ""),
                }, 180).get("data") or {}
            elif path == "/api/models":
                result = fetch_models(data)
            elif path == "/api/test/ai":
                result = test_ai(data)
            elif path == "/api/test/vision-ocr":
                result = test_vision_ocr(data)
            elif path == "/api/test/asr":
                result = test_asr(data)
            elif path == "/api/channels/test":
                result = test_channel(str(data.get("channel_id", "")))
            elif path == "/api/channels/test-all":
                result = test_all_channels()
            elif path == "/api/test/onebot":
                result = test_onebot(data)
            elif path == "/api/test/callback":
                result = test_callback(data)
            elif path == "/api/groups/aliases":
                result = save_group_aliases(data)
            elif path == "/api/groups/sync-ui":
                result = sync_ui_groups()
            elif path == "/api/onebot/send-probe":
                result = send_probe(data)
            elif path == "/api/onebot/recover":
                result = recover_onebot(data)
            elif path == "/api/onebot/media-repair":
                result = warm_media_channel("api", force=True)
            elif path == "/api/messages/send":
                result = send_message(data)
            elif path == "/api/voicepacks":
                result = voicepacks_list(data)
            elif path == "/api/voicepacks/recommend":
                result = voicepack_recommend(data)
            elif path == "/api/voicepacks/plan":
                result = voicepack_import_plan(data)
            elif path == "/api/voicepacks/import":
                result = import_voice_paths(data)
            elif path == "/api/voicepacks/send":
                result = voicepack_send(data)
            elif path == "/api/voicepacks/delete-item":
                result = voicepack_delete_item(data)
            elif path == "/api/voicepacks/delete-pack":
                result = voicepack_delete_pack(data)
            elif path == "/api/faces":
                result = faces_list(data)
            elif path == "/api/faces/send":
                result = face_send(data)
            elif path == "/api/faces/update":
                result = face_update(data)
            elif path == "/api/faces/reindex":
                result = face_reindex(data)
            elif path == "/api/media-reply/test":
                result = media_reply_test(data)
            elif path == "/api/memory/search":
                result = memory_search(data)
            elif path == "/api/memory/members":
                result = memory_members(data)
            elif path == "/api/memory/personas":
                result = memory_personas(data)
            elif path == "/api/memory/persona/save":
                result = memory_persona_save(data)
            elif path == "/api/memory/vector-search":
                result = memory_vector_search(data)
            elif path == "/api/memory/media":
                result = memory_media(data)
            elif path == "/api/memory/media/analyze":
                result = memory_media_analyze(data)
            elif path == "/api/memory/media/annotate":
                result = memory_media_annotate(data)
            elif path == "/api/memory/media/save":
                result = memory_media_annotate(data)
            elif path == "/api/memory/media/ocr":
                result = memory_media_ocr(data)
            elif path == "/api/memory/media/asr":
                result = memory_media_asr(data)
            elif path == "/api/memory/media/sync-transcripts":
                result = sync_record_transcripts(data)
            elif path == "/api/memory/rebuild":
                result = memory_rebuild(data)
            elif path == "/api/memory/export":
                result = memory_export(data)
            elif path == "/api/memory/group":
                result = memory_group_get(str(data.get("group_id") or ""))
            elif path == "/api/memory/group/save":
                result = memory_group_save(data)
            elif path == "/api/memory/import":
                result = memory_import(data)
            elif path.startswith("/api/action/"):
                result = run_action(path.rsplit("/", 1)[-1])
            else:
                return self.json_response(404, {"ok": False, "error": "接口不存在"})
            self.json_response(200, {"ok": True, "data": result})
        except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as e:
            self.json_response(400, {"ok": False, "error": str(e)})
        except Exception as e:
            self.json_response(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})

    def serve_file(self, file: Path) -> None:
        try:
            if not file.resolve().is_relative_to(STATIC.resolve()):
                return self.send_error(403)
            raw = file.read_bytes()
        except OSError:
            return self.send_error(404)
        mime = {".html": "text/html", ".css": "text/css", ".js": "application/javascript"}.get(file.suffix, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", mime + "; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def stream_events(self, logs_only: bool = False) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        positions: Dict[str, int] = {}
        try:
            for source, file in LOG_FILES.items():
                positions[source] = max(0, file.stat().st_size - 40_000) if file.exists() else 0
            positions["brain"] = BRAIN_EVENTS_FILE.stat().st_size if BRAIN_EVENTS_FILE.exists() else 0
            while True:
                sent = False
                for source, file in LOG_FILES.items():
                    if not file.exists():
                        continue
                    size = file.stat().st_size
                    if size < positions[source]:
                        positions[source] = 0
                    if size == positions[source]:
                        continue
                    with file.open("r", encoding="utf-8", errors="replace") as f:
                        f.seek(positions[source])
                        for line in f:
                            event = normalize_log(source, line)
                            event = {"type": "log", **event}
                            payload = json.dumps(event, ensure_ascii=False)
                            self.wfile.write(f"data: {payload}\n\n".encode())
                            sent = True
                        positions[source] = f.tell()
                if not logs_only and BRAIN_EVENTS_FILE.exists():
                    size = BRAIN_EVENTS_FILE.stat().st_size
                    if size < positions["brain"]:
                        positions["brain"] = 0
                    if size > positions["brain"]:
                        with BRAIN_EVENTS_FILE.open("r", encoding="utf-8", errors="replace") as f:
                            f.seek(positions["brain"])
                            for line in f:
                                try:
                                    event = json.loads(line)
                                except ValueError:
                                    continue
                                self.wfile.write(f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode())
                                sent = True
                            positions["brain"] = f.tell()
                if not sent:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
                time.sleep(0.75)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost"}:
        raise SystemExit("管理后台仅允许监听本机")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    normalize_existing_voice_titles()
    threading.Thread(target=health_check_loop, name="channel-health-check", daemon=True).start()
    threading.Thread(target=onebot_monitor_loop, name="onebot-monitor", daemon=True).start()
    threading.Thread(target=persona_worker_loop, name="persona-analysis", daemon=True).start()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"第二微信 Web 管理后台：http://{args.host}:{args.port}", flush=True)
    signal.signal(signal.SIGTERM, lambda *_: (PERSONA_WORKER_STOP.set(), threading.Thread(target=server.shutdown, daemon=True).start()))
    try:
        server.serve_forever(poll_interval=0.4)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
