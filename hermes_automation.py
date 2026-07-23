#!/usr/bin/env python3
"""Permission-gated asynchronous Hermes API Server integration."""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

from memory_store import MemoryStore


@dataclass
class HermesConfig:
    enabled: bool
    base_url: str
    api_key: str
    workspace: str
    poll_seconds: float
    max_run_seconds: int
    owner_user_ids: tuple[str, ...] = ()
    read_workers: int = 16
    cron_workers: int = 6
    ops_workers: int = 8
    high_workers: int = 2
    queue_size: int = 1000
    answer_ack_delay_seconds: float = 30.0
    cache_default_seconds: int = 86400
    cache_weather_seconds: int = 3600
    cache_market_seconds: int = 900
    cache_news_seconds: int = 21600
    cache_status_seconds: int = 120

    @classmethod
    def from_env(cls) -> "HermesConfig":
        return cls(
            enabled=os.getenv("HERMES_ENABLED", "0").lower() in {"1", "true", "yes", "on"},
            base_url=os.getenv("HERMES_API_URL", "http://127.0.0.1:8642").rstrip("/"),
            api_key=os.getenv("HERMES_API_KEY", ""),
            workspace=os.getenv(
                "HERMES_WORKSPACE",
                "/Users/zkx/Projects/wechat-agent-advanced",
            ),
            poll_seconds=max(0.5, min(10.0, float(os.getenv("HERMES_POLL_SECONDS", "1")))),
            max_run_seconds=max(60, min(86400, int(os.getenv("HERMES_MAX_RUN_SECONDS", "3600")))),
            owner_user_ids=tuple(
                value.strip() for value in os.getenv("HERMES_OWNER_USER_IDS", "").split(",")
                if value.strip()
            ),
            read_workers=max(1, min(64, int(os.getenv("HERMES_READ_WORKERS", "16")))),
            cron_workers=max(1, min(32, int(os.getenv("HERMES_CRON_WORKERS", "6")))),
            ops_workers=max(1, min(32, int(os.getenv("HERMES_OPS_WORKERS", "8")))),
            high_workers=max(1, min(16, int(os.getenv("HERMES_HIGH_WORKERS", "2")))),
            queue_size=max(100, min(10000, int(os.getenv("HERMES_QUEUE_SIZE", "1000")))),
            answer_ack_delay_seconds=max(
                1.0, min(300.0, float(os.getenv("HERMES_ANSWER_ACK_DELAY_SECONDS", "30")))
            ),
            cache_default_seconds=max(
                0, min(2592000, int(os.getenv("HERMES_CACHE_DEFAULT_SECONDS", "86400")))
            ),
            cache_weather_seconds=max(
                0, min(604800, int(os.getenv("HERMES_CACHE_WEATHER_SECONDS", "3600")))
            ),
            cache_market_seconds=max(
                0, min(86400, int(os.getenv("HERMES_CACHE_MARKET_SECONDS", "900")))
            ),
            cache_news_seconds=max(
                0, min(604800, int(os.getenv("HERMES_CACHE_NEWS_SECONDS", "21600")))
            ),
            cache_status_seconds=max(
                0, min(3600, int(os.getenv("HERMES_CACHE_STATUS_SECONDS", "120")))
            ),
        )


