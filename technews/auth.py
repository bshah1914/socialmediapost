"""Admin sign-in: one account (a username and a password hash in the database), a signed session
cookie, and a limit on wrong guesses. The password is never stored, only its hash."""

from __future__ import annotations

import hmac
import secrets
import time
from typing import Final

from technews import db, users
from technews.config import settings

SECRET_KEY: Final = "auth:secret"
MAX_FAILURES: Final = 5
WINDOW_SECONDS: Final = 600

_failures: dict[str, list[float]] = {}


def secret_key() -> str:
    """The key that signs session cookies; made once and kept in the database."""
    key = db.kv_get(SECRET_KEY)
    if not key:
        key = secrets.token_hex(32)
        db.kv_set(SECRET_KEY, key)
    return key


def username() -> str:
    """The first admin (what an API token acts as)."""
    found = db.rows("SELECT username FROM users WHERE role = 'admin' AND active = 1 ORDER BY id LIMIT 1")
    return found[0]["username"] if found else ""


def set_credentials(user: str, password: str) -> None:
    """Create or reset an admin (used by first run and by tests)."""
    existing = users.by_name(user)
    if existing:
        users.change(existing["id"], role="admin", active=True, password=password)
    else:
        users.create(user, password, "admin")


def verify(user: str, password: str) -> bool:
    return users.check_password(user, password) is not None


def bootstrap() -> str | None:
    """The first run: create the admin from ADMIN_USER / ADMIN_PASSWORD, or with a generated
    password (returned, for the log). Does nothing once anyone exists."""
    users.migrate()
    if db.rows("SELECT id FROM users LIMIT 1"):
        return None
    password = settings.ADMIN_PASSWORD or secrets.token_urlsafe(12)
    users.create(settings.ADMIN_USER or "admin", password, "admin")
    return None if settings.ADMIN_PASSWORD else password


# --------------------------------------------------------------------------- wrong-guess limit


def blocked(ip: str) -> bool:
    now = time.time()
    recent = [t for t in _failures.get(ip, []) if now - t < WINDOW_SECONDS]
    _failures[ip] = recent
    return len(recent) >= MAX_FAILURES


def failed(ip: str) -> None:
    _failures.setdefault(ip, []).append(time.time())


def cleared(ip: str) -> None:
    _failures.pop(ip, None)


def token_ok(header: str) -> bool:
    """The optional API token (ADMIN_TOKEN) for scripts."""
    return bool(settings.ADMIN_TOKEN) and hmac.compare_digest(header.encode(), settings.ADMIN_TOKEN.encode())
