"""Storage: the stories the scanner found, the posts made from them, and a small key/value table with expiry.

SQLite (a file in the data folder) is the default. Set ``DATABASE_URL=postgresql://...`` to use PostgreSQL instead;
the SQL below is written once and translated for PostgreSQL on the way in."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
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
    story_id INTEGER NOT NULL,  -- 0 for a marketing flyer
    headline TEXT NOT NULL,
    caption TEXT NOT NULL,
    image_path TEXT NOT NULL,
    writer TEXT NOT NULL,
    delivered TEXT NOT NULL,
    media_id TEXT NOT NULL DEFAULT '',
    permalink TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,  -- posted | failed
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    flyer TEXT NOT NULL DEFAULT '',  -- a marketing flyer's id (story_id 0); empty for news
    linkedin TEXT NOT NULL DEFAULT '',  -- the LinkedIn post link, 'dry_run', or 'failed: why'; empty if not sent
    meta TEXT NOT NULL DEFAULT '',  -- JSON of what the draft was built from, so the editor can reopen it
    brand_id INTEGER NOT NULL DEFAULT 1,
    scheduled_at TEXT NOT NULL DEFAULT ''  -- when a scheduled post goes out (UTC)
);
CREATE TABLE IF NOT EXISTS flyers (
    id TEXT PRIMARY KEY,
    kicker TEXT NOT NULL,
    headline TEXT NOT NULL,
    points TEXT NOT NULL,  -- JSON list of lines
    cta TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    caption TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    position INTEGER NOT NULL DEFAULT 0,
    layout TEXT NOT NULL DEFAULT 'classic',  -- one of flyers.LAYOUTS
    style TEXT NOT NULL DEFAULT '',  -- JSON colour overrides: bg, bg2, text, accent
    brand_id INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS brands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    handle TEXT NOT NULL DEFAULT '',  -- shown on the cards
    ig_username TEXT NOT NULL DEFAULT '',
    ig_password TEXT NOT NULL DEFAULT '',  -- sealed when SECRETS_KEY is set
    hashtags TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    site TEXT NOT NULL DEFAULT '',
    categories TEXT NOT NULL DEFAULT '',  -- news tags this brand posts, comma separated; empty means all
    posts_per_day INTEGER NOT NULL DEFAULT 0,  -- 0 uses the general limit
    enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,  -- feed | search
    name TEXT NOT NULL DEFAULT '',
    value TEXT NOT NULL,  -- the feed URL, or the news search
    enabled INTEGER NOT NULL DEFAULT 1,
    UNIQUE (kind, value)
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'viewer',  -- admin | approver | viewer
    active INTEGER NOT NULL DEFAULT 1,
    ver TEXT NOT NULL DEFAULT '0',  -- changes with the password, which signs that person out everywhere
    totp TEXT NOT NULL DEFAULT '',  -- the authenticator secret once 2-step sign-in is on
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS users_name ON users (lower(username));
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    "user" TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    ip TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id INTEGER NOT NULL,
    "user" TEXT NOT NULL,
    text TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent TEXT NOT NULL,
    brand_id INTEGER NOT NULL DEFAULT 1,
    at TEXT NOT NULL,
    ok INTEGER NOT NULL DEFAULT 1,
    summary TEXT NOT NULL DEFAULT '',
    ms INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    brand_id INTEGER NOT NULL DEFAULT 1,
    post_id INTEGER NOT NULL DEFAULT 0,
    kind TEXT NOT NULL,  -- approved | rejected | edited | layout_changed | rewritten | scheduled
    detail TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS playbooks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent TEXT NOT NULL,
    brand_id INTEGER NOT NULL DEFAULT 1,
    text TEXT NOT NULL,
    at TEXT NOT NULL,
    by TEXT NOT NULL DEFAULT 'you'
);
CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent TEXT NOT NULL,
    brand_id INTEGER NOT NULL DEFAULT 1,
    text TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'  -- pending | applied | dismissed
);
CREATE TABLE IF NOT EXISTS metrics (
    post_id INTEGER NOT NULL,
    likes INTEGER NOT NULL DEFAULT 0,
    comments INTEGER NOT NULL DEFAULT 0,
    at TEXT NOT NULL,
    plays INTEGER NOT NULL DEFAULT 0,
    saves INTEGER NOT NULL DEFAULT 0,
    shares INTEGER NOT NULL DEFAULT 0,
    reach INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS followers (
    brand_id INTEGER NOT NULL,
    day TEXT NOT NULL,  -- YYYY-MM-DD in the page's time zone
    count INTEGER NOT NULL,
    following INTEGER NOT NULL DEFAULT 0,
    media INTEGER NOT NULL DEFAULT 0,
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_health (
    source_id INTEGER PRIMARY KEY,
    ok_at TEXT NOT NULL DEFAULT '',
    first_fail_at TEXT NOT NULL DEFAULT '',
    fails INTEGER NOT NULL DEFAULT 0,
    items INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS source_ideas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,  -- add | disable
    name TEXT NOT NULL DEFAULT '',
    value TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    source_id INTEGER NOT NULL DEFAULT 0,
    at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'  -- pending | applied | dismissed
);
CREATE TABLE IF NOT EXISTS ideas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brand_id INTEGER NOT NULL DEFAULT 1,
    at TEXT NOT NULL,
    idea TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    goal TEXT NOT NULL DEFAULT '',
    tone TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'generated',  -- generated | sent
    flyer_id TEXT NOT NULL DEFAULT '',
    post_id INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    expires REAL  -- unix time; NULL never
);
"""


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


