"""More than one page: each brand has its own Instagram account, flyers, posts, hashtags and daily limit.
News stories are shared: a story goes to the first brand whose tags include it."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Final

from technews import config, db, secretbox
from technews.config import settings

DEFAULT_ID: Final = 1
FIELDS: Final = ("name", "handle", "ig_username", "ig_password", "hashtags", "email", "site", "categories", "posts_per_day", "enabled")


def seed() -> None:
    """The first brand is the page that exists today, taken from the current settings."""
    if db.rows("SELECT id FROM brands LIMIT 1"):
        return
    db.execute("INSERT INTO brands (id, name, handle, ig_username, hashtags, email, site, posts_per_day, enabled) VALUES (?,?,?,?,?,?,?,?,1)",
               DEFAULT_ID, "CloudTrio", settings.IG_HANDLE, settings.IG_USERNAME, settings.FLYER_HASHTAGS, settings.BRAND_EMAIL,
               settings.BRAND_SITE, 0)  # fmt: skip
    if db.postgres():  # the next brand must not reuse id 1
        db.execute("SELECT setval(pg_get_serial_sequence('brands', 'id'), 1, true)")


def listing() -> list[dict[str, Any]]:
    out = db.rows("SELECT * FROM brands ORDER BY id")
    return [{**b, "ig_password_set": bool(b["ig_password"]), "ig_password": "", "enabled": bool(b["enabled"]),
             "connected": session_user(b["id"]), "logo": logo_path(b["id"]).exists()} for b in out]  # fmt: skip


def get(brand_id: int) -> dict[str, Any] | None:
    found = db.rows("SELECT * FROM brands WHERE id = ?", brand_id)
    if not found and brand_id == DEFAULT_ID:  # a database that has not been through the app's start yet
        seed()
        found = db.rows("SELECT * FROM brands WHERE id = ?", brand_id)
    return found[0] if found else None


def active() -> list[dict[str, Any]]:
    return [b for b in db.rows("SELECT * FROM brands WHERE enabled = 1 ORDER BY id")]


def suffix(brand_id: int | None = None) -> str:
    """Added to a stored key so each brand keeps its own: nothing for the first brand, so its keys stay as they were."""
    bid = settings.BRAND_ID if brand_id is None else brand_id
    return "" if bid == DEFAULT_ID else f":{bid}"


def session_path(brand_id: int):
    return settings.DATA_DIR / ("ig_session.json" if brand_id == DEFAULT_ID else f"ig_session_{brand_id}.json")


def logo_path(brand_id: int):
    return settings.DATA_DIR / f"brand-logo-{brand_id}.png"


def session_user(brand_id: int) -> str:
    return (db.kv_get(f"instagram:session_user{suffix(brand_id)}") or "") if session_path(brand_id).exists() else ""


def overrides(brand: dict[str, Any]) -> dict[str, object]:
    """The settings that differ for this brand. A blank field keeps the general setting."""
    out: dict[str, object] = {"BRAND_ID": brand["id"], "BRAND_NAME": brand["name"], "IG_SESSION_PATH": session_path(brand["id"])}
    if brand["id"] == DEFAULT_ID:  # the first page runs on the general settings, which are edited under Settings
        return out
    for field, name in (("handle", "IG_HANDLE"), ("ig_username", "IG_USERNAME"), ("hashtags", "FLYER_HASHTAGS"),
                        ("email", "BRAND_EMAIL"), ("site", "BRAND_SITE")):  # fmt: skip
        if brand[field]:
            out[name] = brand[field]
    if brand["ig_password"]:
        out["IG_PASSWORD"] = secretbox.reveal(brand["ig_password"])
    if brand["posts_per_day"] and brand["posts_per_day"] > 0:
        out["POSTS_PER_DAY"] = int(brand["posts_per_day"])
    if brand["id"] != DEFAULT_ID:  # another page never posts to the first page's LinkedIn
        out["LINKEDIN_ENABLED"] = False
    return out


def categories(brand: dict[str, Any]) -> list[str]:
    return [c.strip() for c in (brand.get("categories") or "").split(",") if c.strip()]


@contextmanager
def use(brand: dict[str, Any] | int) -> Iterator[dict[str, Any]]:
    """Run the code inside as this brand."""
    row = get(brand) if isinstance(brand, int) else brand
    if row is None:
        raise ValueError("No such brand")
    with config.override(overrides(row)):
        yield row


def current() -> dict[str, Any]:
    return get(settings.BRAND_ID) or get(DEFAULT_ID) or {"id": DEFAULT_ID, "name": "CloudTrio", "categories": ""}


def save(brand_id: int | None, data: dict[str, Any]) -> int:
    """Add a brand (``brand_id`` None) or change one. Raises ValueError with what to fix."""
    name = str(data.get("name", "")).strip()
    if not name or len(name) > 60:
        raise ValueError("Give the brand a name (up to 60 characters)")
    values = {
        "name": name,
        "handle": str(data.get("handle", "")).strip().lstrip("@")[:60],
        "ig_username": str(data.get("ig_username", "")).strip().lstrip("@")[:60],
        "hashtags": str(data.get("hashtags", "")).strip()[:400],
        "email": str(data.get("email", "")).strip()[:120],
        "site": str(data.get("site", "")).strip()[:120],
        "categories": ",".join(c.strip().lower() for c in str(data.get("categories", "")).split(",") if c.strip())[:200],
        "enabled": int(bool(data.get("enabled", True))),
    }
    try:
        values["posts_per_day"] = max(0, min(50, int(data.get("posts_per_day") or 0)))
    except (TypeError, ValueError):
        raise ValueError("Posts per day is a whole number (0 uses the general limit)") from None
    password = str(data.get("ig_password", "")).strip()
    if brand_id is None:
        values["ig_password"] = secretbox.seal(password)
        cols = list(values)
        return db.execute(f"INSERT INTO brands ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", *values.values())  # noqa: S608
    if get(brand_id) is None:
        raise ValueError("No such brand")
    if password:  # a blank password keeps the saved one
        values["ig_password"] = secretbox.seal(password)
    if brand_id == DEFAULT_ID:
        values["enabled"] = 1  # the first brand cannot be switched off
    sets = ", ".join(f"{k} = ?" for k in values)
    db.execute(f"UPDATE brands SET {sets} WHERE id = ?", *values.values(), brand_id)  # noqa: S608 - fixed column names
    return brand_id


def delete(brand_id: int) -> None:
    if brand_id == DEFAULT_ID:
        raise ValueError("The first brand cannot be removed")
    if db.rows("SELECT id FROM posts WHERE brand_id = ? LIMIT 1", brand_id):
        raise ValueError("This brand has posts. Switch it off instead of removing it.")
    db.execute("DELETE FROM flyers WHERE brand_id = ?", brand_id)
    db.execute("DELETE FROM brands WHERE id = ?", brand_id)
