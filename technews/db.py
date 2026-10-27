"""SQLite storage: the stories the scanner found, the posts made from them, and a small
key/value table with expiry (posting pauses and the last-post time)."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from technews.config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS stories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    source TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    outlets TEXT NOT NULL DEFAULT '[]',
    titles TEXT NOT NULL DEFAULT '[]',
    published_at TEXT NOT NULL,
    found_at TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'other',
    scan_score REAL NOT NULL DEFAULT 0,
    scan_reason TEXT NOT NULL DEFAULT '',
    review_score REAL,
    review_reason TEXT NOT NULL DEFAULT '',
    angle TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'new'  -- new | not_tech | low | rejected | approved | posted | failed
);
CREATE INDEX IF NOT EXISTS stories_status ON stories(status, found_at);
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id INTEGER NOT NULL REFERENCES stories(id),
    headline TEXT NOT NULL,
    caption TEXT NOT NULL,
    image_path TEXT NOT NULL,
    writer TEXT NOT NULL,
    delivered TEXT NOT NULL,
    media_id TEXT NOT NULL DEFAULT '',
    permalink TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,  -- posted | failed
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    expires REAL  -- unix time; NULL never
);
"""


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def init() -> None:
    settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(settings.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def rows(sql: str, *params: Any) -> list[dict[str, Any]]:
    with connect() as conn:
        out = [dict(r) for r in conn.execute(sql, params)]
    for row in out:
        for col in ("outlets", "titles"):
            if col in row:
                row[col] = json.loads(row[col] or "[]")
    return out


def execute(sql: str, *params: Any) -> int:
    with connect() as conn:
        return conn.execute(sql, params).lastrowid or 0


def story(story_id: int) -> dict[str, Any] | None:
    found = rows("SELECT * FROM stories WHERE id = ?", story_id)
    return found[0] if found else None


def update_story(story_id: int, **fields: Any) -> None:
    cols = ", ".join(f"{k} = ?" for k in fields)
    execute(f"UPDATE stories SET {cols} WHERE id = ?", *fields.values(), story_id)  # noqa: S608 - our own column names


# --------------------------------------------------------------------------- kv


def kv_set(key: str, value: str, *, ttl: float | None = None) -> None:
    expires = time.time() + ttl if ttl else None
    execute("INSERT OR REPLACE INTO kv (key, value, expires) VALUES (?, ?, ?)", key, value, expires)


def kv_get(key: str) -> str | None:
    found = rows("SELECT value, expires FROM kv WHERE key = ?", key)
    if not found or (found[0]["expires"] and found[0]["expires"] < time.time()):
        return None
    return found[0]["value"]


def kv_ttl(key: str) -> float:
    """Seconds left on ``key`` (0 when unset or expired)."""
    found = rows("SELECT expires FROM kv WHERE key = ?", key)
    if not found or not found[0]["expires"]:
        return 0.0
    return max(0.0, found[0]["expires"] - time.time())
