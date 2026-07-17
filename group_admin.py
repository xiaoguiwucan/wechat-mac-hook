#!/usr/bin/env python3
"""Group-scoped administrator commands and consistent WeChat text cards."""
from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from memory_store import MemoryStore

ALL_PERMISSIONS = [
    "status.view", "reply.control", "strategy.manage", "media.manage",
    "personality.manage", "members.manage", "memory.manage", "audit.view",
]
ROLE_PERMISSIONS = {
    "observer": ["status.view", "audit.view"],
    "moderator": ["status.view", "reply.control", "members.manage", "memory.manage", "audit.view"],
    "admin": list(ALL_PERMISSIONS),
    "custom": [],
}
ROLE_LABELS = {"observer": "观察员", "moderator": "协管员", "admin": "群管理员", "custom": "自定义"}
PERMISSION_LABELS = {
    "status.view": "状态查看", "reply.control": "回复控制", "strategy.manage": "策略管理",
    "media.manage": "媒介管理", "personality.manage": "性格管理", "members.manage": "成员管理",
    "memory.manage": "上下文管理", "audit.view": "审计查看",
}

COMMAND_PERMISSION = {
    "#状态": "status.view", "#任务": "status.view", "#错误": "status.view",
    "#机器人": "reply.control", "#闭嘴": "reply.control", "#开口": "reply.control",
    "#档位": "strategy.manage", "#阈值": "strategy.manage", "#艾特": "strategy.manage",
    "#自动媒介": "media.manage", "#语音": "media.manage", "#表情": "media.manage",
    "#性格": "personality.manage", "#黑名单": "members.manage", "#屏蔽": "members.manage",
    "#解除屏蔽": "members.manage", "#重置上下文": "memory.manage", "#审计": "audit.view",
}
PUBLIC_PREFIXES = ("#菜单", "#帮助", "#我的权限")
MANAGEMENT_PREFIXES = tuple(COMMAND_PERMISSION)


def _safe_name(value: Any, fallback: str = "群友", limit: int = 16) -> str:
    name = re.sub(r"[\r\n\t]+", " ", str(value or "")).strip()
    if not name or name.lower().startswith("wxid_") or name.endswith("@chatroom"):
        name = fallback
    return name if len(name) <= limit else name[: max(1, limit - 1)] + "…"


def normalize_command(text: str) -> str:
    value = str(text or "").replace("\u2005", " ").replace("\u00a0", " ").strip()
    value = re.sub(r"^@\S+\s+", "", value).strip()
    return re.sub(r"[ \t]+", " ", value)


def is_admin_command(text: str) -> bool:
    value = normalize_command(text)
    return value.startswith(PUBLIC_PREFIXES + MANAGEMENT_PREFIXES)


def permission_for_command(text: str) -> str:
    value = normalize_command(text)
    return next((permission for prefix, permission in COMMAND_PERMISSION.items() if value.startswith(prefix)), "")


def effective_permissions(admin: Dict[str, Any]) -> List[str]:
    role = str(admin.get("role") or "custom")
    explicit = [str(value) for value in admin.get("permissions") or [] if str(value) in ALL_PERMISSIONS]
    return sorted(set(ROLE_PERMISSIONS.get(role, []) + explicit))


def _card(title: str, lines: List[str], footer: str = "") -> str:
    body = "\n".join(f"│  {line}" if line else "│" for line in lines)
    tail = f"\n╰─ {footer}" if footer else "\n╰────────────────"
    return f"╭─ {title}\n{body}{tail}"