_ID_TABLES = frozenset({"stories", "posts", "sources", "users", "audit", "notes", "brands", "agent_runs", "feedback", "playbooks", "proposals", "source_ideas", "ideas"})  # tables whose id the database makes
_pool: Any = None
_pool_lock = threading.Lock()


def postgres() -> bool:
    return bool(settings.DATABASE_URL)


def engine() -> str:
    return "postgresql" if postgres() else "sqlite"


def _pg_schema(sql: str) -> str:
    sql = re.sub(r"[ \t]*--[^\n]*", "", sql)  # comments may hold a semicolon
    sql = sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY")
    return re.sub(r"\bREAL\b", "DOUBLE PRECISION", sql)


def _translate(sql: str, has_params: bool) -> tuple[str, str]:
    """SQLite-flavoured SQL as PostgreSQL wants it. Returns the SQL and the table it inserts into ("" if none)."""
    ignore = bool(re.match(r"\s*INSERT OR IGNORE", sql, re.I))
    replace = bool(re.match(r"\s*INSERT OR REPLACE", sql, re.I))
    sql = re.sub(r"INSERT OR (IGNORE|REPLACE) INTO", "INSERT INTO", sql, flags=re.I)
    if has_params:
        sql = sql.replace("%", "%%").replace("?", "%s")
    table = (re.match(r"\s*INSERT INTO (\w+)", sql, re.I) or [None, ""])[1]
    if ignore:
        sql += " ON CONFLICT DO NOTHING"
    elif replace:  # only the kv table is written this way
        sql += " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, expires = EXCLUDED.expires"
    if table in _ID_TABLES:
        sql += " RETURNING id"
    return sql, table if table in _ID_TABLES else ""


def _get_pool() -> Any:
    global _pool
    with _pool_lock:
        if _pool is None or _pool.name != settings.DATABASE_URL:
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool

            _pool = ConnectionPool(settings.DATABASE_URL, min_size=1, max_size=10, name=settings.DATABASE_URL,
                                   kwargs={"row_factory": dict_row}, open=True)  # fmt: skip
        return _pool


def close() -> None:
    """Drop the connection pool (tests, and a change of database)."""
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.close()
            _pool = None


def columns(table: str) -> set[str]:
    if postgres():
        return {r["column_name"] for r in rows("SELECT column_name FROM information_schema.columns WHERE table_name = ?", table)}
    with connect() as conn:
        return {c["name"] for c in conn.execute(f"PRAGMA table_info({table})")}  # noqa: S608 - our own table names


