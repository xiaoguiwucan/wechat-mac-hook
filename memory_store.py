#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Local long-term memory store for the single-instance WeChat AI assistant.

This module only stores data observed by the current single-instance OneBot/AI pipeline
or imported by the user. It does not read or decrypt WeChat's private databases.
"""
from __future__ import annotations

import json
import hashlib
import html
import math
import re
import sqlite3
import struct
import sys
import time
import urllib.parse
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

DEFAULT_HOME = Path.home() / "Library" / "Application Support" / "WeChatAgent"
DEFAULT_DB = DEFAULT_HOME / "memory" / "wechat-memory.sqlite3"
ROOT = Path(__file__).resolve().parent
RUNTIME_PYTHON = ROOT / "tools" / "runtime" / "python"
if RUNTIME_PYTHON.exists() and str(RUNTIME_PYTHON) not in sys.path:
    sys.path.insert(0, str(RUNTIME_PYTHON))

VOICE_INTENT_ALIASES = {
    "招呼": ("你好", "哈喽", "hello"),
    "问好": ("你好", "哈喽", "hello"),
    "你好": ("你好", "哈喽", "hello"),
    "早安": ("早上好", "早安"),
    "早上好": ("早上好", "早安"),
    "晚安": ("晚安", "晚上好"),
    "别急": ("别急", "稳住", "稳"),
    "别着急": ("别急", "稳住", "稳"),
    "不要着急": ("别急", "稳住", "稳"),
    "稳": ("别急", "稳住", "稳"),
    "道歉": ("不好意思", "对不起", "抱歉"),
    "谢谢": ("谢谢", "感谢"),
    "感谢": ("谢谢", "感谢"),
    "笑话": ("哈哈", "搞笑", "笑话"),
    "搞笑": ("哈哈", "搞笑", "笑话"),
    "好笑": ("哈哈", "搞笑", "笑话"),
    "拒绝": ("不要", "不行", "滚"),
    "不行": ("不要", "不行"),
}

FACE_INTENT_ALIASES = {
    "好笑": ("搞笑", "哈哈", "笑"), "笑死": ("搞笑", "哈哈", "笑"),
    "哈哈": ("搞笑", "哈哈", "笑"), "无语": ("无语", "白眼", "沉默"),
    "生气": ("生气", "愤怒", "气"), "拒绝": ("走开", "不要", "拒绝"),
    "厉害": ("牛", "厉害", "点赞"), "开心": ("开心", "高兴", "欢呼"),
}


class ManagedConnection(sqlite3.Connection):
    """Commit/rollback like sqlite3's context manager, then release the handle."""

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        try:
            super().__exit__(exc_type, exc, tb)
        finally:
            self.close()


def voice_search_text(value: Any) -> str:
    """Normalize user descriptions and filenames without losing Chinese phrase boundaries."""
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(value or "").lower())


def is_readable_member_name(value: Any, user_id: str = "") -> bool:
    name = str(value or "").strip()
    lowered = name.lower()
    placeholders = {"群友", "群成员", "未知成员", "未知发送者", "unknown", "member"}
    return bool(
        name
        and name != str(user_id or "").strip()
        and lowered not in placeholders
        and not lowered.startswith("wxid_")
        and not lowered.endswith("@chatroom")
    )


def effective_member_name(row: Any, user_id: str = "") -> str:
    """Return the first human-readable group identity without leaking wxids."""
    uid = str(user_id or (row["user_id"] if row else "") or "").strip()
    for key in ("card", "display_name", "nickname"):
        try:
            value = row[key]
        except (KeyError, IndexError, TypeError):
            value = ""
        if is_readable_member_name(value, uid):
            return str(value).strip()
    suffix = uid[-6:] if uid else "------"
    return f"未识别成员 · {suffix}"


def persona_message_text(value: Any) -> str:
    """Reduce XML/system payloads to human text before profile extraction."""
    body = html.unescape(str(value or "")).strip()
    if not body:
        return ""
    if re.search(r"<(?:\?xml|msg|appmsg|sysmsg)\b", body, re.I):
        useful: List[str] = []
        for tag in ("title", "des", "content", "displayname"):
            for match in re.findall(rf"<{tag}[^>]*>(.*?)</{tag}>", body, re.I | re.S):
                clean = re.sub(r"<[^>]+>", " ", html.unescape(match))
                clean = re.sub(r"\s+", " ", clean).strip()
                if clean and clean not in useful:
                    useful.append(clean)
        return " ".join(useful)[:1200]
    return re.sub(r"\s+", " ", body).strip()[:1200]


def voice_search_terms(value: str) -> List[str]:
    compact = voice_search_text(value)
    terms = re.findall(r"[a-z0-9]{2,}|[\u4e00-\u9fff]{2,}", compact)
    zh = "".join(re.findall(r"[\u4e00-\u9fff]", compact))
    terms.extend(zh[i:i + 2] for i in range(max(0, len(zh) - 1)))
    return list(dict.fromkeys(terms))


def voice_match_score(query: str, item: Dict[str, Any]) -> tuple[int, str]:
    q = voice_search_text(query)
    title = voice_search_text(item.get("title") or item.get("text"))
    text = voice_search_text(item.get("text") or item.get("title"))
    category = voice_search_text(item.get("category"))
    pack = voice_search_text(item.get("pack_name"))
    if not q:
        return 0, ""
    score = 0
    reasons: List[str] = []
    if q == title or q == text:
        score += 1200
        reasons.append("完整命中")
    elif q in title or q in text:
        score += 900 + min(len(q), 40) * 2
        reasons.append("内容包含")
    elif q in pack or q in category:
        score += 110
        reasons.append("分类/语音包命中")

    aliases: List[str] = []
    for key, values in VOICE_INTENT_ALIASES.items():
        if key in q:
            aliases.extend(values)
    for alias in dict.fromkeys(aliases):
        alias_norm = voice_search_text(alias)
        if len(alias_norm) >= 2 and (alias_norm in title or alias_norm in text):
            score += 280
            reasons.append("意图匹配")

    q_terms = set(voice_search_terms(q))
    title_terms = set(voice_search_terms(title))
    text_terms = set(voice_search_terms(text))
    phrase_hits = len(q_terms & (title_terms | text_terms))
    if phrase_hits:
        score += min(phrase_hits, 5) * 75
        reasons.append("关键词匹配")

    # A single shared Chinese character is too weak: it caused “笑话” to pick
    # unrelated names such as “爱笑的帅哥运气不会差”.
    q_chars = set(re.findall(r"[\u4e00-\u9fff]", q))
    title_chars = set(re.findall(r"[\u4e00-\u9fff]", title))
    shared_chars = len(q_chars & title_chars)
    if shared_chars >= 2:
        score += min(shared_chars, 6) * 18
        reasons.append("语义字面相近")
    elif shared_chars == 1 and phrase_hits == 0 and not reasons:
        score = 0

    if pack and q in pack:
        score += 25
    return score, "、".join(dict.fromkeys(reasons))


def now_ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def text_tokens(text: str) -> List[str]:
    text = str(text or "").lower()
    words = re.findall(r"[a-z0-9_]{2,}|[\u4e00-\u9fff]", text)
    grams: List[str] = []
    zh = "".join(re.findall(r"[\u4e00-\u9fff]", text))
    for i in range(max(0, len(zh) - 1)):
        grams.append(zh[i:i + 2])
    return words + grams


def is_face_metadata(meta_json: str, raw_message: str = "") -> bool:
    meta = str(meta_json or "")
    raw = str(raw_message or "")
    return (
        '"type": "face"' in meta or "'type': 'face'" in meta
        or "<emoji" in meta or "<emoji" in raw or '"normalized_type": "image"' in meta
    )


