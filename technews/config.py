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
