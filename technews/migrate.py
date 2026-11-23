"""Copy the SQLite database into PostgreSQL: ``flask --app run migrate-db --to postgresql://...``.
The SQLite file is only read; it stays as it is, so switching back is just removing DATABASE_URL."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from technews import db

TABLES = ("stories", "posts", "flyers", "sources", "users", "audit", "notes", "brands", "agent_runs", "feedback", "playbooks", "proposals", "metrics", "source_health", "source_ideas", "ideas", "videos", "kv")


def to_postgres(sqlite_path: Path, url: str, *, replace: bool = False) -> dict[str, int]:
    """Create the tables in PostgreSQL and copy every row. Refuses to touch a database that already has data
    unless ``replace`` is set (which empties the copied tables first). Returns the rows copied per table."""
    import psycopg

    src = sqlite3.connect(sqlite_path)
    src.row_factory = sqlite3.Row
    have = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    copied: dict[str, int] = {}
    with psycopg.connect(url) as dst:
        for stmt in (x.strip() for x in db._pg_schema(db.SCHEMA).split(";") if x.strip()):
            dst.execute(stmt)
        for table in TABLES:
            if table not in have:
                continue
            (existing,) = dst.execute(f"SELECT count(*) FROM {table}").fetchone()  # noqa: S608 - our own table names
            if existing and not replace:
                raise RuntimeError(f"PostgreSQL already has data in {table}; use --replace to overwrite it")
            if existing:
                dst.execute(f"DELETE FROM {table}")  # noqa: S608
            cols = [c["name"] for c in src.execute(f"PRAGMA table_info({table})")]
            wanted = [c for c in cols if c in db.pg_columns(dst, table)]
            names = ", ".join(f'"{c}"' for c in wanted)
            marks = ", ".join(["%s"] * len(wanted))
            data = [tuple(r[c] for c in wanted) for r in src.execute(f"SELECT {names} FROM {table}")]  # noqa: S608
            if data:
                with dst.cursor() as cur:
                    cur.executemany(f"INSERT INTO {table} ({names}) VALUES ({marks}) ON CONFLICT DO NOTHING", data)  # noqa: S608
            if table in db._ID_TABLES and "id" in wanted:
                dst.execute(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), COALESCE((SELECT MAX(id) FROM {table}), 1), "  # noqa: S608
                            f"(SELECT MAX(id) IS NOT NULL FROM {table}))")  # noqa: S608
            copied[table] = len(data)
    src.close()
    return copied
