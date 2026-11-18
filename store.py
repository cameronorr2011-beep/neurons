"""SQLite persistence for Neurons.

Everything personal lives in one local database: the user's profile, durable
memory facts and saved conversations. The browser never owns this state, so it
survives restarts, cleared local storage and multiple browser tabs.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("NEURONS_DB") or (APP_ROOT / "data" / "neurons.db"))
STYLES = ("concise", "balanced", "detailed", "direct")
THEMES = ("graphite", "paper", "terminal")

_init_lock = threading.Lock()
_initialized = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH), timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    return con


def init_db(path: str | Path | None = None) -> None:
    global DB_PATH, _initialized
    if path:
        DB_PATH = Path(path)
    with _init_lock:
        with _connect() as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS profile (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    name TEXT NOT NULL DEFAULT '',
                    about TEXT NOT NULL DEFAULT '',
                    style TEXT NOT NULL DEFAULT 'balanced',
                    theme TEXT NOT NULL DEFAULT 'graphite',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'user',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL DEFAULT 'New chat',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    meta TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_messages_conversation
                    ON messages(conversation_id, id);
                """
            )
            con.execute(
                "INSERT OR IGNORE INTO profile (id, name, about, style, theme, updated_at) "
                "VALUES (1, '', '', 'balanced', 'graphite', ?)",
                (_now(),),
            )
        _initialized = True


def _ready() -> None:
    if not _initialized:
        init_db()


def get_profile() -> dict:
    _ready()
    with _connect() as con:
        row = con.execute("SELECT name, about, style, theme, updated_at FROM profile WHERE id = 1").fetchone()
    return dict(row) if row else {
        "name": "", "about": "", "style": "balanced", "theme": "graphite", "updated_at": ""
    }


def update_profile(fields: dict) -> dict:
    _ready()
    allowed: dict = {}
    for key in ("name", "about", "style", "theme"):
        if key in fields and fields[key] is not None:
            allowed[key] = str(fields[key]).strip()[:4000]
    if "style" in allowed and allowed["style"] not in STYLES:
        allowed["style"] = "balanced"
    if "theme" in allowed and allowed["theme"] not in THEMES:
        allowed["theme"] = "graphite"
    if allowed:
        allowed["updated_at"] = _now()
        assignments = ", ".join(f"{key} = ?" for key in allowed)
        with _connect() as con:
            con.execute(f"UPDATE profile SET {assignments} WHERE id = 1", tuple(allowed.values()))
    return get_profile()


def list_memories() -> list[dict]:
    _ready()
    with _connect() as con:
        rows = con.execute(
            "SELECT id, text, source, created_at FROM memories ORDER BY id DESC"
        ).fetchall()
    return [dict(row) for row in rows]


def add_memory(text: str, source: str = "user") -> dict:
    _ready()
    text = str(text or "").strip()
    if not text:
        raise ValueError("memory text is required")
    text = text[:4000]
    with _connect() as con:
        existing = con.execute(
            "SELECT id, text, source, created_at FROM memories WHERE lower(text) = lower(?) "
            "ORDER BY id LIMIT 1", (text,)
        ).fetchone()
        if existing:
            return dict(existing)
        cur = con.execute(
            "INSERT INTO memories (text, source, created_at) VALUES (?, ?, ?)",
            (text, str(source or "user")[:30], _now()),
        )
        row = con.execute(
            "SELECT id, text, source, created_at FROM memories WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
    return dict(row)


def delete_memory(memory_id: int) -> bool:
    _ready()
    with _connect() as con:
        cur = con.execute("DELETE FROM memories WHERE id = ?", (int(memory_id),))
    return cur.rowcount > 0


def _conversation_row(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "title": row["title"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def list_conversations(limit: int = 50) -> list[dict]:
    _ready()
    with _connect() as con:
        rows = con.execute(
            """
            SELECT c.id, c.title, c.created_at, c.updated_at,
                   (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS message_count,
                   (SELECT content FROM messages m WHERE m.conversation_id = c.id
                    ORDER BY m.id DESC LIMIT 1) AS last_message
            FROM conversations c
            ORDER BY c.updated_at DESC, c.rowid DESC
            LIMIT ?
            """,
            (max(1, min(int(limit), 200)),),
        ).fetchall()
    result = []
    for row in rows:
        item = _conversation_row(row)
        item["message_count"] = row["message_count"]
        item["last_message"] = (row["last_message"] or "")[:160]
        result.append(item)
    return result


def create_conversation(title: str = "New chat") -> dict:
    _ready()
    cid = uuid.uuid4().hex[:12]
    title = str(title or "New chat").strip()[:100] or "New chat"
    now = _now()
    with _connect() as con:
        con.execute(
            "INSERT INTO conversations (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (cid, title, now, now),
        )
    return {"id": cid, "title": title, "created_at": now, "updated_at": now,
            "message_count": 0, "last_message": ""}


def get_conversation(conversation_id: str) -> dict | None:
    _ready()
    with _connect() as con:
        row = con.execute(
            "SELECT id, title, created_at, updated_at FROM conversations WHERE id = ?",
            (str(conversation_id),),
        ).fetchone()
        if not row:
            return None
        msgs = con.execute(
            "SELECT id, role, content, meta, created_at FROM messages "
            "WHERE conversation_id = ? ORDER BY id",
            (str(conversation_id),),
        ).fetchall()
    result = _conversation_row(row)
    result["messages"] = []
    for msg in msgs:
        item = dict(msg)
        try:
            item["meta"] = json.loads(item.get("meta") or "{}")
        except Exception:
            item["meta"] = {}
        result["messages"].append(item)
    result["message_count"] = len(result["messages"])
    return result


def append_message(conversation_id: str, role: str, content: str, meta: dict | None = None) -> dict:
    _ready()
    if role not in ("user", "assistant"):
        raise ValueError("role must be user or assistant")
    content = str(content or "").strip()
    if not content:
        raise ValueError("message content is required")
    content = content[:100000]
    conversation_id = str(conversation_id or "")
    with _connect() as con:
        row = con.execute(
            "SELECT id, title FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
        if not row:
            raise ValueError("conversation not found")
        now = _now()
        cur = con.execute(
            "INSERT INTO messages (conversation_id, role, content, meta, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (conversation_id, role, content, json.dumps(meta or {}), now),
        )
        title = row["title"]
        if role == "user" and title == "New chat":
            title = content.replace("\n", " ").strip()[:70] or "New chat"
        con.execute(
            "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
            (title, now, conversation_id),
        )
        msg = con.execute(
            "SELECT id, role, content, meta, created_at FROM messages WHERE id = ?",
            (cur.lastrowid,),
        ).fetchone()
    item = dict(msg)
    try:
        item["meta"] = json.loads(item.get("meta") or "{}")
    except Exception:
        item["meta"] = {}
    return item


def delete_conversation(conversation_id: str) -> bool:
    _ready()
    with _connect() as con:
        cur = con.execute("DELETE FROM conversations WHERE id = ?", (str(conversation_id),))
    return cur.rowcount > 0


def get_state(conversation_id: str | None = None) -> dict:
    conversations = list_conversations()
    active = None
    if conversation_id:
        active = get_conversation(conversation_id)
    if active is None and conversations:
        active = get_conversation(conversations[0]["id"])
    return {
        "profile": get_profile(),
        "memories": list_memories(),
        "conversations": conversations,
        "conversation": active,
    }
