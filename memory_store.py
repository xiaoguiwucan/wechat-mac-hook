#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Local long-term memory store for the isolated WeChat2 AI assistant.

This module only stores data observed by the current isolated OneBot/AI pipeline
or imported by the user. It does not read or decrypt WeChat's private databases.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

DEFAULT_HOME = Path.home() / "Library" / "Application Support" / "WeChatSecond"
DEFAULT_DB = DEFAULT_HOME / "memory" / "wechat-memory.sqlite3"

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


def voice_search_text(value: Any) -> str:
    """Normalize user descriptions and filenames without losing Chinese phrase boundaries."""
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(value or "").lower())


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


def sparse_vector(text: str, dims: int = 128) -> Dict[str, float]:
    vec: Dict[str, float] = {}
    for token in text_tokens(text):
        bucket = str(abs(hash(token)) % dims)
        vec[bucket] = vec.get(bucket, 0.0) + 1.0
    norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
    return {k: round(v / norm, 6) for k, v in vec.items()}


def cosine_sparse(a: Dict[str, float], b: Dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b.get(k, 0.0) for k, v in a.items())


class MemoryStore:
    def __init__(self, path: Path | str = DEFAULT_DB):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

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
                CREATE TABLE IF NOT EXISTS memory_vectors (
                  event_id TEXT PRIMARY KEY,
                  group_id TEXT NOT NULL,
                  text TEXT NOT NULL DEFAULT '',
                  vector_json TEXT NOT NULL DEFAULT '{}',
                  dims INTEGER NOT NULL DEFAULT 128,
                  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_vectors_group ON memory_vectors(group_id, updated_at DESC);
                """
            )
            try:
                db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(event_id UNINDEXED, group_id UNINDEXED, sender_name, text, raw_message)")
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
                        "INSERT OR REPLACE INTO memory_vectors(event_id,group_id,text,vector_json,dims,updated_at) VALUES(?,?,?,?,?,?)",
                        (event_id, group_id, text_for_vector[:2000], json.dumps(sparse_vector(text_for_vector), ensure_ascii=False), 128, now_ts()),
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
                    db.execute(
                        "INSERT INTO media_items(event_id,group_id,media_type,file,url,meta_json,status,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                        (event_id, group_id, media_type, file_value, file_value if file_value.startswith(("http://", "https://", "file://")) else "", json.dumps(media_item, ensure_ascii=False), "indexed", now_ts()),
                    )
            return inserted

    def search_messages(self, query: str = "", group_id: str = "", user_id: str = "", limit: int = 50) -> List[Dict[str, Any]]:
        limit = max(1, min(300, int(limit or 50)))
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
                "vectors": one("SELECT COUNT(*) FROM memory_vectors"),
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
        qvec = sparse_vector(query)
        if not qvec:
            return []
        sql = "SELECT v.event_id,v.group_id,v.text,v.vector_json,m.sender_name,m.user_id,m.created_at,m.direction FROM memory_vectors v LEFT JOIN messages m ON m.event_id=v.event_id"
        params: List[Any] = []
        if group_id:
            sql += " WHERE v.group_id=?"
            params.append(group_id)
        sql += " ORDER BY v.updated_at DESC LIMIT 2000"
        scored: List[Dict[str, Any]] = []
        with self.connect() as db:
            for row in db.execute(sql, params).fetchall():
                try:
                    vec = json.loads(row["vector_json"])
                except Exception:
                    vec = {}
                score = cosine_sparse(qvec, {str(k): float(v) for k, v in vec.items()})
                if score > 0:
                    item = dict(row)
                    item.pop("vector_json", None)
                    item["score"] = round(score, 4)
                    scored.append(item)
        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:max(1, min(100, int(limit or 20)))]

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
                    0 if str(row.get("ocr_text") or "").strip() else 1,
                    0 if str(row.get("status") or "") == "transcribed" else 1,
                    -int(row.get("id") or 0),
                ))
                keep = rows[0]
                # Merge a useful annotation before removing the stale copies.
                for candidate in rows[1:]:
                    if not str(keep.get("ocr_text") or "").strip() and str(candidate.get("ocr_text") or "").strip():
                        keep = candidate
                if str(keep.get("ocr_text") or "").strip() and str(keep.get("status") or "") != "transcribed":
                    db.execute("UPDATE media_items SET status='transcribed', error='', updated_at=? WHERE id=?", (now_ts(), int(keep["id"])))
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

    def save_media_annotation(self, media_id: int, ocr_text: str = "", image_summary: str = "", status: str = "annotated",
                              tags: Optional[List[str]] = None, keywords: Optional[List[str]] = None, error: str = "") -> Dict[str, Any]:
        tags = tags or []
        keywords = keywords or []
        with self.connect() as db:
            db.execute(
                "UPDATE media_items SET ocr_text=?, image_summary=?, tags_json=?, keywords_json=?, error=?, status=?, updated_at=? WHERE id=?",
                (ocr_text, image_summary, json.dumps(tags, ensure_ascii=False), json.dumps(keywords, ensure_ascii=False), error, status, now_ts(), int(media_id)),
            )
            return self.media_detail(int(media_id))

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
               COALESCE(p.summary,'') AS summary, COALESCE(p.tags_json,'[]') AS tags_json, COALESCE(p.facts_json,'[]') AS facts_json,
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
        with self.connect() as db:
            db.execute(
                "INSERT INTO personas(user_id,group_id,summary,tags_json,facts_json,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(user_id,group_id) DO UPDATE SET summary=excluded.summary,tags_json=excluded.tags_json,facts_json=excluded.facts_json,updated_at=excluded.updated_at",
                (user_id, group_id, summary, json.dumps(tags or [], ensure_ascii=False), json.dumps(facts or [], ensure_ascii=False), ts),
            )
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
        with self.connect() as db:
            cur = db.execute(
                """
                INSERT OR IGNORE INTO voice_items(pack_id,category,title,text,file,file_ext,size,duration_ms,tags_json,status,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (int(pack_id or 0), category, title, text, file, file_ext, int(size or 0), int(duration_ms or 0), json.dumps(tags, ensure_ascii=False), "ready", ts),
            )
            inserted = cur.rowcount > 0
            db.execute("UPDATE voice_packs SET item_count=(SELECT COUNT(*) FROM voice_items WHERE pack_id=?), updated_at=? WHERE id=?", (pack_id, ts, pack_id))
            return inserted

    def voice_packs(self) -> List[Dict[str, Any]]:
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM voice_packs ORDER BY updated_at DESC,id DESC").fetchall()]

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
            where.append("(vi.title LIKE ? OR vi.text LIKE ? OR vi.tags_json LIKE ? OR vi.category LIKE ? OR vp.name LIKE ?)")
            params.extend([like, like, like, like, like])
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

    def mark_voice_used(self, item_id: int) -> None:
        with self.connect() as db:
            db.execute("UPDATE voice_items SET usage_count=usage_count+1,last_used_at=?,updated_at=? WHERE id=?", (now_ts(), now_ts(), int(item_id)))

    def rebuild_indexes(self) -> Dict[str, Any]:
        with self.connect() as db:
            db.execute("DELETE FROM memory_vectors")
            db.execute("DELETE FROM media_items")
            vector_count = 0
            media_count = 0
            for row in db.execute("SELECT * FROM messages ORDER BY created_at ASC").fetchall():
                text = str(row["text"] or row["raw_message"] or "").strip()
                if text:
                    db.execute(
                        "INSERT OR REPLACE INTO memory_vectors(event_id,group_id,text,vector_json,dims,updated_at) VALUES(?,?,?,?,?,?)",
                        (row["event_id"], row["group_id"], text[:2000], json.dumps(sparse_vector(text), ensure_ascii=False), 128, now_ts()),
                    )
                    vector_count += 1
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
            return {"vectors": vector_count, "media": media_count, "stats": self.stats()}

    def rebuild_personas(self, group_id: str = "") -> Dict[str, Any]:
        with self.connect() as db:
            members = self.members(group_id, 1000)
            rebuilt = 0
            for member in members:
                uid, gid = member["user_id"], member["group_id"]
                rows = db.execute(
                    "SELECT text,raw_message,created_at FROM messages WHERE group_id=? AND user_id=? ORDER BY event_time DESC,created_at DESC LIMIT 80",
                    (gid, uid),
                ).fetchall()
                texts = [str(r["text"] or r["raw_message"] or "").strip() for r in rows if str(r["text"] or r["raw_message"] or "").strip()]
                tokens = text_tokens(" ".join(texts[:40]))
                freq: Dict[str, int] = {}
                for t in tokens:
                    if len(t) >= 2 or re.match(r"[\u4e00-\u9fff]", t):
                        freq[t] = freq.get(t, 0) + 1
                tags = [k for k, _ in sorted(freq.items(), key=lambda x: x[1], reverse=True)[:12]]
                sample = " / ".join(texts[:3])[:260]
                name = member.get("display_name") or member.get("nickname") or uid
                summary = f"{name} 在本群累计 {member.get('message_count', 0)} 条消息。最近发言摘要：{sample}" if sample else f"{name} 在本群累计 {member.get('message_count', 0)} 条消息。"
                db.execute(
                    "INSERT INTO personas(user_id,group_id,summary,tags_json,facts_json,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(user_id,group_id) DO UPDATE SET summary=excluded.summary,tags_json=excluded.tags_json,updated_at=excluded.updated_at",
                    (uid, gid, summary, json.dumps(tags, ensure_ascii=False), "[]", now_ts()),
                )
                rebuilt += 1
            db.commit()
            return {"rebuilt": rebuilt, "items": self.personas(group_id, 200)}

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
            db.execute("DELETE FROM media_items WHERE group_id=?", (group_id,))
            db.execute("DELETE FROM memory_vectors WHERE group_id=?", (group_id,))