class MemoryStore:
    def __init__(self, path: Path | str = DEFAULT_DB):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False, factory=ManagedConnection)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        self._load_vec_extension(conn)
        return conn

    @staticmethod
    def _load_vec_extension(conn: sqlite3.Connection) -> bool:
        try:
            import sqlite_vec  # type: ignore
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            return True
        except Exception:
            return False

    def init_db(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS groups (
                  group_id TEXT PRIMARY KEY,
                  name TEXT NOT NULL DEFAULT '',
                  alias TEXT NOT NULL DEFAULT '',
                  source TEXT NOT NULL DEFAULT 'event',
                  selected INTEGER NOT NULL DEFAULT 0,
                  last_seen TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS members (
                  user_id TEXT NOT NULL,
                  group_id TEXT NOT NULL,
                  display_name TEXT NOT NULL DEFAULT '',
                  nickname TEXT NOT NULL DEFAULT '',
                  card TEXT NOT NULL DEFAULT '',
                  message_count INTEGER NOT NULL DEFAULT 0,
                  last_seen TEXT NOT NULL DEFAULT '',
                  profile_json TEXT NOT NULL DEFAULT '{}',
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  PRIMARY KEY (user_id, group_id)
                );
                CREATE TABLE IF NOT EXISTS messages (
                  event_id TEXT PRIMARY KEY,
                  trace_id TEXT NOT NULL DEFAULT '',
                  direction TEXT NOT NULL DEFAULT 'incoming',
                  group_id TEXT NOT NULL,
                  group_name TEXT NOT NULL DEFAULT '',
                  user_id TEXT NOT NULL DEFAULT '',
                  sender_name TEXT NOT NULL DEFAULT '',
                  message_id TEXT NOT NULL DEFAULT '',
                  event_time INTEGER NOT NULL DEFAULT 0,
                  text TEXT NOT NULL DEFAULT '',
                  raw_message TEXT NOT NULL DEFAULT '',
                  segments_json TEXT NOT NULL DEFAULT '[]',
                  raw_json TEXT NOT NULL DEFAULT '{}',
                  media_json TEXT NOT NULL DEFAULT '[]',
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS durable_outbox (
                  seq INTEGER PRIMARY KEY AUTOINCREMENT,
                  event_id TEXT NOT NULL UNIQUE,
                  payload_json TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'pending',
                  attempts INTEGER NOT NULL DEFAULT 0,
                  next_attempt_at REAL NOT NULL DEFAULT 0,
                  last_error TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_durable_outbox_pending
                  ON durable_outbox(status,next_attempt_at,seq);
                CREATE INDEX IF NOT EXISTS idx_messages_group_time ON messages(group_id, event_time DESC, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_messages_user_time ON messages(user_id, event_time DESC, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_messages_trace ON messages(trace_id);
                CREATE TABLE IF NOT EXISTS group_memory (
                  group_id TEXT PRIMARY KEY,
                  summary TEXT NOT NULL DEFAULT '',
                  facts_json TEXT NOT NULL DEFAULT '[]',
                  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS personas (
                  user_id TEXT NOT NULL,
                  group_id TEXT NOT NULL DEFAULT '',
                  summary TEXT NOT NULL DEFAULT '',
                  tags_json TEXT NOT NULL DEFAULT '[]',
                  facts_json TEXT NOT NULL DEFAULT '[]',
                  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  PRIMARY KEY (user_id, group_id)
                );
                CREATE TABLE IF NOT EXISTS persona_claims (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  group_id TEXT NOT NULL,
                  user_id TEXT NOT NULL,
                  category TEXT NOT NULL,
                  value TEXT NOT NULL,
                  confidence REAL NOT NULL DEFAULT 0,
                  source TEXT NOT NULL DEFAULT 'auto',
                  evidence_json TEXT NOT NULL DEFAULT '[]',
                  priority INTEGER NOT NULL DEFAULT 0,
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  UNIQUE(group_id,user_id,category,value,source)
                );
                CREATE INDEX IF NOT EXISTS idx_persona_claims_member ON persona_claims(group_id,user_id,category,priority DESC,confidence DESC);
                CREATE TABLE IF NOT EXISTS persona_analysis_jobs (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  group_id TEXT NOT NULL,
                  user_id TEXT NOT NULL,
                  mode TEXT NOT NULL DEFAULT 'full',
                  status TEXT NOT NULL DEFAULT 'queued',
                  total_messages INTEGER NOT NULL DEFAULT 0,
                  processed_messages INTEGER NOT NULL DEFAULT 0,
                  cursor_offset INTEGER NOT NULL DEFAULT 0,
                  error TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  started_at TEXT NOT NULL DEFAULT '',
                  completed_at TEXT NOT NULL DEFAULT '',
                  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_persona_jobs_status ON persona_analysis_jobs(status,id);
                CREATE TABLE IF NOT EXISTS tool_events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  trace_id TEXT NOT NULL DEFAULT '',
                  tool_name TEXT NOT NULL,
                  group_id TEXT NOT NULL DEFAULT '',
                  input_json TEXT NOT NULL DEFAULT '{}',
                  output_json TEXT NOT NULL DEFAULT '{}',
                  status TEXT NOT NULL DEFAULT 'ok',
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS automation_runs (
                  run_id TEXT PRIMARY KEY,
                  idempotency_key TEXT NOT NULL UNIQUE,
                  source_event_id TEXT NOT NULL DEFAULT '',
                  group_id TEXT NOT NULL,
                  user_id TEXT NOT NULL,
                  intent TEXT NOT NULL,
                  risk_level TEXT NOT NULL DEFAULT 'read',
                  status TEXT NOT NULL DEFAULT 'queued',
                  hermes_run_id TEXT NOT NULL DEFAULT '',
                  result_summary TEXT NOT NULL DEFAULT '',
                  error TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_automation_runs_status
                  ON automation_runs(status,created_at);
                CREATE TABLE IF NOT EXISTS automation_events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  run_id TEXT NOT NULL,
                  event_type TEXT NOT NULL,
                  payload_json TEXT NOT NULL DEFAULT '{}',
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  FOREIGN KEY(run_id) REFERENCES automation_runs(run_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS media_items (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  event_id TEXT NOT NULL,
                  group_id TEXT NOT NULL,
                  media_type TEXT NOT NULL,
                  file TEXT NOT NULL DEFAULT '',
                  url TEXT NOT NULL DEFAULT '',
                  meta_json TEXT NOT NULL DEFAULT '{}',
                  ocr_text TEXT NOT NULL DEFAULT '',
                  image_summary TEXT NOT NULL DEFAULT '',
                  tags_json TEXT NOT NULL DEFAULT '[]',
                  keywords_json TEXT NOT NULL DEFAULT '[]',
                  error TEXT NOT NULL DEFAULT '',
                  status TEXT NOT NULL DEFAULT 'indexed',
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_media_group ON media_items(group_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS group_reply_mutes (
                  group_id TEXT PRIMARY KEY,
                  muted_until REAL NOT NULL,
                  triggered_by TEXT NOT NULL DEFAULT '',
                  trigger_message_id TEXT NOT NULL DEFAULT '',
                  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS group_admins (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  group_id TEXT NOT NULL,
                  user_id TEXT NOT NULL,
                  display_name TEXT NOT NULL DEFAULT '',
                  role TEXT NOT NULL DEFAULT 'custom',
                  permissions_json TEXT NOT NULL DEFAULT '[]',
                  source TEXT NOT NULL DEFAULT 'directory',
                  enabled INTEGER NOT NULL DEFAULT 1,
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  UNIQUE(group_id,user_id)
                );
                CREATE INDEX IF NOT EXISTS idx_group_admins_group
                  ON group_admins(group_id,enabled,updated_at DESC);
                CREATE TABLE IF NOT EXISTS group_admin_audit (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  group_id TEXT NOT NULL,
                  user_id TEXT NOT NULL,
                  display_name TEXT NOT NULL DEFAULT '',
                  command TEXT NOT NULL DEFAULT '',
                  permission TEXT NOT NULL DEFAULT '',
                  message_id TEXT NOT NULL DEFAULT '',
                  trace_id TEXT NOT NULL DEFAULT '',
                  before_json TEXT NOT NULL DEFAULT '{}',
                  after_json TEXT NOT NULL DEFAULT '{}',
                  result TEXT NOT NULL DEFAULT '',
                  error TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  UNIQUE(group_id,message_id,command)
                );
                CREATE INDEX IF NOT EXISTS idx_group_admin_audit_group
                  ON group_admin_audit(group_id,id DESC);
                CREATE TABLE IF NOT EXISTS voice_packs (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  name TEXT NOT NULL,
                  category TEXT NOT NULL DEFAULT '',
                  source_path TEXT NOT NULL DEFAULT '',
                  item_count INTEGER NOT NULL DEFAULT 0,
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  UNIQUE(name, category)
                );
                CREATE TABLE IF NOT EXISTS voice_items (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  pack_id INTEGER NOT NULL DEFAULT 0,
                  category TEXT NOT NULL DEFAULT '',
                  title TEXT NOT NULL,
                  text TEXT NOT NULL DEFAULT '',
                  file TEXT NOT NULL,
                  file_ext TEXT NOT NULL DEFAULT '',
                  size INTEGER NOT NULL DEFAULT 0,
                  duration_ms INTEGER NOT NULL DEFAULT 0,
                  tags_json TEXT NOT NULL DEFAULT '[]',
                  usage_count INTEGER NOT NULL DEFAULT 0,
                  last_used_at TEXT NOT NULL DEFAULT '',
                  status TEXT NOT NULL DEFAULT 'ready',
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  UNIQUE(file)
                );
                CREATE INDEX IF NOT EXISTS idx_voice_items_category ON voice_items(category, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_voice_items_title ON voice_items(title);
                CREATE TABLE IF NOT EXISTS semantic_embeddings (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  object_type TEXT NOT NULL,
                  object_id TEXT NOT NULL,
                  group_id TEXT NOT NULL,
                  text TEXT NOT NULL,
                  model TEXT NOT NULL,
                  dimensions INTEGER NOT NULL DEFAULT 4096,
                  content_hash TEXT NOT NULL,
                  vector_blob BLOB NOT NULL,
                  status TEXT NOT NULL DEFAULT 'ready',
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  UNIQUE(object_type, object_id, model)
                );
                CREATE INDEX IF NOT EXISTS idx_semantic_group_type ON semantic_embeddings(group_id, object_type, updated_at DESC);
                CREATE TABLE IF NOT EXISTS embedding_jobs (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  object_type TEXT NOT NULL,
                  object_id TEXT NOT NULL,
                  group_id TEXT NOT NULL,
                  text TEXT NOT NULL,
                  content_hash TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'pending',
                  attempts INTEGER NOT NULL DEFAULT 0,
                  error TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  UNIQUE(object_type, object_id, content_hash)
                );
                CREATE INDEX IF NOT EXISTS idx_embedding_jobs_status ON embedding_jobs(status, id);
                CREATE TABLE IF NOT EXISTS member_aliases (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  group_id TEXT NOT NULL,
                  user_id TEXT NOT NULL,
                  alias TEXT NOT NULL,
                  confidence REAL NOT NULL DEFAULT 0,
                  evidence_json TEXT NOT NULL DEFAULT '[]',
                  source TEXT NOT NULL DEFAULT 'learned',
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  UNIQUE(group_id, user_id, alias)
                );
                CREATE TABLE IF NOT EXISTS member_relations (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  group_id TEXT NOT NULL,
                  from_user_id TEXT NOT NULL,
                  to_user_id TEXT NOT NULL,
                  relation TEXT NOT NULL,
                  confidence REAL NOT NULL DEFAULT 0,
                  evidence_json TEXT NOT NULL DEFAULT '[]',
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  UNIQUE(group_id, from_user_id, to_user_id, relation)
                );
                CREATE TABLE IF NOT EXISTS group_memes (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  group_id TEXT NOT NULL,
                  name TEXT NOT NULL,
                  meaning TEXT NOT NULL DEFAULT '',
                  triggers_json TEXT NOT NULL DEFAULT '[]',
                  evidence_json TEXT NOT NULL DEFAULT '[]',
                  related_media_json TEXT NOT NULL DEFAULT '[]',
                  confidence REAL NOT NULL DEFAULT 0,
                  source TEXT NOT NULL DEFAULT 'learned',
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  UNIQUE(group_id, name)
                );
                CREATE INDEX IF NOT EXISTS idx_group_memes_group ON group_memes(group_id, updated_at DESC);
                CREATE TABLE IF NOT EXISTS reply_tasks (
                  task_id TEXT PRIMARY KEY,
                  trace_id TEXT NOT NULL,
                  thread_id TEXT NOT NULL,
                  group_id TEXT NOT NULL,
                  group_name TEXT NOT NULL DEFAULT '',
                  user_id TEXT NOT NULL DEFAULT '',
                  sender_name TEXT NOT NULL DEFAULT '',
                  message_id TEXT NOT NULL DEFAULT '',
                  question TEXT NOT NULL DEFAULT '',
                  state TEXT NOT NULL DEFAULT 'queued',
                  state_label TEXT NOT NULL DEFAULT '排队等待',
                  score REAL,
                  threshold REAL,
                  medium TEXT NOT NULL DEFAULT '',
                  model TEXT NOT NULL DEFAULT '',
                  result TEXT NOT NULL DEFAULT '',
                  error TEXT NOT NULL DEFAULT '',
                  details_json TEXT NOT NULL DEFAULT '{}',
                  queued_at REAL NOT NULL,
                  started_at REAL,
                  completed_at REAL,
                  updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_reply_tasks_state ON reply_tasks(state, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_reply_tasks_group ON reply_tasks(group_id, updated_at DESC);
                CREATE TABLE IF NOT EXISTS face_assets (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  face_key TEXT NOT NULL UNIQUE,
                  canonical_media_id INTEGER NOT NULL,
                  file TEXT NOT NULL DEFAULT '',
                  ocr_text TEXT NOT NULL DEFAULT '',
                  image_summary TEXT NOT NULL DEFAULT '',
                  tags_json TEXT NOT NULL DEFAULT '[]',
                  keywords_json TEXT NOT NULL DEFAULT '[]',
                  aliases_json TEXT NOT NULL DEFAULT '[]',
                  emotions_json TEXT NOT NULL DEFAULT '[]',
                  intents_json TEXT NOT NULL DEFAULT '[]',
                  actions_json TEXT NOT NULL DEFAULT '[]',
                  subjects_json TEXT NOT NULL DEFAULT '[]',
                  searchable_text TEXT NOT NULL DEFAULT '',
                  enabled INTEGER NOT NULL DEFAULT 1,
                  usage_count INTEGER NOT NULL DEFAULT 0,
                  success_count INTEGER NOT NULL DEFAULT 0,
                  failure_count INTEGER NOT NULL DEFAULT 0,
                  last_used_at TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_face_assets_enabled ON face_assets(enabled, updated_at DESC);
                CREATE TABLE IF NOT EXISTS face_asset_groups (
                  face_id INTEGER NOT NULL,
                  group_id TEXT NOT NULL,
                  source_media_id INTEGER NOT NULL,
                  enabled INTEGER NOT NULL DEFAULT 1,
                  first_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  last_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  PRIMARY KEY(face_id, group_id),
                  FOREIGN KEY(face_id) REFERENCES face_assets(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_face_asset_groups_group ON face_asset_groups(group_id, enabled, last_seen DESC);
                """
            )
            # Remove the obsolete deterministic 128-bucket hash store. Real
            # embeddings live in semantic_embeddings + sqlite-vec.
            db.execute("DROP TABLE IF EXISTS memory_vectors")
            if self._load_vec_extension(db):
                try:
                    db.execute(
                        "CREATE VIRTUAL TABLE IF NOT EXISTS vec_embeddings USING vec0(embedding float[4096], group_id text partition key)"
                    )
                except sqlite3.Error:
                    pass
            try:
                db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(event_id UNINDEXED, group_id UNINDEXED, sender_name, text, raw_message)")
                db.execute(
                    """INSERT INTO messages_fts(event_id,group_id,sender_name,text,raw_message)
                       SELECT m.event_id,m.group_id,m.sender_name,m.text,m.raw_message FROM messages m
                       WHERE NOT EXISTS(SELECT 1 FROM messages_fts f WHERE f.event_id=m.event_id)"""
                )
            except sqlite3.Error:
                pass
            try:
                db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS face_assets_fts USING fts5(face_id UNINDEXED, searchable_text)")
            except sqlite3.Error:
                pass
            # Lightweight migrations for existing local databases.
            cols = {r["name"] for r in db.execute("PRAGMA table_info(media_items)").fetchall()}
            migrations = {
                "tags_json": "ALTER TABLE media_items ADD COLUMN tags_json TEXT NOT NULL DEFAULT '[]'",
                "keywords_json": "ALTER TABLE media_items ADD COLUMN keywords_json TEXT NOT NULL DEFAULT '[]'",
                "error": "ALTER TABLE media_items ADD COLUMN error TEXT NOT NULL DEFAULT ''",
                "updated_at": "ALTER TABLE media_items ADD COLUMN updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP",
            }
            for col, sql in migrations.items():
                if col not in cols:
                    db.execute(sql)

            persona_cols = {r["name"] for r in db.execute("PRAGMA table_info(personas)").fetchall()}
            persona_migrations = {
                "auto_summary": "ALTER TABLE personas ADD COLUMN auto_summary TEXT NOT NULL DEFAULT ''",
                "manual_summary": "ALTER TABLE personas ADD COLUMN manual_summary TEXT NOT NULL DEFAULT ''",
                "manual_tags_json": "ALTER TABLE personas ADD COLUMN manual_tags_json TEXT NOT NULL DEFAULT '[]'",
                "manual_facts_json": "ALTER TABLE personas ADD COLUMN manual_facts_json TEXT NOT NULL DEFAULT '[]'",
                "structured_json": "ALTER TABLE personas ADD COLUMN structured_json TEXT NOT NULL DEFAULT '{}'",
                "metrics_json": "ALTER TABLE personas ADD COLUMN metrics_json TEXT NOT NULL DEFAULT '{}'",
                "analysis_status": "ALTER TABLE personas ADD COLUMN analysis_status TEXT NOT NULL DEFAULT 'legacy_auto'",
                "analysis_progress": "ALTER TABLE personas ADD COLUMN analysis_progress REAL NOT NULL DEFAULT 0",
                "analysis_cursor": "ALTER TABLE personas ADD COLUMN analysis_cursor INTEGER NOT NULL DEFAULT 0",
                "analysis_error": "ALTER TABLE personas ADD COLUMN analysis_error TEXT NOT NULL DEFAULT ''",
                "analysis_version": "ALTER TABLE personas ADD COLUMN analysis_version INTEGER NOT NULL DEFAULT 1",
                "last_analyzed_at": "ALTER TABLE personas ADD COLUMN last_analyzed_at TEXT NOT NULL DEFAULT ''",
                "new_messages_since_analysis": "ALTER TABLE personas ADD COLUMN new_messages_since_analysis INTEGER NOT NULL DEFAULT 0",
            }
            for col, sql in persona_migrations.items():
                if col not in persona_cols:
                    db.execute(sql)
            # Existing summaries remain automatic legacy data. Manual fields start empty,
            # so an old edit is never silently mistaken for a new manual override.
            db.execute("UPDATE personas SET auto_summary=summary WHERE auto_summary='' AND summary!=''")

            # Voice pack migrations.
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS voice_packs (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  name TEXT NOT NULL,
                  category TEXT NOT NULL DEFAULT '',
                  source_path TEXT NOT NULL DEFAULT '',
                  item_count INTEGER NOT NULL DEFAULT 0,
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  UNIQUE(name, category)
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS voice_items (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  pack_id INTEGER NOT NULL DEFAULT 0,
                  category TEXT NOT NULL DEFAULT '',
                  title TEXT NOT NULL,
                  text TEXT NOT NULL DEFAULT '',
                  file TEXT NOT NULL,
                  file_ext TEXT NOT NULL DEFAULT '',
                  size INTEGER NOT NULL DEFAULT 0,
                  duration_ms INTEGER NOT NULL DEFAULT 0,
                  tags_json TEXT NOT NULL DEFAULT '[]',
                  usage_count INTEGER NOT NULL DEFAULT 0,
                  last_used_at TEXT NOT NULL DEFAULT '',
                  status TEXT NOT NULL DEFAULT 'ready',
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  UNIQUE(file)
                )
                """
            )
            db.execute("CREATE INDEX IF NOT EXISTS idx_voice_items_category ON voice_items(category, created_at DESC)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_voice_items_title ON voice_items(title)")

            voice_cols = {r["name"] for r in db.execute("PRAGMA table_info(voice_items)").fetchall()}
            voice_migrations = {
                "aliases_json": "ALTER TABLE voice_items ADD COLUMN aliases_json TEXT NOT NULL DEFAULT '[]'",
                "emotions_json": "ALTER TABLE voice_items ADD COLUMN emotions_json TEXT NOT NULL DEFAULT '[]'",
                "intents_json": "ALTER TABLE voice_items ADD COLUMN intents_json TEXT NOT NULL DEFAULT '[]'",
                "searchable_text": "ALTER TABLE voice_items ADD COLUMN searchable_text TEXT NOT NULL DEFAULT ''",
                "success_count": "ALTER TABLE voice_items ADD COLUMN success_count INTEGER NOT NULL DEFAULT 0",
                "failure_count": "ALTER TABLE voice_items ADD COLUMN failure_count INTEGER NOT NULL DEFAULT 0",
            }
            for col, sql in voice_migrations.items():
                if col not in voice_cols:
                    db.execute(sql)

    def upsert_group(self, group_id: str, name: str = "", source: str = "event", selected: bool = False) -> None:
        if not group_id:
            return
        ts = now_ts()
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO groups(group_id,name,source,selected,last_seen,updated_at)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(group_id) DO UPDATE SET
                  name=CASE WHEN excluded.name!='' THEN excluded.name ELSE groups.name END,
                  source=excluded.source,
                  selected=MAX(groups.selected, excluded.selected),
                  last_seen=excluded.last_seen,
                  updated_at=excluded.updated_at
                """,
                (group_id, name or group_id, source, 1 if selected else 0, ts, ts),
            )

    def upsert_member(self, group_id: str, user_id: str, display_name: str = "", nickname: str = "", card: str = "") -> None:
        if not group_id or not user_id:
            return
        ts = now_ts()
        # OneBot occasionally emits the wxid itself as nickname. It is identity
        # metadata, not a display name, and must never overwrite a name learned
        # from a real group message.
        display_name = display_name if is_readable_member_name(display_name, user_id) else ""
        nickname = nickname if is_readable_member_name(nickname, user_id) else ""
        card = card if is_readable_member_name(card, user_id) else ""
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO members(user_id,group_id,display_name,nickname,card,message_count,last_seen,updated_at)
                VALUES(?,?,?,?,?,1,?,?)
                ON CONFLICT(user_id,group_id) DO UPDATE SET
                  display_name=CASE WHEN excluded.display_name!='' THEN excluded.display_name ELSE members.display_name END,
                  nickname=CASE WHEN excluded.nickname!='' THEN excluded.nickname ELSE members.nickname END,
                  card=CASE WHEN excluded.card!='' THEN excluded.card ELSE members.card END,
                  message_count=members.message_count+1,
                  last_seen=excluded.last_seen,
                  updated_at=excluded.updated_at
                """,
                (user_id, group_id, display_name or nickname or card or user_id, nickname, card, ts, ts),
            )

    def upsert_directory_member(self, group_id: str, user_id: str, display_name: str = "", nickname: str = "", card: str = "") -> None:
        """Merge a contact-directory row without pretending it is a new message."""
        if not group_id or not user_id:
            return
        ts = now_ts()
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO members(user_id,group_id,display_name,nickname,card,message_count,last_seen,updated_at)
                VALUES(?,?,?,?,?,0,'',?)
                ON CONFLICT(user_id,group_id) DO UPDATE SET
                  display_name=CASE WHEN excluded.display_name!='' THEN excluded.display_name ELSE members.display_name END,
                  nickname=CASE WHEN excluded.nickname!='' THEN excluded.nickname ELSE members.nickname END,
                  card=CASE WHEN excluded.card!='' THEN excluded.card ELSE members.card END,
                  updated_at=excluded.updated_at
                """,
                (user_id, group_id, display_name or nickname or card or user_id, nickname, card, ts),
            )

    def add_message(self, item: Dict[str, Any]) -> bool:
        group_id = str(item.get("group_id") or "")
        if not group_id:
            return False
        event_id = str(item.get("event_id") or "")
        if not event_id:
            event_id = f"{group_id}|{item.get('direction','incoming')}|{item.get('message_id','')}|{item.get('event_time',0)}|{hash(str(item.get('text','')))}"
        segments = item.get("segments") if isinstance(item.get("segments"), list) else item.get("message", [])
        raw = item.get("raw") if isinstance(item.get("raw"), dict) else {}
        media = []
        for seg in segments or []:
            if isinstance(seg, dict) and seg.get("type") in {"image", "file", "video", "record", "face"}:
                media.append(seg)
        self.upsert_group(group_id, str(item.get("group_name") or group_id), str(item.get("source") or "event"), bool(item.get("selected", False)))
        self.upsert_member(group_id, str(item.get("user_id") or ""), str(item.get("sender_name") or ""), str(item.get("nickname") or ""), str(item.get("card") or ""))
        row = (
            event_id, str(item.get("trace_id") or ""), str(item.get("direction") or "incoming"), group_id,
            str(item.get("group_name") or ""), str(item.get("user_id") or ""), str(item.get("sender_name") or ""),
            str(item.get("message_id") or ""), int(item.get("event_time") or time.time()), str(item.get("text") or ""),
            str(item.get("raw_message") or ""), json.dumps(segments or [], ensure_ascii=False),
            json.dumps(raw, ensure_ascii=False), json.dumps(media, ensure_ascii=False),
        )
        inserted_media_ids: List[int] = []
        with self.connect() as db:
            cur = db.execute(
                """
                INSERT OR IGNORE INTO messages(event_id,trace_id,direction,group_id,group_name,user_id,sender_name,message_id,event_time,text,raw_message,segments_json,raw_json,media_json)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                row,
            )
            inserted = cur.rowcount > 0
            if inserted:
                durable_payload = {
                    "event_id": row[0], "trace_id": row[1], "direction": row[2],
                    "account_id": str(item.get("account_id") or "current-wechat"),
                    "group_id": row[3], "group_name": row[4], "user_id": row[5],
                    "sender_name": row[6], "message_id": row[7], "event_time": row[8],
                    "text": row[9], "raw_message": row[10],
                    "segments": segments or [], "raw": raw,
                    "source": str(item.get("source") or "event"),
                }
                db.execute(
                    """INSERT OR IGNORE INTO durable_outbox(event_id,payload_json,status,next_attempt_at,updated_at)
                       VALUES(?,?,'pending',0,?)""",
                    (event_id, json.dumps(durable_payload, ensure_ascii=False), now_ts()),
                )
                if row[2] == "incoming" and row[5]:
                    db.execute(
                        """INSERT INTO personas(user_id,group_id,analysis_status,new_messages_since_analysis,updated_at)
                           VALUES(?,?,'not_analyzed',1,?) ON CONFLICT(user_id,group_id) DO UPDATE SET
                           new_messages_since_analysis=personas.new_messages_since_analysis+1,
                           updated_at=excluded.updated_at""",
                        (row[5], group_id, now_ts()),
                    )
                try:
                    db.execute(
                        "INSERT INTO messages_fts(event_id,group_id,sender_name,text,raw_message) VALUES(?,?,?,?,?)",
                        (event_id, group_id, row[6], row[9], row[10]),
                    )
                except sqlite3.Error:
                    pass
                text_for_vector = (row[9] or row[10] or "").strip()
                if text_for_vector:
                    db.execute(
                        """
                        INSERT OR IGNORE INTO embedding_jobs(object_type,object_id,group_id,text,content_hash,status,updated_at)
                        VALUES('message',?,?,?,?, 'pending',?)
                        """,
                        (event_id, group_id, text_for_vector[:8000], hashlib.sha256(text_for_vector.encode("utf-8", "ignore")).hexdigest(), now_ts()),
                    )
                for media_item in media:
                    data = media_item.get("data") if isinstance(media_item.get("data"), dict) else {}
                    file_value = str(data.get("file") or data.get("url") or "")
                    media_type = str(media_item.get("type") or "media")
                    # 微信表情/动图在 OneBot 里通常是 face，但本地会落一个 gif 文件；
                    # 归入 image 图库，方便放大、OCR/视觉解析和后续图片记忆检索。
                    if media_type == "face":
                        raw_face = json.dumps(media_item, ensure_ascii=False)
                        md5_match = re.search(r'md5=\\"([0-9a-fA-F]{16,64})\\"|md5="([0-9a-fA-F]{16,64})"', raw_face)
                        if md5_match:
                            md5_value = next(x for x in md5_match.groups() if x)
                            exists = db.execute(
                                "SELECT 1 FROM media_items WHERE group_id=? AND media_type='image' AND meta_json LIKE ? LIMIT 1",
                                (group_id, f"%{md5_value}%"),
                            ).fetchone()
                            if exists:
                                continue
                        media_type = "image"
                        media_item = dict(media_item)
                        media_item["normalized_type"] = "image"
                    media_cur = db.execute(
                        "INSERT INTO media_items(event_id,group_id,media_type,file,url,meta_json,status,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                        (event_id, group_id, media_type, file_value, file_value if file_value.startswith(("http://", "https://", "file://")) else "", json.dumps(media_item, ensure_ascii=False), "indexed", now_ts()),
                    )
                    if is_face_metadata(json.dumps(media_item, ensure_ascii=False)):
                        inserted_media_ids.append(int(media_cur.lastrowid))
        for media_id in inserted_media_ids:
            try:
                self.sync_face_asset(media_id)
            except Exception:
                pass
        return inserted

    def pending_durable_outbox(self, limit: int = 100) -> List[Dict[str, Any]]:
        now = time.time()
        with self.connect() as db:
            rows = db.execute(
                """SELECT * FROM durable_outbox
                   WHERE status IN ('pending','retry') AND next_attempt_at<=?
                   ORDER BY seq ASC LIMIT ?""",
                (now, max(1, min(500, int(limit or 100)))),
            ).fetchall()
        result: List[Dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                item["payload"] = json.loads(str(item.get("payload_json") or "{}"))
            except ValueError:
                item["payload"] = {}
            result.append(item)
        return result

    def mark_durable_outbox_synced(self, seq: int) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE durable_outbox SET status='synced',last_error='',updated_at=? WHERE seq=?",
                (now_ts(), int(seq)),
            )

    def mark_durable_outbox_retry(self, seq: int, error: str) -> None:
        with self.connect() as db:
            row = db.execute(
                "SELECT attempts FROM durable_outbox WHERE seq=?", (int(seq),)
            ).fetchone()
            attempts = int(row["attempts"] if row else 0) + 1
            delay = min(300.0, 2.0 ** min(attempts, 8))
            db.execute(
                """UPDATE durable_outbox SET status='retry',attempts=?,next_attempt_at=?,
                   last_error=?,updated_at=? WHERE seq=?""",
                (attempts, time.time() + delay, str(error)[:1000], now_ts(), int(seq)),
            )

    def durable_outbox_stats(self) -> Dict[str, Any]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT status,COUNT(*) AS count FROM durable_outbox GROUP BY status"
            ).fetchall()
            oldest = db.execute(
                """SELECT created_at FROM durable_outbox
                   WHERE status IN ('pending','retry') ORDER BY seq LIMIT 1"""
            ).fetchone()
        counts = {str(row["status"]): int(row["count"]) for row in rows}
        return {
            "pending": counts.get("pending", 0) + counts.get("retry", 0),
            "synced": counts.get("synced", 0),
            "failed": counts.get("failed", 0),
            "oldest_pending": str(oldest["created_at"] if oldest else ""),
        }

    def create_automation_run(self, item: Dict[str, Any]) -> tuple[bool, Dict[str, Any]]:
        with self.connect() as db:
            cur = db.execute(
                """INSERT OR IGNORE INTO automation_runs(
                     run_id,idempotency_key,source_event_id,group_id,user_id,intent,risk_level,status,updated_at)
                   VALUES(?,?,?,?,?,?,?,? ,?)""",
                (
                    str(item["run_id"]), str(item["idempotency_key"]),
                    str(item.get("source_event_id") or ""), str(item["group_id"]),
                    str(item["user_id"]), str(item.get("intent") or ""),
                    str(item.get("risk_level") or "read"), str(item.get("status") or "queued"),
                    now_ts(),
                ),
            )
            row = db.execute(
                "SELECT * FROM automation_runs WHERE idempotency_key=?",
                (str(item["idempotency_key"]),),
            ).fetchone()
        return cur.rowcount > 0, dict(row) if row else {}

    def update_automation_run(self, run_id: str, **values: Any) -> None:
        allowed = {"status", "hermes_run_id", "result_summary", "error"}
        clean = {key: str(value) for key, value in values.items() if key in allowed}
        if not clean:
            return
        clean["updated_at"] = now_ts()
        assignments = ",".join(f"{key}=?" for key in clean)
        with self.connect() as db:
            db.execute(
                f"UPDATE automation_runs SET {assignments} WHERE run_id=?",
                (*clean.values(), str(run_id)),
            )

    def add_automation_event(self, run_id: str, event_type: str, payload: Dict[str, Any]) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO automation_events(run_id,event_type,payload_json) VALUES(?,?,?)",
                (str(run_id), str(event_type), json.dumps(payload or {}, ensure_ascii=False)),
            )

    def automation_run(self, run_id: str) -> Dict[str, Any]:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM automation_runs WHERE run_id=?", (str(run_id),)
            ).fetchone()
        return dict(row) if row else {}

    def automation_runs(self, group_id: str = "", limit: int = 50) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM automation_runs"
        params: List[Any] = []
        if group_id:
            sql += " WHERE group_id=?"
            params.append(str(group_id))
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(200, int(limit or 50))))
        with self.connect() as db:
            return [dict(row) for row in db.execute(sql, params).fetchall()]

    def search_messages(self, query: str = "", group_id: str = "", user_id: str = "", limit: int = 50) -> List[Dict[str, Any]]:
        limit = max(1, min(300, int(limit or 50)))
        if query and not user_id:
            rows = self.search_messages_fts(query, group_id, limit)
            if rows:
                return rows
        where: List[str] = []
        params: List[Any] = []
        if group_id:
            where.append("group_id=?")
            params.append(group_id)
        if user_id:
            where.append("user_id=?")
            params.append(user_id)
        if query:
            where.append("(text LIKE ? OR raw_message LIKE ? OR sender_name LIKE ?)")
            like = f"%{query}%"
            params.extend([like, like, like])
        sql = "SELECT * FROM messages"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY event_time DESC, created_at DESC LIMIT ?"
        params.append(limit)
        with self.connect() as db:
            return [dict(r) for r in db.execute(sql, params).fetchall()]

    @staticmethod
    def _fts_query(value: str) -> str:
        parts = re.findall(r"[a-z0-9_]{2,}|[\u4e00-\u9fff]{2,}", str(value or "").lower())
        if not parts:
            return ""
        return " OR ".join('"' + part.replace('"', '""') + '"' for part in dict.fromkeys(parts[:12]))

    def search_messages_fts(self, query: str, group_id: str = "", limit: int = 30) -> List[Dict[str, Any]]:
        expression = self._fts_query(query)
        if not expression:
            return []
        sql = """
            SELECT m.*, bm25(messages_fts, 0.0, 0.0, 1.0, 1.0) AS bm25_score
            FROM messages_fts JOIN messages m ON m.event_id=messages_fts.event_id
            WHERE messages_fts MATCH ?
        """
        params: List[Any] = [expression]
        if group_id:
            sql += " AND m.group_id=?"
            params.append(group_id)
        sql += " ORDER BY bm25_score ASC, m.event_time DESC LIMIT ?"
        params.append(max(1, min(300, int(limit or 30))))
        try:
            with self.connect() as db:
                return [dict(r) for r in db.execute(sql, params).fetchall()]
        except sqlite3.Error:
            return []

    @staticmethod
    def parse_time_range(query: str, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
        text = str(query or "")
        current = now or datetime.now()
        start: Optional[datetime] = None
        end: Optional[datetime] = None
        label = ""
        if "前天" in text:
            start = (current - timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0)
            end, label = start + timedelta(days=1), "前天"
        elif "昨天" in text:
            start = (current - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            end, label = start + timedelta(days=1), "昨天"
        elif "上周" in text:
            this_monday = (current - timedelta(days=current.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
            start, end, label = this_monday - timedelta(days=7), this_monday, "上周"
        elif "去年" in text:
            start = current.replace(year=current.year - 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
            end, label = start.replace(year=start.year + 1), "去年"
        else:
            relative = re.search(r"(\d{1,3})\s*(天|周|个?月|年)前", text)
            if relative:
                count = max(1, int(relative.group(1)))
                unit = relative.group(2)
                days = count if unit == "天" else count * 7 if unit == "周" else count * 30 if "月" in unit else count * 365
                center = current - timedelta(days=days)
                width = 1 if unit == "天" else 7 if unit == "周" else 30 if "月" in unit else 365
                start = center.replace(hour=0, minute=0, second=0, microsecond=0)
                end, label = start + timedelta(days=width), relative.group(0)
            else:
                absolute = re.search(r"(?:(\d{4})[\-/\u5e74])?(\d{1,2})[\-/\u6708](\d{1,2})(?:\u65e5)?", text)
                if absolute:
                    year = int(absolute.group(1) or current.year)
                    try:
                        start = datetime(year, int(absolute.group(2)), int(absolute.group(3)))
                        end, label = start + timedelta(days=1), absolute.group(0)
                    except ValueError:
                        return None
        if start is None or end is None:
            return None
        return {"start": int(start.timestamp()), "end": int(end.timestamp()), "label": label}

    def search_time_messages(self, query: str, group_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        window = self.parse_time_range(query)
        if not window or not group_id:
            return []
        with self.connect() as db:
            rows = db.execute(
                """SELECT * FROM messages WHERE group_id=? AND event_time>=? AND event_time<?
                   ORDER BY event_time DESC,created_at DESC LIMIT ?""",
                (group_id, window["start"], window["end"], max(1, min(100, int(limit)))),
            ).fetchall()
        return [{**dict(row), "time_route": window["label"]} for row in rows]

    def route_people(self, group_id: str, query: str, limit: int = 12) -> List[Dict[str, Any]]:
        text = str(query or "").lower()
        if not text or not group_id:
            return []
        with self.connect() as db:
            rows = db.execute(
                """SELECT m.*,GROUP_CONCAT(a.alias,' ') AS aliases FROM members m
                   LEFT JOIN member_aliases a ON a.group_id=m.group_id AND a.user_id=m.user_id
                   WHERE m.group_id=? GROUP BY m.user_id,m.group_id""",
                (group_id,),
            ).fetchall()
            matched = []
            for raw in rows:
                row = dict(raw)
                names = [row.get("display_name"), row.get("nickname"), row.get("card"), row.get("aliases")]
                if any(str(name or "").strip().lower() in text for name in names if len(str(name or "").strip()) >= 2):
                    matched.append(row)
            if not matched:
                return []
            user_ids = [str(row["user_id"]) for row in matched]
            placeholders = ",".join("?" for _ in user_ids)
            messages = db.execute(
                f"SELECT * FROM messages WHERE group_id=? AND user_id IN ({placeholders}) ORDER BY event_time DESC LIMIT ?",
                [group_id, *user_ids, max(1, min(100, int(limit)))],
            ).fetchall()
        return [{**dict(row), "matched_people": user_ids} for row in messages]

    def route_memes(self, group_id: str, query: str, limit: int = 12) -> List[Dict[str, Any]]:
        text = re.sub(r"\s+", "", str(query or "").lower())
        if not text or not group_id:
            return []
        with self.connect() as db:
            rows = [dict(row) for row in db.execute(
                "SELECT * FROM group_memes WHERE group_id=? ORDER BY confidence DESC,updated_at DESC",
                (group_id,),
            ).fetchall()]
        result = []
        for row in rows:
            fields = [row.get("name"), row.get("meaning"), row.get("triggers_json"), row.get("evidence_json")]
            values = [re.sub(r"\s+", "", str(value or "").lower()) for value in fields]
            exact = any(value and (value in text or text in value) for value in values)
            tokens = set(text_tokens(text))
            overlap = max((len(tokens & set(text_tokens(value))) for value in values), default=0)
            if exact or overlap >= 2:
                result.append({
                    **row, "object_type": "meme", "object_id": str(row["id"]),
                    "text": " ".join(str(value or "") for value in fields), "exact_route": exact,
                })
        return result[:max(1, min(100, int(limit)))]

    def route_media(self, group_id: str, query: str, limit: int = 16) -> List[Dict[str, Any]]:
        terms = [term for term in text_tokens(query) if len(term) >= 2][:12]
        if not group_id or not terms:
            return []
        rows = self.media(group_id=group_id, limit=300)
        scored = []
        for row in rows:
            body = " ".join(str(row.get(key) or "") for key in ("ocr_text", "image_summary", "tags_json", "keywords_json", "raw_message"))
            body_tokens = set(text_tokens(body))
            hits = sum(1 for term in terms if term in body or term in body_tokens)
            if hits:
                scored.append((hits, int(row.get("event_time") or 0), {
                    **row, "object_type": "media", "object_id": str(row["id"]), "text": body,
                }))
        scored.sort(key=lambda item: (-item[0], -item[1]))
        return [row for _, _, row in scored[:max(1, min(100, int(limit)))]]

    def recent_context(self, group_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        return list(reversed(self.search_messages(group_id=group_id, limit=limit)))

    def members(self, group_id: str = "", limit: int = 200) -> List[Dict[str, Any]]:
        limit = max(1, min(1000, int(limit or 200)))
        sql = "SELECT * FROM members"
        params: List[Any] = []
        if group_id:
            sql += " WHERE group_id=?"
            params.append(group_id)
        sql += " ORDER BY message_count DESC,last_seen DESC LIMIT ?"
        params.append(limit)
        with self.connect() as db:
            return [dict(r) for r in db.execute(sql, params).fetchall()]

    def resolve_member_name(self, group_id: str, user_id: str, preferred: str = "") -> str:
        """Return a human-readable group name without exposing internal WeChat IDs."""
        if is_readable_member_name(preferred, user_id):
            return str(preferred).strip()
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT group_id,display_name,nickname,card,message_count,last_seen
                FROM members WHERE user_id=?
                ORDER BY CASE WHEN group_id=? THEN 0 ELSE 1 END,
                         message_count DESC,last_seen DESC
                """,
                (user_id, group_id),
            ).fetchall()
        # A member may use a different nickname in every group. Prefer only the
        # current group's row before inspecting local quoted-message evidence.
        for row in (item for item in rows if item["group_id"] == group_id):
            for key in ("card", "display_name", "nickname"):
                if is_readable_member_name(row[key], user_id):
                    return str(row[key]).strip()
        # Quoted group messages preserve the pair <chatusr>/<displayname> even when
        # OneBot only reported an internal wxid for the original sender. Recover
        # that evidence before falling back to a generic label.
        patterns = (f"%<chatusr>{user_id}</chatusr>%", f"%&lt;chatusr&gt;{user_id}&lt;/chatusr&gt;%")
        with self.connect() as db:
            evidence_rows = db.execute(
                """SELECT raw_message,text FROM messages
                   WHERE group_id=? AND (raw_message LIKE ? OR raw_message LIKE ? OR text LIKE ? OR text LIKE ?)
                   ORDER BY event_time DESC,created_at DESC LIMIT 80""",
                (group_id, patterns[0], patterns[1], patterns[0], patterns[1]),
            ).fetchall()
        for evidence in evidence_rows:
            source = "\n".join(str(evidence[key] or "") for key in ("raw_message", "text"))
            variants = [source]
            for _ in range(2):
                decoded = html.unescape(variants[-1])
                if decoded == variants[-1]:
                    break
                variants.append(decoded)
            for value in variants:
                for block in re.findall(r"<refermsg\b[^>]*>(.*?)</refermsg>", value, re.I | re.S):
                    uid_match = re.search(r"<chatusr>(.*?)</chatusr>", block, re.I | re.S)
                    name_match = re.search(r"<displayname>(.*?)</displayname>", block, re.I | re.S)
                    if not uid_match or not name_match or html.unescape(uid_match.group(1)).strip() != user_id:
                        continue
                    recovered = html.unescape(name_match.group(1)).strip()
                    if not is_readable_member_name(recovered, user_id):
                        continue
                    with self.connect() as db:
                        db.execute(
                            """UPDATE members SET display_name=?,updated_at=?
                               WHERE group_id=? AND user_id=?""",
                            (recovered, now_ts(), group_id, user_id),
                        )
                    return recovered
        # Cross-group identity is the last safe fallback. It is useful when a
        # group has no nickname evidence yet, but must never override a nickname
        # observed in the current group.
        for row in (item for item in rows if item["group_id"] != group_id):
            for key in ("card", "display_name", "nickname"):
                if is_readable_member_name(row[key], user_id):
                    return str(row[key]).strip()
        return "群友"

    def stats(self) -> Dict[str, Any]:
        with self.connect() as db:
            one = lambda sql: db.execute(sql).fetchone()[0]
            return {
                "db_path": str(self.path),
                "groups": one("SELECT COUNT(*) FROM groups"),
                "members": one("SELECT COUNT(*) FROM members"),
                "messages": one("SELECT COUNT(*) FROM messages"),
                "incoming": one("SELECT COUNT(*) FROM messages WHERE direction='incoming'"),
                "outgoing": one("SELECT COUNT(*) FROM messages WHERE direction='outgoing'"),
                "media": one("SELECT COUNT(*) FROM messages WHERE media_json!='[]'"),
                "media_items": one("SELECT COUNT(*) FROM media_items"),
                "face_assets": one("SELECT COUNT(*) FROM face_assets"),
                "vectors": one("SELECT COUNT(*) FROM semantic_embeddings WHERE status='ready'"),
                "embedding_pending": one("SELECT COUNT(*) FROM embedding_jobs WHERE status IN ('pending','retry')"),
                "reply_tasks_active": one("SELECT COUNT(*) FROM reply_tasks WHERE state NOT IN ('completed','skipped','failed','cancelled')"),
                "aliases": one("SELECT COUNT(*) FROM member_aliases"),
                "memes": one("SELECT COUNT(*) FROM group_memes"),
                "personas": one("SELECT COUNT(*) FROM personas"),
                "latest_message_at": (db.execute("SELECT created_at FROM messages ORDER BY created_at DESC LIMIT 1").fetchone() or [""])[0],
            }

    def group_memory(self, group_id: str) -> Dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM group_memory WHERE group_id=?", (group_id,)).fetchone()
            return dict(row) if row else {"group_id": group_id, "summary": "", "facts_json": "[]", "updated_at": ""}

    def save_group_memory(self, group_id: str, summary: str, facts: Optional[List[Any]] = None) -> Dict[str, Any]:
        ts = now_ts()
        with self.connect() as db:
            db.execute(
                "INSERT INTO group_memory(group_id,summary,facts_json,updated_at) VALUES(?,?,?,?) ON CONFLICT(group_id) DO UPDATE SET summary=excluded.summary,facts_json=excluded.facts_json,updated_at=excluded.updated_at",
                (group_id, summary, json.dumps(facts or [], ensure_ascii=False), ts),
            )
        return self.group_memory(group_id)

    def vector_search(self, query: str, group_id: str = "", limit: int = 20) -> List[Dict[str, Any]]:
        # Semantic queries require a real embedding generated by EmbeddingService.
        # Keep this method as a compatibility fallback without recreating fake vectors.
        return self.search_messages(query=query, group_id=group_id, limit=limit)

    @staticmethod
    def _pack_vector(vector: Iterable[float]) -> bytes:
        values = [float(x) for x in vector]
        return struct.pack(f"<{len(values)}f", *values)

    @staticmethod
    def _unpack_vector(raw: bytes, dimensions: int) -> List[float]:
        return list(struct.unpack(f"<{int(dimensions)}f", raw))

    def upsert_embedding(self, object_type: str, object_id: str, group_id: str, text: str,
                         model: str, vector: Iterable[float]) -> int:
        values = [float(x) for x in vector]
        if not values:
            raise ValueError("embedding vector is empty")
        content_hash = hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()
        packed = self._pack_vector(values)
        with self.connect() as db:
            old = db.execute(
                "SELECT id FROM semantic_embeddings WHERE object_type=? AND object_id=? AND model=?",
                (object_type, object_id, model),
            ).fetchone()
            db.execute(
                """
                INSERT INTO semantic_embeddings(object_type,object_id,group_id,text,model,dimensions,content_hash,vector_blob,status,updated_at)
                VALUES(?,?,?,?,?,?,?,?, 'ready',?)
                ON CONFLICT(object_type,object_id,model) DO UPDATE SET
                  group_id=excluded.group_id,text=excluded.text,dimensions=excluded.dimensions,
                  content_hash=excluded.content_hash,vector_blob=excluded.vector_blob,status='ready',updated_at=excluded.updated_at
                """,
                (object_type, object_id, group_id, text, model, len(values), content_hash, packed, now_ts()),
            )
            row = db.execute(
                "SELECT id FROM semantic_embeddings WHERE object_type=? AND object_id=? AND model=?",
                (object_type, object_id, model),
            ).fetchone()
            embedding_id = int(row["id"])
            if len(values) == 4096 and self._load_vec_extension(db):
                try:
                    if old:
                        db.execute("DELETE FROM vec_embeddings WHERE rowid=?", (embedding_id,))
                    db.execute(
                        "INSERT INTO vec_embeddings(rowid,embedding,group_id) VALUES(?,?,?)",
                        (embedding_id, packed, group_id),
                    )
                except sqlite3.Error:
                    pass
            db.execute(
                "UPDATE embedding_jobs SET status='completed',error='',updated_at=? WHERE object_type=? AND object_id=? AND content_hash=?",
                (now_ts(), object_type, object_id, content_hash),
            )
            return embedding_id

    def upsert_embeddings_batch(self, jobs: List[Dict[str, Any]], vectors: List[List[float]],
                                model: str) -> int:
        if len(jobs) != len(vectors):
            raise ValueError("embedding batch length mismatch")
        completed = 0
        with self.connect() as db:
            vec_ready = self._load_vec_extension(db)
            for job, raw_vector in zip(jobs, vectors):
                if job.get("object_type") == "voice_pack" and not db.execute(
                    "SELECT 1 FROM voice_items WHERE id=?", (int(job.get("object_id") or 0),)
                ).fetchone():
                    continue
                values = [float(x) for x in raw_vector]
                if not values:
                    raise ValueError(f"empty embedding for job {job.get('id')}")
                text = str(job.get("text") or "")
                content_hash = hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()
                packed = self._pack_vector(values)
                old = db.execute(
                    "SELECT id FROM semantic_embeddings WHERE object_type=? AND object_id=? AND model=?",
                    (job["object_type"], job["object_id"], model),
                ).fetchone()
                db.execute(
                    """INSERT INTO semantic_embeddings(object_type,object_id,group_id,text,model,dimensions,content_hash,vector_blob,status,updated_at)
                       VALUES(?,?,?,?,?,?,?,?, 'ready',?) ON CONFLICT(object_type,object_id,model) DO UPDATE SET
                       group_id=excluded.group_id,text=excluded.text,dimensions=excluded.dimensions,
                       content_hash=excluded.content_hash,vector_blob=excluded.vector_blob,status='ready',updated_at=excluded.updated_at""",
                    (job["object_type"], job["object_id"], job["group_id"], text, model,
                     len(values), content_hash, packed, now_ts()),
                )
                row = db.execute(
                    "SELECT id FROM semantic_embeddings WHERE object_type=? AND object_id=? AND model=?",
                    (job["object_type"], job["object_id"], model),
                ).fetchone()
                embedding_id = int(row["id"])
                if len(values) == 4096 and vec_ready:
                    try:
                        if old:
                            db.execute("DELETE FROM vec_embeddings WHERE rowid=?", (embedding_id,))
                        db.execute("INSERT INTO vec_embeddings(rowid,embedding,group_id) VALUES(?,?,?)",
                                   (embedding_id, packed, job["group_id"]))
                    except sqlite3.Error:
                        pass
                db.execute(
                    "UPDATE embedding_jobs SET status='completed',error='',updated_at=? WHERE id=?",
                    (now_ts(), int(job["id"])),
                )
                completed += 1
        return completed

    def semantic_search(self, query_vector: Iterable[float], group_id: str, model: str,
                        limit: int = 50) -> List[Dict[str, Any]]:
        vector = [float(x) for x in query_vector]
        limit = max(1, min(200, int(limit or 50)))
        if not vector:
            return []
        with self.connect() as db:
            if len(vector) == 4096 and self._load_vec_extension(db):
                try:
                    rows = db.execute(
                        """
                        SELECT s.*,v.distance FROM vec_embeddings v
                        JOIN semantic_embeddings s ON s.id=v.rowid
                        WHERE v.embedding MATCH ? AND v.k=? AND v.group_id=? AND s.model=?
                        ORDER BY v.distance
                        """,
                        (self._pack_vector(vector), limit, group_id, model),
                    ).fetchall()
                    result = []
                    for raw in rows:
                        item = dict(raw)
                        item.pop("vector_blob", None)
                        # sqlite-vec returns L2 distance. Qwen3 embeddings are
                        # normalized, so cosine similarity is 1 - d^2 / 2.
                        distance = float(raw["distance"])
                        item["score"] = round(max(-1.0, min(1.0, 1.0 - distance * distance / 2.0)), 6)
                        result.append(item)
                    return result
                except sqlite3.Error:
                    pass
            rows = db.execute(
                "SELECT * FROM semantic_embeddings WHERE group_id=? AND model=? AND status='ready'",
                (group_id, model),
            ).fetchall()
        norm = math.sqrt(sum(x * x for x in vector)) or 1.0
        scored: List[Dict[str, Any]] = []
        for row in rows:
            values = self._unpack_vector(row["vector_blob"], row["dimensions"])
            if len(values) != len(vector):
                continue
            other_norm = math.sqrt(sum(x * x for x in values)) or 1.0
            score = sum(a * b for a, b in zip(vector, values)) / (norm * other_norm)
            item = dict(row)
            item.pop("vector_blob", None)
            item["score"] = round(score, 6)
            scored.append(item)
        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:limit]

    def pending_embedding_jobs(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self.connect() as db:
            return [dict(r) for r in db.execute(
                "SELECT * FROM embedding_jobs WHERE status IN ('pending','retry') ORDER BY id LIMIT ?",
                (max(1, min(500, int(limit))),),
            ).fetchall()]

    def recover_interrupted_work(self) -> Dict[str, int]:
        with self.connect() as db:
            jobs = db.execute(
                "UPDATE embedding_jobs SET status='retry',error='service_restarted',updated_at=? WHERE status='running'",
                (now_ts(),),
            ).rowcount
            tasks = db.execute(
                """UPDATE reply_tasks SET state='cancelled',state_label='任务已取消',error='service_restarted',
                   completed_at=?,updated_at=? WHERE state NOT IN ('completed','skipped','failed','cancelled')""",
                (time.time(), time.time()),
            ).rowcount
            return {"embedding_jobs": max(0, jobs), "reply_tasks": max(0, tasks)}

    def mark_embedding_jobs(self, ids: Iterable[int], status: str, error: str = "") -> None:
        values = [int(x) for x in ids]
        if not values:
            return
        placeholders = ",".join("?" for _ in values)
        with self.connect() as db:
            db.execute(
                f"UPDATE embedding_jobs SET status=?,error=?,attempts=attempts+1,updated_at=? WHERE id IN ({placeholders})",
                [status, error[:1000], now_ts(), *values],
            )

    def enqueue_all_embeddings(self) -> int:
        with self.connect() as db:
            added = 0
            sources: List[Tuple[str, str, str, str]] = []
            sources.extend(("message", str(r["event_id"]), str(r["group_id"]), str(r["body"] or "")) for r in db.execute(
                "SELECT event_id,group_id,COALESCE(NULLIF(text,''),raw_message) AS body FROM messages"
            ).fetchall())
            sources.extend(("member", f"{r['group_id']}:{r['user_id']}", str(r["group_id"]),
                            " ".join(str(r[x] or "") for x in ("display_name", "nickname", "card", "profile_json")))
                           for r in db.execute("SELECT * FROM members").fetchall())
            sources.extend(("alias", str(r["id"]), str(r["group_id"]),
                            f"{r['alias']} {r['user_id']} {r['evidence_json']}")
                           for r in db.execute("SELECT * FROM member_aliases").fetchall())
            sources.extend(("relation", str(r["id"]), str(r["group_id"]),
                            f"{r['from_user_id']} {r['relation']} {r['to_user_id']} {r['evidence_json']}")
                           for r in db.execute("SELECT * FROM member_relations").fetchall())
            sources.extend(("meme", str(r["id"]), str(r["group_id"]),
                            f"{r['name']} {r['meaning']} {r['triggers_json']} {r['evidence_json']}")
                           for r in db.execute("SELECT * FROM group_memes").fetchall())
            sources.extend(("persona_claim", str(r["id"]), str(r["group_id"]),
                            f"{r['category']} {r['value']} {r['evidence_json']}")
                           for r in db.execute("SELECT * FROM persona_claims").fetchall())
            sources.extend(("persona", f"{r['group_id']}:{r['user_id']}", str(r["group_id"]),
                            " ".join(str(r[x] or "") for x in ("manual_summary", "auto_summary", "manual_tags_json", "manual_facts_json", "structured_json")))
                           for r in db.execute("SELECT * FROM personas").fetchall())
            sources.extend(("media", str(r["id"]), str(r["group_id"]),
                            " ".join(str(r[x] or "") for x in ("ocr_text", "image_summary", "tags_json", "keywords_json")))
                           for r in db.execute("SELECT * FROM media_items").fetchall())
            sources.extend(("voice_pack", str(r["id"]), "__global__",
                            " ".join(str(r[x] or "") for x in ("title", "text", "category", "tags_json", "aliases_json", "emotions_json", "intents_json")))
                           for r in db.execute("SELECT * FROM voice_items").fetchall())
            sources.extend(("face_asset", str(r["id"]), "__global__", str(r["searchable_text"] or ""))
                           for r in db.execute("SELECT * FROM face_assets").fetchall())
            for object_type, object_id, group_id, raw_text in sources:
                text = raw_text.strip()
                if not text:
                    continue
                content_hash = hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()
                cur = db.execute(
                    "INSERT OR IGNORE INTO embedding_jobs(object_type,object_id,group_id,text,content_hash,status,updated_at) VALUES(?,?,?,?,?,'pending',?)",
                    (object_type, object_id, group_id, text[:8000], content_hash, now_ts()),
                )
                added += max(0, cur.rowcount)
            return added

    def enqueue_embedding(self, object_type: str, object_id: str, group_id: str, text: str) -> int:
        value = str(text or "").strip()
        if not value:
            return 0
        content_hash = hashlib.sha256(value.encode("utf-8", "ignore")).hexdigest()
        with self.connect() as db:
            cur = db.execute(
                "INSERT OR IGNORE INTO embedding_jobs(object_type,object_id,group_id,text,content_hash,status,updated_at) VALUES(?,?,?,?,?,'pending',?)",
                (object_type, object_id, group_id, value[:8000], content_hash, now_ts()),
            )
            return max(0, cur.rowcount)

    def upsert_alias(self, group_id: str, user_id: str, alias: str, confidence: float = 0,
                     evidence: Optional[List[Any]] = None, source: str = "learned") -> Dict[str, Any]:
        value = str(alias or "").strip()
        if not value:
            raise ValueError("alias is empty")
        with self.connect() as db:
            db.execute(
                """INSERT INTO member_aliases(group_id,user_id,alias,confidence,evidence_json,source,updated_at)
                   VALUES(?,?,?,?,?,?,?) ON CONFLICT(group_id,user_id,alias) DO UPDATE SET
                   confidence=MAX(member_aliases.confidence,excluded.confidence),evidence_json=excluded.evidence_json,
                   source=excluded.source,updated_at=excluded.updated_at""",
                (group_id, user_id, value, float(confidence), json.dumps(evidence or [], ensure_ascii=False), source, now_ts()),
            )
            row = dict(db.execute("SELECT * FROM member_aliases WHERE group_id=? AND user_id=? AND alias=?",
                                  (group_id, user_id, value)).fetchone())
        self.enqueue_embedding("alias", str(row["id"]), group_id, f"{value} {user_id} {row['evidence_json']}")
        return row

    def upsert_relation(self, group_id: str, from_user_id: str, to_user_id: str, relation: str,
                        confidence: float = 0, evidence: Optional[List[Any]] = None) -> Dict[str, Any]:
        value = str(relation or "").strip()
        if not value:
            raise ValueError("relation is empty")
        with self.connect() as db:
            db.execute(
                """INSERT INTO member_relations(group_id,from_user_id,to_user_id,relation,confidence,evidence_json,updated_at)
                   VALUES(?,?,?,?,?,?,?) ON CONFLICT(group_id,from_user_id,to_user_id,relation) DO UPDATE SET
                   confidence=MAX(member_relations.confidence,excluded.confidence),evidence_json=excluded.evidence_json,
                   updated_at=excluded.updated_at""",
                (group_id, from_user_id, to_user_id, value, float(confidence),
                 json.dumps(evidence or [], ensure_ascii=False), now_ts()),
            )
            row = dict(db.execute("SELECT * FROM member_relations WHERE group_id=? AND from_user_id=? AND to_user_id=? AND relation=?",
                                  (group_id, from_user_id, to_user_id, value)).fetchone())
        self.enqueue_embedding("relation", str(row["id"]), group_id,
                               f"{from_user_id} {value} {to_user_id} {row['evidence_json']}")
        return row

    def upsert_meme(self, group_id: str, name: str, meaning: str = "", triggers: Optional[List[Any]] = None,
                    evidence: Optional[List[Any]] = None, related_media: Optional[List[Any]] = None,
                    confidence: float = 0, source: str = "learned") -> Dict[str, Any]:
        value = str(name or "").strip()
        if not value:
            raise ValueError("meme name is empty")
        with self.connect() as db:
            db.execute(
                """INSERT INTO group_memes(group_id,name,meaning,triggers_json,evidence_json,related_media_json,confidence,source,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(group_id,name) DO UPDATE SET
                   meaning=excluded.meaning,triggers_json=excluded.triggers_json,evidence_json=excluded.evidence_json,
                   related_media_json=excluded.related_media_json,confidence=MAX(group_memes.confidence,excluded.confidence),
                   source=excluded.source,updated_at=excluded.updated_at""",
                (group_id, value, str(meaning or ""), json.dumps(triggers or [], ensure_ascii=False),
                 json.dumps(evidence or [], ensure_ascii=False), json.dumps(related_media or [], ensure_ascii=False),
                 float(confidence), source, now_ts()),
            )
            row = dict(db.execute("SELECT * FROM group_memes WHERE group_id=? AND name=?", (group_id, value)).fetchone())
        self.enqueue_embedding("meme", str(row["id"]), group_id,
                               f"{value} {meaning} {row['triggers_json']} {row['evidence_json']}")
        return row

    def media(self, group_id: str = "", media_type: str = "", limit: int = 100, status: str = "", query: str = "") -> List[Dict[str, Any]]:
        sql = """
            SELECT mi.*, m.sender_name, m.user_id, m.message_id, m.event_time, m.raw_message
            FROM media_items mi
            LEFT JOIN messages m ON m.event_id=mi.event_id
        """
        where: List[str] = []
        params: List[Any] = []
        if group_id:
            where.append("mi.group_id=?")
            params.append(group_id)
        if media_type:
            where.append("mi.media_type=?")
            params.append(media_type)
        if status:
            where.append("mi.status=?")
            params.append(status)
        if query:
            like = f"%{query}%"
            where.append("(mi.ocr_text LIKE ? OR mi.image_summary LIKE ? OR mi.tags_json LIKE ? OR mi.keywords_json LIKE ? OR m.sender_name LIKE ? OR m.raw_message LIKE ?)")
            params.extend([like, like, like, like, like, like])
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY mi.created_at DESC LIMIT ?"
        params.append(max(1, min(500, int(limit or 100))))
        with self.connect() as db:
            return [dict(r) for r in db.execute(sql, params).fetchall()]

    def set_group_reply_mute(self, group_id: str, duration_seconds: int, triggered_by: str = "",
                             trigger_message_id: str = "") -> Dict[str, Any]:
        muted_until = time.time() + max(1, int(duration_seconds))
        with self.connect() as db:
            db.execute(
                """INSERT INTO group_reply_mutes(group_id,muted_until,triggered_by,trigger_message_id,updated_at)
                   VALUES(?,?,?,?,?) ON CONFLICT(group_id) DO UPDATE SET
                   muted_until=excluded.muted_until,triggered_by=excluded.triggered_by,
                   trigger_message_id=excluded.trigger_message_id,updated_at=excluded.updated_at""",
                (str(group_id), muted_until, str(triggered_by), str(trigger_message_id), now_ts()),
            )
        return self.group_reply_mute(group_id)

    def group_reply_mute(self, group_id: str) -> Dict[str, Any]:
        now = time.time()
        with self.connect() as db:
            row = db.execute("SELECT * FROM group_reply_mutes WHERE group_id=?", (str(group_id),)).fetchone()
            if not row:
                return {"group_id": str(group_id), "active": False, "muted_until": 0.0, "remaining_seconds": 0}
            result = dict(row)
            remaining = max(0, int(float(result.get("muted_until") or 0) - now + 0.999))
            if remaining <= 0:
                db.execute("DELETE FROM group_reply_mutes WHERE group_id=?", (str(group_id),))
                return {"group_id": str(group_id), "active": False, "muted_until": 0.0, "remaining_seconds": 0}
            result.update({"active": True, "remaining_seconds": remaining})
            return result

    def active_group_reply_mutes(self) -> List[Dict[str, Any]]:
        now = time.time()
        with self.connect() as db:
            db.execute("DELETE FROM group_reply_mutes WHERE muted_until<=?", (now,))
            rows = [dict(row) for row in db.execute(
                "SELECT * FROM group_reply_mutes WHERE muted_until>? ORDER BY muted_until DESC", (now,)
            ).fetchall()]
        for row in rows:
            row["active"] = True
            row["remaining_seconds"] = max(1, int(float(row["muted_until"]) - now + 0.999))
        return rows

    def clear_group_reply_mute(self, group_id: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM group_reply_mutes WHERE group_id=?", (str(group_id),))

    def group_admins(self, group_id: str = "", include_disabled: bool = False) -> List[Dict[str, Any]]:
        where: List[str] = []
        params: List[Any] = []
        if group_id:
            where.append("group_id=?")
            params.append(str(group_id))
        if not include_disabled:
            where.append("enabled=1")
        sql = "SELECT * FROM group_admins"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY group_id,updated_at DESC,id DESC"
        with self.connect() as db:
            rows = [dict(row) for row in db.execute(sql, params).fetchall()]
        for row in rows:
            try:
                row["permissions"] = json.loads(row.pop("permissions_json") or "[]")
            except Exception:
                row["permissions"] = []
            row["enabled"] = bool(row.get("enabled"))
        return rows

    def group_admin(self, group_id: str, user_id: str) -> Dict[str, Any]:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM group_admins WHERE group_id=? AND user_id=? AND enabled=1",
                (str(group_id), str(user_id)),
            ).fetchone()
        if not row:
            return {}
        result = dict(row)
        try:
            result["permissions"] = json.loads(result.pop("permissions_json") or "[]")
        except Exception:
            result["permissions"] = []
        result["enabled"] = bool(result.get("enabled"))
        return result

    def save_group_admins(self, group_id: str, admins: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        clean: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for item in admins:
            user_id = str(item.get("user_id") or "").strip()
            if not user_id or user_id.endswith("@chatroom") or user_id in seen:
                continue
            seen.add(user_id)
            clean.append({
                "user_id": user_id,
                "display_name": str(item.get("display_name") or "")[:100],
                "role": str(item.get("role") or "custom")[:40],
                "permissions": sorted({str(value) for value in item.get("permissions") or [] if str(value)}),
                "source": str(item.get("source") or "directory")[:40],
                "enabled": bool(item.get("enabled", True)),
            })
        with self.connect() as db:
            existing = {
                str(row["user_id"]) for row in db.execute(
                    "SELECT user_id FROM group_admins WHERE group_id=?", (str(group_id),)
                ).fetchall()
            }
            for item in clean:
                db.execute(
                    """INSERT INTO group_admins(
                         group_id,user_id,display_name,role,permissions_json,source,enabled,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?)
                       ON CONFLICT(group_id,user_id) DO UPDATE SET
                         display_name=excluded.display_name,role=excluded.role,
                         permissions_json=excluded.permissions_json,source=excluded.source,
                         enabled=excluded.enabled,updated_at=excluded.updated_at""",
                    (
                        str(group_id), item["user_id"], item["display_name"], item["role"],
                        json.dumps(item["permissions"], ensure_ascii=False), item["source"],
                        1 if item["enabled"] else 0, now_ts(),
                    ),
                )
            removed = existing - seen
            if removed:
                placeholders = ",".join("?" for _ in removed)
                db.execute(
                    f"DELETE FROM group_admins WHERE group_id=? AND user_id IN ({placeholders})",
                    [str(group_id), *sorted(removed)],
                )
        return self.group_admins(str(group_id), include_disabled=True)

    def add_group_admin_audit(self, payload: Dict[str, Any]) -> tuple[bool, Dict[str, Any]]:
        values = (
            str(payload.get("group_id") or ""), str(payload.get("user_id") or ""),
            str(payload.get("display_name") or "")[:100], str(payload.get("command") or "")[:500],
            str(payload.get("permission") or "")[:80], str(payload.get("message_id") or ""),
            str(payload.get("trace_id") or ""), json.dumps(payload.get("before") or {}, ensure_ascii=False),
            json.dumps(payload.get("after") or {}, ensure_ascii=False), str(payload.get("result") or "")[:80],
            str(payload.get("error") or "")[:1000], now_ts(),
        )
        try:
            with self.connect() as db:
                cur = db.execute(
                    """INSERT INTO group_admin_audit(
                         group_id,user_id,display_name,command,permission,message_id,trace_id,
                         before_json,after_json,result,error,created_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    values,
                )
                audit_id = int(cur.lastrowid)
            return True, {"id": audit_id, **payload}
        except sqlite3.IntegrityError:
            with self.connect() as db:
                row = db.execute(
                    """SELECT * FROM group_admin_audit
                       WHERE group_id=? AND message_id=? AND command=?""",
                    (values[0], values[5], values[3]),
                ).fetchone()
            return False, dict(row) if row else {}

    def group_admin_audit(self, group_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        with self.connect() as db:
            rows = [dict(row) for row in db.execute(
                "SELECT * FROM group_admin_audit WHERE group_id=? ORDER BY id DESC LIMIT ?",
                (str(group_id), max(1, min(200, int(limit or 50)))),
            ).fetchall()]
        for row in rows:
            for key in ("before_json", "after_json"):
                try:
                    row[key[:-5]] = json.loads(row.pop(key) or "{}")
                except Exception:
                    row[key[:-5]] = {}
        return rows

    def finish_group_admin_audit(self, audit_id: int, before: Dict[str, Any],
                                 after: Dict[str, Any], result: str, error: str = "") -> None:
        with self.connect() as db:
            db.execute(
                """UPDATE group_admin_audit SET before_json=?,after_json=?,result=?,error=?
                   WHERE id=?""",
                (
                    json.dumps(before or {}, ensure_ascii=False),
                    json.dumps(after or {}, ensure_ascii=False),
                    str(result or "")[:80], str(error or "")[:1000], int(audit_id),
                ),
            )

    def claim_media_status(self, media_id: int, expected: Iterable[str], status: str) -> bool:
        values = [str(value) for value in expected if str(value)]
        if not values:
            return False
        placeholders = ",".join("?" for _ in values)
        with self.connect() as db:
            cur = db.execute(
                f"UPDATE media_items SET status=?,error='',updated_at=? WHERE id=? AND status IN ({placeholders})",
                [str(status), now_ts(), int(media_id), *values],
            )
            return cur.rowcount > 0

    def pending_media_analysis(self, media_type: str = "image", limit: int = 100) -> List[Dict[str, Any]]:
        statuses = ("indexed", "ocr_queued", "ocr_running", "ocr_failed") if media_type == "image" else (
            "indexed", "asr_queued", "asr_running", "asr_failed", "waiting_transcript"
        )
        placeholders = ",".join("?" for _ in statuses)
        with self.connect() as db:
            rows = db.execute(
                f"""SELECT mi.*,m.raw_json,m.message_id,m.event_time,m.trace_id
                    FROM media_items mi JOIN messages m ON m.event_id=mi.event_id
                    WHERE mi.media_type=? AND mi.status IN ({placeholders})
                    ORDER BY mi.id DESC LIMIT ?""",
                [str(media_type), *statuses, max(1, min(300, int(limit)))],
            ).fetchall()
            return [dict(row) for row in rows]

    def latest_image_before(self, group_id: str, event_time: int = 0, user_id: str = "") -> Dict[str, Any]:
        # Prefer the current member's immediately preceding image, then fall back
        # to the newest image in the group. This is what phrases such as
        # "我刚发了什么图" naturally refer to.
        def fetch(member_only: bool) -> Dict[str, Any]:
            where = ["mi.group_id=?", "mi.media_type='image'"]
            params: List[Any] = [str(group_id)]
            if event_time:
                where.append("m.event_time<=?")
                params.append(int(event_time))
            if member_only and user_id:
                where.append("m.user_id=?")
                params.append(str(user_id))
            sql = f"""SELECT mi.*,m.sender_name,m.user_id,m.message_id,m.event_time,m.raw_message,m.raw_json
                      FROM media_items mi JOIN messages m ON m.event_id=mi.event_id
                      WHERE {' AND '.join(where)} ORDER BY m.event_time DESC,mi.id DESC LIMIT 1"""
            with self.connect() as db:
                row = db.execute(sql, params).fetchone()
                return dict(row) if row else {}
        return fetch(True) or fetch(False)

    def referenced_image(self, group_id: str, message_id: str = "", md5: str = "") -> Dict[str, Any]:
        """Resolve a quoted image inside the current group without using recency.

        WeChat quote XML preserves the original ``svrid`` and usually the image
        MD5.  The message id is authoritative; MD5 is a fallback for duplicate
        callback/log rows whose event id differs.  Both routes remain strictly
        group-scoped so an identical asset in another group cannot leak in.
        """
        group_id = str(group_id or "").strip()
        message_id = str(message_id or "").strip()
        md5 = re.sub(r"[^0-9a-f]", "", str(md5 or "").lower())
        if not group_id or (not message_id and len(md5) < 16):
            return {}
        conditions: List[str] = []
        params: List[Any] = [group_id]
        if message_id:
            conditions.append("m.message_id=?")
            params.append(message_id)
        if len(md5) >= 16:
            conditions.append("(mi.meta_json LIKE ? OR m.raw_message LIKE ? OR m.raw_json LIKE ?)")
            like = f"%{md5}%"
            params.extend([like, like, like])
        with self.connect() as db:
            row = db.execute(
                f"""
                SELECT mi.*,m.sender_name,m.user_id,m.message_id,m.event_time,m.raw_message,m.raw_json
                FROM media_items mi
                JOIN messages m ON m.event_id=mi.event_id
                WHERE mi.group_id=? AND mi.media_type='image' AND ({' OR '.join(conditions)})
                ORDER BY CASE WHEN m.message_id=? THEN 0 ELSE 1 END,
                         CASE WHEN mi.image_summary<>'' OR mi.ocr_text<>'' THEN 0 ELSE 1 END,
                         mi.id DESC LIMIT 1
                """,
                [*params, message_id],
            ).fetchone()
            return dict(row) if row else {}

    def normalize_voice_placeholders(self, group_id: str = "") -> int:
        """Keep missing Silk payload diagnostics out of the user-facing error state.

        OneBot can expose a record message before it exports the media bytes. That is
        still a valid indexed voice bubble: WeChat's own transcript can arrive later.
        Older rows stored the transport diagnostic as a red ``保存音频失败`` error, which
        incorrectly made a successfully captured voice message appear lost.
        """
        sql = """
            SELECT id, group_id, file, ocr_text, image_summary, status, error
            FROM media_items
            WHERE media_type='record'
        """
        params: List[Any] = []
        if group_id:
            sql += " AND group_id=?"
            params.append(group_id)

        diagnostic_re = re.compile(r"保存音频失败|empty audio payload|JSON 序列化失败", re.IGNORECASE)
        normalized = 0
        with self.connect() as db:
            rows = [dict(row) for row in db.execute(sql, params).fetchall()]
            for row in rows:
                transcript = str(row.get("ocr_text") or "").strip()
                summary = str(row.get("image_summary") or "").strip()
                error = str(row.get("error") or "").strip()
                if transcript:
                    desired_summary = f"微信客户端自动转文字：{transcript}"
                    if row.get("status") != "transcribed" or error or summary != desired_summary:
                        db.execute(
                            "UPDATE media_items SET image_summary=?, status='transcribed', error='', updated_at=? WHERE id=?",
                            (desired_summary, now_ts(), int(row["id"])),
                        )
                        normalized += 1
                    continue
                if diagnostic_re.search(summary) or diagnostic_re.search(error):
                    has_file = bool(str(row.get("file") or "").strip())
                    desired_summary = (
                        "语音泡原始文件已抓取，等待 ASR 转文字。"
                        if has_file else
                        "语音泡已建立索引，等待补抓原始音频文件。"
                    )
                    desired_status = "indexed" if has_file else "waiting_audio"
                    db.execute(
                        "UPDATE media_items SET image_summary=?, error='', status=?, updated_at=? WHERE id=?",
                        (desired_summary, desired_status, now_ts(), int(row["id"])),
                    )
                    normalized += 1
        return normalized

    def deduplicate_media_items(self, group_id: str = "", media_type: str = "") -> int:
        """Collapse duplicate media rows emitted by callback and log recovery paths.

        A record callback can be persisted first by the AI service and then replayed by
        the log recovery worker after its CDN diagnostic is printed.  The stable key is
        the real message id when available, otherwise the chatroom voice file second.
        Prefer any row that already has a transcript/OCR annotation.
        """
        sql = """
            SELECT mi.*, m.message_id, m.event_time, m.raw_message
            FROM media_items mi
            LEFT JOIN messages m ON m.event_id=mi.event_id
        """
        where: List[str] = []
        params: List[Any] = []
        if group_id:
            where.append("mi.group_id=?")
            params.append(group_id)
        if media_type:
            where.append("mi.media_type=?")
            params.append(media_type)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY mi.id ASC"

        file_second = re.compile(r"(?:\d+@chatroom_)?(\d{10})_\d+")
        buckets: Dict[str, List[Dict[str, Any]]] = {}
        with self.connect() as db:
            all_rows = [dict(raw) for raw in db.execute(sql, params).fetchall()]
            for row in all_rows:
                typ = str(row.get("media_type") or "")
                gid = str(row.get("group_id") or "")
                message_id = str(row.get("message_id") or "")
                key = f"{gid}|{typ}|{message_id}" if message_id and not message_id.endswith("@chatroom") else ""
                if typ == "record":
                    source = "\n".join([
                        message_id,
                        str(row.get("raw_message") or ""),
                        str(row.get("meta_json") or ""),
                    ])
                    match = file_second.search(source)
                    if match:
                        key = f"{gid}|record|{match.group(1)}"
                    elif row.get("event_time"):
                        event_time = int(row.get("event_time") or 0)
                        event_second = event_time // 1000 if event_time > 10_000_000_000 else event_time
                        key = f"{gid}|record|{event_second}"
                if key:
                    buckets.setdefault(key, []).append(row)

            removed = 0
            # A log fallback row has a generated event timestamp, but its file id
            # carries the original voice second.  Match it to the real callback row
            # with a small timestamp tolerance and remove only the fallback row.
            synthetic_ids: set[int] = set()
            real_records = [
                row for row in all_rows
                if str(row.get("media_type") or "") == "record"
                and not str(row.get("event_id") or "").startswith("onebot-record-failed-")
            ]
            for row in all_rows:
                if not str(row.get("event_id") or "").startswith("onebot-record-failed-"):
                    continue
                source = "\n".join([str(row.get("message_id") or ""), str(row.get("raw_message") or ""), str(row.get("meta_json") or "")])
                match = file_second.search(source)
                if not match:
                    continue
                voice_second = int(match.group(1))
                for candidate in real_records:
                    if str(candidate.get("group_id") or "") != str(row.get("group_id") or ""):
                        continue
                    event_time = int(candidate.get("event_time") or 0)
                    event_second = event_time // 1000 if event_time > 10_000_000_000 else event_time
                    if abs(event_second - voice_second) <= 2:
                        synthetic_ids.add(int(row["id"]))
                        break
            for rows in buckets.values():
                if len(rows) < 2:
                    continue
                rows.sort(key=lambda row: (
                    0 if (str(row.get("ocr_text") or "").strip() or str(row.get("image_summary") or "").strip()) else 1,
                    0 if str(row.get("status") or "") in {"transcribed", "ocr_done", "annotated"} else 1,
                    -int(row.get("id") or 0),
                ))
                keep = rows[0]
                # Merge a useful annotation before removing the stale copies.
                for candidate in rows[1:]:
                    if not (str(keep.get("ocr_text") or "").strip() or str(keep.get("image_summary") or "").strip()) and (
                        str(candidate.get("ocr_text") or "").strip() or str(candidate.get("image_summary") or "").strip()
                    ):
                        for field in ("ocr_text", "image_summary", "tags_json", "keywords_json", "status", "error"):
                            keep[field] = candidate.get(field)
                if str(keep.get("ocr_text") or "").strip() or str(keep.get("image_summary") or "").strip():
                    desired = "transcribed" if str(keep.get("media_type") or "") == "record" else "ocr_done"
                    db.execute(
                        """UPDATE media_items SET ocr_text=?,image_summary=?,tags_json=?,keywords_json=?,status=?,error=?,updated_at=?
                           WHERE id=?""",
                        (str(keep.get("ocr_text") or ""), str(keep.get("image_summary") or ""),
                         str(keep.get("tags_json") or "[]"), str(keep.get("keywords_json") or "[]"),
                         desired, str(keep.get("error") or ""), now_ts(), int(keep["id"])),
                    )
                stale_ids = [int(row["id"]) for row in rows if int(row["id"]) != int(keep["id"])]
                if stale_ids:
                    placeholders = ",".join("?" for _ in stale_ids)
                    db.execute(f"DELETE FROM media_items WHERE id IN ({placeholders})", stale_ids)
                    removed += len(stale_ids)
            if synthetic_ids:
                placeholders = ",".join("?" for _ in synthetic_ids)
                db.execute(f"DELETE FROM media_items WHERE id IN ({placeholders})", list(synthetic_ids))
                removed += len(synthetic_ids)
            return removed

    def media_detail(self, media_id: int) -> Dict[str, Any]:
        with self.connect() as db:
            row = db.execute(
                """
                SELECT mi.*, m.sender_name, m.user_id, m.message_id, m.event_time, m.raw_message
                FROM media_items mi
                LEFT JOIN messages m ON m.event_id=mi.event_id
                WHERE mi.id=?
                """,
                (int(media_id),),
            ).fetchone()
            return dict(row) if row else {}

    def media_by_event(self, event_id: str) -> List[Dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT mi.*, m.sender_name, m.user_id, m.message_id, m.event_time, m.raw_message
                FROM media_items mi
                LEFT JOIN messages m ON m.event_id=mi.event_id
                WHERE mi.event_id=?
                ORDER BY mi.id ASC
                """,
                (str(event_id),),
            ).fetchall()
            return [dict(r) for r in rows]

    @staticmethod
    def _face_file_path(value: str) -> Optional[Path]:
        raw = str(value or "").strip()
        if raw.startswith("file://"):
            raw = urllib.parse.unquote(urllib.parse.urlparse(raw).path)
        if not raw or raw.startswith(("http://", "https://", "base64://", "data:")):
            return None
        path = Path(raw).expanduser()
        return path if path.exists() and path.is_file() else None

    def face_key_for_media(self, row: Dict[str, Any]) -> str:
        raw = str(row.get("meta_json") or "") + "\n" + str(row.get("raw_message") or "")
        match = re.search(r'md5=\\?"([0-9a-fA-F]{16,64})\\?"|"md5"\s*:\s*"([0-9a-fA-F]{16,64})"', raw)
        if match:
            return next(value.lower() for value in match.groups() if value)
        path = self._face_file_path(str(row.get("file") or row.get("url") or ""))
        if path:
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            return digest.hexdigest()
        return hashlib.sha256(str(row.get("file") or row.get("url") or row.get("id") or "").encode("utf-8", "ignore")).hexdigest()

    @staticmethod
    def _json_words(value: Any) -> List[str]:
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        try:
            parsed = json.loads(str(value or "[]"))
            return [str(item).strip() for item in parsed if str(item).strip()] if isinstance(parsed, list) else []
        except (ValueError, TypeError):
            return [part.strip() for part in re.split(r"[,，;；\s]+", str(value or "")) if part.strip()]

    @classmethod
    def face_searchable_text(cls, row: Dict[str, Any]) -> str:
        fields: List[str] = [str(row.get("ocr_text") or ""), str(row.get("image_summary") or "")]
        for key in ("tags_json", "keywords_json", "aliases_json", "emotions_json", "intents_json", "actions_json", "subjects_json"):
            fields.extend(cls._json_words(row.get(key)))
        return " ".join(dict.fromkeys(part.strip() for part in fields if part.strip()))[:8000]

    def sync_face_asset(self, media_id: int) -> Dict[str, Any]:
        row = self.media_detail(int(media_id))
        if not row or not is_face_metadata(str(row.get("meta_json") or ""), str(row.get("raw_message") or "")):
            return {}
        face_key = self.face_key_for_media(row)
        searchable = self.face_searchable_text(row)
        ts = now_ts()
        with self.connect() as db:
            existing = db.execute("SELECT * FROM face_assets WHERE face_key=?", (face_key,)).fetchone()
            if existing:
                current = dict(existing)
                prefer_new = bool(searchable) and len(searchable) >= len(str(current.get("searchable_text") or ""))
                if prefer_new:
                    db.execute(
                        """UPDATE face_assets SET canonical_media_id=?,file=?,ocr_text=?,image_summary=?,tags_json=?,
                           keywords_json=?,searchable_text=?,updated_at=? WHERE id=?""",
                        (int(media_id), str(row.get("file") or row.get("url") or ""), str(row.get("ocr_text") or ""),
                         str(row.get("image_summary") or ""), str(row.get("tags_json") or "[]"),
                         str(row.get("keywords_json") or "[]"), searchable, ts, int(current["id"])),
                    )
                face_id = int(current["id"])
            else:
                cur = db.execute(
                    """INSERT INTO face_assets(face_key,canonical_media_id,file,ocr_text,image_summary,tags_json,
                       keywords_json,searchable_text,updated_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (face_key, int(media_id), str(row.get("file") or row.get("url") or ""),
                     str(row.get("ocr_text") or ""), str(row.get("image_summary") or ""),
                     str(row.get("tags_json") or "[]"), str(row.get("keywords_json") or "[]"), searchable, ts),
                )
                face_id = int(cur.lastrowid)
            db.execute(
                """INSERT INTO face_asset_groups(face_id,group_id,source_media_id,last_seen)
                   VALUES(?,?,?,?) ON CONFLICT(face_id,group_id) DO UPDATE SET
                   source_media_id=excluded.source_media_id,last_seen=excluded.last_seen""",
                (face_id, str(row.get("group_id") or ""), int(media_id), ts),
            )
            try:
                db.execute("DELETE FROM face_assets_fts WHERE face_id=?", (str(face_id),))
                db.execute("INSERT INTO face_assets_fts(face_id,searchable_text) VALUES(?,?)", (str(face_id), searchable))
            except sqlite3.Error:
                pass
        if searchable:
            self.enqueue_embedding("face_asset", str(face_id), "__global__", searchable)
        return self.face_asset(face_id)

    def import_face_asset(self, file_path: str, label: str = "手动上传") -> Dict[str, Any]:
        path = Path(file_path).expanduser().resolve()
        if not path.is_file():
            raise ValueError("表情文件不存在")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        ts = now_ts()
        searchable = str(label or path.stem).strip()[:500]
        with self.connect() as db:
            existing = db.execute("SELECT id FROM face_assets WHERE face_key=?", (digest,)).fetchone()
            if existing:
                face_id = int(existing["id"])
                db.execute("UPDATE face_assets SET file=?,enabled=1,updated_at=? WHERE id=?", (str(path), ts, face_id))
            else:
                cur = db.execute(
                    """INSERT INTO face_assets(face_key,canonical_media_id,file,image_summary,tags_json,
                       keywords_json,searchable_text,updated_at) VALUES(?,0,?,?,?, ?,?,?)""",
                    (digest, str(path), searchable, json.dumps(["手动上传", "拍一拍"], ensure_ascii=False),
                     json.dumps([searchable], ensure_ascii=False), searchable, ts),
                )
                face_id = int(cur.lastrowid)
            try:
                db.execute("DELETE FROM face_assets_fts WHERE face_id=?", (str(face_id),))
                db.execute("INSERT INTO face_assets_fts(face_id,searchable_text) VALUES(?,?)", (str(face_id), searchable))
            except sqlite3.Error:
                pass
        self.enqueue_embedding("face_asset", str(face_id), "__global__", searchable)
        return self.face_asset(face_id)

    def rebuild_face_assets(self) -> Dict[str, int]:
        with self.connect() as db:
            rows = [dict(row) for row in db.execute(
                """SELECT mi.*,m.raw_message FROM media_items mi
                   LEFT JOIN messages m ON m.event_id=mi.event_id WHERE mi.media_type='image' ORDER BY mi.id"""
            ).fetchall()]
        indexed = 0
        for row in rows:
            if is_face_metadata(str(row.get("meta_json") or ""), str(row.get("raw_message") or "")):
                if self.sync_face_asset(int(row["id"])):
                    indexed += 1
        return {"indexed_occurrences": indexed, "assets": self.face_asset_count()}

    def face_asset_count(self) -> int:
        with self.connect() as db:
            return int(db.execute("SELECT COUNT(*) FROM face_assets").fetchone()[0])

    def face_asset(self, face_id: int) -> Dict[str, Any]:
        with self.connect() as db:
            row = db.execute(
                """SELECT fa.*,mi.group_id AS source_group_id,mi.meta_json,mi.status,mi.url
                   FROM face_assets fa LEFT JOIN media_items mi ON mi.id=fa.canonical_media_id WHERE fa.id=?""",
                (int(face_id),),
            ).fetchone()
            return dict(row) if row else {}

    def face_asset_available(self, face_id: int, group_id: str = "", global_shared: bool = True) -> bool:
        """Check global and per-group switches before a vector result can be sent."""
        with self.connect() as db:
            row = db.execute(
                """SELECT fa.enabled,mi.group_id AS source_group_id,
                          fag.face_id AS group_row,COALESCE(fag.enabled,1) AS group_enabled
                   FROM face_assets fa LEFT JOIN media_items mi ON mi.id=fa.canonical_media_id
                   LEFT JOIN face_asset_groups fag ON fag.face_id=fa.id AND fag.group_id=?
                   WHERE fa.id=?""",
                (str(group_id or ""), int(face_id)),
            ).fetchone()
        if not row or not bool(row["enabled"]) or not bool(row["group_enabled"]):
            return False
        return bool(global_shared or row["group_row"] is not None)

    def search_face_assets(self, query: str = "", group_id: str = "", limit: int = 50,
                           global_shared: bool = True, include_disabled: bool = False) -> List[Dict[str, Any]]:
        sql = """
            SELECT fa.*,mi.group_id AS source_group_id,mi.meta_json,mi.status,mi.url,
                   COALESCE(fag.enabled,1) AS group_enabled
            FROM face_assets fa LEFT JOIN media_items mi ON mi.id=fa.canonical_media_id
            LEFT JOIN face_asset_groups fag ON fag.face_id=fa.id AND fag.group_id=?
            WHERE 1=1
        """
        params: List[Any] = [group_id]
        if not include_disabled:
            sql += " AND fa.enabled=1 AND COALESCE(fag.enabled,1)=1"
        if group_id and not global_shared:
            sql += " AND fag.group_id=?"
            params.append(group_id)
        sql += " ORDER BY fa.success_count DESC,fa.failure_count ASC,fa.updated_at DESC LIMIT 5000"
        with self.connect() as db:
            rows = [dict(row) for row in db.execute(sql, params).fetchall()]
        q = voice_search_text(query)
        if not q:
            return rows[:max(1, min(500, int(limit)))]
        q_terms = set(voice_search_terms(q))
        intent_aliases: List[str] = []
        for intent, aliases in FACE_INTENT_ALIASES.items():
            if intent in q:
                for alias in aliases:
                    intent_aliases.append(voice_search_text(alias))
                    q_terms.update(voice_search_terms(alias))
        fts_ids: set[int] = set()
        expression = self._fts_query(" ".join([str(query), *intent_aliases]))
        if expression:
            try:
                with self.connect() as db:
                    fts_ids = {int(row["face_id"]) for row in db.execute(
                        "SELECT face_id FROM face_assets_fts WHERE face_assets_fts MATCH ? LIMIT 100",
                        (expression,),
                    ).fetchall()}
            except sqlite3.Error:
                fts_ids = set()
        scored = []
        for row in rows:
            body = voice_search_text(row.get("searchable_text"))
            body_terms = set(voice_search_terms(body))
            exact = bool(q and (q == body or q in body))
            intent_hit = any(alias and alias in body for alias in intent_aliases)
            fts_hit = int(row.get("id") or 0) in fts_ids
            phrase_hits = len(q_terms & body_terms)
            shared = len(set(q) & set(body))
            score = (0.88 if exact else 0.0) + (0.72 if intent_hit else 0.0) + (0.25 if fts_hit else 0.0) + min(0.36, phrase_hits * 0.12) + min(0.12, shared * 0.02)
            if not exact and not intent_hit and not fts_hit and phrase_hits == 0:
                continue
            reliability = (int(row.get("success_count") or 0) + 1) / (
                int(row.get("success_count") or 0) + int(row.get("failure_count") or 0) + 2
            )
            score = min(1.0, score * (0.9 + reliability * 0.1))
            row["match_score"] = round(score, 4)
            row["match_reason"] = "OCR/别名完整命中" if exact else "情绪意图匹配" if intent_hit else f"关键词命中 {phrase_hits}"
            scored.append((score, int(row.get("success_count") or 0), -int(row["id"]), row))
        scored.sort(key=lambda item: (-item[0], -item[1], item[2]))
        return [row for _, _, _, row in scored[:max(1, min(500, int(limit)))]]

    def update_face_asset(self, face_id: int, values: Dict[str, Any]) -> Dict[str, Any]:
        allowed_json = ("aliases_json", "emotions_json", "intents_json", "actions_json", "subjects_json", "tags_json", "keywords_json")
        updates: Dict[str, Any] = {}
        for key in allowed_json:
            if key in values:
                raw = values[key]
                updates[key] = json.dumps(raw if isinstance(raw, list) else self._json_words(raw), ensure_ascii=False)
        for key in ("ocr_text", "image_summary"):
            if key in values:
                updates[key] = str(values[key] or "")[:4000]
        if "enabled" in values:
            updates["enabled"] = 1 if bool(values["enabled"]) else 0
        if not updates:
            return self.face_asset(face_id)
        current = self.face_asset(face_id)
        if not current:
            return {}
        merged = {**current, **updates}
        updates["searchable_text"] = self.face_searchable_text(merged)
        updates["updated_at"] = now_ts()
        assignments = ",".join(f"{key}=?" for key in updates)
        with self.connect() as db:
            db.execute(f"UPDATE face_assets SET {assignments} WHERE id=?", [*updates.values(), int(face_id)])
            try:
                db.execute("DELETE FROM face_assets_fts WHERE face_id=?", (str(face_id),))
                db.execute("INSERT INTO face_assets_fts(face_id,searchable_text) VALUES(?,?)", (str(face_id), updates["searchable_text"]))
            except sqlite3.Error:
                pass
        self.enqueue_embedding("face_asset", str(face_id), "__global__", updates["searchable_text"])
        return self.face_asset(face_id)

    def set_face_group_enabled(self, face_id: int, group_id: str, enabled: bool) -> Dict[str, Any]:
        asset = self.face_asset(face_id)
        if not asset:
            return {}
        with self.connect() as db:
            db.execute(
                """INSERT INTO face_asset_groups(face_id,group_id,source_media_id,enabled,last_seen)
                   VALUES(?,?,?,?,?) ON CONFLICT(face_id,group_id) DO UPDATE SET enabled=excluded.enabled,last_seen=excluded.last_seen""",
                (int(face_id), group_id, int(asset.get("canonical_media_id") or 0), 1 if enabled else 0, now_ts()),
            )
        return self.face_asset(face_id)

    def mark_face_used(self, face_id: int, success: bool) -> None:
        column = "success_count" if success else "failure_count"
        with self.connect() as db:
            db.execute(
                f"UPDATE face_assets SET usage_count=usage_count+1,{column}={column}+1,last_used_at=?,updated_at=? WHERE id=?",
                (now_ts(), now_ts(), int(face_id)),
            )

    def save_media_annotation(self, media_id: int, ocr_text: str = "", image_summary: str = "", status: str = "annotated",
                              tags: Optional[List[str]] = None, keywords: Optional[List[str]] = None, error: str = "") -> Dict[str, Any]:
        tags = tags or []
        keywords = keywords or []
        with self.connect() as db:
            db.execute(
                "UPDATE media_items SET ocr_text=?, image_summary=?, tags_json=?, keywords_json=?, error=?, status=?, updated_at=? WHERE id=?",
                (ocr_text, image_summary, json.dumps(tags, ensure_ascii=False), json.dumps(keywords, ensure_ascii=False), error, status, now_ts(), int(media_id)),
            )
        row = self.media_detail(int(media_id))
        if row and is_face_metadata(str(row.get("meta_json") or ""), str(row.get("raw_message") or "")):
            self.sync_face_asset(int(media_id))
        elif row:
            searchable = self.face_searchable_text(row)
            if searchable:
                self.enqueue_embedding("media", str(media_id), str(row.get("group_id") or ""), searchable)
        return row

    def mark_media_status(self, media_id: int, status: str, error: str = "") -> Dict[str, Any]:
        with self.connect() as db:
            db.execute("UPDATE media_items SET status=?, error=?, updated_at=? WHERE id=?", (status, error, now_ts(), int(media_id)))
        return self.media_detail(int(media_id))

    def analyze_media_metadata(self, media_id: int) -> Dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM media_items WHERE id=?", (int(media_id),)).fetchone()
            if not row:
                return {}
            item = dict(row)
            meta: Dict[str, Any] = {}
            file_value = item.get("file") or item.get("url") or ""
            local_value = str(file_value or "")
            if local_value.startswith("file://"):
                local_value = urllib.parse.unquote(urllib.parse.urlparse(local_value).path)
            path = Path(local_value).expanduser()
            if file_value and path.exists():
                meta["file_size"] = path.stat().st_size
                meta["suffix"] = path.suffix.lower()
                meta["local_path"] = str(path)
                # Lightweight image dimension parser for PNG/JPEG without extra dependencies.
                try:
                    raw = path.read_bytes()[:4096]
                    if raw.startswith(b"\x89PNG\r\n\x1a\n") and len(raw) >= 24:
                        meta["width"] = int.from_bytes(raw[16:20], "big")
                        meta["height"] = int.from_bytes(raw[20:24], "big")
                    elif raw.startswith(b"\xff\xd8"):
                        i = 2
                        while i + 9 < len(raw):
                            if raw[i] != 0xFF:
                                i += 1
                                continue
                            marker = raw[i + 1]
                            size = int.from_bytes(raw[i + 2:i + 4], "big")
                            if marker in {0xC0, 0xC2} and i + 8 < len(raw):
                                meta["height"] = int.from_bytes(raw[i + 5:i + 7], "big")
                                meta["width"] = int.from_bytes(raw[i + 7:i + 9], "big")
                                break
                            i += max(size + 2, 2)
                except Exception as exc:
                    meta["parse_error"] = str(exc)
            elif str(file_value).startswith(("http://", "https://")):
                meta["remote_url"] = file_value
                meta["note"] = "远程媒体暂不下载，只建立索引。"
            else:
                meta["note"] = "文件不存在或 OneBot 未提供可解析路径。"
            merged = {}
            try:
                merged.update(json.loads(item.get("meta_json") or "{}"))
            except Exception:
                pass
            merged["analysis"] = meta
            db.execute(
                "UPDATE media_items SET meta_json=?, status=? WHERE id=?",
                (json.dumps(merged, ensure_ascii=False), "metadata_ready", int(media_id)),
            )
            item.update({"meta_json": json.dumps(merged, ensure_ascii=False), "status": "metadata_ready"})
            return item

    def personas(self, group_id: str = "", limit: int = 200) -> List[Dict[str, Any]]:
        sql = """
        SELECT m.user_id,m.group_id,m.display_name,m.nickname,m.card,m.message_count,m.last_seen,
               COALESCE(NULLIF(p.manual_summary,''),NULLIF(p.auto_summary,''),p.summary,'') AS summary,
               COALESCE(NULLIF(p.manual_tags_json,'[]'),p.tags_json,'[]') AS tags_json,
               COALESCE(NULLIF(p.manual_facts_json,'[]'),p.facts_json,'[]') AS facts_json,
               COALESCE(p.analysis_status,'not_analyzed') AS analysis_status,
               COALESCE(p.analysis_progress,0) AS analysis_progress,
               COALESCE(p.updated_at,m.updated_at) AS updated_at
        FROM members m LEFT JOIN personas p ON p.user_id=m.user_id AND p.group_id=m.group_id
        """
        params: List[Any] = []
        if group_id:
            sql += " WHERE m.group_id=?"
            params.append(group_id)
        sql += " ORDER BY m.message_count DESC,m.last_seen DESC LIMIT ?"
        params.append(max(1, min(1000, int(limit or 200))))
        with self.connect() as db:
            return [dict(r) for r in db.execute(sql, params).fetchall()]

    @staticmethod
    def _persona_member_is_real(user_id: Any, group_id: str = "") -> bool:
        uid = str(user_id or "").strip()
        return bool(uid and uid != group_id and not uid.endswith("@chatroom") and uid.lower() not in {
            "ai", "onebot-log", "web-admin-test-user", "voice-test-user", "voice-fix-test-user",
            "wxid_tester", "wxid_test_codex"
        })

    def sync_persona_directory(self, group_id: str = "") -> Dict[str, int]:
        """Recover every observed member and best real name from permanent history."""
        where = "WHERE direction='incoming' AND user_id!=''"
        params: List[Any] = []
        if group_id:
            where += " AND group_id=?"
            params.append(group_id)
        created = renamed = 0
        with self.connect() as db:
            rows = [dict(r) for r in db.execute(
                f"""SELECT group_id,user_id,COUNT(*) message_count,MAX(event_time) last_event
                    FROM messages {where} GROUP BY group_id,user_id""", params
            ).fetchall()]
            for row in rows:
                gid, uid = str(row["group_id"]), str(row["user_id"])
                if not self._persona_member_is_real(uid, gid):
                    continue
                names = db.execute(
                    """SELECT sender_name,MAX(event_time) latest,COUNT(*) frequency FROM messages
                       WHERE group_id=? AND user_id=? AND direction='incoming'
                       GROUP BY sender_name ORDER BY latest DESC,frequency DESC""", (gid, uid)
                ).fetchall()
                best = next((str(x["sender_name"]).strip() for x in names
                             if is_readable_member_name(x["sender_name"], uid)), "")
                old = db.execute("SELECT * FROM members WHERE group_id=? AND user_id=?", (gid, uid)).fetchone()
                if old is None:
                    db.execute(
                        """INSERT INTO members(user_id,group_id,display_name,nickname,message_count,last_seen,updated_at)
                           VALUES(?,?,?,?,?,?,?)""",
                        (uid, gid, best or uid, best, int(row["message_count"] or 0),
                         datetime.fromtimestamp(int(row["last_event"] or time.time())).strftime("%Y-%m-%d %H:%M:%S"), now_ts()),
                    )
                    created += 1
                elif best and effective_member_name(old, uid) != best:
                    db.execute("UPDATE members SET display_name=?,nickname=?,updated_at=? WHERE group_id=? AND user_id=?",
                               (best, best, now_ts(), gid, uid))
                    renamed += 1
                db.execute(
                    """INSERT INTO personas(user_id,group_id,analysis_status,new_messages_since_analysis,updated_at)
                       VALUES(?,?,'not_analyzed',?,?) ON CONFLICT(user_id,group_id) DO NOTHING""",
                    (uid, gid, int(row["message_count"] or 0), now_ts()),
                )
        return {"created": created, "renamed": renamed}

    @staticmethod
    def _json_value(value: Any, fallback: Any) -> Any:
        if isinstance(value, (list, dict)):
            return value
        try:
            parsed = json.loads(str(value or ""))
            return parsed if isinstance(parsed, type(fallback)) else fallback
        except (TypeError, ValueError):
            return fallback

    def persona_list(self, group_id: str, query: str = "", status: str = "", page: int = 1,
                     page_size: int = 100) -> Dict[str, Any]:
        self.sync_persona_directory(group_id)
        page = max(1, int(page or 1))
        page_size = max(1, min(500, int(page_size or 100)))
        where = ["m.group_id=?", "m.user_id!=m.group_id", "m.user_id NOT IN ('AI','onebot-log','web-admin-test-user','voice-test-user','voice-fix-test-user','wxid_tester','wxid_test_codex')"]
        params: List[Any] = [group_id]
        if query:
            like = f"%{query.strip()}%"
            where.append("(m.display_name LIKE ? OR m.nickname LIKE ? OR m.card LIKE ? OR m.user_id LIKE ? OR EXISTS(SELECT 1 FROM member_aliases a WHERE a.group_id=m.group_id AND a.user_id=m.user_id AND a.alias LIKE ?))")
            params.extend([like] * 5)
        if status:
            where.append("COALESCE(p.analysis_status,'not_analyzed')=?")
            params.append(status)
        sql_from = " FROM members m LEFT JOIN personas p ON p.group_id=m.group_id AND p.user_id=m.user_id WHERE " + " AND ".join(where)
        with self.connect() as db:
            total = int(db.execute("SELECT COUNT(*)" + sql_from, params).fetchone()[0])
            rows = db.execute(
                """SELECT m.user_id,m.group_id,m.display_name,m.nickname,m.card,
                          (SELECT COUNT(*) FROM messages msg WHERE msg.group_id=m.group_id AND msg.user_id=m.user_id AND msg.direction='incoming') message_count,
                          COALESCE((SELECT MAX(msg.created_at) FROM messages msg WHERE msg.group_id=m.group_id AND msg.user_id=m.user_id AND msg.direction='incoming'),m.last_seen) last_seen,
                          COALESCE(p.analysis_status,'not_analyzed') analysis_status,
                          COALESCE(p.analysis_progress,0) analysis_progress,
                          COALESCE(NULLIF(p.manual_summary,''),NULLIF(p.auto_summary,''),p.summary,'') summary,
                          COALESCE(p.updated_at,m.updated_at) updated_at""" + sql_from +
                " ORDER BY message_count DESC,last_seen DESC LIMIT ? OFFSET ?",
                [*params, page_size, (page - 1) * page_size],
            ).fetchall()
            items = []
            for raw in rows:
                item = dict(raw)
                item["display_name"] = effective_member_name(item, item["user_id"])
                item["aliases"] = [r[0] for r in db.execute(
                    "SELECT alias FROM member_aliases WHERE group_id=? AND user_id=? ORDER BY confidence DESC,id LIMIT 8",
                    (group_id, item["user_id"]),
                ).fetchall()]
                items.append(item)
        return {"items": items, "count": len(items), "total": total, "page": page, "page_size": page_size}

    @staticmethod
    def _message_datetime(row: Dict[str, Any]) -> datetime:
        raw = int(row.get("event_time") or 0)
        if raw > 10_000_000_000:
            raw //= 1000
        if raw > 0:
            try:
                return datetime.fromtimestamp(raw)
            except (OverflowError, OSError, ValueError):
                pass
        try:
            return datetime.strptime(str(row.get("created_at") or "")[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return datetime.now()

    def persona_metrics(self, group_id: str, user_id: str) -> Dict[str, Any]:
        with self.connect() as db:
            own = [dict(r) for r in db.execute(
                "SELECT event_id,message_id,event_time,created_at,text,raw_message,segments_json,media_json FROM messages WHERE group_id=? AND user_id=? AND direction='incoming' ORDER BY event_time,created_at,event_id",
                (group_id, user_id),
            ).fetchall()]
            stream = [dict(r) for r in db.execute(
                "SELECT event_id,user_id,sender_name,event_time,created_at,text,raw_message FROM messages WHERE group_id=? AND direction='incoming' ORDER BY event_time,created_at,event_id",
                (group_id,),
            ).fetchall()]
            names = {str(r["user_id"]): effective_member_name(r, str(r["user_id"])) for r in db.execute(
                "SELECT * FROM members WHERE group_id=?", (group_id,)
            ).fetchall()}
            learned = [dict(r) for r in db.execute(
                "SELECT * FROM member_relations WHERE group_id=? AND (from_user_id=? OR to_user_id=?)",
                (group_id, user_id, user_id),
            ).fetchall()]
        heatmap = [[0 for _ in range(24)] for _ in range(7)]
        daily: Counter[str] = Counter()
        media: Counter[str] = Counter()
        lengths: List[int] = []
        term_counts: Counter[str] = Counter()
        term_evidence: Dict[str, Dict[str, Any]] = {}
        stop = {"这个", "那个", "什么", "怎么", "还是", "就是", "可以", "不是", "一个", "我们", "你们", "他们", "然后", "没有", "知道", "现在", "已经", "因为", "所以", "但是"}
        for row in own:
            dt = self._message_datetime(row)
            heatmap[dt.weekday()][dt.hour] += 1
            daily[dt.strftime("%Y-%m-%d")] += 1
            text_value = str(row.get("text") or row.get("raw_message") or "").strip()
            lengths.append(len(text_value))
            try:
                segments = self._json_value(row.get("segments_json"), [])
                types = [str(x.get("type") or "") for x in segments if isinstance(x, dict)]
            except Exception:
                types = []
            for kind in set(types):
                if kind and kind != "text":
                    media[kind] += 1
            if not types and self._json_value(row.get("media_json"), []):
                media["media"] += 1
            for token in text_tokens(text_value):
                if token in stop or len(token) < 2 or token.isdigit():
                    continue
                term_counts[token] += 1
                term_evidence.setdefault(token, {"event_id": row["event_id"], "message_id": row.get("message_id") or "", "time": dt.strftime("%Y-%m-%d %H:%M:%S"), "text": text_value[:240]})
        interactions: Counter[str] = Counter()
        previous: Optional[Dict[str, Any]] = None
        for row in stream:
            if row["user_id"] == user_id and previous and previous["user_id"] != user_id:
                delta = (self._message_datetime(row) - self._message_datetime(previous)).total_seconds()
                if 0 <= delta <= 300:
                    interactions[str(previous["user_id"])] += 1
            elif row["user_id"] != user_id:
                body = str(row.get("text") or row.get("raw_message") or "")
                if user_id in body or any(name and name in body for name in (names.get(user_id, ""),)):
                    interactions[str(row["user_id"])] += 2
            previous = row
        relation_map: Dict[str, List[str]] = defaultdict(list)
        for rel in learned:
            other = rel["to_user_id"] if rel["from_user_id"] == user_id else rel["from_user_id"]
            relation_map[str(other)].append(str(rel["relation"]))
        top_interactions = [{"user_id": uid, "name": names.get(uid, uid), "count": count, "relations": relation_map.get(uid, [])} for uid, count in interactions.most_common(20)]
        topics = [{"name": token, "count": count, "evidence": term_evidence[token]} for token, count in term_counts.most_common(16)]
        trend = [{"date": day, "count": daily[day]} for day in sorted(daily)[-90:]]
        return {
            "message_count": len(own), "average_length": round(sum(lengths) / len(lengths), 1) if lengths else 0,
            "heatmap": heatmap, "trend": trend, "media": dict(media),
            "media_ratio": round(sum(media.values()) / len(own), 4) if own else 0,
            "interactions": top_interactions, "topics": topics,
        }

    def persona_detail(self, group_id: str, user_id: str) -> Dict[str, Any]:
        with self.connect() as db:
            member = db.execute("SELECT * FROM members WHERE group_id=? AND user_id=?", (group_id, user_id)).fetchone()
            if not member:
                return {}
            persona = db.execute("SELECT * FROM personas WHERE group_id=? AND user_id=?", (group_id, user_id)).fetchone()
            aliases = [dict(r) for r in db.execute("SELECT * FROM member_aliases WHERE group_id=? AND user_id=? ORDER BY confidence DESC,id", (group_id, user_id)).fetchall()]
            claims = [dict(r) for r in db.execute("SELECT * FROM persona_claims WHERE group_id=? AND user_id=? ORDER BY priority DESC,confidence DESC,updated_at DESC,id DESC", (group_id, user_id)).fetchall()]
            jobs = [dict(r) for r in db.execute("SELECT * FROM persona_analysis_jobs WHERE group_id=? AND user_id=? ORDER BY id DESC LIMIT 10", (group_id, user_id)).fetchall()]
            memes = [dict(r) for r in db.execute("SELECT * FROM group_memes WHERE group_id=? ORDER BY confidence DESC,id", (group_id,)).fetchall()]
            member_messages = [dict(r) for r in db.execute(
                "SELECT event_id,message_id,text,raw_message FROM messages WHERE group_id=? AND user_id=? AND direction='incoming'",
                (group_id, user_id),
            ).fetchall()]
        p = dict(persona) if persona else {}
        for claim in claims:
            claim["evidence"] = self._json_value(claim.pop("evidence_json", "[]"), [])
        # Metrics are cheap local SQL/Python aggregates and must reflect newly
        # arrived messages immediately, even before the next semantic rebuild.
        metrics = self.persona_metrics(group_id, user_id)
        facts = self._json_value(p.get("facts_json"), [])
        manual_facts = self._json_value(p.get("manual_facts_json"), [])
        tags = self._json_value(p.get("tags_json"), [])
        manual_tags = self._json_value(p.get("manual_tags_json"), [])
        effective_summary = p.get("manual_summary") or p.get("auto_summary") or p.get("summary") or ""
        profile = {
            **dict(member), **p, "display_name": effective_member_name(member, user_id),
            "message_count": metrics.get("message_count", 0),
            "summary": effective_summary, "tags": list(dict.fromkeys([*manual_tags, *tags])),
            "facts": [*manual_facts, *[x for x in facts if x not in manual_facts]],
            "manual_tags": manual_tags, "manual_facts": manual_facts,
        }
        message_ids = {str(value) for row in member_messages for value in (row.get("event_id"), row.get("message_id")) if value}
        member_body = "\n".join(str(row.get("text") or row.get("raw_message") or "") for row in member_messages).lower()
        related_memes = []
        for meme in memes:
            evidence = self._json_value(meme.get("evidence_json"), [])
            refs = set()
            for item in evidence:
                if isinstance(item, dict):
                    refs.update(str(item.get(key) or "") for key in ("event_id", "message_id") if item.get(key))
                elif item:
                    refs.add(str(item))
            triggers = [str(x).lower() for x in self._json_value(meme.get("triggers_json"), []) if str(x).strip()]
            name = str(meme.get("name") or "").lower()
            if refs & message_ids or (name and name in member_body) or any(trigger in member_body for trigger in triggers):
                meme["evidence"] = evidence
                related_memes.append(meme)
        return {"profile": profile, "aliases": aliases, "claims": claims, "metrics": metrics,
                "relationships": metrics.get("interactions", []), "memes": related_memes, "jobs": jobs}

    def search_voice_items(self, query: str, category: str = "", pack_id: int = 0, limit: int = 8) -> List[Dict[str, Any]]:
        """Search the complete local voice index and rank content, intent, and metadata."""
        rows = self.voice_items(category=category, pack_id=pack_id, limit=50000)
        query = str(query or "").strip()
        if not query:
            return rows[:max(1, min(50000, int(limit or 8)))]
        scored: List[tuple[int, int, int, Dict[str, Any]]] = []
        for row in rows:
            score, reason = voice_match_score(query, row)
            if score < 75:
                continue
            row["match_score"] = score
            row["match_reason"] = reason or "内容相关"
            scored.append((score, int(row.get("usage_count") or 0), -int(row.get("id") or 0), row))
        scored.sort(key=lambda item: (-item[0], item[1], item[2]))
        return [item[3] for item in scored[:max(1, min(50000, int(limit or 8)))]]

    def save_persona(self, user_id: str, group_id: str, summary: str, tags: Optional[List[Any]] = None, facts: Optional[List[Any]] = None) -> Dict[str, Any]:
        ts = now_ts()
        manual_claims: List[tuple[str, str]] = []
        with self.connect() as db:
            db.execute(
                """INSERT INTO personas(user_id,group_id,summary,tags_json,facts_json,manual_summary,manual_tags_json,manual_facts_json,analysis_status,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id,group_id) DO UPDATE SET
                   manual_summary=excluded.manual_summary,manual_tags_json=excluded.manual_tags_json,
                   manual_facts_json=excluded.manual_facts_json,updated_at=excluded.updated_at""",
                (user_id, group_id, summary, json.dumps(tags or [], ensure_ascii=False), json.dumps(facts or [], ensure_ascii=False),
                 summary, json.dumps(tags or [], ensure_ascii=False), json.dumps(facts or [], ensure_ascii=False), "manual", ts),
            )
            db.execute("DELETE FROM persona_claims WHERE group_id=? AND user_id=? AND source='manual'", (group_id, user_id))
            for fact in facts or []:
                value = str(fact.get("value") if isinstance(fact, dict) else fact).strip()
                if value:
                    db.execute("INSERT OR IGNORE INTO persona_claims(group_id,user_id,category,value,confidence,source,priority,updated_at) VALUES(?,?,?,?,1,'manual',100,?)", (group_id, user_id, "fact", value, ts))
                    manual_claims.append((f"manual:{group_id}:{user_id}:{hashlib.sha1(value.encode()).hexdigest()}", value))
        for claim_id, value in manual_claims:
            self.enqueue_embedding("persona_claim", claim_id, group_id, value)
        self.enqueue_embedding("persona", f"{group_id}:{user_id}", group_id, " ".join([summary, *map(str, tags or []), *map(str, facts or [])]))
        rows = [x for x in self.personas(group_id, 1000) if x["user_id"] == user_id]
        return rows[0] if rows else {"user_id": user_id, "group_id": group_id, "summary": summary, "tags_json": json.dumps(tags or [], ensure_ascii=False), "facts_json": json.dumps(facts or [], ensure_ascii=False), "updated_at": ts}

    def upsert_voice_pack(self, name: str, category: str = "", source_path: str = "") -> int:
        ts = now_ts()
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO voice_packs(name,category,source_path,item_count,updated_at)
                VALUES(?,?,?,0,?)
                ON CONFLICT(name,category) DO UPDATE SET
                  source_path=CASE WHEN excluded.source_path!='' THEN excluded.source_path ELSE voice_packs.source_path END,
                  updated_at=excluded.updated_at
                """,
                (name, category, source_path, ts),
            )
            row = db.execute("SELECT id FROM voice_packs WHERE name=? AND category=?", (name, category)).fetchone()
            return int(row["id"])

    def add_voice_item(self, pack_id: int, category: str, title: str, text: str, file: str, file_ext: str,
                       size: int = 0, duration_ms: int = 0, tags: Optional[List[str]] = None) -> bool:
        ts = now_ts()
        tags = tags or []
        searchable = " ".join(dict.fromkeys([title, text, category, *tags])).strip()
        item_id = 0
        with self.connect() as db:
            cur = db.execute(
                """
                INSERT OR IGNORE INTO voice_items(pack_id,category,title,text,file,file_ext,size,duration_ms,tags_json,searchable_text,status,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (int(pack_id or 0), category, title, text, file, file_ext, int(size or 0), int(duration_ms or 0),
                 json.dumps(tags, ensure_ascii=False), searchable, "ready", ts),
            )
            inserted = cur.rowcount > 0
            row = db.execute("SELECT id FROM voice_items WHERE file=?", (file,)).fetchone()
            item_id = int(row["id"]) if row else 0
            db.execute("UPDATE voice_packs SET item_count=(SELECT COUNT(*) FROM voice_items WHERE pack_id=?), updated_at=? WHERE id=?", (pack_id, ts, pack_id))
        if item_id and searchable:
            self.enqueue_embedding("voice_pack", str(item_id), "__global__", searchable)
        return inserted

    def voice_packs(self) -> List[Dict[str, Any]]:
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM voice_packs ORDER BY updated_at DESC,id DESC").fetchall()]

    def voice_pack(self, pack_id: int) -> Dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM voice_packs WHERE id=?", (int(pack_id),)).fetchone()
            return dict(row) if row else {}

    def voice_items(self, category: str = "", pack_id: int = 0, query: str = "", limit: int = 200) -> List[Dict[str, Any]]:
        sql = """
        SELECT vi.*, vp.name AS pack_name
        FROM voice_items vi LEFT JOIN voice_packs vp ON vp.id=vi.pack_id
        """
        where: List[str] = []
        params: List[Any] = []
        if category:
            where.append("vi.category=?")
            params.append(category)
        if pack_id:
            where.append("vi.pack_id=?")
            params.append(int(pack_id))
        if query:
            like = f"%{query}%"
            where.append("(vi.title LIKE ? OR vi.text LIKE ? OR vi.tags_json LIKE ? OR vi.aliases_json LIKE ? OR vi.emotions_json LIKE ? OR vi.intents_json LIKE ? OR vi.searchable_text LIKE ? OR vi.category LIKE ? OR vp.name LIKE ?)")
            params.extend([like] * 9)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY vi.usage_count ASC, vi.created_at DESC LIMIT ?"
        params.append(max(1, min(50000, int(limit or 200))))
        with self.connect() as db:
            return [dict(r) for r in db.execute(sql, params).fetchall()]

    def voice_item(self, item_id: int) -> Dict[str, Any]:
        with self.connect() as db:
            row = db.execute(
                "SELECT vi.*, vp.name AS pack_name FROM voice_items vi LEFT JOIN voice_packs vp ON vp.id=vi.pack_id WHERE vi.id=?",
                (int(item_id),),
            ).fetchone()
            return dict(row) if row else {}

    def _delete_voice_rows(self, db: sqlite3.Connection, item_ids: Iterable[int]) -> Dict[str, Any]:
        ids = sorted({int(x) for x in item_ids if int(x) > 0})
        if not ids:
            return {"deleted": 0, "items": [], "embedding_count": 0, "job_count": 0}
        placeholders = ",".join("?" for _ in ids)
        rows = [dict(r) for r in db.execute(
            f"SELECT * FROM voice_items WHERE id IN ({placeholders})", ids
        ).fetchall()]
        if not rows:
            return {"deleted": 0, "items": [], "embedding_count": 0, "job_count": 0}
        actual_ids = [int(row["id"]) for row in rows]
        actual_placeholders = ",".join("?" for _ in actual_ids)
        object_ids = [str(x) for x in actual_ids]
        embedding_placeholders = ",".join("?" for _ in object_ids)
        embedding_rows = db.execute(
            f"SELECT id FROM semantic_embeddings WHERE object_type='voice_pack' AND object_id IN ({embedding_placeholders})",
            object_ids,
        ).fetchall()
        if embedding_rows and self._load_vec_extension(db):
            for embedding in embedding_rows:
                try:
                    db.execute("DELETE FROM vec_embeddings WHERE rowid=?", (int(embedding["id"]),))
                except sqlite3.Error:
                    break
        embedding_count = db.execute(
            f"DELETE FROM semantic_embeddings WHERE object_type='voice_pack' AND object_id IN ({embedding_placeholders})",
            object_ids,
        ).rowcount
        job_count = db.execute(
            f"DELETE FROM embedding_jobs WHERE object_type='voice_pack' AND object_id IN ({embedding_placeholders})",
            object_ids,
        ).rowcount
        db.execute(f"DELETE FROM voice_items WHERE id IN ({actual_placeholders})", actual_ids)
        for pack_id in sorted({int(row["pack_id"] or 0) for row in rows if int(row["pack_id"] or 0) > 0}):
            db.execute(
                "UPDATE voice_packs SET item_count=(SELECT COUNT(*) FROM voice_items WHERE pack_id=?),updated_at=? WHERE id=?",
                (pack_id, now_ts(), pack_id),
            )
        return {
            "deleted": len(rows), "items": rows,
            "embedding_count": max(0, embedding_count), "job_count": max(0, job_count),
        }

    def delete_voice_item(self, item_id: int) -> Dict[str, Any]:
        with self.connect() as db:
            return self._delete_voice_rows(db, [item_id])

    def delete_voice_pack(self, pack_id: int) -> Dict[str, Any]:
        with self.connect() as db:
            pack = db.execute("SELECT * FROM voice_packs WHERE id=?", (int(pack_id),)).fetchone()
            if not pack:
                return {"deleted": 0, "items": [], "pack": {}, "embedding_count": 0, "job_count": 0}
            item_ids = [int(r["id"]) for r in db.execute(
                "SELECT id FROM voice_items WHERE pack_id=?", (int(pack_id),)
            ).fetchall()]
            result = self._delete_voice_rows(db, item_ids)
            db.execute("DELETE FROM voice_packs WHERE id=?", (int(pack_id),))
            result["pack"] = dict(pack)
            return result

    def mark_voice_used(self, item_id: int, success: bool = True) -> None:
        column = "success_count" if success else "failure_count"
        with self.connect() as db:
            db.execute(
                f"UPDATE voice_items SET usage_count=usage_count+1,{column}={column}+1,last_used_at=?,updated_at=? WHERE id=?",
                (now_ts(), now_ts(), int(item_id)),
            )

    def save_reply_task(self, task: Dict[str, Any]) -> None:
        now = float(task.get("updated_at") or time.time())
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO reply_tasks(task_id,trace_id,thread_id,group_id,group_name,user_id,sender_name,message_id,
                  question,state,state_label,score,threshold,medium,model,result,error,details_json,
                  queued_at,started_at,completed_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(task_id) DO UPDATE SET
                  state=excluded.state,state_label=excluded.state_label,score=excluded.score,
                  threshold=excluded.threshold,medium=excluded.medium,model=excluded.model,
                  result=excluded.result,error=excluded.error,details_json=excluded.details_json,
                  started_at=COALESCE(reply_tasks.started_at,excluded.started_at),
                  completed_at=excluded.completed_at,updated_at=excluded.updated_at
                """,
                (
                    task["task_id"], task.get("trace_id", ""), task.get("thread_id", ""), task.get("group_id", ""),
                    task.get("group_name", ""), task.get("user_id", ""), task.get("sender_name", ""),
                    task.get("message_id", ""), task.get("question", ""), task.get("state", "queued"),
                    task.get("state_label", "排队等待"), task.get("score"), task.get("threshold"),
                    task.get("medium", ""), task.get("model", ""), task.get("result", ""), task.get("error", ""),
                    json.dumps(task.get("details") or {}, ensure_ascii=False), float(task.get("queued_at") or now),
                    task.get("started_at"), task.get("completed_at"), now,
                ),
            )

    def reply_tasks(self, group_id: str = "", limit: int = 100, active_only: bool = False) -> List[Dict[str, Any]]:
        where: List[str] = []
        params: List[Any] = []
        if group_id:
            where.append("group_id=?")
            params.append(group_id)
        if active_only:
            where.append("state NOT IN ('completed','skipped','failed','cancelled')")
        sql = "SELECT * FROM reply_tasks"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(max(1, min(500, int(limit))))
        with self.connect() as db:
            rows = [dict(r) for r in db.execute(sql, params).fetchall()]
        for row in rows:
            try:
                row["details"] = json.loads(row.pop("details_json") or "{}")
            except (ValueError, TypeError):
                row["details"] = {}
        return rows

    def culture_context(self, group_id: str, query: str, limit: int = 8) -> Dict[str, Any]:
        like = f"%{query.strip()}%" if query.strip() else "%"
        with self.connect() as db:
            aliases = [dict(r) for r in db.execute(
                """SELECT a.*,m.display_name FROM member_aliases a
                   LEFT JOIN members m ON m.group_id=a.group_id AND m.user_id=a.user_id
                   WHERE a.group_id=? AND (a.alias LIKE ? OR m.display_name LIKE ?)
                   ORDER BY a.confidence DESC,a.updated_at DESC LIMIT ?""",
                (group_id, like, like, limit),
            ).fetchall()]
            memes = [dict(r) for r in db.execute(
                """SELECT * FROM group_memes WHERE group_id=? AND
                   (name LIKE ? OR meaning LIKE ? OR triggers_json LIKE ?)
                   ORDER BY confidence DESC,updated_at DESC LIMIT ?""",
                (group_id, like, like, like, limit),
            ).fetchall()]
            relations = [dict(r) for r in db.execute(
                "SELECT * FROM member_relations WHERE group_id=? ORDER BY confidence DESC,updated_at DESC LIMIT ?",
                (group_id, limit),
            ).fetchall()]
        return {"aliases": aliases, "memes": memes, "relations": relations}

    def rebuild_indexes(self) -> Dict[str, Any]:
        with self.connect() as db:
            db.execute("DELETE FROM media_items")
            vector_count = 0
            media_count = 0
            for row in db.execute("SELECT * FROM messages ORDER BY created_at ASC").fetchall():
                text = str(row["text"] or row["raw_message"] or "").strip()
                if text:
                    content_hash = hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()
                    cur = db.execute(
                        "INSERT OR IGNORE INTO embedding_jobs(object_type,object_id,group_id,text,content_hash,status,updated_at) VALUES('message',?,?,?,?, 'pending',?)",
                        (row["event_id"], row["group_id"], text[:8000], content_hash, now_ts()),
                    )
                    vector_count += max(0, cur.rowcount)
                try:
                    medias = json.loads(row["media_json"] or "[]")
                except Exception:
                    medias = []
                for media_item in medias:
                    data = media_item.get("data") if isinstance(media_item, dict) and isinstance(media_item.get("data"), dict) else {}
                    file_value = str(data.get("file") or data.get("url") or "")
                    db.execute(
                        "INSERT INTO media_items(event_id,group_id,media_type,file,url,meta_json,status,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                        (row["event_id"], row["group_id"], str(media_item.get("type") or "media"), file_value, file_value if file_value.startswith(("http://", "https://", "file://")) else "", json.dumps(media_item, ensure_ascii=False), "indexed", now_ts()),
                    )
                    media_count += 1
            db.commit()
            return {"embedding_jobs": vector_count, "media": media_count, "stats": self.stats()}

    def rebuild_personas(self, group_id: str = "") -> Dict[str, Any]:
        result = self.queue_persona_analysis(group_id=group_id, mode="full")
        return {"rebuilt": 0, "queued": result["queued"], "jobs": result["jobs"], "items": self.personas(group_id, 200)}

    def queue_persona_analysis(self, group_id: str, user_id: str = "", mode: str = "full") -> Dict[str, Any]:
        if not group_id:
            raise ValueError("需要 group_id")
        ts = now_ts()
        with self.connect() as db:
            if user_id:
                members = db.execute("""SELECT m.user_id FROM members m WHERE m.group_id=? AND m.user_id=?
                                      AND EXISTS(SELECT 1 FROM messages msg WHERE msg.group_id=m.group_id AND msg.user_id=m.user_id AND msg.direction='incoming')""",
                                     (group_id, user_id)).fetchall()
            else:
                members = db.execute("""SELECT m.user_id FROM members m WHERE m.group_id=?
                                      AND EXISTS(SELECT 1 FROM messages msg WHERE msg.group_id=m.group_id AND msg.user_id=m.user_id AND msg.direction='incoming')
                                      ORDER BY m.message_count DESC""", (group_id,)).fetchall()
            jobs = []
            for row in members:
                uid = str(row["user_id"])
                active = db.execute("SELECT id FROM persona_analysis_jobs WHERE group_id=? AND user_id=? AND status IN ('queued','running','paused') ORDER BY id DESC LIMIT 1", (group_id, uid)).fetchone()
                if active:
                    jobs.append(int(active["id"]))
                    continue
                total = int(db.execute("SELECT COUNT(*) FROM messages WHERE group_id=? AND user_id=? AND direction='incoming'", (group_id, uid)).fetchone()[0])
                persona = db.execute("SELECT new_messages_since_analysis FROM personas WHERE group_id=? AND user_id=?", (group_id, uid)).fetchone()
                start_offset = max(0, total - int(persona["new_messages_since_analysis"] or 0)) if mode == "incremental" and persona else 0
                cur = db.execute(
                    "INSERT INTO persona_analysis_jobs(group_id,user_id,mode,status,total_messages,processed_messages,cursor_offset,updated_at) VALUES(?,?,?,'queued',?,?,?,?)",
                    (group_id, uid, mode if mode in {"full", "incremental"} else "full", total, start_offset, start_offset, ts),
                )
                job_id = int(cur.lastrowid)
                jobs.append(job_id)
                db.execute(
                    """INSERT INTO personas(user_id,group_id,analysis_status,analysis_progress,analysis_error,updated_at)
                       VALUES(?,?,'queued',0,'',?) ON CONFLICT(user_id,group_id) DO UPDATE SET
                       analysis_status='queued',analysis_progress=0,analysis_error='',updated_at=excluded.updated_at""",
                    (uid, group_id, ts),
                )
        return {"queued": len(jobs), "jobs": jobs}

    def queue_due_persona_analysis(self, six_hours_ago: str) -> Dict[str, Any]:
        """Queue low-priority incremental jobs after 30 messages or six hours."""
        # This also seeds legacy databases and newly observed members. Initial
        # profiles therefore require no manual visit or button click.
        self.sync_persona_directory()
        with self.connect() as db:
            due = [dict(r) for r in db.execute(
                """SELECT p.group_id,p.user_id,p.analysis_status FROM personas p
                   WHERE ((p.analysis_status='not_analyzed' AND p.new_messages_since_analysis>0)
                          OR (p.analysis_status IN ('completed','manual','legacy_auto')
                              AND (p.new_messages_since_analysis>=30 OR (p.last_analyzed_at!='' AND p.last_analyzed_at<=? AND p.new_messages_since_analysis>0))))
                     AND NOT EXISTS(SELECT 1 FROM persona_analysis_jobs j WHERE j.group_id=p.group_id AND j.user_id=p.user_id AND j.status IN ('queued','running','paused'))
                   ORDER BY CASE p.analysis_status WHEN 'not_analyzed' THEN 0 ELSE 1 END,p.new_messages_since_analysis DESC LIMIT 8""",
                (six_hours_ago,),
            ).fetchall()]
        queued = 0
        jobs: List[int] = []
        for row in due:
            mode = "full" if row.get("analysis_status") == "not_analyzed" else "incremental"
            result = self.queue_persona_analysis(row["group_id"], row["user_id"], mode)
            queued += int(result.get("queued") or 0)
            jobs.extend(result.get("jobs") or [])
        return {"queued": queued, "jobs": jobs}

    def persona_jobs(self, group_id: str = "", status: str = "", limit: int = 200) -> List[Dict[str, Any]]:
        where, params = [], []
        if group_id:
            where.append("j.group_id=?")
            params.append(group_id)
        if status:
            where.append("j.status=?")
            params.append(status)
        sql = """SELECT j.*,COALESCE(NULLIF(m.card,''),NULLIF(m.display_name,''),NULLIF(m.nickname,''),j.user_id) display_name
                 FROM persona_analysis_jobs j LEFT JOIN members m ON m.group_id=j.group_id AND m.user_id=j.user_id"""
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY CASE j.status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 WHEN 'paused' THEN 2 ELSE 3 END,j.id DESC LIMIT ?"
        with self.connect() as db:
            return [dict(r) for r in db.execute(sql, [*params, max(1, min(1000, int(limit or 200)))]).fetchall()]

    def persona_job_batch_payload(self, job_id: int, batch_size: int = 100) -> Dict[str, Any]:
        with self.connect() as db:
            job = db.execute("SELECT * FROM persona_analysis_jobs WHERE id=?", (int(job_id),)).fetchone()
            if not job:
                raise ValueError("画像任务不存在")
            rows = [dict(r) for r in db.execute(
                """SELECT event_id,message_id,event_time,created_at,text,raw_message FROM messages
                   WHERE group_id=? AND user_id=? AND direction='incoming'
                   ORDER BY event_time,created_at,event_id LIMIT ? OFFSET ?""",
                (job["group_id"], job["user_id"], max(1, min(100, int(batch_size))), int(job["cursor_offset"] or 0)),
            ).fetchall()]
        return {"job_id": int(job_id), "group_id": job["group_id"], "user_id": job["user_id"], "messages": [
            {"event_id": row["event_id"], "message_id": row["message_id"], "time": self._message_datetime(row).strftime("%Y-%m-%d %H:%M:%S"),
             "text": persona_message_text(row["text"] or row["raw_message"])} for row in rows
            if persona_message_text(row["text"] or row["raw_message"])
        ]}

    def add_persona_model_claims(self, group_id: str, user_id: str, claims: List[Dict[str, Any]],
                                 allowed_messages: List[Dict[str, Any]]) -> int:
        evidence_by_id: Dict[str, Dict[str, Any]] = {}
        for row in allowed_messages:
            normalized = {"event_id": str(row.get("event_id") or ""), "message_id": str(row.get("message_id") or ""),
                          "time": str(row.get("time") or ""), "text": str(row.get("text") or "")[:300]}
            if normalized["event_id"]:
                evidence_by_id[normalized["event_id"]] = normalized
            if normalized["message_id"]:
                evidence_by_id[normalized["message_id"]] = normalized
        valid_categories = {"fact", "interest", "habit", "style", "role", "topic", "quote"}
        inserted: List[tuple[int, str]] = []
        with self.connect() as db:
            for item in claims[:80]:
                if not isinstance(item, dict) or str(item.get("category")) not in valid_categories:
                    continue
                refs = item.get("evidence_ids") if isinstance(item.get("evidence_ids"), list) else []
                evidence = []
                seen = set()
                for ref in refs:
                    proof = evidence_by_id.get(str(ref))
                    if proof and proof["event_id"] not in seen:
                        evidence.append(proof)
                        seen.add(proof["event_id"])
                # Reject every unsupported model conclusion instead of displaying a hallucinated profile.
                if not evidence:
                    continue
                claim_id = self._upsert_persona_claim(db, group_id, user_id, str(item["category"]), str(item.get("value") or ""),
                                                      float(item.get("confidence") or 0), evidence)
                if claim_id:
                    inserted.append((claim_id, str(item.get("value") or "")))
        for claim_id, value in inserted:
            self.enqueue_embedding("persona_claim", str(claim_id), group_id, value)
        return len(inserted)

    def set_persona_job_status(self, job_id: int, status: str) -> Dict[str, Any]:
        if status not in {"queued", "paused", "cancelled"}:
            raise ValueError("不支持的任务状态")
        with self.connect() as db:
            job = db.execute("SELECT * FROM persona_analysis_jobs WHERE id=?", (int(job_id),)).fetchone()
            if not job:
                raise ValueError("画像任务不存在")
            db.execute("UPDATE persona_analysis_jobs SET status=?,error='',updated_at=? WHERE id=?", (status, now_ts(), int(job_id)))
            db.execute("UPDATE personas SET analysis_status=?,analysis_error='',updated_at=? WHERE group_id=? AND user_id=?", (status, now_ts(), job["group_id"], job["user_id"]))
        return self.persona_jobs(limit=1000)[0] if False else {"id": int(job_id), "status": status}

    def _upsert_persona_claim(self, db: sqlite3.Connection, group_id: str, user_id: str, category: str,
                              value: str, confidence: float, evidence: List[Dict[str, Any]]) -> int:
        clean = str(value or "").strip()[:500]
        if not clean or not evidence:
            return 0
        ts = now_ts()
        db.execute(
            """INSERT INTO persona_claims(group_id,user_id,category,value,confidence,source,evidence_json,updated_at)
               VALUES(?,?,?,?,?,'auto',?,?) ON CONFLICT(group_id,user_id,category,value,source) DO UPDATE SET
               confidence=MAX(persona_claims.confidence,excluded.confidence),evidence_json=excluded.evidence_json,updated_at=excluded.updated_at""",
            (group_id, user_id, category, clean, max(0, min(1, float(confidence))), json.dumps(evidence[:8], ensure_ascii=False), ts),
        )
        row = db.execute("SELECT id FROM persona_claims WHERE group_id=? AND user_id=? AND category=? AND value=? AND source='auto'", (group_id, user_id, category, clean)).fetchone()
        return int(row["id"]) if row else 0

    def process_persona_job_batch(self, job_id: int, batch_size: int = 100) -> Dict[str, Any]:
        batch_size = max(1, min(100, int(batch_size or 100)))
        embedding_claims: List[tuple[int, str, str]] = []
        with self.connect() as db:
            job_row = db.execute("SELECT * FROM persona_analysis_jobs WHERE id=?", (int(job_id),)).fetchone()
            if not job_row:
                raise ValueError("画像任务不存在")
            job = dict(job_row)
            if job["status"] in {"paused", "cancelled", "completed"}:
                return job
            if job["status"] == "queued":
                db.execute("UPDATE persona_analysis_jobs SET status='running',started_at=CASE WHEN started_at='' THEN ? ELSE started_at END,updated_at=? WHERE id=?", (now_ts(), now_ts(), job_id))
            gid, uid, offset = str(job["group_id"]), str(job["user_id"]), int(job["cursor_offset"] or 0)
            if offset == 0 and job["mode"] == "full":
                db.execute("DELETE FROM persona_claims WHERE group_id=? AND user_id=? AND source='auto'", (gid, uid))
            rows = [dict(r) for r in db.execute(
                """SELECT event_id,message_id,event_time,created_at,text,raw_message,segments_json,media_json
                   FROM messages WHERE group_id=? AND user_id=? AND direction='incoming'
                   ORDER BY event_time,created_at,event_id LIMIT ? OFFSET ?""",
                (gid, uid, batch_size, offset),
            ).fetchall()]
            topic_evidence: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
            topic_count: Counter[str] = Counter()
            for row in rows:
                body = persona_message_text(row.get("text") or row.get("raw_message"))
                if not body:
                    continue
                dt = self._message_datetime(row).strftime("%Y-%m-%d %H:%M:%S")
                evidence = {"event_id": row["event_id"], "message_id": row.get("message_id") or "", "time": dt, "text": body[:300]}
                for pattern, label in ((r"(?:我叫|叫我)([^，。！？\s]{1,12})", "fact"), (r"我(?:最)?喜欢([^，。！？]{1,30})", "interest"), (r"我是([^，。！？]{1,30})", "fact")):
                    match = re.search(pattern, body)
                    if match:
                        value = match.group(1).strip()
                        claim_id = self._upsert_persona_claim(db, gid, uid, label, value, .88, [evidence])
                        if claim_id:
                            embedding_claims.append((claim_id, gid, value))
                if 6 <= len(body) <= 100 and not body.startswith(("http://", "https://")):
                    score = .78 if any(mark in body for mark in ("哈哈", "！", "？", "我", "绝对", "真的")) else .68
                    if score >= .75:
                        claim_id = self._upsert_persona_claim(db, gid, uid, "quote", body, score, [evidence])
                        if claim_id:
                            embedding_claims.append((claim_id, gid, body))
                for token in text_tokens(body):
                    if len(token) < 2 or token.isdigit() or token in {"这个", "那个", "什么", "怎么", "就是", "可以", "不是", "然后", "没有", "现在", "已经"}:
                        continue
                    topic_count[token] += 1
                    if len(topic_evidence[token]) < 3:
                        topic_evidence[token].append(evidence)
            for token, count in topic_count.most_common(6):
                if count < 2:
                    continue
                claim_id = self._upsert_persona_claim(db, gid, uid, "topic", token, min(.9, .55 + count * .04), topic_evidence[token])
                if claim_id:
                    embedding_claims.append((claim_id, gid, token))
            processed = offset + len(rows)
            total = max(int(job["total_messages"] or 0), processed)
            done = len(rows) < batch_size or processed >= total
            progress = 1.0 if done else (processed / total if total else 1.0)
            status = "completed" if done else "running"
            db.execute("UPDATE persona_analysis_jobs SET status=?,processed_messages=?,cursor_offset=?,completed_at=?,updated_at=? WHERE id=?", (status, processed, processed, now_ts() if done else "", now_ts(), job_id))
            db.execute("UPDATE personas SET analysis_status=?,analysis_progress=?,analysis_cursor=?,analysis_error='',updated_at=? WHERE group_id=? AND user_id=?", (status, progress, processed, now_ts(), gid, uid))
        for claim_id, gid, value in embedding_claims:
            self.enqueue_embedding("persona_claim", str(claim_id), gid, value)
        if done:
            self.finalize_persona_analysis(gid, uid)
        jobs = self.persona_jobs(group_id=gid, limit=1000)
        return next((x for x in jobs if int(x["id"]) == int(job_id)), {"id": job_id, "status": status})

    def finalize_persona_analysis(self, group_id: str, user_id: str) -> Dict[str, Any]:
        metrics = self.persona_metrics(group_id, user_id)
        with self.connect() as db:
            member = db.execute("SELECT * FROM members WHERE group_id=? AND user_id=?", (group_id, user_id)).fetchone()
            claims = [dict(r) for r in db.execute("SELECT * FROM persona_claims WHERE group_id=? AND user_id=? AND source='auto' ORDER BY confidence DESC,id DESC", (group_id, user_id)).fetchall()]
            name = effective_member_name(member, user_id) if member else effective_member_name({}, user_id)
            topics = [x["value"] for x in claims if x["category"] in {"topic", "interest"}][:8]
            summary = f"{name} 在本群共有 {metrics['message_count']} 条历史消息"
            if topics:
                summary += "，常聊 " + "、".join(topics[:6])
            if metrics["interactions"]:
                summary += "；常与 " + "、".join(x["name"] for x in metrics["interactions"][:3]) + " 互动"
            summary += "。"
            tags = list(dict.fromkeys(topics))[:12]
            facts = [x["value"] for x in claims if x["category"] == "fact"][:20]
            ts = now_ts()
            db.execute(
                """INSERT INTO personas(user_id,group_id,summary,tags_json,facts_json,auto_summary,structured_json,metrics_json,analysis_status,analysis_progress,analysis_cursor,analysis_error,analysis_version,last_analyzed_at,new_messages_since_analysis,updated_at)
                   VALUES(?,?,?,?,?,?,?,?, 'completed',1,?, '',2,?,0,?) ON CONFLICT(user_id,group_id) DO UPDATE SET
                   summary=excluded.summary,tags_json=excluded.tags_json,facts_json=excluded.facts_json,auto_summary=excluded.auto_summary,
                   structured_json=excluded.structured_json,metrics_json=excluded.metrics_json,analysis_status='completed',analysis_progress=1,
                   analysis_cursor=excluded.analysis_cursor,analysis_error='',analysis_version=2,last_analyzed_at=excluded.last_analyzed_at,
                   new_messages_since_analysis=0,updated_at=excluded.updated_at""",
                (user_id, group_id, summary, json.dumps(tags, ensure_ascii=False), json.dumps(facts, ensure_ascii=False), summary,
                 json.dumps({"topics": topics}, ensure_ascii=False), json.dumps(metrics, ensure_ascii=False), metrics["message_count"], ts, ts),
            )
        self.enqueue_embedding("persona", f"{group_id}:{user_id}", group_id, " ".join([summary, *tags, *facts]))
        return self.persona_detail(group_id, user_id)

    def effective_persona_context(self, group_id: str, user_id: str, limit: int = 12) -> Dict[str, Any]:
        # Reply generation needs only compact effective fields. Do not call
        # persona_detail() here because that intentionally recomputes all-history
        # visual metrics and would add avoidable latency to every group reply.
        with self.connect() as db:
            row = db.execute(
                """SELECT m.display_name,m.nickname,m.card,p.* FROM members m
                   LEFT JOIN personas p ON p.group_id=m.group_id AND p.user_id=m.user_id
                   WHERE m.group_id=? AND m.user_id=?""", (group_id, user_id),
            ).fetchone()
            if not row:
                return {}
            aliases = [str(x[0]) for x in db.execute(
                "SELECT alias FROM member_aliases WHERE group_id=? AND user_id=? ORDER BY confidence DESC,id LIMIT 12",
                (group_id, user_id),
            ).fetchall()]
            claim_rows = [dict(x) for x in db.execute(
                """SELECT category,value,confidence,source,evidence_json FROM persona_claims
                   WHERE group_id=? AND user_id=? ORDER BY CASE source WHEN 'manual' THEN 0 ELSE 1 END,priority DESC,confidence DESC,id DESC LIMIT ?""",
                (group_id, user_id, max(1, min(30, int(limit or 12)))),
            ).fetchall()]
        p = dict(row)
        claims = []
        for claim in claim_rows:
            claim["evidence"] = self._json_value(claim.pop("evidence_json", "[]"), [])
            claims.append(claim)
        manual_tags = self._json_value(p.get("manual_tags_json"), [])
        auto_tags = self._json_value(p.get("tags_json"), [])
        manual_facts = self._json_value(p.get("manual_facts_json"), [])
        auto_facts = self._json_value(p.get("facts_json"), [])
        return {
            "name": effective_member_name(p, user_id),
            "summary": p.get("manual_summary") or p.get("auto_summary") or p.get("summary") or "",
            "aliases": aliases,
            "tags": list(dict.fromkeys([*manual_tags, *auto_tags]))[:12],
            "facts": [*manual_facts, *[x for x in auto_facts if x not in manual_facts]][:12],
            "claims": claims,
        }

    def export_json(self, group_id: str = "", limit: int = 5000) -> Dict[str, Any]:
        return {
            "exported_at": now_ts(),
            "stats": self.stats(),
            "messages": self.search_messages(group_id=group_id, limit=limit),
            "members": self.members(group_id=group_id, limit=1000),
            "personas": self.personas(group_id=group_id, limit=1000),
            "media": self.media(group_id=group_id, limit=1000),
        }

    def clear_group_memory(self, group_id: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM group_memory WHERE group_id=?", (group_id,))
            db.execute("DELETE FROM messages WHERE group_id=?", (group_id,))
            db.execute("DELETE FROM members WHERE group_id=?", (group_id,))
            db.execute("DELETE FROM personas WHERE group_id=?", (group_id,))
            db.execute("DELETE FROM persona_claims WHERE group_id=?", (group_id,))
            db.execute("DELETE FROM persona_analysis_jobs WHERE group_id=?", (group_id,))
            db.execute("DELETE FROM media_items WHERE group_id=?", (group_id,))
            db.execute("DELETE FROM semantic_embeddings WHERE group_id=?", (group_id,))
            db.execute("DELETE FROM embedding_jobs WHERE group_id=?", (group_id,))
            db.execute("DELETE FROM member_aliases WHERE group_id=?", (group_id,))
            db.execute("DELETE FROM member_relations WHERE group_id=?", (group_id,))
            db.execute("DELETE FROM group_memes WHERE group_id=?", (group_id,))
            db.execute("DELETE FROM reply_tasks WHERE group_id=?", (group_id,))
