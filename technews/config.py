"""Settings from the environment (and ``.env``)."""

from __future__ import annotations

import os
import re
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

from technews import secretbox

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = Path(os.environ.get("ENV_FILE") or BASE_DIR / ".env")  # a container keeps it on its data volume
load_dotenv(ENV_PATH)
for _name, _value in list(os.environ.items()):  # secrets sealed with SECRETS_KEY are opened here, in memory only
    if _value.startswith(secretbox.PREFIX):
        os.environ[_name] = secretbox.reveal(_value)


def _str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int) -> int:
    return int(_str(name) or default)


def _float(name: str, default: float) -> float:
    return float(_str(name) or default)


def _bool(name: str, default: bool) -> bool:
    raw = _str(name).lower()
    return default if not raw else raw in ("1", "true", "yes", "on")


class Config:
    DATA_DIR = Path(_str("DATA_DIR") or BASE_DIR / "data")
    DB_PATH = DATA_DIR / "technews.db"
    BRAND_ID = 1  # the brand the running code is working for (see brands.py)
    BRAND_NAME = "CloudTrio"
    DATABASE_URL = _str("DATABASE_URL")  # postgresql://user:password@host/db ; empty means the SQLite file above
    CARDS_DIR = DATA_DIR / "cards"
    OUTBOX_DIR = DATA_DIR / "outbox"
    IG_SESSION_PATH = DATA_DIR / "ig_session.json"

    NVIDIA_API_KEY = _str("NVIDIA_API_KEY")
    NVIDIA_API_BASE_URL = _str("NVIDIA_API_BASE_URL", "https://integrate.api.nvidia.com/v1")
    NVIDIA_MODEL = _str("NVIDIA_MODEL", "nvidia/nemotron-3-ultra-550b-a55b")
    NVIDIA_FALLBACK_MODELS = [m.strip() for m in _str("NVIDIA_FALLBACK_MODELS", "openai/gpt-oss-20b").split(",") if m.strip()]
    NVIDIA_TIMEOUT_SECONDS = _int("NVIDIA_TIMEOUT_SECONDS", 120)
    LLM2_BASE_URL = _str("LLM2_BASE_URL")  # a second AI service, used when NVIDIA fails (any OpenAI-compatible endpoint)
    LLM2_API_KEY = _str("LLM2_API_KEY")
    LLM2_MODEL = _str("LLM2_MODEL")
    COLLECT_METRICS = _bool("COLLECT_METRICS", True)  # read the audience numbers back from Instagram (Analytics; a few requests a day)
    BLOCKED_WORDS = _str("BLOCKED_WORDS")  # comma separated: stories or drafts containing these are not posted
    WATCHDOG_MINUTES = _int("WATCHDOG_MINUTES", 60)  # alert when no cycle has finished for this long (0 turns it off)

    BACKUP_S3_ENDPOINT = _str("BACKUP_S3_ENDPOINT")  # an S3-compatible service (R2, B2, AWS); blank for AWS itself
    BACKUP_S3_BUCKET = _str("BACKUP_S3_BUCKET")
    BACKUP_S3_ID = _str("BACKUP_S3_ID")
    BACKUP_S3_SECRET = _str("BACKUP_S3_SECRET")
    BACKUP_COPY_DIR = _str("BACKUP_COPY_DIR")  # a folder outside the data volume (a NAS, a synced drive)
    HEARTBEAT_URL = _str("HEARTBEAT_URL")  # an outside service (Healthchecks.io, UptimeRobot) that raises the alarm when this stops calling it
    GOOGLE_CLIENT_ID = _str("GOOGLE_CLIENT_ID")
    GOOGLE_CLIENT_SECRET = _str("GOOGLE_CLIENT_SECRET")

    SCHEDULER_ENABLED = _bool("SCHEDULER_ENABLED", True)
    SCAN_INTERVAL_MINUTES = _int("SCAN_INTERVAL_MINUTES", 5)
    SCAN_MAX_AGE_HOURS = _int("SCAN_MAX_AGE_HOURS", 12)
    TIMEZONE = _str("TIMEZONE", "Asia/Kolkata")
    SCAN_MIN_SCORE = _float("SCAN_MIN_SCORE", 6)
    REVIEW_MIN_SCORE = _float("REVIEW_MIN_SCORE", 8)

    IG_DELIVERY = _str("IG_DELIVERY", "dry_run")  # dry_run | instagram
    IG_USERNAME = _str("IG_USERNAME")
    IG_PASSWORD = _str("IG_PASSWORD")
    IG_TOTP_SECRET = _str("IG_TOTP_SECRET")
    IG_HANDLE = _str("IG_HANDLE", "yourtechpage")
    POSTS_PER_DAY = _int("POSTS_PER_DAY", 6)
    FLYERS_ENABLED = _bool("FLYERS_ENABLED", True)  # CloudTrio promo flyers between the news cards
    FLYER_AFTER_NEWS = _int("FLYER_AFTER_NEWS", 2)  # one flyer after this many news posts (2 flyers per 4 news)
    MIN_POST_GAP_MINUTES = _int("MIN_POST_GAP_MINUTES", 90)
    REELS_PER_WEEK = _int("REELS_PER_WEEK", 3)  # drafts made as animated Reels each week (0 = only when you choose)
    REEL_STYLE = _str("REEL_STYLE", "auto")  # auto | slide | zoom | reveal | countup
    REEL_MUSIC = _str("REEL_MUSIC", "upbeat")  # upbeat | calm (built-in instrumentals) | custom (your upload) | none
    REEL_SECONDS = _int("REEL_SECONDS", 12)
    REEL_CTA = _str("REEL_CTA", "Follow for daily cloud news and tips")
    IG_RESTRICT_PAUSE_HOURS = _float("IG_RESTRICT_PAUSE_HOURS", 24)
    IG_WARNED_PAUSE_MINUTES = _float("IG_WARNED_PAUSE_MINUTES", 10)
    HASHTAG_LIMIT = 5  # Instagram's cap since Dec 2025

    BRAND_EMAIL = _str("BRAND_EMAIL", "info@cloudtrio.in")  # contact line on the flyers
    BRAND_SITE = _str("BRAND_SITE", "cloudtrio.in")
    FLYER_HASHTAGS = _str("FLYER_HASHTAGS", "#cloudcomputing #aws #azure #devops #cloudtrio")
    APPROVAL_MODE = _str("APPROVAL_MODE", "all")  # all | flyers | off: what waits for a human OK before posting
    APPROVAL_QUEUE_MAX = _int("APPROVAL_QUEUE_MAX", 3)  # drafts waiting at once; no more are made until you decide
    LINKEDIN_ENABLED = _bool("LINKEDIN_ENABLED", False)  # post the flyers to LinkedIn too
    LINKEDIN_ACCESS_TOKEN = _str("LINKEDIN_ACCESS_TOKEN")
    LINKEDIN_AUTHOR = _str("LINKEDIN_AUTHOR")  # urn:li:organization:<id> (company page) or urn:li:person:<id>

    PUBLIC_URL = _str("PUBLIC_URL", "https://post.cloudtrio.in")  # the address in notification links
    NOTIFY_EMAIL_TO = _str("NOTIFY_EMAIL_TO")  # who is told when a post waits for approval (comma separated)
    SMTP_HOST = _str("SMTP_HOST")
    SMTP_PORT = _int("SMTP_PORT", 587)
    SMTP_USER = _str("SMTP_USER")
    SMTP_PASSWORD = _str("SMTP_PASSWORD")
    SMTP_FROM = _str("SMTP_FROM")
    TELEGRAM_BOT_TOKEN = _str("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID = _str("TELEGRAM_CHAT_ID")
    NOTIFY_WEBHOOK_URL = _str("NOTIFY_WEBHOOK_URL")  # Slack, Discord or Teams incoming webhook
    WHATSAPP_PROVIDER = _str("WHATSAPP_PROVIDER")  # callmebot | twilio | (empty: off)
    WHATSAPP_TO = _str("WHATSAPP_TO")  # your number with the country code
    WHATSAPP_APIKEY = _str("WHATSAPP_APIKEY")  # the CallMeBot key, or the Twilio auth token
    TWILIO_SID = _str("TWILIO_SID")
    TWILIO_WHATSAPP_FROM = _str("TWILIO_WHATSAPP_FROM")  # the Twilio WhatsApp number
    ADMIN_USER = _str("ADMIN_USER")  # seeds the admin account on the first run only
    ADMIN_PASSWORD = _str("ADMIN_PASSWORD")  # (change it afterwards under Settings, Access)
    ADMIN_TOKEN = _str("ADMIN_TOKEN")  # optional: an API token for scripts (X-Admin-Token header)


_override: ContextVar[dict[str, object] | None] = ContextVar("settings_override", default=None)


class _Settings(Config):
    """The settings, except that code running for one brand sees that brand's own values (only in that thread)."""

    def __getattribute__(self, name: str) -> object:
        ov = _override.get()
        if ov is not None and name in ov:
            return ov[name]
        return super().__getattribute__(name)


settings = _Settings()


def set_override(values: dict[str, object] | None) -> object:
    return _override.set(values)


def reset_override(token: object) -> None:
    _override.reset(token)  # type: ignore[arg-type]


def current_override() -> dict[str, object] | None:
    return _override.get()


@contextmanager
def override(values: dict[str, object] | None) -> Iterator[None]:
    token = _override.set(values)
    try:
        yield
    finally:
        _override.reset(token)


# --------------------------------------------------------------------------- editable from the dashboard


#: name -> (type, secret). Secrets are never sent back to the browser, only masked.
EDITABLE: dict[str, tuple[type, bool]] = {
    "NVIDIA_API_KEY": (str, True),
    "NVIDIA_MODEL": (str, False),
    "LLM2_BASE_URL": (str, False),
    "LLM2_API_KEY": (str, True),
    "LLM2_MODEL": (str, False),
    "WATCHDOG_MINUTES": (int, False),
    "BLOCKED_WORDS": (str, False),
    "COLLECT_METRICS": (bool, False),
    "IG_USERNAME": (str, False),
    "IG_PASSWORD": (str, True),
    "IG_HANDLE": (str, False),
    "IG_DELIVERY": (str, False),
    "POSTS_PER_DAY": (int, False),
    "MIN_POST_GAP_MINUTES": (int, False),
    "APPROVAL_MODE": (str, False),
    "SCAN_INTERVAL_MINUTES": (int, False),
    "SCAN_MAX_AGE_HOURS": (int, False),
    "SCAN_MIN_SCORE": (float, False),
    "REVIEW_MIN_SCORE": (float, False),
    "FLYERS_ENABLED": (bool, False),
    "FLYER_AFTER_NEWS": (int, False),
    "REELS_PER_WEEK": (int, False),
    "REEL_STYLE": (str, False),
    "REEL_MUSIC": (str, False),
    "REEL_SECONDS": (int, False),
    "REEL_CTA": (str, False),
    "APPROVAL_QUEUE_MAX": (int, False),
    "TIMEZONE": (str, False),
    "BRAND_EMAIL": (str, False),
    "BRAND_SITE": (str, False),
    "FLYER_HASHTAGS": (str, False),
    "LINKEDIN_ENABLED": (bool, False),
    "PUBLIC_URL": (str, False),
    "NOTIFY_EMAIL_TO": (str, False),
    "SMTP_HOST": (str, False),
    "SMTP_PORT": (int, False),
    "SMTP_USER": (str, False),
    "SMTP_PASSWORD": (str, True),
    "SMTP_FROM": (str, False),
    "TELEGRAM_BOT_TOKEN": (str, True),
    "TELEGRAM_CHAT_ID": (str, False),
    "NOTIFY_WEBHOOK_URL": (str, True),
    "WHATSAPP_PROVIDER": (str, False),
    "WHATSAPP_TO": (str, False),
    "WHATSAPP_APIKEY": (str, True),
    "TWILIO_SID": (str, False),
    "TWILIO_WHATSAPP_FROM": (str, False),
    "LINKEDIN_ACCESS_TOKEN": (str, True),
    "LINKEDIN_AUTHOR": (str, False),
    "BACKUP_S3_ENDPOINT": (str, False),
    "BACKUP_S3_BUCKET": (str, False),
    "BACKUP_S3_ID": (str, True),
    "BACKUP_S3_SECRET": (str, True),
    "BACKUP_COPY_DIR": (str, False),
    "HEARTBEAT_URL": (str, True),
    "GOOGLE_CLIENT_ID": (str, False),
    "GOOGLE_CLIENT_SECRET": (str, True),
}


#: Allowed range per number setting (the default for a whole number is 1-1440).
RANGES: dict[str, tuple[float, float]] = {
    "WATCHDOG_MINUTES": (0, 1440),
    "SCAN_MAX_AGE_HOURS": (1, 168), "SCAN_MIN_SCORE": (0, 10), "REVIEW_MIN_SCORE": (0, 10),
    "SMTP_PORT": (1, 65535), "FLYER_AFTER_NEWS": (1, 10), "APPROVAL_QUEUE_MAX": (1, 20), "POSTS_PER_DAY": (1, 50),
    "REELS_PER_WEEK": (0, 21), "REEL_SECONDS": (6, 30),
}  # fmt: skip


def mask(value: str, *, tail: bool = False) -> str:
    """Dots, plus the last 4 characters for an API key (never for a password)."""
    if not value:
        return ""
    return "•" * 8 + value[-4:] if tail and len(value) > 10 else "•" * 8


def public() -> dict[str, object]:
    """The editable settings for the dashboard, secrets masked."""
    out: dict[str, object] = {}
    for name, (_kind, secret) in EDITABLE.items():
        value = getattr(settings, name)
        out[name] = mask(value, tail=name.endswith("_KEY")) if secret else value
        if secret:
            out[f"{name}_set"] = bool(value)
    return out


def update(values: dict[str, object]) -> list[str]:
    """Apply ``values`` to the running app and save them to ``.env``. A blank secret keeps the
    saved one. Returns the names that changed; raises ValueError on a bad value."""
    changed: dict[str, object] = {}
    for name, raw in values.items():
        if name not in EDITABLE:
            continue
        kind, secret = EDITABLE[name]
        text = str(raw if raw is not None else "").strip()
        if secret and not text:
            continue
        if name in ("IG_USERNAME", "IG_HANDLE"):
            text = text.lstrip("@")
        if name == "IG_DELIVERY" and text not in ("dry_run", "instagram"):
            raise ValueError("IG_DELIVERY must be dry_run or instagram")
        if name == "LLM2_BASE_URL" and text and not text.startswith("https://"):
            raise ValueError("The second AI address must start with https:// (like https://api.openai.com/v1)")
        if name == "REEL_STYLE" and text not in ("auto", "bold", "corporate", "tech", "offer", "slide", "zoom", "reveal", "countup"):
            raise ValueError("REEL_STYLE must be auto, a preset (bold, corporate, tech, offer) or a simple movement")
        if name == "REEL_MUSIC" and text not in ("upbeat", "calm", "custom", "none"):
            raise ValueError("REEL_MUSIC must be upbeat, calm, custom or none")
        if name == "REEL_CTA" and len(text) > 60:
            raise ValueError("The closing line of a Reel is at most 60 characters")
        if name == "APPROVAL_MODE" and text not in ("all", "flyers", "off"):
            raise ValueError("APPROVAL_MODE must be all, flyers or off")
        if name == "LINKEDIN_AUTHOR" and text and not re.fullmatch(r"urn:li:(organization|person):[\w-]+", text):
            raise ValueError("LINKEDIN_AUTHOR looks like urn:li:organization:12345678")
        if kind is bool:
            value = text.lower() in ("1", "true", "yes", "on")
        elif kind is int:
            try:
                value: object = int(text)
            except ValueError:
                raise ValueError(f"{name} must be a whole number") from None
            lo, hi = RANGES.get(name, (1, 1440))
            if not lo <= int(value) <= hi:
                raise ValueError(f"{name} must be between {lo:g} and {hi:g}")
        elif kind is float:
            try:
                value = float(text)
            except ValueError:
                raise ValueError(f"{name} must be a number") from None
            lo, hi = RANGES[name]
            if not lo <= value <= hi:
                raise ValueError(f"{name} must be between {lo:g} and {hi:g}")
        else:
            if name == "TIMEZONE":
                try:
                    ZoneInfo(text)
                except (ZoneInfoNotFoundError, ValueError):
                    raise ValueError("TIMEZONE must be a name like Asia/Kolkata") from None
            if name in ("NOTIFY_EMAIL_TO", "SMTP_FROM") and text:
                for part in re.split(r"[,;\s]+", text):
                    if part and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", part):
                        raise ValueError(f"{name}: {part!r} is not an email address")
            if name in ("HEARTBEAT_URL", "BACKUP_S3_ENDPOINT") and text and not text.startswith("https://"):
                raise ValueError(f"{name} must start with https://")
            if name == "NOTIFY_WEBHOOK_URL" and not text.startswith("https://"):
                raise ValueError("The webhook address must start with https://")
            if name == "PUBLIC_URL":
                if text and not re.fullmatch(r"https?://[^\s/]+", text.rstrip("/")):
                    raise ValueError("PUBLIC_URL looks like https://post.cloudtrio.in")
                text = text.rstrip("/")
            if name == "WHATSAPP_PROVIDER" and text not in ("", "callmebot", "twilio"):
                raise ValueError("WHATSAPP_PROVIDER must be callmebot, twilio or empty")
            if name in ("WHATSAPP_TO", "TWILIO_WHATSAPP_FROM") and text and not 8 <= len(re.sub(r"\D", "", text)) <= 15:
                raise ValueError(f"{name} must be a phone number with the country code, like 919876543210")
            if name == "BRAND_EMAIL" and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", text):
                raise ValueError("BRAND_EMAIL must be an email address")
            if name in ("BRAND_SITE", "FLYER_HASHTAGS") and not text:
                raise ValueError(f"{name} cannot be empty")
            value = text
        if value != getattr(settings, name):
            changed[name] = value
    for name, value in changed.items():
        setattr(settings, name, value)
    if changed:
        _persist({k: str(v) for k, v in changed.items()})
    return list(changed)


def rotate_admin_token() -> str:
    """A new admin token, live at once and saved to ``.env``. The old one stops working."""
    token = secrets.token_urlsafe(24)
    settings.ADMIN_TOKEN = token
    _persist({"ADMIN_TOKEN": token})
    return token


def _quote(value: str) -> str:
    if value and all(c.isalnum() or c in "-_.:/@" for c in value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


# --------------------------------------------------------------------------- settings in the database
# The web app and the scheduler run as separate processes. Both read the settings from the database, and a
# change made in one is picked up by the other within seconds.

VERSION_KEY = "settings:version"
_applied: dict[str, str] = {}  # what this process last took from the database
_seen_version = ""


def _names() -> list[str]:
    return [*EDITABLE, "ADMIN_TOKEN"]


def _is_secret(name: str) -> bool:
    return name == "ADMIN_TOKEN" or EDITABLE[name][1]


def _text(value: object) -> str:
    return ("1" if value else "0") if isinstance(value, bool) else str(value)


def _persist(values: dict[str, str]) -> None:
    """Save changed settings to the database (secrets sealed) and to ``.env``, and tell the other process."""
    from technews import db

    for name, text in values.items():
        stored = secretbox.seal(text) if _is_secret(name) else text
        db.kv_set(f"setting:{name}", stored)
        _applied[name] = text
    db.kv_set(VERSION_KEY, secrets.token_hex(6))
    global _seen_version
    _seen_version = db.kv_get(VERSION_KEY) or ""
    _write_env(values)


def seed() -> bool:
    """First start on the database: keep what is set now (from ``.env`` and the environment). Returns True if it seeded."""
    from technews import db

    if db.kv_get(VERSION_KEY):
        return False
    for name in _names():
        text = _text(getattr(settings, name))
        db.kv_set(f"setting:{name}", secretbox.seal(text) if _is_secret(name) and text else text)
        _applied[name] = text
    db.kv_set(VERSION_KEY, secrets.token_hex(6))
    return True


def _convert(name: str, text: str) -> object:
    kind = EDITABLE[name][0] if name in EDITABLE else str
    if kind is bool:
        return text.lower() in ("1", "true", "yes", "on")
    if kind is int:
        return int(text)
    if kind is float:
        return float(text)
    return text


def refresh(*, force: bool = False) -> list[str]:
    """Take settings another process changed. Only the ones that differ from what this process last took are
    applied, so nothing a caller set by hand in the meantime is overwritten. Returns the names that changed."""
    from technews import db

    global _seen_version
    version = db.kv_get(VERSION_KEY) or ""
    if not version or (version == _seen_version and not force):
        return []
    _seen_version = version
    changed = []
    for row in db.rows("SELECT key, value FROM kv WHERE key LIKE 'setting:%'"):
        name = row["key"].split(":", 1)[1]
        if name not in _names():
            continue
        text = secretbox.reveal(row["value"])
        if _applied.get(name) == text:
            continue
        _applied[name] = text
        try:
            setattr(settings, name, _convert(name, text))
        except ValueError:
            continue
        changed.append(name)
    return changed


def _unquote(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        return raw[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return raw


def seal_env() -> int:
    """Seal every plain secret still in ``.env`` (needs SECRETS_KEY). Returns how many are sealed now."""
    if not secretbox.available() or not ENV_PATH.exists():
        return 0
    _write_env({})
    names = {n for n, (_k, is_secret) in EDITABLE.items() if is_secret} | {"ADMIN_TOKEN"}
    return sum(1 for n, v in dotenv_values(ENV_PATH).items() if n in names and secretbox.sealed(v or ""))


def secrets_status() -> dict[str, object]:
    names = {n for n, (_k, is_secret) in EDITABLE.items() if is_secret} | {"ADMIN_TOKEN"}
    values = {n: v or "" for n, v in dotenv_values(ENV_PATH).items() if n in names and v} if ENV_PATH.exists() else {}
    return {"key_set": secretbox.available(), "sealed": sum(secretbox.sealed(v) for v in values.values()),
            "plain": sum(not secretbox.sealed(v) for v in values.values())}  # fmt: skip


def _write_env(values: dict[str, str]) -> None:
    """Replace each name's active line in ``.env`` (or append it), keeping everything else."""
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    pending = dict(values)
    for i, line in enumerate(lines):
        name = line.split("=", 1)[0].strip()
        if name in pending and not line.lstrip().startswith("#"):
            lines[i] = f"{name}={_quote(pending.pop(name))}"
    lines += [f"{name}={_quote(value)}" for name, value in pending.items()]
    secret_names = {n for n, (_k, is_secret) in EDITABLE.items() if is_secret} | {"ADMIN_TOKEN"}
    lines = [f"{ln.split('=', 1)[0].strip()}={_quote(secretbox.seal(_unquote(ln.split('=', 1)[1])))}"
             if "=" in ln and not ln.lstrip().startswith("#") and ln.split("=", 1)[0].strip() in secret_names else ln for ln in lines]
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ENV_PATH.chmod(0o600)
