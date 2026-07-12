#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""macOS 第二微信 / OneBot / AI 回复桌面管理器。"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Tuple

import tkinter as tk
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText

ROOT_DIR = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT_DIR / "config" / "ai_reply_config.json"
ENV_PATH = ROOT_DIR / "config" / "ai_reply.env"
SECOND_HOME = Path.home() / "Library" / "Application Support" / "WeChatSecond"
LOG_DIR = SECOND_HOME / "logs"
AI_LOG = LOG_DIR / "ai-reply.log"
ONEBOT_LOG = LOG_DIR / "onebot-wechat2.log"
WECHAT2_APP = Path.home() / "Applications" / "WeChat2.app"

SCRIPT = lambda name: str(ROOT_DIR / "scripts" / name)

PROVIDER_PRESETS = {
    "DeepSeek": ("https://api.deepseek.com/v1", "deepseek-chat"),
    "OpenAI": ("https://api.openai.com/v1", "gpt-4o-mini"),
    "第三方中转站(OpenAI兼容)": ("https://你的中转站域名/v1", ""),
    "OpenRouter": ("https://openrouter.ai/api/v1", "openai/gpt-4o-mini"),
    "本地OpenAI兼容": ("http://127.0.0.1:1234/v1", "local-model"),
}


def read_json(path: Path, default: Dict[str, Any] | None = None) -> Dict[str, Any]:
    if not path.exists():
        return dict(default or {})
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_env_file(path: Path) -> Dict[str, str]:
    env: Dict[str, str] = {}
    if not path.exists():
        return env
    for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        try:
            parts = shlex.split(line, posix=True)
        except Exception:
            parts = [line]
        for part in parts:
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            key = key.strip()
            if key:
                env[key] = value
    return env


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def env_bool_value(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def infer_provider(base_url: str) -> str:
    b = base_url.lower()
    if "deepseek" in b:
        return "DeepSeek"
    if "openrouter" in b:
        return "OpenRouter"
    if "api.openai.com" in b:
        return "OpenAI"
    if "127.0.0.1" in b or "localhost" in b:
        return "本地OpenAI兼容"
    return "第三方中转站(OpenAI兼容)"


class ManagerApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("第二微信 AI 助手管理器")
        self.geometry("1080x760")
        self.minsize(980, 680)
        self._setup_style()
        self._build_vars()
        self._build_ui()
        self.load_config()
        self.refresh_status()
        self.after(3000, self._periodic_status)

    def _setup_style(self) -> None:
        """Force a visible cross-platform ttk theme.

        macOS CommandLineTools Python currently ships with an old Tk 8.5.  In
        some dark/transparent Aqua combinations ttk widgets render white text
        and borders on a white background, leaving only the raw Text scrollbar
        visible.  Using the non-native "clam" theme plus explicit colors makes
        every label/button/notebook/treeview visible.
        """
        self.configure(bg="#f2f4f7")
        self.option_add("*Font", "Arial 13")
        self.option_add("*foreground", "#111827")
        self.option_add("*background", "#f2f4f7")
        self.option_add("*Entry.background", "#ffffff")
        self.option_add("*Text.background", "#ffffff")
        self.option_add("*Text.foreground", "#111827")
        self.option_add("*Listbox.background", "#ffffff")
        self.option_add("*Listbox.foreground", "#111827")
        style = ttk.Style(self)
        try:
            if "clam" in style.theme_names():
                style.theme_use("clam")
        except Exception:
            pass
        bg = "#f2f4f7"
        panel = "#ffffff"
        border = "#d1d5db"
        fg = "#111827"
        muted = "#374151"
        accent = "#2563eb"
        style.configure(".", background=bg, foreground=fg, fieldbackground=panel)
        style.configure("TFrame", background=bg)
        style.configure("TLabel", background=bg, foreground=fg)
        style.configure("TLabelframe", background=bg, foreground=fg, bordercolor=border, lightcolor=border, darkcolor=border)
        style.configure("TLabelframe.Label", background=bg, foreground=fg)
        style.configure("TButton", background="#e5e7eb", foreground=fg, bordercolor=border, focusthickness=1, focuscolor=accent, padding=(8, 4))
        style.map("TButton", background=[("active", "#dbeafe"), ("pressed", "#bfdbfe")], foreground=[("disabled", "#9ca3af")])
        style.configure("TCheckbutton", background=bg, foreground=fg)
        style.configure("TNotebook", background=bg, borderwidth=1)
        style.configure("TNotebook.Tab", background="#e5e7eb", foreground=fg, padding=(12, 7))
        style.map("TNotebook.Tab", background=[("selected", panel), ("active", "#dbeafe")], foreground=[("selected", accent), ("active", fg)])
        style.configure("Treeview", background=panel, foreground=fg, fieldbackground=panel, rowheight=24, bordercolor=border)
        style.configure("Treeview.Heading", background="#e5e7eb", foreground=fg, relief="flat")
        style.map("Treeview", background=[("selected", "#bfdbfe")], foreground=[("selected", "#111827")])
        style.configure("TEntry", fieldbackground=panel, foreground=fg, bordercolor=border)
        style.configure("TCombobox", fieldbackground=panel, background=panel, foreground=fg, selectbackground="#bfdbfe", selectforeground=fg)

    def _build_vars(self) -> None:
        self.provider_var = tk.StringVar(value="DeepSeek")
        self.base_url_var = tk.StringVar(value="https://api.deepseek.com/v1")
        self.api_key_var = tk.StringVar(value="")
        self.model_var = tk.StringVar(value="deepseek-chat")
        self.temperature_var = tk.StringVar(value="0.3")
        self.max_tokens_var = tk.StringVar(value="600")
        self.timeout_var = tk.StringVar(value="30")
        self.system_prompt_var = tk.StringVar(value="你是微信群值班助手。只根据群聊最新消息和少量上下文，用中文简洁回复；不确定时先澄清；不要编造事实；不要输出多余客套；回复适合直接发到群里。")

        self.reply_prefix_var = tk.StringVar(value="AI：")
        self.keywords_var = tk.StringVar(value="")
        self.require_keyword_var = tk.BooleanVar(value=False)
        self.dry_run_var = tk.BooleanVar(value=False)
        self.ignore_self_var = tk.BooleanVar(value=False)
        self.cooldown_var = tk.StringVar(value="2")
        self.max_reply_chars_var = tk.StringVar(value="600")
        self.onebot_api_var = tk.StringVar(value="http://127.0.0.1:58080")

        self.group_id_var = tk.StringVar(value="")
        self.group_name_var = tk.StringVar(value="值班群")
        self.test_prompt_var = tk.StringVar(value="请回复：AI接口测试成功")
        self.callback_test_text_var = tk.StringVar(value="值班群AI回调测试")
        self.send_test_text_var = tk.StringVar(value="第二微信助手固定消息测试")

        self.status_summary_var = tk.StringVar(value="状态加载中…")
        self.wechat2_status_var = tk.StringVar(value="WeChat2：未知")
        self.onebot_status_var = tk.StringVar(value="OneBot：未知")
        self.ai_status_var = tk.StringVar(value="AI服务：未知")

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        nb = ttk.Notebook(self)
        nb.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)

        self.tab_dashboard = ttk.Frame(nb)
        self.tab_ai = ttk.Frame(nb)
        self.tab_groups = ttk.Frame(nb)
        self.tab_test = ttk.Frame(nb)
        self.tab_logs = ttk.Frame(nb)
        nb.add(self.tab_dashboard, text="总控/后台")
        nb.add(self.tab_ai, text="AI 配置")
        nb.add(self.tab_groups, text="群配置")
        nb.add(self.tab_test, text="测试")
        nb.add(self.tab_logs, text="日志")

        self._build_dashboard_tab()
        self._build_ai_tab()
        self._build_groups_tab()
        self._build_test_tab()
        self._build_logs_tab()

    def _button(self, parent: tk.Widget, text: str, command, **kw) -> ttk.Button:
        return ttk.Button(parent, text=text, command=command, **kw)

    def _build_dashboard_tab(self) -> None:
        f = self.tab_dashboard
        f.columnconfigure(0, weight=1)
        f.rowconfigure(4, weight=1)

        top = ttk.LabelFrame(f, text="当前状态")
        top.grid(row=0, column=0, sticky="ew", padx=8, pady=8)
        for i in range(3):
            top.columnconfigure(i, weight=1)
        ttk.Label(top, textvariable=self.wechat2_status_var, font=("Menlo", 13)).grid(row=0, column=0, sticky="w", padx=10, pady=8)
        ttk.Label(top, textvariable=self.onebot_status_var, font=("Menlo", 13)).grid(row=0, column=1, sticky="w", padx=10, pady=8)
        ttk.Label(top, textvariable=self.ai_status_var, font=("Menlo", 13)).grid(row=0, column=2, sticky="w", padx=10, pady=8)
        ttk.Label(top, textvariable=self.status_summary_var, foreground="#555").grid(row=1, column=0, columnspan=3, sticky="w", padx=10, pady=(0, 8))

        actions = ttk.LabelFrame(f, text="启动 / 管理后台")
        actions.grid(row=1, column=0, sticky="ew", padx=8, pady=8)
        for i in range(5):
            actions.columnconfigure(i, weight=1)
        self._button(actions, "保存配置", self.save_config).grid(row=0, column=0, sticky="ew", padx=6, pady=6)
        self._button(actions, "启动第二微信", lambda: self.run_script("启动第二微信", "launch_wechat2_4_1_11_53.sh")).grid(row=0, column=1, sticky="ew", padx=6, pady=6)
        self._button(actions, "启动 OneBot", lambda: self.run_script("启动 OneBot", "start_onebot_wechat2.sh")).grid(row=0, column=2, sticky="ew", padx=6, pady=6)
        self._button(actions, "启动/复用 AI 服务", self.start_ai_after_save).grid(row=0, column=3, sticky="ew", padx=6, pady=6)
        self._button(actions, "一键启动全部", self.start_all_after_save).grid(row=0, column=4, sticky="ew", padx=6, pady=6)

        self._button(actions, "停止 AI 服务", lambda: self.run_script("停止 AI 服务", "stop_ai_reply.sh")).grid(row=1, column=0, sticky="ew", padx=6, pady=6)
        self._button(actions, "停止 OneBot", lambda: self.run_script("停止 OneBot", "stop_onebot_wechat2.sh")).grid(row=1, column=1, sticky="ew", padx=6, pady=6)
        self._button(actions, "停止后台(AI+OneBot)", lambda: self.run_script("停止后台", "stop_backend_wechat2.sh")).grid(row=1, column=2, sticky="ew", padx=6, pady=6)
        self._button(actions, "刷新状态", self.refresh_status).grid(row=1, column=3, sticky="ew", padx=6, pady=6)
        self._button(actions, "打开日志目录", lambda: self.open_path(LOG_DIR)).grid(row=1, column=4, sticky="ew", padx=6, pady=6)

        note = ttk.LabelFrame(f, text="固定约束")
        note.grid(row=2, column=0, sticky="ew", padx=8, pady=8)
        ttk.Label(
            note,
            text=(
                f"第二微信：{WECHAT2_APP}\n"
                "所有启动/附加脚本都只识别 com.tencent.xinWeChat.instance2 和 WeChat2.app；"
                "停止后台只停止 AI/OneBot，不关闭主微信，也不关闭第二微信。"
            ),
            foreground="#555",
            justify="left",
        ).grid(row=0, column=0, sticky="w", padx=10, pady=8)

        out_frame = ttk.LabelFrame(f, text="命令输出")
        out_frame.grid(row=4, column=0, sticky="nsew", padx=8, pady=8)
        out_frame.columnconfigure(0, weight=1)
        out_frame.rowconfigure(0, weight=1)
        self.dashboard_output = ScrolledText(out_frame, height=16, wrap="word")
        self.dashboard_output.grid(row=0, column=0, sticky="nsew")

    def _build_ai_tab(self) -> None:
        f = self.tab_ai
        f.columnconfigure(1, weight=1)
        f.rowconfigure(11, weight=1)
        pad = {"padx": 8, "pady": 5}

        ttk.Label(f, text="供应商预设").grid(row=0, column=0, sticky="e", **pad)
        provider = ttk.Combobox(f, textvariable=self.provider_var, values=list(PROVIDER_PRESETS.keys()), state="readonly")
        provider.grid(row=0, column=1, sticky="ew", **pad)
        provider.bind("<<ComboboxSelected>>", lambda _e: self.apply_provider_preset())

        ttk.Label(f, text="Base URL").grid(row=1, column=0, sticky="e", **pad)
        ttk.Entry(f, textvariable=self.base_url_var).grid(row=1, column=1, sticky="ew", **pad)

        ttk.Label(f, text="API Key").grid(row=2, column=0, sticky="e", **pad)
        key_entry = ttk.Entry(f, textvariable=self.api_key_var, show="•")
        key_entry.grid(row=2, column=1, sticky="ew", **pad)

        ttk.Label(f, text="模型").grid(row=3, column=0, sticky="e", **pad)
        model_row = ttk.Frame(f)
        model_row.grid(row=3, column=1, sticky="ew", **pad)
        model_row.columnconfigure(0, weight=1)
        self.model_combo = ttk.Combobox(model_row, textvariable=self.model_var)
        self.model_combo.grid(row=0, column=0, sticky="ew")
        self._button(model_row, "获取模型列表", self.fetch_models).grid(row=0, column=1, padx=(8, 0))

        param = ttk.Frame(f)
        param.grid(row=4, column=1, sticky="ew", **pad)
        for i in range(6):
            param.columnconfigure(i, weight=1)
        ttk.Label(param, text="temperature").grid(row=0, column=0, sticky="e")
        ttk.Entry(param, textvariable=self.temperature_var, width=8).grid(row=0, column=1, sticky="w")
        ttk.Label(param, text="max_tokens").grid(row=0, column=2, sticky="e")
        ttk.Entry(param, textvariable=self.max_tokens_var, width=8).grid(row=0, column=3, sticky="w")
        ttk.Label(param, text="timeout秒").grid(row=0, column=4, sticky="e")
        ttk.Entry(param, textvariable=self.timeout_var, width=8).grid(row=0, column=5, sticky="w")

        ttk.Label(f, text="回复前缀").grid(row=5, column=0, sticky="e", **pad)
        ttk.Entry(f, textvariable=self.reply_prefix_var).grid(row=5, column=1, sticky="ew", **pad)

        ttk.Label(f, text="触发关键词").grid(row=6, column=0, sticky="e", **pad)
        ttk.Entry(f, textvariable=self.keywords_var).grid(row=6, column=1, sticky="ew", **pad)
        ttk.Label(f, text="多个关键词用逗号分隔；为空时按群配置直接回复。", foreground="#666").grid(row=7, column=1, sticky="w", padx=8)

        opts = ttk.Frame(f)
        opts.grid(row=8, column=1, sticky="ew", **pad)
        ttk.Checkbutton(opts, text="必须包含关键词才回复", variable=self.require_keyword_var).grid(row=0, column=0, sticky="w", padx=(0, 16))
        ttk.Checkbutton(opts, text="Dry Run：只记录不发送", variable=self.dry_run_var).grid(row=0, column=1, sticky="w", padx=(0, 16))
        ttk.Checkbutton(opts, text="忽略第二微信自己发出的消息", variable=self.ignore_self_var).grid(row=0, column=2, sticky="w")

        more = ttk.Frame(f)
        more.grid(row=9, column=1, sticky="ew", **pad)
        ttk.Label(more, text="群冷却秒").grid(row=0, column=0, sticky="e")
        ttk.Entry(more, textvariable=self.cooldown_var, width=8).grid(row=0, column=1, sticky="w", padx=(4, 20))
        ttk.Label(more, text="最大回复字数").grid(row=0, column=2, sticky="e")
        ttk.Entry(more, textvariable=self.max_reply_chars_var, width=8).grid(row=0, column=3, sticky="w", padx=(4, 20))
        ttk.Label(more, text="OneBot API").grid(row=0, column=4, sticky="e")
        ttk.Entry(more, textvariable=self.onebot_api_var, width=26).grid(row=0, column=5, sticky="w", padx=(4, 0))

        ttk.Label(f, text="System Prompt").grid(row=10, column=0, sticky="ne", **pad)
        self.system_prompt_text = ScrolledText(f, height=7, wrap="word")
        self.system_prompt_text.grid(row=10, column=1, sticky="nsew", **pad)

        bottom = ttk.Frame(f)
        bottom.grid(row=12, column=1, sticky="ew", **pad)
        self._button(bottom, "保存配置", self.save_config).grid(row=0, column=0, padx=(0, 8))
        self._button(bottom, "保存并重启 AI", self.restart_ai_after_save).grid(row=0, column=1, padx=(0, 8))
        self._button(bottom, "测试 AI 接口", self.test_ai_api).grid(row=0, column=2, padx=(0, 8))

    def _build_groups_tab(self) -> None:
        f = self.tab_groups
        f.columnconfigure(0, weight=1)
        f.columnconfigure(1, weight=1)
        f.rowconfigure(1, weight=1)

        target_frame = ttk.LabelFrame(f, text="AI 会回复的群")
        target_frame.grid(row=0, column=0, rowspan=2, sticky="nsew", padx=8, pady=8)
        target_frame.columnconfigure(0, weight=1)
        target_frame.rowconfigure(0, weight=1)
        self.groups_tree = ttk.Treeview(target_frame, columns=("name", "id"), show="headings", height=14)
        self.groups_tree.heading("name", text="群名")
        self.groups_tree.heading("id", text="group_id")
        self.groups_tree.column("name", width=150, anchor="w")
        self.groups_tree.column("id", width=270, anchor="w")
        self.groups_tree.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        self.groups_tree.bind("<<TreeviewSelect>>", self.on_group_select)

        editor = ttk.Frame(target_frame)
        editor.grid(row=1, column=0, sticky="ew", padx=8, pady=4)
        editor.columnconfigure(1, weight=1)
        ttk.Label(editor, text="群名").grid(row=0, column=0, sticky="e", padx=4, pady=3)
        ttk.Entry(editor, textvariable=self.group_name_var).grid(row=0, column=1, sticky="ew", padx=4, pady=3)
        ttk.Label(editor, text="group_id").grid(row=1, column=0, sticky="e", padx=4, pady=3)
        ttk.Entry(editor, textvariable=self.group_id_var).grid(row=1, column=1, sticky="ew", padx=4, pady=3)

        btns = ttk.Frame(target_frame)
        btns.grid(row=2, column=0, sticky="ew", padx=8, pady=8)
        self._button(btns, "添加/更新", self.add_or_update_group).grid(row=0, column=0, padx=(0, 8))
        self._button(btns, "删除选中", self.delete_selected_group).grid(row=0, column=1, padx=(0, 8))
        self._button(btns, "保存群配置", self.save_config).grid(row=0, column=2, padx=(0, 8))

        recent_frame = ttk.LabelFrame(f, text="最近真实群消息：用于确认值班群 ID")
        recent_frame.grid(row=0, column=1, sticky="nsew", padx=8, pady=8)
        recent_frame.columnconfigure(0, weight=1)
        recent_frame.rowconfigure(0, weight=1)
        self.recent_tree = ttk.Treeview(recent_frame, columns=("id", "members", "sender", "text"), show="headings", height=12)
        self.recent_tree.heading("id", text="group_id")
        self.recent_tree.heading("members", text="人数")
        self.recent_tree.heading("sender", text="发送者")
        self.recent_tree.heading("text", text="最近文本")
        self.recent_tree.column("id", width=230, anchor="w")
        self.recent_tree.column("members", width=50, anchor="center")
        self.recent_tree.column("sender", width=100, anchor="w")
        self.recent_tree.column("text", width=230, anchor="w")
        self.recent_tree.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        self.recent_tree.bind("<<TreeviewSelect>>", self.on_recent_select)
        rbtns = ttk.Frame(recent_frame)
        rbtns.grid(row=1, column=0, sticky="ew", padx=8, pady=8)
        self._button(rbtns, "刷新最近群ID", lambda: self.load_recent_groups(async_run=True)).grid(row=0, column=0, padx=(0, 8))
        self._button(rbtns, "选中设为值班群", self.add_recent_as_target).grid(row=0, column=1, padx=(0, 8))

        hint = ttk.LabelFrame(f, text="使用方法")
        hint.grid(row=1, column=1, sticky="nsew", padx=8, pady=8)
        ttk.Label(
            hint,
            text="如果不确定哪个是值班群：先在值班群发一句话，再点“刷新最近群ID”，选中对应 group_id 后保存。",
            foreground="#555",
            wraplength=450,
            justify="left",
        ).grid(row=0, column=0, sticky="nw", padx=10, pady=10)

    def _build_test_tab(self) -> None:
        f = self.tab_test
        f.columnconfigure(1, weight=1)
        f.rowconfigure(5, weight=1)
        pad = {"padx": 8, "pady": 6}

        ttk.Label(f, text="AI测试提示词").grid(row=0, column=0, sticky="e", **pad)
        ttk.Entry(f, textvariable=self.test_prompt_var).grid(row=0, column=1, sticky="ew", **pad)
        self._button(f, "测试 AI 接口(不发微信)", self.test_ai_api).grid(row=0, column=2, sticky="ew", **pad)

        ttk.Label(f, text="回调测试文本").grid(row=1, column=0, sticky="e", **pad)
        ttk.Entry(f, textvariable=self.callback_test_text_var).grid(row=1, column=1, sticky="ew", **pad)
        self._button(f, "测试 AI 回调链路", self.test_callback_chain).grid(row=1, column=2, sticky="ew", **pad)

        ttk.Label(f, text="固定发送文本").grid(row=2, column=0, sticky="e", **pad)
        ttk.Entry(f, textvariable=self.send_test_text_var).grid(row=2, column=1, sticky="ew", **pad)
        self._button(f, "测试 OneBot 发送", self.test_onebot_send).grid(row=2, column=2, sticky="ew", **pad)

        ttk.Label(f, text="说明", foreground="#555").grid(row=3, column=0, sticky="ne", **pad)
        ttk.Label(
            f,
            text="AI接口测试只请求模型接口，不发送微信。AI回调链路会模拟值班群消息；如果AI服务已配置Key且dry_run关闭，会由第二微信发到目标群。OneBot发送测试会直接发送固定文本到目标群。",
            foreground="#555",
            wraplength=760,
            justify="left",
        ).grid(row=3, column=1, columnspan=2, sticky="w", **pad)

        out_frame = ttk.LabelFrame(f, text="测试输出")
        out_frame.grid(row=5, column=0, columnspan=3, sticky="nsew", padx=8, pady=8)
        out_frame.columnconfigure(0, weight=1)
        out_frame.rowconfigure(0, weight=1)
        self.test_output = ScrolledText(out_frame, height=20, wrap="word")
        self.test_output.grid(row=0, column=0, sticky="nsew")

    def _build_logs_tab(self) -> None:
        f = self.tab_logs
        f.columnconfigure(0, weight=1)
        f.rowconfigure(1, weight=1)
        btns = ttk.Frame(f)
        btns.grid(row=0, column=0, sticky="ew", padx=8, pady=8)
        self._button(btns, "刷新 AI 日志", lambda: self.load_log_file(AI_LOG)).grid(row=0, column=0, padx=(0, 8))
        self._button(btns, "刷新 OneBot 日志", lambda: self.load_log_file(ONEBOT_LOG)).grid(row=0, column=1, padx=(0, 8))
        self._button(btns, "打开 AI 日志", lambda: self.open_path(AI_LOG)).grid(row=0, column=2, padx=(0, 8))
        self._button(btns, "打开 OneBot 日志", lambda: self.open_path(ONEBOT_LOG)).grid(row=0, column=3, padx=(0, 8))
        self._button(btns, "打开日志目录", lambda: self.open_path(LOG_DIR)).grid(row=0, column=4, padx=(0, 8))
        self.log_output = ScrolledText(f, wrap="none")
        self.log_output.grid(row=1, column=0, sticky="nsew", padx=8, pady=8)

    def append_text(self, widget: ScrolledText, text: str) -> None:
        widget.insert("end", text)
        widget.see("end")

    def append_dashboard(self, text: str) -> None:
        self.append_text(self.dashboard_output, text)

    def append_test(self, text: str) -> None:
        self.append_text(self.test_output, text)

    def current_system_prompt(self) -> str:
        if hasattr(self, "system_prompt_text"):
            return self.system_prompt_text.get("1.0", "end").strip()
        return self.system_prompt_var.get().strip()

    def set_system_prompt(self, text: str) -> None:
        self.system_prompt_var.set(text)
        if hasattr(self, "system_prompt_text"):
            self.system_prompt_text.delete("1.0", "end")
            self.system_prompt_text.insert("1.0", text)

    def apply_provider_preset(self) -> None:
        name = self.provider_var.get()
        base, model = PROVIDER_PRESETS.get(name, ("", ""))
        if base:
            self.base_url_var.set(base)
        if model:
            self.model_var.set(model)

    def load_config(self) -> None:
        raw = read_json(CONFIG_PATH, {})
        env = parse_env_file(ENV_PATH)
        ai = raw.get("ai", {}) or {}

        base_url = env.get("AI_REPLY_BASE_URL") or ai.get("base_url") or "https://api.deepseek.com/v1"
        model = env.get("AI_REPLY_MODEL") or ai.get("model") or "deepseek-chat"
        api_key = env.get("AI_REPLY_API_KEY") or env.get("OPENAI_API_KEY") or ""
        self.provider_var.set(infer_provider(str(base_url)))
        self.base_url_var.set(str(base_url))
        self.api_key_var.set(str(api_key))
        self.model_var.set(str(model))
        self.temperature_var.set(str(ai.get("temperature", 0.3)))
        self.max_tokens_var.set(str(ai.get("max_tokens", 600)))
        self.timeout_var.set(str(ai.get("timeout_seconds", 30)))
        self.set_system_prompt(str(env.get("AI_REPLY_SYSTEM_PROMPT") or ai.get("system_prompt") or self.system_prompt_var.get()))

        self.reply_prefix_var.set(str(raw.get("reply_prefix", "AI：")))
        self.keywords_var.set("，".join(str(x) for x in raw.get("trigger_keywords", []) if str(x)))
        self.require_keyword_var.set(env_bool_value(env.get("AI_REPLY_REQUIRE_KEYWORD"), bool(raw.get("require_keyword", False))))
        self.dry_run_var.set(env_bool_value(env.get("AI_REPLY_DRY_RUN"), bool(raw.get("dry_run", False))))
        self.ignore_self_var.set(env_bool_value(env.get("AI_REPLY_IGNORE_SELF"), bool(raw.get("ignore_self_messages", False))))
        self.cooldown_var.set(str(raw.get("min_seconds_between_replies_per_group", 2)))
        self.max_reply_chars_var.set(str(raw.get("max_reply_chars", 600)))
        self.onebot_api_var.set(str(env.get("AI_REPLY_ONEBOT_API") or raw.get("onebot_api", "http://127.0.0.1:58080")))

        for item in self.groups_tree.get_children():
            self.groups_tree.delete(item)
        for item in raw.get("target_groups", []) or []:
            if isinstance(item, dict):
                gid = str(item.get("id") or "")
                name = str(item.get("name") or gid)
            else:
                gid = str(item)
                name = gid
            if gid:
                self.groups_tree.insert("", "end", values=(name, gid))
        self.load_recent_groups(async_run=True)

    def groups_from_tree(self) -> List[Dict[str, str]]:
        groups = []
        seen = set()
        for item in self.groups_tree.get_children():
            name, gid = self.groups_tree.item(item, "values")[:2]
            gid = str(gid).strip()
            name = str(name).strip() or gid
            if gid and gid not in seen:
                seen.add(gid)
                groups.append({"name": name, "id": gid})
        return groups

    def keyword_list(self) -> List[str]:
        raw = self.keywords_var.get().replace("，", ",")
        return [x.strip() for x in raw.split(",") if x.strip()]

    def save_config(self, show_message: bool = True) -> bool:
        try:
            raw = read_json(CONFIG_PATH, {})
            raw["enabled"] = True
            raw["listen_host"] = raw.get("listen_host", "127.0.0.1")
            raw["listen_port"] = int(raw.get("listen_port", 36060))
            raw["onebot_api"] = self.onebot_api_var.get().strip() or "http://127.0.0.1:58080"
            raw["target_groups"] = self.groups_from_tree()
            raw["reply_prefix"] = self.reply_prefix_var.get()
            raw["ignore_prefixes"] = raw.get("ignore_prefixes", ["AI：", "AI:", "🤖"])
            raw["trigger_keywords"] = self.keyword_list()
            raw["require_keyword"] = bool(self.require_keyword_var.get())
            raw["dry_run"] = bool(self.dry_run_var.get())
            raw["ignore_self_messages"] = bool(self.ignore_self_var.get())
            raw["allowed_user_ids"] = raw.get("allowed_user_ids", [])
            raw["ignored_user_ids"] = raw.get("ignored_user_ids", [])
            raw["min_seconds_between_replies_per_group"] = float(self.cooldown_var.get() or 2)
            raw["max_reply_chars"] = int(float(self.max_reply_chars_var.get() or 600))
            raw["max_context_messages"] = int(raw.get("max_context_messages", 8))
            raw["send_delay_seconds"] = float(raw.get("send_delay_seconds", 0.2))
            ai = raw.get("ai", {}) or {}
            ai["provider"] = "openai_compatible"
            ai["base_url"] = self.base_url_var.get().strip().rstrip("/")
            ai["api_key_env"] = "OPENAI_API_KEY"
            ai["model"] = self.model_var.get().strip()
            ai["temperature"] = float(self.temperature_var.get() or 0.3)
            ai["max_tokens"] = int(float(self.max_tokens_var.get() or 600))
            ai["timeout_seconds"] = int(float(self.timeout_var.get() or 30))
            ai["system_prompt"] = self.current_system_prompt()
            raw["ai"] = ai
            write_json(CONFIG_PATH, raw)

            ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
            env_lines = [
                "# 由 第二微信 AI 助手管理器 生成。",
                "# OpenAI-compatible：DeepSeek / OpenAI / 第三方中转站 / OpenRouter / 本地服务均可。",
                f"export AI_REPLY_PROVIDER={shell_quote('openai_compatible')}",
                f"export AI_REPLY_API_KEY={shell_quote(self.api_key_var.get().strip())}",
                f"export AI_REPLY_BASE_URL={shell_quote(ai['base_url'])}",
                f"export AI_REPLY_MODEL={shell_quote(ai['model'])}",
                f"export AI_REPLY_TEMPERATURE={shell_quote(str(ai['temperature']))}",
                f"export AI_REPLY_MAX_TOKENS={shell_quote(str(ai['max_tokens']))}",
                f"export AI_REPLY_TIMEOUT_SECONDS={shell_quote(str(ai['timeout_seconds']))}",
                f"export AI_REPLY_ONEBOT_API={shell_quote(raw['onebot_api'])}",
                f"export AI_REPLY_DRY_RUN={shell_quote('1' if raw['dry_run'] else '0')}",
                f"export AI_REPLY_REQUIRE_KEYWORD={shell_quote('1' if raw['require_keyword'] else '0')}",
                f"export AI_REPLY_IGNORE_SELF={shell_quote('1' if raw['ignore_self_messages'] else '0')}",
                "",
            ]
            ENV_PATH.write_text("\n".join(env_lines), encoding="utf-8")
            try:
                ENV_PATH.chmod(0o600)
            except Exception:
                pass
            if show_message:
                messagebox.showinfo("已保存", f"配置已保存：\n{CONFIG_PATH}\n{ENV_PATH}")
            self.append_dashboard(f"[{time.strftime('%H:%M:%S')}] 配置已保存\n")
            return True
        except Exception as e:
            messagebox.showerror("保存失败", f"{e}\n\n{traceback.format_exc()}")
            return False

    def add_or_update_group(self) -> None:
        gid = self.group_id_var.get().strip()
        name = self.group_name_var.get().strip() or gid
        if not gid:
            messagebox.showerror("缺少 group_id", "请填写 group_id")
            return
        for item in self.groups_tree.get_children():
            old_name, old_gid = self.groups_tree.item(item, "values")[:2]
            if str(old_gid) == gid:
                self.groups_tree.item(item, values=(name, gid))
                return
        self.groups_tree.insert("", "end", values=(name, gid))

    def delete_selected_group(self) -> None:
        for item in self.groups_tree.selection():
            self.groups_tree.delete(item)

    def on_group_select(self, _event=None) -> None:
        sel = self.groups_tree.selection()
        if not sel:
            return
        name, gid = self.groups_tree.item(sel[0], "values")[:2]
        self.group_name_var.set(str(name))
        self.group_id_var.set(str(gid))

    def on_recent_select(self, _event=None) -> None:
        sel = self.recent_tree.selection()
        if not sel:
            return
        gid, members, sender, text = self.recent_tree.item(sel[0], "values")[:4]
        self.group_id_var.set(str(gid))
        if not self.group_name_var.get().strip() or self.group_name_var.get().strip() == "值班群":
            self.group_name_var.set("值班群")

    def add_recent_as_target(self) -> None:
        sel = self.recent_tree.selection()
        if not sel:
            messagebox.showinfo("未选择", "先在右侧列表选择一个最近群 ID")
            return
        gid = str(self.recent_tree.item(sel[0], "values")[0])
        self.group_id_var.set(gid)
        if not self.group_name_var.get().strip():
            self.group_name_var.set("值班群")
        self.add_or_update_group()

    def first_target_group(self) -> Tuple[str, str]:
        groups = self.groups_from_tree()
        if not groups:
            return "", ""
        return groups[0]["id"], groups[0]["name"]

    def load_recent_groups(self, async_run: bool = False) -> None:
        def work():
            try:
                proc = subprocess.run([SCRIPT("recent_group_ids.sh"), "30"], cwd=str(ROOT_DIR), text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=10)
                out = proc.stdout
                rows = []
                for line in out.splitlines():
                    m = re.search(r"group_id=([^\s]+)\s+members=([^\s]+)\s+sender=(.*?)\s+text=(.*)$", line.strip())
                    if m:
                        rows.append((m.group(1), m.group(2), m.group(3), m.group(4)))
                def update():
                    for item in self.recent_tree.get_children():
                        self.recent_tree.delete(item)
                    seen = set()
                    for gid, members, sender, text in reversed(rows):
                        key = gid
                        if key in seen:
                            continue
                        seen.add(key)
                        self.recent_tree.insert("", "end", values=(gid, members, sender, text))
                    self.append_dashboard(out + ("\n" if not out.endswith("\n") else ""))
                self.after(0, update)
            except Exception as e:
                self.after(0, lambda: self.append_dashboard(f"刷新最近群ID失败：{e}\n"))
        if async_run:
            threading.Thread(target=work, daemon=True).start()
        else:
            work()

    def make_request_json(self, method: str, url: str, payload: Dict[str, Any] | None = None, timeout: int = 30) -> Dict[str, Any]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        key = self.api_key_var.get().strip()
        if key:
            headers["Authorization"] = "Bearer " + key
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            return json.loads(body)

    def fetch_models(self) -> None:
        base = self.base_url_var.get().strip().rstrip("/")
        if not base:
            messagebox.showerror("缺少 Base URL", "请先填写 Base URL")
            return
        self.append_test(f"[{time.strftime('%H:%M:%S')}] GET {base}/models\n")
        def work():
            try:
                obj = self.make_request_json("GET", base + "/models", timeout=int(float(self.timeout_var.get() or 30)))
                models: List[str] = []
                data = obj.get("data")
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict):
                            mid = item.get("id") or item.get("name")
                        else:
                            mid = str(item)
                        if mid:
                            models.append(str(mid))
                elif isinstance(obj.get("models"), list):
                    for item in obj["models"]:
                        models.append(str(item.get("id") if isinstance(item, dict) else item))
                models = sorted(dict.fromkeys(models))
                def update():
                    if models:
                        self.model_combo["values"] = models
                        if not self.model_var.get().strip() or self.model_var.get().strip() not in models:
                            self.model_var.set(models[0])
                        self.append_test("获取到模型：\n" + "\n".join(models[:200]) + "\n")
                    else:
                        self.append_test("接口返回成功，但没有解析到模型ID：\n" + json.dumps(obj, ensure_ascii=False, indent=2)[:3000] + "\n")
                self.after(0, update)
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace") if e.fp else ""
                self.after(0, lambda: self.append_test(f"获取模型失败 HTTP {e.code}: {body[:2000]}\n"))
            except Exception as e:
                self.after(0, lambda: self.append_test(f"获取模型失败：{e}\n"))
        threading.Thread(target=work, daemon=True).start()

    def test_ai_api(self) -> None:
        base = self.base_url_var.get().strip().rstrip("/")
        model = self.model_var.get().strip()
        if not base or not model:
            messagebox.showerror("缺少配置", "请填写 Base URL 和模型")
            return
        prompt = self.test_prompt_var.get().strip() or "请回复：AI接口测试成功"
        self.append_test(f"[{time.strftime('%H:%M:%S')}] POST {base}/chat/completions model={model}\n")
        def work():
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": self.current_system_prompt() or "你是测试助手。"},
                    {"role": "user", "content": prompt},
                ],
                "temperature": float(self.temperature_var.get() or 0.3),
                "max_tokens": int(float(self.max_tokens_var.get() or 600)),
            }
            try:
                obj = self.make_request_json("POST", base + "/chat/completions", payload, timeout=int(float(self.timeout_var.get() or 30)))
                content = obj.get("choices", [{}])[0].get("message", {}).get("content", "")
                if not content:
                    content = json.dumps(obj, ensure_ascii=False, indent=2)[:3000]
                self.after(0, lambda: self.append_test("AI接口测试返回：\n" + str(content) + "\n"))
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace") if e.fp else ""
                self.after(0, lambda: self.append_test(f"AI接口测试失败 HTTP {e.code}: {body[:3000]}\n"))
            except Exception as e:
                self.after(0, lambda: self.append_test(f"AI接口测试失败：{e}\n"))
        threading.Thread(target=work, daemon=True).start()

    def test_callback_chain(self) -> None:
        gid, name = self.first_target_group()
        if not gid:
            messagebox.showerror("没有目标群", "请先在“群配置”里添加目标群并保存")
            return
        text = self.callback_test_text_var.get().strip() or "值班群AI回调测试"
        self.save_config(show_message=False)
        self.run_command("测试 AI 回调链路", [SCRIPT("test_ai_reply_event.sh"), gid, text], output="test")

    def test_onebot_send(self) -> None:
        gid, name = self.first_target_group()
        if not gid:
            messagebox.showerror("没有目标群", "请先在“群配置”里添加目标群并保存")
            return
        text = self.send_test_text_var.get().strip() or "第二微信助手固定消息测试"
        url = self.onebot_api_var.get().strip().rstrip("/") + "/send_group_msg"
        self.append_test(f"[{time.strftime('%H:%M:%S')}] POST {url} group_id={gid}\n")
        def work():
            payload = {"group_id": gid, "message": [{"type": "text", "data": {"text": text}}]}
            try:
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=25) as resp:
                    body = resp.read().decode("utf-8", "replace")
                    status = resp.status
                self.after(0, lambda: self.append_test(f"OneBot发送返回 status={status}: {body}\n"))
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace") if e.fp else ""
                self.after(0, lambda: self.append_test(f"OneBot发送失败 HTTP {e.code}: {body[:2000]}\n"))
            except Exception as e:
                self.after(0, lambda: self.append_test(f"OneBot发送失败：{e}\n"))
        threading.Thread(target=work, daemon=True).start()

    def run_script(self, title: str, script_name: str, output: str = "dashboard") -> None:
        self.run_command(title, [SCRIPT(script_name)], output=output)

    def run_command(self, title: str, cmd: List[str], output: str = "dashboard", after_refresh: bool = True) -> None:
        target = self.test_output if output == "test" else self.dashboard_output
        def write(text: str) -> None:
            self.after(0, lambda t=text: self.append_text(target, t))
        write(f"\n[{time.strftime('%H:%M:%S')}] {title}\n$ " + " ".join(shlex.quote(x) for x in cmd) + "\n")
        def work():
            try:
                proc = subprocess.Popen(cmd, cwd=str(ROOT_DIR), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
                assert proc.stdout is not None
                for line in proc.stdout:
                    write(line)
                rc = proc.wait()
                write(f"[{time.strftime('%H:%M:%S')}] 退出码：{rc}\n")
            except Exception as e:
                write(f"执行失败：{e}\n")
            finally:
                if after_refresh:
                    self.after(500, self.refresh_status)
        threading.Thread(target=work, daemon=True).start()

    def start_ai_after_save(self) -> None:
        if self.save_config(show_message=False):
            self.run_script("启动/复用 AI 服务", "start_ai_reply.sh")

    def restart_ai_after_save(self) -> None:
        if self.save_config(show_message=False):
            self.run_command("保存并重启 AI", ["/bin/bash", "-lc", f"{shlex.quote(SCRIPT('stop_ai_reply.sh'))}; {shlex.quote(SCRIPT('start_ai_reply.sh'))}"])

    def start_all_after_save(self) -> None:
        if self.save_config(show_message=False):
            self.run_script("一键启动第二微信 + OneBot + AI", "run_wechat2_ai_reply.sh")

    def refresh_status(self) -> None:
        def work():
            out_parts = []
            wechat_pid = onebot_pid = ai_pid = ""
            try:
                p = subprocess.run([SCRIPT("status_wechat2_onebot.sh")], cwd=str(ROOT_DIR), text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=12)
                out_parts.append(p.stdout)
                m = re.search(r"WeChat2 PID=(\d+)", p.stdout)
                if m:
                    wechat_pid = m.group(1)
                m = re.search(r"OneBot PID=(\d+)", p.stdout)
                if m:
                    onebot_pid = m.group(1)
            except Exception as e:
                out_parts.append(f"status_wechat2_onebot.sh failed: {e}\n")
            try:
                p = subprocess.run([SCRIPT("status_ai_reply.sh")], cwd=str(ROOT_DIR), text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=8)
                out_parts.append("\n--- AI ---\n" + p.stdout)
                m = re.search(r"AI reply PID=(\d+)", p.stdout)
                if m:
                    ai_pid = m.group(1)
            except Exception as e:
                out_parts.append(f"status_ai_reply.sh failed: {e}\n")
            summary = f"配置：{CONFIG_PATH} | env：{ENV_PATH} | 日志：{LOG_DIR}"
            def update():
                self.wechat2_status_var.set(f"WeChat2：{'运行 PID=' + wechat_pid if wechat_pid else '未运行'}")
                self.onebot_status_var.set(f"OneBot：{'运行 PID=' + onebot_pid if onebot_pid else '未运行'}")
                self.ai_status_var.set(f"AI服务：{'运行 PID=' + ai_pid if ai_pid else '未运行'}")
                self.status_summary_var.set(summary)
                self.dashboard_output.delete("1.0", "end")
                self.dashboard_output.insert("1.0", "\n".join(out_parts)[-20000:])
                self.dashboard_output.see("end")
            self.after(0, update)
        threading.Thread(target=work, daemon=True).start()

    def _periodic_status(self) -> None:
        # 只轻量刷新标签，不清空输出。避免用户看日志时被打断。
        def work():
            try:
                out1 = subprocess.run([SCRIPT("status_wechat2_onebot.sh")], cwd=str(ROOT_DIR), text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=8).stdout
                out2 = subprocess.run([SCRIPT("status_ai_reply.sh")], cwd=str(ROOT_DIR), text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5).stdout
                wechat = re.search(r"WeChat2 PID=(\d+)", out1)
                onebot = re.search(r"OneBot PID=(\d+)", out1)
                ai = re.search(r"AI reply PID=(\d+)", out2)
                self.after(0, lambda: (
                    self.wechat2_status_var.set(f"WeChat2：{'运行 PID=' + wechat.group(1) if wechat else '未运行'}"),
                    self.onebot_status_var.set(f"OneBot：{'运行 PID=' + onebot.group(1) if onebot else '未运行'}"),
                    self.ai_status_var.set(f"AI服务：{'运行 PID=' + ai.group(1) if ai else '未运行'}"),
                ))
            except Exception:
                pass
            finally:
                self.after(15000, self._periodic_status)
        threading.Thread(target=work, daemon=True).start()

    def load_log_file(self, path: Path) -> None:
        try:
            if not path.exists():
                text = f"日志不存在：{path}\n"
            else:
                data = path.read_bytes()
                if len(data) > 120000:
                    data = data[-120000:]
                text = data.decode("utf-8", "replace")
            self.log_output.delete("1.0", "end")
            self.log_output.insert("1.0", text)
            self.log_output.see("end")
        except Exception as e:
            messagebox.showerror("读取日志失败", str(e))

    def open_path(self, path: Path) -> None:
        try:
            if path.exists():
                subprocess.Popen(["/usr/bin/open", str(path)])
            else:
                subprocess.Popen(["/usr/bin/open", str(path.parent)])
        except Exception as e:
            messagebox.showerror("打开失败", str(e))


def main() -> int:
    if "--check" in sys.argv:
        raw = read_json(CONFIG_PATH, {})
        env = parse_env_file(ENV_PATH)
        print(json.dumps({
            "root": str(ROOT_DIR),
            "config_exists": CONFIG_PATH.exists(),
            "env_exists": ENV_PATH.exists(),
            "target_groups": raw.get("target_groups", []),
            "base_url": env.get("AI_REPLY_BASE_URL") or (raw.get("ai", {}) or {}).get("base_url"),
            "model": env.get("AI_REPLY_MODEL") or (raw.get("ai", {}) or {}).get("model"),
            "api_key_present": bool(env.get("AI_REPLY_API_KEY") or env.get("OPENAI_API_KEY")),
        }, ensure_ascii=False, indent=2))
        return 0
    app = ManagerApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