def menu_card(group_name: str, admin: Dict[str, Any], category: str = "") -> str:
    group = _safe_name(group_name, "当前群", 20)
    is_admin = bool(admin)
    permissions = effective_permissions(admin)
    role = ROLE_LABELS.get(str(admin.get("role") or ""), "本群管理员")
    header = [f"{group} · {'本群管理员' if is_admin else '普通群友'}"]
    if is_admin:
        header.append(f"{role} · 权限 {len(permissions)}/{len(ALL_PERMISSIONS)}")
    sections = {
        "运行": [("#状态", "status.view", "查看本群机器人、任务与配置"),
                 ("#机器人 开 / 关", "reply.control", "开启或暂停普通回复"),
                 ("#闭嘴 3m", "reply.control", "临时静默本群"),
                 ("#开口", "reply.control", "立即解除静默")],
        "策略": [("#档位 克制 / 自然 / 老油条", "strategy.manage", "切换本群接话风格"),
                 ("#阈值 0-100", "strategy.manage", "设置本群接话门槛"),
                 ("#艾特 开 / 关", "strategy.manage", "控制回复时是否艾特提问人")],
        "媒介": [("#自动媒介 开 / 关", "media.manage", "控制语音和表情自动选择"),
                 ("#语音 0-100", "media.manage", "设置合格后的语音抽样概率"),
                 ("#表情 0-100", "media.manage", "设置合格后的表情抽样概率")],
        "性格": [("#性格 查看", "personality.manage", "查看当前群独立性格"),
                 ("#性格 开 / 关", "personality.manage", "启停本群独立性格"),
                 ("#性格 设置 <提示词>", "personality.manage", "设置本群表达方式")],
        "成员": [("#黑名单", "members.manage", "查看本群屏蔽成员"),
                 ("#屏蔽 @成员", "members.manage", "屏蔽指定成员的普通对话"),
                 ("#解除屏蔽 @成员", "members.manage", "恢复指定成员的普通对话")],
        "记忆": [("#重置上下文 确认", "memory.manage", "清空本群短期上下文"),
                 ("#任务", "status.view", "查看本群活动任务"),
                 ("#错误", "status.view", "查看本群最近错误"),
                 ("#审计 5", "audit.view", "查看最近管理记录")],
    }
    aliases = {"接话": "策略", "语音": "媒介", "表情": "媒介", "人格": "性格", "审计": "记忆"}
    selected = aliases.get(category, category)
    if not is_admin:
        return _card("✦ 小风 · 群管理台", header + [
            "", "这里是群管理入口", "操作仅限本群授权管理员", "", "#我的权限",
        ], "发送命令查看当前身份")
    if selected not in sections:
        return _card("✦ 小风 · 群管理台", header + [
            "",
            "◈ 快捷",
            f"{'●' if 'status.view' in permissions else '○'} #状态",
            f"{'●' if 'reply.control' in permissions else '○'} #机器人 开 / 关",
            f"{'●' if 'reply.control' in permissions else '○'} #闭嘴 3m  ·  #开口",
            "",
            "◈ 功能",
            f"{'●' if 'strategy.manage' in permissions else '○'} #菜单 策略",
            f"{'●' if 'media.manage' in permissions else '○'} #菜单 媒介",
            f"{'●' if 'personality.manage' in permissions else '○'} #菜单 性格",
            f"{'●' if 'members.manage' in permissions else '○'} #菜单 成员",
            f"{'●' if ('memory.manage' in permissions or 'audit.view' in permissions) else '○'} #菜单 记忆",
        ], "● 可执行  ○ 仅查看 · #帮助 命令")
    visible = {selected: sections[selected]}
    lines = [f"{group} · {role}", f"分类  {selected}", ""]
    for label, commands in visible.items():
        for command, permission, description in commands:
            mark = "●" if permission in permissions else "○"
            lines.append(f"{mark} {command}")
            lines.append(f"  {description}")
        lines.append("")
    return _card(f"✦ 小风 · {selected}控制", lines[:-1], "返回主菜单：#菜单")


def unauthorized_card(permission: str) -> str:
    return _card("! 操作未执行", [
        "当前账号没有", f"「{PERMISSION_LABELS.get(permission, '群管理')}」权限", "",
        "发送 #我的权限 查看授权",
    ])


