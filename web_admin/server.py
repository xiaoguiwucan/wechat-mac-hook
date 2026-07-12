#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Local web control plane for the isolated second WeChat instance."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import signal
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
STATIC = Path(__file__).resolve().parent / "static"
CONFIG_PATH = ROOT / "config" / "ai_reply_config.json"
ENV_PATH = ROOT / "config" / "ai_reply.env"
SECOND_HOME = Path.home() / "Library" / "Application Support" / "WeChatSecond"
LOG_DIR = SECOND_HOME / "logs"
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


def config_revision() -> str:
    digest = hashlib.sha256()
    for path in (CONFIG_PATH, ENV_PATH):
        try:
            digest.update(path.read_bytes())
        except OSError:
            pass
    return digest.hexdigest()[:16]


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
    })
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
    hook_ready = "Dynamic Text Message Setup Complete" in onebot_log and "Cannot locate runtime base" not in onebot_log
    send_ready = hook_ready and "捕获到 StartTask" in onebot_tail
    cfg = read_config()
    return {
        "wechat2": process_item(wp, wc),
        "onebot": {**process_item(op, oc, 58080), "hook_ready": hook_ready, "send_ready": send_ready},
        "ai": {**process_item(ap, ac, 36060), "configured": any(
            x.get("enabled") and (x.get("api_key") or str(x.get("base_url", "")).startswith(("http://127.0.0.1", "http://localhost")))
            for x in cfg.get("channels", [])
        )},
        "isolation": {"app": str(WECHAT2_APP), "bundle_id": "com.tencent.xinWeChat.instance2", "main_wechat_touched": False},
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
    selected = {str(x["id"]): str(x.get("name") or x["id"]) for x in cfg.get("target_groups", []) if isinstance(x, dict) and x.get("id")}
    groups: Dict[str, Dict[str, Any]] = {
        gid: {"id": gid, "name": name, "selected": True, "last_seen": "", "source": "config"}
        for gid, name in selected.items()
    }

    def remember(group_id: str, name: str = "", last_seen: str = "", source: str = "event", preview: str = "") -> None:
        if not re.fullmatch(r"\d{6,}@chatroom", group_id):
            return
        item = groups.setdefault(group_id, {"id": group_id, "name": group_id, "selected": False, "last_seen": "", "source": source, "preview": ""})
        if group_id in selected and name and name != group_id:
            item["name"] = name
        if last_seen:
            item["last_seen"] = last_seen
        if preview:
            item["preview"] = preview[:80]
        if group_id in selected:
            item["selected"] = True
            item["name"] = selected[group_id]

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


def test_onebot(data: Dict[str, Any]) -> Dict[str, Any]:
    cfg = read_config()
    current = status()
    if not current["onebot"]["send_ready"]:
        raise RuntimeError("发送 Hook 尚未捕获 StartTask；OneBot 已在线，但发送通道还未就绪")
    group_id = str(data.get("group_id", "")).strip()
    text = str(data.get("text", "")).strip()
    if not group_id.endswith("@chatroom") or not text:
        raise ValueError("需要有效群 ID 和测试消息")
    code, parsed, raw = request_json(cfg["onebot_api"].rstrip("/") + "/send_group_msg", "POST", {
        "group_id": group_id,
        "message": [{"type": "text", "data": {"text": text}}],
    }, timeout=20)
    if code >= 300:
        raise RuntimeError(f"OneBot HTTP {code}: {raw[:800]}")
    success = isinstance(parsed, dict) and parsed.get("status") in {"ok", "success"} and parsed.get("retcode", 0) in {0, None}
    if not success:
        raise RuntimeError(f"OneBot 发送失败: {raw[:800]}")
    return {"response": parsed, "group_id": group_id}


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
            details = " ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in obj.items())
            message = event + ("  " + details if details else "")
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
        if path == "/api/logs/stream":
            return self.stream_logs()
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
            elif path == "/api/models":
                result = fetch_models(data)
            elif path == "/api/test/ai":
                result = test_ai(data)
            elif path == "/api/channels/test":
                result = test_channel(str(data.get("channel_id", "")))
            elif path == "/api/channels/test-all":
                result = test_all_channels()
            elif path == "/api/test/onebot":
                result = test_onebot(data)
            elif path == "/api/test/callback":
                result = test_callback(data)
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

    def stream_logs(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        positions: Dict[str, int] = {}
        try:
            for source, file in LOG_FILES.items():
                positions[source] = max(0, file.stat().st_size - 40_000) if file.exists() else 0
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
                            payload = json.dumps(event, ensure_ascii=False)
                            self.wfile.write(f"data: {payload}\n\n".encode())
                            sent = True
                        positions[source] = f.tell()
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
    threading.Thread(target=health_check_loop, name="channel-health-check", daemon=True).start()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"第二微信 Web 管理后台：http://{args.host}:{args.port}", flush=True)
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
    try:
        server.serve_forever(poll_interval=0.4)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