def pg_columns(conn: Any, table: str) -> set[str]:
    """The columns of a PostgreSQL table, on a connection that is not the app's own."""
    return {r[0] for r in conn.execute("SELECT column_name FROM information_schema.columns WHERE table_name = %s", (table,))}


def init() -> None:
    settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
    if postgres():
        with _get_pool().connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(727274)")  # the web app and the scheduler start together; one creates the tables
            for stmt in (x.strip() for x in _pg_schema(SCHEMA).split(";") if x.strip()):
                conn.execute(stmt)
    else:
        with connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
    added = (("posts", "flyer", "TEXT NOT NULL DEFAULT ''"), ("posts", "linkedin", "TEXT NOT NULL DEFAULT ''"),
             ("posts", "meta", "TEXT NOT NULL DEFAULT ''"), ("posts", "brand_id", "INTEGER NOT NULL DEFAULT 1"),
             ("posts", "scheduled_at", "TEXT NOT NULL DEFAULT ''"), ("flyers", "layout", "TEXT NOT NULL DEFAULT 'classic'"),
             ("flyers", "style", "TEXT NOT NULL DEFAULT ''"), ("flyers", "brand_id", "INTEGER NOT NULL DEFAULT 1"),
             ("users", "email", "TEXT NOT NULL DEFAULT ''"), ("metrics", "plays", "INTEGER NOT NULL DEFAULT 0"),
             ("metrics", "saves", "INTEGER NOT NULL DEFAULT 0"), ("metrics", "shares", "INTEGER NOT NULL DEFAULT 0"),
             ("metrics", "reach", "INTEGER NOT NULL DEFAULT 0"))  # fmt: skip
    for table, col, ddl in added:  # older databases
        if col not in columns(table):
            _ddl(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


def _ddl(sql: str) -> None:
    if postgres():
        with _get_pool().connection() as conn:
            conn.execute(sql)
    else:
        with connect() as conn:
            conn.execute(sql)


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """The SQLite connection (the PostgreSQL path goes through ``rows`` and ``execute``)."""
    conn = sqlite3.connect(settings.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def rows(sql: str, *params: Any) -> list[dict[str, Any]]:
    if postgres():
        text, _ = _translate(sql, bool(params))
        with _get_pool().connection() as conn:
            out = [dict(r) for r in conn.execute(text, params or None).fetchall()]
    else:
        with connect() as conn:
            out = [dict(r) for r in conn.execute(sql, params)]
    for row in out:
        for col in ("outlets", "titles"):
            if col in row:
                row[col] = json.loads(row[col] or "[]")
    return out


def execute(sql: str, *params: Any) -> int:
    """Run one statement; an INSERT returns the new row's id (0 if nothing was inserted)."""
    if postgres():
        text, table = _translate(sql, bool(params))
        with _get_pool().connection() as conn:
            cur = conn.execute(text, params or None)
            if table:
                got = cur.fetchone()
                return int(got["id"]) if got else 0
            return 0
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


def kv_claim(key: str, ttl: float) -> bool:
    """Take a lease on ``key`` for ``ttl`` seconds. True if it was free (or expired), False if someone holds it."""
    if postgres():
        with _get_pool().connection() as conn:
            conn.execute("DELETE FROM kv WHERE key = %s AND expires IS NOT NULL AND expires < %s", (key, time.time()))
            return conn.execute("INSERT INTO kv (key, value, expires) VALUES (%s, '1', %s) ON CONFLICT DO NOTHING", (key, time.time() + ttl)).rowcount == 1
    with connect() as conn:
        conn.execute("DELETE FROM kv WHERE key = ? AND expires IS NOT NULL AND expires < ?", (key, time.time()))
        return conn.execute("INSERT OR IGNORE INTO kv (key, value, expires) VALUES (?, '1', ?)", (key, time.time() + ttl)).rowcount == 1


def kv_release(key: str) -> None:
    execute("DELETE FROM kv WHERE key = ?", key)


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
