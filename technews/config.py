"""Settings from the environment (and ``.env``)."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


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
    CARDS_DIR = DATA_DIR / "cards"
    OUTBOX_DIR = DATA_DIR / "outbox"
    IG_SESSION_PATH = DATA_DIR / "ig_session.json"

    NVIDIA_API_KEY = _str("NVIDIA_API_KEY")
    NVIDIA_API_BASE_URL = _str("NVIDIA_API_BASE_URL", "https://integrate.api.nvidia.com/v1")
    NVIDIA_MODEL = _str("NVIDIA_MODEL", "nvidia/nemotron-3-ultra-550b-a55b")
    NVIDIA_FALLBACK_MODELS = [m.strip() for m in _str("NVIDIA_FALLBACK_MODELS", "openai/gpt-oss-20b").split(",") if m.strip()]
    NVIDIA_TIMEOUT_SECONDS = _int("NVIDIA_TIMEOUT_SECONDS", 120)

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
    MIN_POST_GAP_MINUTES = _int("MIN_POST_GAP_MINUTES", 90)
    IG_RESTRICT_PAUSE_HOURS = _float("IG_RESTRICT_PAUSE_HOURS", 24)
    IG_WARNED_PAUSE_MINUTES = _float("IG_WARNED_PAUSE_MINUTES", 10)
    HASHTAG_LIMIT = 5  # Instagram's cap since Dec 2025

    ADMIN_TOKEN = _str("ADMIN_TOKEN")


settings = Config()


# --------------------------------------------------------------------------- editable from the dashboard

ENV_PATH = BASE_DIR / ".env"

#: name -> (type, secret). Secrets are never sent back to the browser, only masked.
EDITABLE: dict[str, tuple[type, bool]] = {
    "NVIDIA_API_KEY": (str, True),
    "NVIDIA_MODEL": (str, False),
    "IG_USERNAME": (str, False),
    "IG_PASSWORD": (str, True),
    "IG_HANDLE": (str, False),
    "IG_DELIVERY": (str, False),
    "POSTS_PER_DAY": (int, False),
    "MIN_POST_GAP_MINUTES": (int, False),
}


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
        if kind is int:
            try:
                value: object = int(text)
            except ValueError:
                raise ValueError(f"{name} must be a whole number") from None
            if not 1 <= int(value) <= 1440:
                raise ValueError(f"{name} must be between 1 and 1440")
        else:
            value = text
        if value != getattr(settings, name):
            changed[name] = value
    for name, value in changed.items():
        setattr(settings, name, value)
    if changed:
        _write_env({k: str(v) for k, v in changed.items()})
    return list(changed)


def _quote(value: str) -> str:
    if value and all(c.isalnum() or c in "-_.:/@" for c in value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _write_env(values: dict[str, str]) -> None:
    """Replace each name's active line in ``.env`` (or append it), keeping everything else."""
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    pending = dict(values)
    for i, line in enumerate(lines):
        name = line.split("=", 1)[0].strip()
        if name in pending and not line.lstrip().startswith("#"):
            lines[i] = f"{name}={_quote(pending.pop(name))}"
    lines += [f"{name}={_quote(value)}" for name, value in pending.items()]
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ENV_PATH.chmod(0o600)
