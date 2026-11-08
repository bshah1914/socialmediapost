"""Notices when the newsroom has stopped doing its job and says so once, through the alert channels
(email, Telegram, webhook). It also says when things are back."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any, Final

from technews import db, notify, pipeline
from technews.config import settings

logger = logging.getLogger(__name__)

ALERTED_KEY: Final = "watchdog:alerted"


def minutes_since_cycle() -> float | None:
    raw = db.kv_get(pipeline.LAST_CYCLE_KEY)
    try:
        finished = json.loads(raw or "{}").get("finished_at")
        return (datetime.now(UTC) - datetime.fromisoformat(finished)).total_seconds() / 60 if finished else None
    except (ValueError, TypeError):
        return None


def check() -> dict[str, Any]:
    """Compare the last finished cycle with the limit; alert once when it is too old, and once when it recovers."""
    limit = settings.WATCHDOG_MINUTES
    if limit <= 0:
        return {"state": "off"}
    age = minutes_since_cycle()
    stale = age is not None and age > limit
    alerted = db.kv_get(ALERTED_KEY) == "1"
    if stale and not alerted:
        db.kv_set(ALERTED_KEY, "1")
        notify.send("CloudTrio newsroom: stopped", f"No cycle has finished for {int(age)} minutes (limit {limit}). Open the dashboard to see why.")
        logger.error("watchdog.stale minutes=%d", age)
        return {"state": "alerted", "minutes": int(age)}
    if not stale and alerted:
        db.kv_set(ALERTED_KEY, "")
        notify.send("CloudTrio newsroom: running again", "A cycle has finished again.")
        return {"state": "recovered"}
    return {"state": "stale" if stale else "ok", "minutes": None if age is None else int(age)}
