"""Shared test setup: every test runs on its own empty data folder and database, never the real ones."""

import os

import pytest

from technews import config, db
from technews.config import settings


def _reset_postgres(url):
    """An empty PostgreSQL schema. Connections a background thread of an earlier test still holds are closed first."""
    import psycopg

    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid()")
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")


@pytest.fixture(autouse=True)
def tmp_data(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(settings, "CARDS_DIR", tmp_path / "cards")
    monkeypatch.setattr(settings, "OUTBOX_DIR", tmp_path / "outbox")
    monkeypatch.setattr(settings, "IG_SESSION_PATH", tmp_path / "ig_session.json")  # never the real saved login
    monkeypatch.setattr(settings, "IG_DELIVERY", "dry_run")
    monkeypatch.setattr(settings, "NVIDIA_API_KEY", "")
    monkeypatch.setattr(settings, "APPROVAL_MODE", "off")
    monkeypatch.setattr(settings, "ADMIN_TOKEN", "")
    monkeypatch.setattr(settings, "REELS_PER_WEEK", 0)  # a test that wants a Reel asks for one
    monkeypatch.setattr(settings, "SCHEDULER_ENABLED", False)  # no background cycles outliving the test
    monkeypatch.setattr(settings, "COLLECT_METRICS", False)
    monkeypatch.setattr(settings, "VIDEO_WORKER", False)  # a test runs the video jobs itself
    monkeypatch.setattr(config, "ENV_PATH", tmp_path / ".env")  # a test must never write the real .env
    url = os.environ.get("TEST_DATABASE_URL", "")  # run the suite on PostgreSQL: TEST_DATABASE_URL=postgresql://...
    monkeypatch.setattr(settings, "DATABASE_URL", url)
    if url:
        db.close()
        _reset_postgres(url)
    db.init()



def client_as(username, password, role="admin", brand_id=0):
    """A test client signed in as this person (made first if they do not exist yet)."""
    from technews import auth, create_app, users

    app = create_app()
    if role == "admin":
        auth.set_credentials(username, password)
    elif not users.by_name(username):
        users.create(username, password, role, brand_id)
    client = app.test_client()
    assert client.post("/login", data={"username": username, "password": password}).status_code == 302
    return client


def make_brand(name, **fields):
    """Add a business with these fields; returns its id."""
    from technews import brands

    brands.seed()  # business #1 exists first, as it does when the app starts

    return brands.save(None, {"name": name, **fields})


@pytest.fixture
def approved_story():
    """One approved, fresh tech story waiting to be posted; returns its id."""
    return db.execute("INSERT INTO stories (key, title, url, source, published_at, found_at, category, status, review_score) "
                      "VALUES ('k-approved', 'AWS launches a new region in Spain', 'https://x.test/approved', 'AWS News Blog', ?, ?, 'cloud', 'approved', 9)",
                      db.now(), db.now())  # fmt: skip


SOURDOUGH = {"kicker": "NEW", "headline": "Sourdough every Sunday", "points": ["Baked at 6am"], "cta": "Visit us", "caption": "Fresh bread", "enabled": True}