def error_card(reason: str, example: str = "") -> str:
    lines = ["原因", str(reason)[:180]]
    if example:
        lines += ["", "示例", example]
    return _card("× 配置更新失败", lines, "原配置未发生变化")


def success_card(label: str, before: Any, after: Any, group_name: str,
                 operator: str, pid: int = 0) -> str:
    return _card("✓ 配置已更新", [
        label, f"{before}  →  {after}", "",
        f"作用范围  仅当前群", f"操作者    {_safe_name(operator)}", "生效方式  实时热加载",
    ], f"AI PID {pid or os.getpid()} 未变化")


def _duration_seconds(value: str) -> int:
    match = re.fullmatch(r"(\d+)(s|m|h)", value.lower())
    if not match:
        raise ValueError("静默时长格式错误")
    amount = int(match.group(1))
    factor = {"s": 1, "m": 60, "h": 3600}[match.group(2)]
    seconds = amount * factor
    if not 10 <= seconds <= 86400:
        raise ValueError("静默时长必须在 10 秒到 24 小时之间")
    return seconds


class GroupAdminService:
    def __init__(self, store: MemoryStore, config_path: Path | str,
                 reload_callback: Optional[Callable[[], Dict[str, Any]]] = None,
                 runtime_callback: Optional[Callable[[str], Dict[str, Any]]] = None):
        self.store = store
        self.config_path = Path(config_path)
        self.reload_callback = reload_callback
        self.runtime_callback = runtime_callback
        self.lock_path = self.config_path.with_suffix(self.config_path.suffix + ".lock")

    def admin(self, group_id: str, user_id: str) -> Dict[str, Any]:
        return self.store.group_admin(group_id, user_id)

    def _read_config(self) -> Dict[str, Any]:
        try:
            return json.loads(self.config_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _write_config(self, config: Dict[str, Any]) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            self._write_config_unlocked(config)
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _write_config_unlocked(self, config: Dict[str, Any]) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=self.config_path.name + ".", dir=str(self.config_path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(config, file, ensure_ascii=False, indent=2)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(tmp, self.config_path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    @staticmethod
    def _group_setting(config: Dict[str, Any], group_id: str) -> Dict[str, Any]:
        strategy = config.get("reply_strategy") if isinstance(config.get("reply_strategy"), dict) else {}
        overrides = strategy.get("group_overrides") if isinstance(strategy.get("group_overrides"), dict) else {}
        group_strategy = dict(overrides.get(group_id) or {})
        media = config.get("media_reply") if isinstance(config.get("media_reply"), dict) else {}
        media_overrides = media.get("group_overrides") if isinstance(media.get("group_overrides"), dict) else {}
        group_media = dict(media_overrides.get(group_id) or {})
        personalities = config.get("group_personalities") if isinstance(config.get("group_personalities"), dict) else {}
        personality = personalities.get(group_id) or {}
        if isinstance(personality, str):
            personality = {"enabled": True, "prompt": personality}
        enabled_map = config.get("group_reply_enabled") if isinstance(config.get("group_reply_enabled"), dict) else {}
        return {
            "reply_enabled": bool(enabled_map.get(group_id, True)),
            "mode": group_strategy.get("mode", strategy.get("mode", "veteran")),
            "threshold": float(group_strategy.get("threshold", strategy.get("threshold", 52))),
            "mention_user_on_reply": bool(group_strategy.get(
                "mention_user_on_reply", strategy.get("mention_user_on_reply", True)
            )),
            "automatic_enabled": bool(group_media.get("automatic_enabled", media.get("automatic_enabled", True))),
            "voice_probability": float(group_media.get("voice_probability", media.get("voice_probability", 0.15))),
            "face_probability": float(group_media.get("face_probability", media.get("face_probability", 0.20))),
            "personality_enabled": bool(personality.get("enabled", bool(personality.get("prompt")))),
            "personality": str(personality.get("prompt") or ""),
        }

    def _mutate_config(self, mutator: Callable[[Dict[str, Any]], tuple[str, Any, Any]]) -> tuple[str, Any, Any]:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            old = self._read_config()
            updated = json.loads(json.dumps(old, ensure_ascii=False))
            label, before, after = mutator(updated)
            self._write_config_unlocked(updated)
            if self.reload_callback:
                try:
                    result = self.reload_callback() or {}
                    if result.get("applied") is False:
                        raise RuntimeError(str(result.get("error") or "热加载失败"))
                except Exception:
                    self._write_config_unlocked(old)
                    if self.reload_callback:
                        try:
                            self.reload_callback()
                        except Exception:
                            pass
                    raise
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            return label, before, after

    def _target_user(self, raw: Dict[str, Any]) -> str:
        for segment in raw.get("message") or []:
            if isinstance(segment, dict) and segment.get("type") == "at":
                data = segment.get("data") or {}
                target = str(data.get("user_id") or data.get("qq") or "").strip()
                if target and not target.endswith("@chatroom"):
                    return target
        quoted = raw.get("_quoted_message") if isinstance(raw.get("_quoted_message"), dict) else {}
        return str(quoted.get("user_id") or quoted.get("sender_id") or "").strip()

    def handle(self, evt: Any) -> Optional[Dict[str, Any]]:
        command = normalize_command(evt.text)
        if not is_admin_command(command):
            return None
        admin = self.admin(evt.group_id, evt.user_id)
        if command.startswith("#菜单"):
            category = command.removeprefix("#菜单").strip()
            return {"handled": True, "card": menu_card(evt.group_name, admin, category), "public": True}
        if command == "#我的权限":
            if not admin:
                return {"handled": True, "card": _card("◇ 我的群管理权限", [
                    f"群聊  {_safe_name(evt.group_name, '当前群', 20)}", "身份  普通群友", "权限  无",
                ])}
            permissions = effective_permissions(admin)
            return {"handled": True, "card": _card("◇ 我的群管理权限", [
                f"群聊  {_safe_name(evt.group_name, '当前群', 20)}",
                f"角色  {ROLE_LABELS.get(admin.get('role'), '自定义')}",
                *[f"● {PERMISSION_LABELS.get(value, value)}" for value in permissions],
            ])}
        if command.startswith("#帮助"):
            topic = command.removeprefix("#帮助").strip() or "#菜单"
            return {"handled": True, "card": self._help_card(topic)}
        permission = permission_for_command(command)
        if not admin or permission not in effective_permissions(admin):
            return {"handled": True, "card": unauthorized_card(permission), "authorized": False}
        if not evt.message_id or not evt.user_id or evt.user_id == evt.self_id:
            return {"handled": True, "card": error_card("管理命令必须来自真实群消息"), "authorized": False}

        created, audit = self.store.add_group_admin_audit({
            "group_id": evt.group_id, "user_id": evt.user_id, "display_name": evt.sender_name,
            "command": command, "permission": permission, "message_id": evt.message_id,
            "trace_id": evt.trace_id, "result": "processing",
        })
        if not created:
            return {"handled": True, "duplicate": True, "card": ""}
        audit_id = int(audit["id"])
        before_snapshot: Dict[str, Any] = {}
        after_snapshot: Dict[str, Any] = {}
        try:
            card, before_snapshot, after_snapshot = self._execute(command, evt)
            self.store.finish_group_admin_audit(audit_id, before_snapshot, after_snapshot, "success")
            return {"handled": True, "authorized": True, "card": card, "audit_id": audit_id}
        except Exception as exc:
            self.store.finish_group_admin_audit(audit_id, before_snapshot, after_snapshot, "failed", str(exc))
            return {"handled": True, "authorized": True, "card": error_card(str(exc), self._example(command)),
                    "audit_id": audit_id, "error": str(exc)}

    def _execute(self, command: str, evt: Any) -> tuple[str, Dict[str, Any], Dict[str, Any]]:
        current = self._group_setting(self._read_config(), evt.group_id)
        if command in {"#状态", "#任务", "#错误"}:
            runtime = self.runtime_callback(evt.group_id) if self.runtime_callback else {}
            if command == "#状态":
                mute = self.store.group_reply_mute(evt.group_id)
                personality = current["personality"] or "使用全局性格"
                card = _card(f"◉ {_safe_name(evt.group_name, '当前群', 18)} · 运行状态", [
                    f"机器人回复    {'● 开启' if current['reply_enabled'] else '○ 暂停'}",
                    f"当前档位      {self._mode_label(current['mode'])}",
                    f"接话阈值      {current['threshold']:g}",
                    f"艾特提问人    {'● 开启' if current['mention_user_on_reply'] else '○ 关闭'}",
                    f"自动语音      {current['voice_probability'] * 100:g}%",
                    f"自动表情      {current['face_probability'] * 100:g}%",
                    f"群聊性格      {_safe_name(personality, '使用全局性格', 24)}",
                    f"静默状态      {str(mute.get('remaining_seconds')) + ' 秒' if mute.get('active') else '未静默'}",
                    f"活动任务      {int(runtime.get('active_tasks') or 0)}",
                ], "更新于 " + time.strftime("%H:%M:%S"))
                return card, current, current
            key = "tasks" if command == "#任务" else "errors"
            rows = list(runtime.get(key) or [])[:8]
            lines = [str(value)[:160] for value in rows] or ["当前没有记录"]
            return _card("◇ " + ("当前任务" if command == "#任务" else "最近错误"), lines), current, current
        if command.startswith("#审计"):
            amount_text = command.removeprefix("#审计").strip()
            amount = max(1, min(20, int(amount_text or 5)))
            rows = self.store.group_admin_audit(evt.group_id, amount)
            lines: List[str] = []
            for row in reversed(rows):
                lines += [f"{str(row.get('created_at') or '')[11:16]}  {_safe_name(row.get('display_name'))}",
                          str(row.get("command") or "")[:80], ""]
            return _card("◇ 最近管理记录", lines or ["暂无管理记录"], f"共显示最近 {len(rows)} 条"), current, current
        if command == "#黑名单":
            config = self._read_config()
            ids = list((config.get("ignored_group_members") or {}).get(evt.group_id) or [])
            names = [self.store.resolve_member_name(evt.group_id, value, "") for value in ids]
            return _card("◇ 本群对话黑名单", [f"● {_safe_name(name)}" for name in names] or ["当前没有屏蔽成员"]), current, current
        if command == "#性格 查看":
            return _card("◇ 本群群聊性格", [
                f"状态  {'● 开启' if current['personality_enabled'] else '○ 关闭'}",
                current["personality"] or "尚未设置，当前使用全局性格",
            ]), current, current
        if command.startswith("#闭嘴"):
            value = command.removeprefix("#闭嘴").strip() or "3m"
            seconds = _duration_seconds(value)
            before = self.store.group_reply_mute(evt.group_id)
            after = self.store.set_group_reply_mute(evt.group_id, seconds, evt.user_id, evt.message_id)
            return success_card("本群静默", "未静默" if not before.get("active") else f"{before['remaining_seconds']} 秒",
                                f"{seconds} 秒", evt.group_name, evt.sender_name), before, after
        if command == "#开口":
            before = self.store.group_reply_mute(evt.group_id)
            self.store.clear_group_reply_mute(evt.group_id)
            return success_card("本群静默", f"{before.get('remaining_seconds', 0)} 秒", "已解除",
                                evt.group_name, evt.sender_name), before, {"active": False}
        if command == "#重置上下文 确认":
            before = self.store.group_memory(evt.group_id)
            self.store.save_group_memory(evt.group_id, "", [])
            return success_card("短期上下文", "已有记录", "已清空", evt.group_name, evt.sender_name), before, {}
        if command.startswith("#屏蔽") or command.startswith("#解除屏蔽"):
            remove = command.startswith("#解除屏蔽")
            target = self._target_user(evt.raw)
            if not target:
                raise ValueError("请真实 @ 或引用需要管理的群成员")
            if self.store.group_admin(evt.group_id, target):
                raise ValueError("不能屏蔽本群管理员")
            def mutate_member(config: Dict[str, Any]) -> tuple[str, Any, Any]:
                mapping = config.setdefault("ignored_group_members", {})
                values = set(str(x) for x in mapping.get(evt.group_id) or [])
                before = target in values
                values.discard(target) if remove else values.add(target)
                mapping[evt.group_id] = sorted(values)
                return ("解除成员屏蔽" if remove else "屏蔽成员"), ("已屏蔽" if before else "未屏蔽"), ("未屏蔽" if remove else "已屏蔽")
            label, before, after = self._mutate_config(mutate_member)
            return success_card(label, before, after, evt.group_name, evt.sender_name), {"target": target, "state": before}, {"target": target, "state": after}

        def mutate(config: Dict[str, Any]) -> tuple[str, Any, Any]:
            group_id = evt.group_id
            if command.startswith("#机器人"):
                value = command.removeprefix("#机器人").strip()
                enabled = self._on_off(value)
                mapping = config.setdefault("group_reply_enabled", {})
                before = bool(mapping.get(group_id, True))
                mapping[group_id] = enabled
                return "机器人普通回复", self._on_off_label(before), self._on_off_label(enabled)
            if command.startswith("#档位") or command.startswith("#阈值") or command.startswith("#艾特"):
                strategy = config.setdefault("reply_strategy", {})
                overrides = strategy.setdefault("group_overrides", {})
                group = overrides.setdefault(group_id, {})
                if command.startswith("#艾特"):
                    enabled = self._on_off(command.removeprefix("#艾特").strip())
                    before = bool(group.get(
                        "mention_user_on_reply", strategy.get("mention_user_on_reply", True)
                    ))
                    group["mention_user_on_reply"] = enabled
                    return "回复艾特提问人", self._on_off_label(before), self._on_off_label(enabled)
                if command.startswith("#档位"):
                    value = command.removeprefix("#档位").strip()
                    modes = {"克制": ("reserved", 78), "自然": ("natural", 65), "老油条": ("veteran", 52)}
                    if value not in modes:
                        raise ValueError("档位必须是 克制、自然 或 老油条")
                    before = self._mode_label(group.get("mode", strategy.get("mode", "veteran")))
                    group["mode"], group["threshold"] = modes[value]
                    return "接话档位", before, value
                value = float(command.removeprefix("#阈值").strip())
                if not 0 <= value <= 100:
                    raise ValueError("参数必须在 0-100 之间")
                before = float(group.get("threshold", strategy.get("threshold", 52)))
                group.update({"mode": "custom", "threshold": value})
                return "接话阈值", f"{before:g}", f"{value:g}"
            if command.startswith(("#自动媒介", "#语音", "#表情")):
                media = config.setdefault("media_reply", {})
                group = media.setdefault("group_overrides", {}).setdefault(group_id, {})
                if command.startswith("#自动媒介"):
                    enabled = self._on_off(command.removeprefix("#自动媒介").strip())
                    before = bool(group.get("automatic_enabled", media.get("automatic_enabled", True)))
                    group["automatic_enabled"] = enabled
                    return "自动媒介", self._on_off_label(before), self._on_off_label(enabled)
                prefix, key, label = ("#语音", "voice_probability", "自动语音") if command.startswith("#语音") else ("#表情", "face_probability", "自动表情")
                value = float(command.removeprefix(prefix).strip())
                if not 0 <= value <= 100:
                    raise ValueError("参数必须在 0-100 之间")
                before = float(group.get(key, media.get(key, 0.15 if key.startswith("voice") else 0.2))) * 100
                group[key] = value / 100
                return label, f"{before:g}%", f"{value:g}%"
            if command.startswith("#性格"):
                personalities = config.setdefault("group_personalities", {})
                item = personalities.get(group_id)
                if not isinstance(item, dict):
                    item = {"enabled": bool(item), "prompt": str(item or "")}
                arg = command.removeprefix("#性格").strip()
                if arg in {"开", "关"}:
                    before = bool(item.get("enabled", bool(item.get("prompt"))))
                    item["enabled"] = arg == "开"
                    personalities[group_id] = item
                    return "群聊性格", self._on_off_label(before), self._on_off_label(item["enabled"])
                if arg.startswith("设置 "):
                    prompt = arg.removeprefix("设置 ").strip()
                    if not 2 <= len(prompt) <= 1000:
                        raise ValueError("性格提示词长度必须在 2-1000 字之间")
                    before = str(item.get("prompt") or "未设置")[:40]
                    item.update({"enabled": True, "prompt": prompt})
                    personalities[group_id] = item
                    return "群聊性格", before, prompt[:40]
                raise ValueError("请使用 #性格 查看、#性格 开/关 或 #性格 设置 <提示词>")
            raise ValueError("无法识别该管理命令")

        label, before, after = self._mutate_config(mutate)
        latest = self._group_setting(self._read_config(), evt.group_id)
        return success_card(label, before, after, evt.group_name, evt.sender_name), current, latest

    @staticmethod
    def _on_off(value: str) -> bool:
        if value in {"开", "开启"}:
            return True
        if value in {"关", "关闭"}:
            return False
        raise ValueError("参数必须是 开 或 关")

    @staticmethod
    def _on_off_label(value: bool) -> str:
        return "开启" if value else "关闭"

    @staticmethod
    def _mode_label(value: Any) -> str:
        return {"reserved": "克制", "natural": "自然", "veteran": "老油条", "custom": "自定义"}.get(str(value), str(value))

    @staticmethod
    def _example(command: str) -> str:
        prefix = next((value for value in MANAGEMENT_PREFIXES if command.startswith(value)), "")
        return {
            "#机器人": "#机器人 开", "#闭嘴": "#闭嘴 3m", "#档位": "#档位 自然",
            "#阈值": "#阈值 65", "#艾特": "#艾特 关",
            "#自动媒介": "#自动媒介 开", "#语音": "#语音 30",
            "#表情": "#表情 40", "#性格": "#性格 设置 会接梗的损友",
            "#屏蔽": "#屏蔽 @成员", "#解除屏蔽": "#解除屏蔽 @成员",
            "#重置上下文": "#重置上下文 确认", "#审计": "#审计 5",
        }.get(prefix, "#菜单")

    @staticmethod
    def _help_card(topic: str) -> str:
        examples = {
            "#状态": ("查看本群回复、阈值、媒介、性格、静默和任务状态", "#状态"),
            "#机器人": ("只控制当前群普通 AI 回复，管理命令仍然有效", "#机器人 开"),
            "#闭嘴": ("临时暂停本群普通回复，支持 s/m/h", "#闭嘴 3m"),
            "#阈值": ("设置当前群接话门槛，范围 0-100", "#阈值 65"),
            "#艾特": ("控制当前群普通回复是否艾特提问人；引用和管理卡片不受影响", "#艾特 关"),
            "#表情": ("设置语境与素材合格后的表情抽样概率", "#表情 40"),
            "#语音": ("设置语境与素材合格后的语音抽样概率", "#语音 30"),
            "#性格": ("查看、启停或设置当前群独立性格", "#性格 设置 会接梗的损友"),
            "#屏蔽": ("屏蔽真实 @ 或引用的本群成员", "#屏蔽 @成员"),
        }
        detail, example = examples.get(topic if topic.startswith("#") else "#" + topic, ("查看群管理命令与分类菜单", "#菜单 策略"))
        return _card("◇ 命令帮助", ["说明", detail, "", "示例", example])
