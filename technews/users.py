"""People who can sign in: a role each (admin, approver or viewer), an optional authenticator-app
code (2-step sign-in, free, no service needed), and an audit log of who did what."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import struct
import time
from typing import Any, Final
from urllib.parse import quote

from werkzeug.security import check_password_hash, generate_password_hash

from technews import db, secretbox

ROLES: Final = ("admin", "approver", "viewer")
MIN_PASSWORD: Final = 8
TWO_STEP_KEY: Final = "security:two_step"  # off | optional | required (set on the dashboard)
ISSUER: Final = "CloudTrio Newsroom"


def _name(user: str) -> str:
    user = user.strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{3,32}", user):
        raise ValueError("The username is 3 to 32 letters, numbers, dots, dashes or underscores")
    return user


def _password(password: str) -> str:
    if len(password) < MIN_PASSWORD:
        raise ValueError(f"The password needs at least {MIN_PASSWORD} characters")
    return generate_password_hash(password)


def migrate() -> None:
    """The old single account becomes the first admin."""
    if db.rows("SELECT id FROM users LIMIT 1"):
        return
    legacy_hash, legacy_user = db.kv_get("auth:hash"), db.kv_get("auth:user")
    if legacy_hash and legacy_user:
        db.execute("INSERT INTO users (username, hash, role, ver, created_at) VALUES (?,?,?,?,?)",
                   legacy_user, legacy_hash, "admin", db.kv_get("auth:version") or "0", db.now())  # fmt: skip


def listing() -> list[dict[str, Any]]:
    return [{**{k: u[k] for k in ("id", "username", "role", "created_at")}, "active": bool(u["active"]), "two_step": bool(u["totp"]), "email": u["email"]}
            for u in db.rows("SELECT * FROM users ORDER BY id")]  # fmt: skip


def get(user_id: int) -> dict[str, Any] | None:
    found = db.rows("SELECT * FROM users WHERE id = ?", user_id)
    return found[0] if found else None


def by_name(name: str) -> dict[str, Any] | None:
    found = db.rows("SELECT * FROM users WHERE lower(username) = lower(?)", name.strip())
    return found[0] if found else None


def _email(email: str) -> str:
    email = email.strip().lower()
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ValueError("That is not an email address")
    return email


def by_email(email: str) -> dict[str, Any] | None:
    """The active person whose account carries this email (the link for sign in with Google)."""
    email = email.strip().lower()
    found = db.rows("SELECT * FROM users WHERE email != '' AND lower(email) = ? AND active = 1", email) if email else []
    return found[0] if found else None


def set_email(user_id: int, email: str) -> str:
    email = _email(email)
    other = by_email(email) if email else None
    if other and other["id"] != user_id:
        raise ValueError("Another person already uses that email")
    db.execute("UPDATE users SET email = ? WHERE id = ?", email, user_id)
    return email


def create(username: str, password: str, role: str) -> int:
    if role not in ROLES:
        raise ValueError("Pick admin, approver or viewer")
    username = _name(username)
    if by_name(username):
        raise ValueError("That username is taken")
    return db.execute("INSERT INTO users (username, hash, role, ver, created_at) VALUES (?,?,?,?,?)",
                      username, _password(password), role, secrets.token_hex(8), db.now())  # fmt: skip


def admins_left(without: int) -> int:
    return len(db.rows("SELECT id FROM users WHERE role = 'admin' AND active = 1 AND id != ?", without))


def change(user_id: int, *, role: str | None = None, active: bool | None = None, password: str | None = None,
           username: str | None = None) -> None:  # fmt: skip
    user = get(user_id)
    if user is None:
        raise ValueError("No such person")
    new_role = role if role is not None else user["role"]
    new_active = bool(user["active"]) if active is None else active
    if new_role not in ROLES:
        raise ValueError("Pick admin, approver or viewer")
    if (new_role != "admin" or not new_active) and user["role"] == "admin" and user["active"] and admins_left(user_id) == 0:
        raise ValueError("There must always be one active admin")
    sets, params = ["role = ?", "active = ?"], [new_role, int(new_active)]
    if username is not None and username != user["username"]:
        name = _name(username)
        other = by_name(name)
        if other and other["id"] != user_id:
            raise ValueError("That username is taken")
        sets.append("username = ?")
        params.append(name)
    if password:
        sets += ["hash = ?", "ver = ?"]
        params += [_password(password), secrets.token_hex(8)]
    elif not new_active or new_role != user["role"]:
        sets.append("ver = ?")  # signs them out so the new role or the block applies at once
        params.append(secrets.token_hex(8))
    db.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", *params, user_id)  # noqa: S608 - fixed column names


def delete(user_id: int) -> None:
    user = get(user_id)
    if user and user["role"] == "admin" and user["active"] and admins_left(user_id) == 0:
        raise ValueError("There must always be one active admin")
    db.execute("DELETE FROM users WHERE id = ?", user_id)


def check_password(name: str, password: str) -> dict[str, Any] | None:
    user = by_name(name)
    good = check_password_hash(user["hash"] if user else generate_password_hash("x"), password)  # same work either way
    return user if user and good and user["active"] else None


# --------------------------------------------------------------------------- 2-step sign-in (RFC 6238)


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _code(secret: str, step: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 15
    return f"{(struct.unpack('>I', digest[offset : offset + 4])[0] & 0x7FFFFFFF) % 1_000_000:06d}"


def code_ok(secret: str, code: str, now: float | None = None) -> bool:
    code = re.sub(r"\s", "", code)
    step = int((now if now is not None else time.time()) // 30)
    return bool(secret) and code.isdigit() and any(hmac.compare_digest(_code(secret, step + d), code) for d in (-1, 0, 1))


def otpauth(username: str, secret: str) -> str:
    return f"otpauth://totp/{quote(ISSUER)}:{quote(username)}?secret={secret}&issuer={quote(ISSUER)}"


def two_step_mode() -> str:
    mode = db.kv_get(TWO_STEP_KEY) or "optional"
    return mode if mode in ("off", "optional", "required") else "optional"


def set_two_step_mode(mode: str) -> None:
    if mode not in ("off", "optional", "required"):
        raise ValueError("Pick off, optional or required")
    db.kv_set(TWO_STEP_KEY, mode)


def set_totp(user_id: int, secret: str) -> None:
    db.execute("UPDATE users SET totp = ? WHERE id = ?", secretbox.seal(secret), user_id)


def totp_secret(user: dict[str, Any]) -> str:
    """The authenticator secret of a user, opened (it is stored sealed when a secrets key is set)."""
    return secretbox.reveal(user["totp"] or "")


def seal_secrets() -> int:
    """Seal authenticator secrets stored as plain text. Returns how many were changed."""
    if not secretbox.available():
        return 0
    changed = 0
    for u in db.rows("SELECT id, totp FROM users WHERE totp != ''"):
        if not secretbox.sealed(u["totp"]):
            db.execute("UPDATE users SET totp = ? WHERE id = ?", secretbox.seal(u["totp"]), u["id"])
            changed += 1
    return changed


# --------------------------------------------------------------------------- audit log


def record(user: str, action: str, detail: str = "", ip: str = "") -> None:
    db.execute('INSERT INTO audit (at, "user", action, detail, ip) VALUES (?,?,?,?,?)', db.now(), user, action, detail[:300], ip)


def recent(limit: int = 100) -> list[dict[str, Any]]:
    return db.rows('SELECT at, "user" AS user, action, detail, ip FROM audit ORDER BY id DESC LIMIT ?', limit)
