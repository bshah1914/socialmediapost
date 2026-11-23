"""Notices when the newsroom has stopped doing its job and says so once, through the alert channels
(email, Telegram, webhook). It also says when things are back."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from technews import db, notify, pipeline, sources
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


# --------------------------------------------------------------------------- the health checks: running is not enough


QUEUE_HOURS: Final = 6  # a draft waiting this long gets a reminder
QUIET_HOURS: Final = 24  # no post for this long means something is in the way
MISSED_OUTLETS: Final = 6  # an outage / breach / exploit this many outlets carry, that the page passed over, is worth a second look
MISSED_OUTLETS_OTHER: Final = 10  # any other tech story needs more (a press release is syndicated widely)
MISSED_PER_CHECK: Final = 2


def _hours_ago(stamp: str) -> float:
    return (datetime.now(UTC) - datetime.fromisoformat(stamp)).total_seconds() / 3600


def _since(hours: float) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours)).isoformat(timespec="seconds")


def _queue() -> str:
    waiting = db.rows("SELECT created_at FROM posts WHERE status = 'pending' AND brand_id = ? ORDER BY created_at LIMIT 50", settings.BRAND_ID)
    if not waiting or _hours_ago(waiting[0]["created_at"]) < QUEUE_HOURS or not db.kv_claim("watchdog:queue", QUEUE_HOURS * 3600):
        return ""
    oldest = int(_hours_ago(waiting[0]["created_at"]))
    note = f"{len(waiting)} post(s) are waiting for your approval, the oldest for {oldest} hours."
    notify.send("CloudTrio: posts are waiting for you", note + " Approve or reject them in Approvals (or with the Telegram buttons). "
                f"News drafts are withdrawn after {settings.NEWS_DRAFT_MAX_HOURS} hours." + (f"\n{notify.link()}" if notify.link() else ""))  # fmt: skip
    return note


def _quiet() -> str:
    if settings.POSTS_PER_DAY <= 0:
        return ""
    last = db.kv_get(pipeline.LAST_POST_KEY)
    if not last or _hours_ago(last) < QUIET_HOURS or not db.kv_claim("watchdog:quiet", 12 * 3600):
        return ""
    try:
        why = json.loads(db.kv_get(pipeline.LAST_CYCLE_KEY) or "{}").get("post", {}).get("reason", "")
    except (ValueError, AttributeError):
        why = ""
    note = f"Nothing has been posted for {int(_hours_ago(last))} hours." + (f" The newsroom says: {why}." if why else "")
    notify.send("CloudTrio: nothing posted for a day", note + " Open the dashboard to see what is in the way.")
    return note


def _ai() -> str:
    """The scanner falls back to keywords when the AI does not answer: fine for a moment, poor for hours."""
    since = _since(2)
    total = int(db.rows("SELECT COUNT(*) AS n FROM stories WHERE found_at >= ?", since)[0]["n"])
    plain = int(db.rows("SELECT COUNT(*) AS n FROM stories WHERE found_at >= ? AND scan_reason LIKE ?", since, "keyword scan%")[0]["n"])
    if total < 10 or plain / total < 0.5 or not db.kv_claim("watchdog:ai", 6 * 3600):
        return ""
    note = f"The AI did not answer for {plain} of the last {total} stories, so they were scored by keywords only and the picks are weaker."
    notify.send("CloudTrio: the AI is not answering", note + " Check the AI key and model under Settings.")
    return note


def _models() -> str:
    """NVIDIA retires models without notice; the newsroom skips a retired one, and you are told to pick another."""
    from technews.agents import llm

    gone = llm.retired()
    if not gone or not db.kv_claim("watchdog:models:" + ",".join(sorted(gone)), 7 * 86400):
        return ""
    note = "NVIDIA has retired these AI models: " + ", ".join(gone) + ". The newsroom skips them and uses the others."
    notify.send("CloudTrio: an AI model was retired", note + " To keep a spare, set another model under Settings, AI.")
    return note


def _sources() -> str:
    rows = db.rows("SELECT s.name, s.value, s.kind, h.fails FROM sources s LEFT JOIN source_health h ON h.source_id = s.id WHERE s.enabled = 1")
    failing = [r["name"] or r["value"] for r in rows if (r["fails"] or 0) >= 3]
    if len(rows) < 5 or len(failing) / len(rows) < 0.3 or not db.kv_claim("watchdog:sources", 12 * 3600):
        return ""
    note = f"{len(failing)} of {len(rows)} news sources are failing: {', '.join(failing[:10])}."
    notify.send("CloudTrio: news sources are failing", note + " The server may have lost its internet connection, or the sites block it.")
    return note


def _missed() -> list[str]:
    """Stories many outlets carry that the newsroom passed over: each is sent once, with a button to post it anyway."""
    covered = [r["title"] for r in db.rows("SELECT title FROM stories WHERE status IN ('posted', 'pending', 'approved') AND found_at >= ?", _since(48))]
    out = []
    candidates = db.rows("SELECT id, title, outlets, category, status, review_reason, scan_reason FROM stories "
                         "WHERE status IN ('rejected', 'low', 'not_tech') AND found_at >= ?", _since(12))  # fmt: skip
    for s in sorted(candidates, key=lambda r: len(r["outlets"]), reverse=True):
        if len(out) >= MISSED_PER_CHECK or len(s["outlets"]) < MISSED_OUTLETS:
            break
        incident = bool(sources.event(s["title"]))
        if str(s["review_reason"] or "").startswith(pipeline.FINAL_REASONS) or not (incident or s["category"] != "other"):
            continue
        if not incident and len(s["outlets"]) < MISSED_OUTLETS_OTHER:
            continue
        if any(sources.related(s["title"], t) for t in covered) or not db.kv_claim(f"watchdog:missed:{s['id']}", 14 * 86400):
            continue
        why = s["review_reason"] or s["scan_reason"] or s["status"]
        notify.send("CloudTrio: a big story the newsroom passed over",
                    f"{s['title']}\n\n{len(s['outlets'])} outlets carry it: {', '.join(s['outlets'][:6])}.\nThe newsroom skipped it: {why}.\n\n"
                    "Tap Make a post to post it anyway. The reviewer learns from it.", markup=notify.story_buttons(s["id"]))  # fmt: skip
        out.append(s["title"])
    return out


def health() -> dict[str, Any]:
    """Every check, each one quiet until it finds something (and then said at most every few hours). Never raises."""
    if settings.IG_DELIVERY != "instagram":  # a dry run (staging) alarms nobody
        return {"state": "skipped"}
    found: dict[str, Any] = {}
    for name, check in (("queue", _queue), ("quiet", _quiet), ("ai", _ai), ("models", _models), ("sources", _sources), ("missed", _missed)):
        try:
            if note := check():
                found[name] = note
        except Exception:  # noqa: BLE001 - one broken check must not stop the others
            logger.exception("watchdog.%s_failed", name)
    if found:
        logger.warning("watchdog.health %s", found)
    return found
