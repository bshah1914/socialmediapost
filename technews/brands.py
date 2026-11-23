"""More than one business: each has its own Instagram account, AI key, files, flyers, posts, hashtags, voice and
daily limit, and chooses what it posts (news, flyers, ideas, videos). News stories are shared: a story goes to the
first business that posts news and whose tags include it.

Business #1 (CloudTrio) runs on the general settings and keeps today's file paths; everything per business that is
new here applies to the others, or to #1 only when it is set."""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Final

from technews import config, db, secretbox
from technews.config import settings

DEFAULT_ID: Final = 1
FIELDS: Final = ("name", "handle", "ig_username", "ig_password", "hashtags", "email", "site", "categories", "posts_per_day", "enabled")
KINDS: Final = ("news", "flyers", "ideas", "videos")
NEW_KINDS: Final = "flyers,ideas,videos"  # a new business posts no tech news unless it is switched on
_CHAT = re.compile(r"^-?\d{3,20}$")
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


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
    return [{**b, "ig_password_set": bool(b["ig_password"]), "ig_password": "", "nvidia_key_set": bool(b.get("nvidia_key")), "nvidia_key": "",
             "enabled": bool(b["enabled"]), "connected": session_user(b["id"]), "logo": logo_path(b["id"]).exists()} for b in out]  # fmt: skip


def get(brand_id: int) -> dict[str, Any] | None:
    found = db.rows("SELECT * FROM brands WHERE id = ?", brand_id)
    if not found and brand_id == DEFAULT_ID:  # a database that has not been through the app's start yet
        seed()
        found = db.rows("SELECT * FROM brands WHERE id = ?", brand_id)
    return found[0] if found else None


def active() -> list[dict[str, Any]]:
    return [b for b in db.rows("SELECT * FROM brands WHERE enabled = 1 ORDER BY id")]


def kinds(brand: dict[str, Any]) -> set[str]:
    """What this business posts."""
    raw = brand.get("kinds")
    return {k.strip() for k in (",".join(KINDS) if raw is None else str(raw)).split(",") if k.strip() in KINDS}


def has(kind: str, brand: dict[str, Any] | None = None) -> bool:
    return kind in kinds(brand if brand is not None else current())


def suffix(brand_id: int | None = None) -> str:
    """Added to a stored key so each brand keeps its own: nothing for the first brand, so its keys stay as they were."""
    bid = settings.BRAND_ID if brand_id is None else brand_id
    return "" if bid == DEFAULT_ID else f":{bid}"


def folder(brand_id: int) -> Path:
    """Where a business keeps its own files (cards, videos, uploads, logo, Instagram session)."""
    path = settings.DATA_DIR / "brands" / str(brand_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _moved(old: Path, new: Path) -> Path:
    """A file kept at its old place (before businesses had their own folder) is moved to the new one."""
    if old.exists() and not new.exists():
        shutil.move(str(old), str(new))
    return new


def session_path(brand_id: int) -> Path:
    if brand_id == DEFAULT_ID:
        return settings.DATA_DIR / "ig_session.json"
    return _moved(settings.DATA_DIR / f"ig_session_{brand_id}.json", folder(brand_id) / "ig_session.json")


def logo_path(brand_id: int) -> Path:
    if brand_id == DEFAULT_ID:
        return settings.DATA_DIR / f"brand-logo-{brand_id}.png"
    return _moved(settings.DATA_DIR / f"brand-logo-{brand_id}.png", folder(brand_id) / "logo.png")


def session_user(brand_id: int) -> str:
    return (db.kv_get(f"instagram:session_user{suffix(brand_id)}") or "") if session_path(brand_id).exists() else ""


def colors(brand: dict[str, Any]) -> dict[str, str]:
    try:
        found = json.loads(brand.get("colors") or "{}")
    except ValueError:
        return {}
    return {k: v for k, v in found.items() if k in ("bg", "accent") and isinstance(v, str) and _HEX.match(v)} if isinstance(found, dict) else {}


def overrides(brand: dict[str, Any]) -> dict[str, object]:
    """The settings that differ for this brand. A blank field keeps the general setting."""
    out: dict[str, object] = {"BRAND_ID": brand["id"], "BRAND_NAME": brand["name"], "IG_SESSION_PATH": session_path(brand["id"])}
    if brand.get("about"):
        out["BRAND_ABOUT"] = brand["about"]
    if brand.get("delivery") == "dry_run":
        out["IG_DELIVERY"] = "dry_run"
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
    home = folder(brand["id"])
    out["CARDS_DIR"], out["OUTBOX_DIR"] = home / "cards", home / "outbox"
    # its own AI key or none: a business never spends another business's key (nor the shared second AI service)
    out["NVIDIA_API_KEY"] = secretbox.reveal(brand.get("nvidia_key") or "")
    out["LLM2_API_KEY"] = ""
    out["LINKEDIN_ENABLED"] = False  # another page never posts to the first page's LinkedIn
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


def _profile(data: dict[str, Any]) -> dict[str, Any]:
    """The fields a business's own owner may change, checked. Only the fields given are returned."""
    out: dict[str, Any] = {}
    if "about" in data:
        out["about"] = str(data.get("about") or "").strip()[:1500]
    for field, size in (("hashtags", 400), ("email", 120), ("site", 120)):
        if field in data:
            out[field] = str(data.get(field) or "").strip()[:size]
    if "telegram_chat" in data:
        chat = str(data.get("telegram_chat") or "").strip()
        if chat and not _CHAT.match(chat):
            raise ValueError("Telegram chat id is a number (like -1001234567890)")
        out["telegram_chat"] = chat
    if "colors" in data:
        raw = data.get("colors") or {}
        if isinstance(raw, str):
            try:
                raw = json.loads(raw or "{}")
            except ValueError:
                raise ValueError("Colours must be like #1a2b3c") from None
        picked = {k: str(raw.get(k, "")).strip() for k in ("bg", "accent") if isinstance(raw, dict) and raw.get(k)}
        if any(not _HEX.match(v) for v in picked.values()):
            raise ValueError("Colours must be like #1a2b3c")
        out["colors"] = json.dumps(picked) if picked else ""
    return out


def _extras(data: dict[str, Any], *, new: bool) -> dict[str, Any]:
    """The business fields only an admin sets: kinds, own AI key, delivery."""
    out: dict[str, Any] = {}
    if "kinds" in data or new:
        raw = data.get("kinds", NEW_KINDS)
        items = raw if isinstance(raw, (list, tuple, set)) else str(raw or "").split(",")
        out["kinds"] = ",".join(k for k in KINDS if k in {str(i).strip().lower() for i in items})
    if data.get("clear_nvidia_key"):
        out["nvidia_key"] = ""
    elif str(data.get("nvidia_key") or "").strip():  # a blank key keeps the saved one
        out["nvidia_key"] = secretbox.seal(str(data["nvidia_key"]).strip()[:200])
    if "delivery" in data:
        out["delivery"] = "dry_run" if data.get("delivery") == "dry_run" else ""
    return out


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
    values.update(_profile({k: v for k, v in data.items() if k not in ("hashtags", "email", "site")}))
    values.update(_extras(data, new=brand_id is None))
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


def save_profile(brand_id: int, data: dict[str, Any]) -> int:
    """A business's owner changes its profile: what it does, hashtags, contact, colours, its Telegram chat. Nothing else."""
    if get(brand_id) is None:
        raise ValueError("No such brand")
    values = _profile(data)
    if values:
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