class HermesAutomationService:
    def __init__(self, store: MemoryStore,
                 send_callback: Callable[[str, str, str], None],
                 config: Optional[HermesConfig] = None):
        self.store = store
        self.send_callback = send_callback
        self.cfg = config or HermesConfig.from_env()
        self.queues: Dict[str, "queue.Queue[Dict[str, Any]]"] = {
            name: queue.Queue(maxsize=self.cfg.queue_size)
            for name in ("read", "cron", "ops", "high")
        }
        # Kept as a compatibility alias for callers that only inspect queue size.
        self.tasks = self.queues["ops"]
        self.stop_event = threading.Event()
        self.workers: List[threading.Thread] = []
        self.lock = threading.RLock()
        self.state: Dict[str, Any] = {
            "enabled": self.cfg.enabled,
            "healthy": False,
            "queued": 0,
            "running": 0,
            "completed": 0,
            "failed": 0,
            "last_error": "",
            "cache_hits": 0,
            "pools": {
                name: {"workers": workers, "queued": 0, "running": 0}
                for name, workers in self._worker_counts().items()
            },
        }

    def start(self) -> None:
        if not self.cfg.enabled or any(worker.is_alive() for worker in self.workers):
            return
        self.stop_event.clear()
        # Probe once before spawning the pool instead of making every read
        # worker hit the Hermes health endpoint at the same time.
        self.health()
        for pool_name, count in self._worker_counts().items():
            for index in range(count):
                worker = threading.Thread(
                    target=self._loop,
                    args=(pool_name,),
                    name=f"hermes-{pool_name}-{index + 1}",
                    daemon=True,
                )
                worker.start()
                self.workers.append(worker)

    def stop(self) -> None:
        self.stop_event.set()
        for pool_name, count in self._worker_counts().items():
            for _ in range(count):
                try:
                    self.queues[pool_name].put_nowait({})
                except queue.Full:
                    break

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            result = dict(self.state)
            result["pools"] = {
                name: dict(values) for name, values in self.state["pools"].items()
            }
        result["queue_size"] = sum(item.qsize() for item in self.queues.values())
        result["worker_count"] = sum(self._worker_counts().values())
        result["answer_ack_delay_seconds"] = self.cfg.answer_ack_delay_seconds
        result["cache_seconds"] = {
            "default": self.cfg.cache_default_seconds,
            "weather": self.cfg.cache_weather_seconds,
            "market": self.cfg.cache_market_seconds,
            "news": self.cfg.cache_news_seconds,
            "status": self.cfg.cache_status_seconds,
        }
        return result

    def _worker_counts(self) -> Dict[str, int]:
        return {
            "read": self.cfg.read_workers,
            "cron": self.cfg.cron_workers,
            "ops": self.cfg.ops_workers,
            "high": self.cfg.high_workers,
        }

    @staticmethod
    def _is_schedule_intent(intent: str) -> bool:
        return any(marker in str(intent or "") for marker in (
            "提醒", "闹钟", "定时", "分钟后", "小时后", "每天", "每周", "每月",
            "暂停任务", "恢复任务", "删除任务", "cron", "Cron",
        ))

    @classmethod
    def _queue_for(cls, purpose: str, risk_level: str, intent: str) -> str:
        if purpose == "answer":
            return "read"
        if risk_level == "high":
            return "high"
        if cls._is_schedule_intent(intent):
            return "cron"
        return "ops"

    @staticmethod
    def _normalize_cache_intent(intent: str) -> str:
        text = str(intent or "").lower().strip()
        text = re.sub(r"@[\w\u4e00-\u9fff·._-]+", "", text)
        text = re.sub(r"(请|帮我|麻烦|小风|机器人|查一下|查下|查询|查)", "", text)
        return re.sub(r"[\s，。！？、,.!?：:；;\"'“”‘’（）()【】\[\]]+", "", text)

    @classmethod
    def _cache_key(cls, intent: str) -> str:
        normalized = cls._normalize_cache_intent(intent)
        return hashlib.sha256(normalized.encode("utf-8", "ignore")).hexdigest() if normalized else ""

    @staticmethod
    def _cache_bypassed(intent: str) -> bool:
        return any(marker in str(intent or "") for marker in (
            "强制刷新", "重新查询", "重新查", "不要缓存", "忽略缓存", "刷新一下",
        ))

    def _cache_ttl_for(self, intent: str) -> int:
        text = str(intent or "").lower()
        if any(marker in text for marker in ("天气", "气温", "下雨", "台风", "空气质量")):
            return self.cfg.cache_weather_seconds
        if any(marker in text for marker in (
            "价格", "金价", "银价", "黄金", "白银", "汇率", "股票", "股价",
            "基金", "币价", "比特币", "行情",
        )):
            return self.cfg.cache_market_seconds
        if any(marker in text for marker in ("新闻", "热搜", "最新消息", "头条", "资讯")):
            return self.cfg.cache_news_seconds
        if any(marker in text for marker in ("状态", "健康", "运行情况", "在线吗")):
            return self.cfg.cache_status_seconds
        return self.cfg.cache_default_seconds

    def _request(self, method: str, path: str,
                 payload: Optional[Dict[str, Any]] = None,
                 timeout: int = 10) -> Dict[str, Any]:
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.cfg.base_url + path, data=body, method=method)
        req.add_header("Accept", "application/json")
        if body is not None:
            req.add_header("Content-Type", "application/json")
        if self.cfg.api_key:
            req.add_header("Authorization", "Bearer " + self.cfg.api_key)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace") or "{}")

    def health(self) -> bool:
        if not self.cfg.enabled:
            return False
        try:
            self._request("GET", "/health/detailed", timeout=2)
            with self.lock:
                self.state["healthy"] = True
                self.state["last_error"] = ""
            return True
        except Exception as exc:
            with self.lock:
                self.state["healthy"] = False
                self.state["last_error"] = str(exc)[:500]
            return False

    def _queue_run(self, row: Dict[str, Any], trace_id: str = "",
                   purpose: str = "automation") -> Dict[str, Any]:
        run_id = str(row["run_id"])
        queue_name = str(row.get("queue_name") or self._queue_for(
            purpose,
            str(row.get("risk_level") or "read"),
            str(row.get("intent") or ""),
        ))
        task = {
            "run_id": run_id, "group_id": str(row.get("group_id") or ""),
            "user_id": str(row.get("user_id") or ""), "trace_id": trace_id or run_id,
            "intent": str(row.get("intent") or ""), "risk_level": str(row.get("risk_level") or "read"),
            "source_event_id": str(row.get("source_event_id") or ""),
            "purpose": "answer" if purpose == "answer" else "automation",
            "queue_name": queue_name,
            "cache_key": str(row.get("cache_key") or ""),
            "queued_monotonic": time.monotonic(),
        }
        try:
            self.queues[queue_name].put_nowait(task)
        except queue.Full:
            self.store.update_automation_run(run_id, status="failed", error="automation_queue_full")
            return {"accepted": False, "run_id": run_id, "message": "自动化队列已满，请稍后重试。"}
        with self.lock:
            self.state["queued"] += 1
            self.state["pools"][queue_name]["queued"] += 1
        message = "" if task["purpose"] == "answer" else (
            f"任务已接收（{run_id[-8:]}），完成后我会把结果发回群里。"
        )
        return {
            "accepted": True, "run_id": run_id,
            "message": message, "queue": queue_name,
            "deferred_ack": task["purpose"] == "answer",
        }

    def submit(self, event: Any, route: Dict[str, Any],
               trusted: bool = False, approved: bool = False) -> Dict[str, Any]:
        risk = str(route.get("risk_level") or "read")
        admin = self.store.group_admin(str(event.group_id), str(event.user_id))
        owner = str(event.user_id) in set(self.cfg.owner_user_ids)
        if risk == "write" and not trusted and not admin and not owner:
            return {"accepted": False, "message": "这个操作需要群管理员权限。"}
        intent = str(route.get("automation_intent") or event.text).strip()[:4000]
        purpose = "answer" if str(route.get("hermes_mode") or "") == "answer" else "automation"
        queue_name = self._queue_for(purpose, risk, intent)
        cache_key = self._cache_key(intent) if purpose == "answer" and risk == "read" else ""
        cache_ttl = self._cache_ttl_for(intent)
        if cache_key and cache_ttl > 0 and not self._cache_bypassed(intent):
            cached = self.store.cached_automation_result(
                str(event.group_id), cache_key, cache_ttl
            )
            if cached:
                with self.lock:
                    self.state["cache_hits"] += 1
                return {
                    "accepted": True,
                    "cached": True,
                    "final": True,
                    "run_id": str(cached.get("run_id") or ""),
                    "message": str(cached.get("result_summary") or "")[:1800],
                    "cache_age_seconds": float(cached.get("cache_age_seconds") or 0),
                    "cache_ttl_seconds": cache_ttl,
                    "queue": "cache",
                }
        key_source = f"{event.event_id}|{event.message_id}|{intent}"
        idempotency_key = hashlib.sha256(key_source.encode("utf-8", "ignore")).hexdigest()
        run_id = "auto-" + uuid.uuid4().hex
        created, row = self.store.create_automation_run({
            "run_id": run_id, "idempotency_key": idempotency_key,
            "source_event_id": event.event_id, "group_id": event.group_id,
            "user_id": event.user_id, "intent": intent, "risk_level": risk,
            "purpose": purpose, "queue_name": queue_name, "cache_key": cache_key,
            "status": "queued" if risk != "high" or approved else "awaiting_approval",
        })
        if not created:
            return {
                "accepted": str(row.get("status") or "") != "awaiting_approval",
                "duplicate": True, "approval_required": str(row.get("status") or "") == "awaiting_approval",
                "run_id": row.get("run_id"), "message": "这个自动化任务已经接收，正在处理。",
            }
        if risk == "high" and not approved:
            self.store.add_automation_event(run_id, "approval_required", {
                "source": "web_admin" if trusted else "group", "intent": intent,
            })
            return {"accepted": False, "approval_required": True, "run_id": run_id,
                    "message": "这个操作风险较高，已暂停，等待项目所有者确认。"}
        return self._queue_run(
            row, str(getattr(event, "trace_id", "") or run_id), purpose=purpose
        )

    def submit_manual(self, group_id: str, intent: str, risk_level: str = "write") -> Dict[str, Any]:
        event_id = f"web-admin|{time.time_ns()}"
        event = SimpleNamespace(
            group_id=str(group_id), user_id="web-admin-owner", event_id=event_id,
            message_id=event_id, text=str(intent), trace_id=event_id,
        )
        return self.submit(event, {
            "automation_intent": str(intent), "risk_level": str(risk_level),
        }, trusted=True)

    def approve(self, run_id: str) -> Dict[str, Any]:
        row = self.store.automation_run(str(run_id))
        if not row:
            return {"accepted": False, "message": "没有找到这个任务。"}
        if str(row.get("status") or "") != "awaiting_approval":
            return {"accepted": False, "message": f"任务当前状态为 {row.get('status') or 'unknown'}，无需审批。"}
        self.store.update_automation_run(str(run_id), status="queued", error="")
        row = self.store.automation_run(str(run_id))
        self.store.add_automation_event(str(run_id), "approved", {"source": "web_admin"})
        return self._queue_run(row, f"approval-{run_id}")

    def stop_run(self, run_id: str) -> Dict[str, Any]:
        row = self.store.automation_run(str(run_id))
        if not row:
            return {"stopped": False, "message": "没有找到这个任务。"}
        status = str(row.get("status") or "")
        if status in {"completed", "failed", "cancelled"}:
            return {"stopped": False, "message": f"任务已经是 {status} 状态。"}
        hermes_run_id = str(row.get("hermes_run_id") or "")
        if hermes_run_id:
            self._request("POST", f"/v1/runs/{hermes_run_id}/stop", {}, timeout=10)
        self.store.update_automation_run(str(run_id), status="cancelled", error="")
        self.store.add_automation_event(str(run_id), "cancelled", {"source": "web_admin"})
        return {"stopped": True, "run_id": str(run_id), "message": "任务已停止。"}

    def _consume_sse(self, local_run_id: str, hermes_run_id: str) -> None:
        req = urllib.request.Request(
            f"{self.cfg.base_url}/v1/runs/{hermes_run_id}/events",
            headers={"Accept": "text/event-stream"},
        )
        if self.cfg.api_key:
            req.add_header("Authorization", "Bearer " + self.cfg.api_key)
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.max_run_seconds) as resp:
                for raw in resp:
                    if self.stop_event.is_set():
                        return
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    value = line[5:].strip()
                    if not value or value == "[DONE]":
                        continue
                    try:
                        payload = json.loads(value)
                    except ValueError:
                        payload = {"raw": value[:4000]}
                    event_type = str(payload.get("type") or payload.get("event") or "hermes_event")
                    self.store.add_automation_event(local_run_id, event_type, payload)
        except Exception as exc:
            self.store.add_automation_event(
                local_run_id, "sse_disconnected", {"error": str(exc)[:500]}
            )

    def _execute(self, task: Dict[str, Any]) -> None:
        local_run_id = str(task["run_id"])
        self.store.update_automation_run(local_run_id, status="starting")
        answer_mode = str(task.get("purpose") or "") == "answer"
        schedule_mode = self._is_schedule_intent(str(task.get("intent") or ""))
        queue_wait_ms = round(
            max(0.0, time.monotonic() - float(task.get("queued_monotonic") or time.monotonic()))
            * 1000,
            1,
        )
        self.store.add_automation_event(local_run_id, "worker_started", {
            "queue": str(task.get("queue_name") or "ops"),
            "queue_wait_ms": queue_wait_ms,
        })
        ack_cancel = threading.Event()
        task["_ack_cancel"] = ack_cancel
        if answer_mode:
            def delayed_ack() -> None:
                if ack_cancel.wait(self.cfg.answer_ack_delay_seconds):
                    return
                try:
                    self.send_callback(
                        str(task["group_id"]),
                        "正在查询最新结果，完成后直接回复你。",
                        str(task.get("trace_id") or local_run_id),
                    )
                    self.store.add_automation_event(local_run_id, "delayed_ack_sent", {
                        "delay_seconds": self.cfg.answer_ack_delay_seconds,
                    })
                except Exception as exc:
                    self.store.add_automation_event(local_run_id, "delayed_ack_failed", {
                        "error": str(exc)[:500],
                    })

            threading.Thread(
                target=delayed_ack,
                name=f"hermes-ack-{local_run_id[-8:]}",
                daemon=True,
            ).start()
        if answer_mode:
            instructions = (
                "你是微信群机器人的工具执行层。必须按问题需要主动调用天气、网页搜索、"
                "浏览器、文件、代码或其他可用工具获取真实结果；优先使用最新数据，"
                "不得用模型记忆猜测实时信息。直接用简洁中文回答原问题，不提内部任务、"
                "Hermes、自动化编号或工具过程；只读查询不得修改文件或服务。"
                f"\n允许工作区：{self.cfg.workspace}"
            )
        else:
            instructions = (
                "你是微信项目运维自动化执行器。只在允许的工作区执行任务；"
                "保留现有单微信进程，不启动第二个微信；运行测试后再提交或部署；"
                "不要输出密钥；返回简洁的中文执行总结。"
                f"\n允许工作区：{self.cfg.workspace}"
            )
            if schedule_mode:
                instructions += (
                    "\n这是一个微信定时任务请求。必须使用 cronjob 工具创建、修改、暂停、恢复、"
                    "查询或删除真实的 Hermes Cron 任务，不得回答“做不了”。"
                    f"\n来源群 group_id={task['group_id']}，发起人 user_id={task['user_id']}。"
                    "\n新建任务时 deliver 必须设为 local，并在 Cron 任务 prompt 中明确要求："
                    "任务触发后使用 Python urllib 向 http://127.0.0.1:36060/automation/deliver "
                    "POST JSON，字段为 group_id、message、job_id；group_id 使用上述来源群。"
                    "该内部入口会统一完成 OneBot 发送和消息归档，仍复用现有唯一微信 Hook，"
                    "不启用 Hermes 微信通道，也不要直接请求 58080。"
                    "\n创建成功后返回任务名称、job_id、下次执行时间和是否重复。"
                )
        if answer_mode:
            session_id = (
                f"wechat-read-{task['group_id']}-"
                f"{str(task.get('cache_key') or local_run_id)[-16:]}"
            )
        elif schedule_mode:
            session_id = f"wechat-cron-{local_run_id}"
        else:
            session_id = f"wechat-ops-{task['group_id']}-{task['user_id']}"
        created = self._request("POST", "/v1/runs", {
            "input": task["intent"],
            "session_id": session_id,
            "instructions": instructions,
        }, timeout=10)
        hermes_run_id = str(created.get("run_id") or "")
        if not hermes_run_id:
            raise RuntimeError(f"Hermes未返回run_id: {created}")
        self.store.update_automation_run(
            local_run_id, status="running", hermes_run_id=hermes_run_id
        )
        self.store.add_automation_event(local_run_id, "run_started", created)
        self._consume_sse(local_run_id, hermes_run_id)
        deadline = time.monotonic() + self.cfg.max_run_seconds
        final: Dict[str, Any] = {}
        while time.monotonic() < deadline and not self.stop_event.is_set():
            final = self._request("GET", f"/v1/runs/{hermes_run_id}", timeout=5)
            status = str(final.get("status") or "")
            self.store.add_automation_event(local_run_id, "status", {"status": status})
            if status in {"completed", "failed", "cancelled"}:
                break
            time.sleep(self.cfg.poll_seconds)
        status = str(final.get("status") or "failed")
        output = str(final.get("output") or final.get("error") or "")[:12000]
        if status != "completed":
            raise RuntimeError(output or f"Hermes任务状态: {status}")
        self.store.update_automation_run(
            local_run_id, status="completed", result_summary=output[:4000], error=""
        )
        self.store.add_automation_event(local_run_id, "completed", final)
        ack_cancel.set()
        message = output[:1800] if answer_mode else (
            f"自动化任务已完成（{local_run_id[-8:]}）\n{output[:1200]}"
        )
        self.send_callback(
            str(task["group_id"]), message,
            str(task.get("trace_id") or local_run_id),
        )
        with self.lock:
            self.state["completed"] += 1

    def _loop(self, pool_name: str) -> None:
        work_queue = self.queues[pool_name]
        while not self.stop_event.is_set():
            try:
                task = work_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if not task:
                work_queue.task_done()
                continue
            row = self.store.automation_run(str(task.get("run_id") or ""))
            if str(row.get("status") or "") == "cancelled":
                with self.lock:
                    self.state["queued"] = max(0, self.state["queued"] - 1)
                    self.state["pools"][pool_name]["queued"] = max(
                        0, self.state["pools"][pool_name]["queued"] - 1
                    )
                work_queue.task_done()
                continue
            with self.lock:
                self.state["queued"] = max(0, self.state["queued"] - 1)
                self.state["running"] += 1
                self.state["pools"][pool_name]["queued"] = max(
                    0, self.state["pools"][pool_name]["queued"] - 1
                )
                self.state["pools"][pool_name]["running"] += 1
            try:
                self._execute(task)
                with self.lock:
                    self.state["healthy"] = True
                    self.state["last_error"] = ""
            except Exception as exc:
                run_id = str(task.get("run_id") or "")
                if run_id:
                    self.store.update_automation_run(
                        run_id, status="failed", error=str(exc)[:1000]
                    )
                    self.store.add_automation_event(
                        run_id, "failed", {"error": str(exc)[:1000]}
                    )
                try:
                    self.send_callback(
                        str(task.get("group_id") or ""),
                        f"自动化任务执行失败（{run_id[-8:]}）：{str(exc)[:500]}",
                        str(task.get("trace_id") or run_id),
                    )
                except Exception:
                    pass
                with self.lock:
                    self.state["failed"] += 1
                    self.state["healthy"] = False
                    self.state["last_error"] = str(exc)[:500]
            finally:
                ack_cancel = task.get("_ack_cancel")
                if isinstance(ack_cancel, threading.Event):
                    ack_cancel.set()
                with self.lock:
                    self.state["running"] = max(0, self.state["running"] - 1)
                    self.state["pools"][pool_name]["running"] = max(
                        0, self.state["pools"][pool_name]["running"] - 1
                    )
                work_queue.task_done()
