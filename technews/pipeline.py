"""One cycle of the newsroom: scan -> review -> write -> check -> card -> post.

    scan()       the News Scanner agent reads every feed, keeps the tech stories, scores them
    review()     the Reviewer agent approves only the top candidates
    post_next()  the best approved story is written, checked by the reviewer, rendered and
                 posted, if the pacing allows (daily cap, gap between posts, Instagram pause)

``run_cycle`` runs all three under a lock, so the scheduler and a manual trigger never
overlap.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from technews import card, db, instagram, sources
from technews.agents import reviewer, scanner, writer
from technews.config import settings

logger = logging.getLogger(__name__)

LOCK = threading.Lock()
LAST_POST_KEY = "pipeline:last_post_at"
LAST_CYCLE_KEY = "pipeline:last_cycle"
LAST_ERROR_KEY = "pipeline:last_error"


def _cutoff(hours: float) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours)).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- scan


def scan() -> dict[str, int]:
    """Fetch, group, drop what is already stored, and have the scanner score the rest."""
    items = sources.fetch_all(timedelta(hours=settings.SCAN_MAX_AGE_HOURS))
    stories = sources.group(items)
    known = db.rows("SELECT id, key, title, titles, outlets FROM stories WHERE found_at >= ?", _cutoff(48))
    fresh: list[sources.Story] = []
    for story in stories:
        match = next((k for k in known if k["key"] == story.key or any(
            sources.same_story(t, title) for t in story.titles for title in [k["title"], *k["titles"][:10]])), None)  # fmt: skip
        if match is None:
            fresh.append(story)
            continue
        # A story we already have, now told by more outlets: more buzz for the reviewer.
        outlets = list(dict.fromkeys([*match["outlets"], *story.outlets]))
        if len(outlets) > len(match["outlets"]):
            titles = list(dict.fromkeys([*match["titles"], *story.titles]))[:20]
            db.update_story(match["id"], outlets=json.dumps(outlets), titles=json.dumps(titles))
    scans = scanner.scan(fresh)
    counts = {"items": len(items), "stories": len(stories), "new": len(fresh), "candidates": 0}
    for story, s in zip(fresh, scans, strict=True):
        status = "not_tech" if not s.tech else "low" if s.score < settings.SCAN_MIN_SCORE else "new"
        counts["candidates"] += status == "new"
        db.execute(
            "INSERT OR IGNORE INTO stories (key, title, url, source, summary, outlets, titles, published_at, "
            "found_at, category, scan_score, scan_reason, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            story.key, story.title, story.url, story.source, story.summary, json.dumps(story.outlets),
            json.dumps(story.titles[:20]), story.published_at.isoformat(timespec="seconds"), db.now(),
            s.category, s.score, s.why, status,
        )  # fmt: skip
    logger.info("pipeline.scan %s", counts)
    return counts


# --------------------------------------------------------------------------- review


def review() -> dict[str, int]:
    """The reviewer reads the fresh candidates side by side and approves the top ones."""
    candidates = db.rows(
        "SELECT * FROM stories WHERE status = 'new' AND published_at >= ? ORDER BY scan_score DESC LIMIT 30",
        _cutoff(settings.SCAN_MAX_AGE_HOURS),
    )
    reviews = reviewer.select(candidates, min_score=settings.REVIEW_MIN_SCORE)
    for r in reviews:
        db.update_story(r.story_id, status="approved" if r.approve else "rejected",
                        review_score=r.score, review_reason=r.reason, angle=r.angle)  # fmt: skip
    counts = {"reviewed": len(reviews), "approved": sum(r.approve for r in reviews)}
    logger.info("pipeline.review %s", counts)
    return counts


# --------------------------------------------------------------------------- post


def posts_today() -> int:
    tz = ZoneInfo(settings.TIMEZONE)
    midnight = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    found = db.rows("SELECT COUNT(*) AS n FROM posts WHERE status = 'posted' AND created_at >= ?",
                    midnight.isoformat(timespec="seconds"))  # fmt: skip
    return int(found[0]["n"])


def pacing() -> tuple[bool, str]:
    """Whether a post may go out now, and why not."""
    if (left := instagram.paused()) > 0:
        return False, f"Instagram pause, {left / 60:.0f} min left"
    if posts_today() >= settings.POSTS_PER_DAY:
        return False, f"daily cap of {settings.POSTS_PER_DAY} reached"
    last = db.kv_get(LAST_POST_KEY)
    if last:
        wait = datetime.fromisoformat(last) + timedelta(minutes=settings.MIN_POST_GAP_MINUTES) - datetime.now(UTC)
        if wait.total_seconds() > 0:
            return False, f"next post in {wait.total_seconds() / 60:.0f} min (gap)"
    return True, "ok"


def post_next() -> dict[str, Any]:
    ok, why = pacing()
    if not ok:
        return {"posted": False, "reason": why}
    approved = db.rows(
        "SELECT id, title FROM stories WHERE status = 'approved' AND published_at >= ? "
        "ORDER BY review_score DESC, published_at DESC",
        _cutoff(settings.SCAN_MAX_AGE_HOURS),
    )
    posted = [r["title"] for r in db.rows("SELECT title FROM stories WHERE status = 'posted' AND found_at >= ?", _cutoff(48))]
    for story in approved:
        # One subject told in different words (GMO "warns users" / "refuses demands") is
        # posted once: the page already covered it.
        if any(sources.same_subject(story["title"], p) or sources.same_story(story["title"], p) for p in posted):
            db.update_story(story["id"], status="rejected", review_reason="already posted this subject")
            continue
        return publish_story(story["id"])
    return {"posted": False, "reason": "no approved story waiting"}


def publish_story(story_id: int) -> dict[str, Any]:
    """Write, check, render and post one story (the pacing is the caller's)."""
    story = db.story(story_id)
    if story is None:
        return {"posted": False, "reason": f"no story {story_id}"}
    if story["status"] == "posted":
        return {"posted": False, "reason": "already posted"}
    draft = writer.write(story, angle=story["angle"])
    image = card.render(story_id=story_id, category=story["category"], headline=draft.headline,
                        points=draft.points, source=story["source"])  # fmt: skip
    caption = draft.full_caption()
    try:
        result = instagram.publish_photo(image, caption)
    except instagram.InstagramRestrictedError as exc:
        _record_post(story_id, draft, image, caption, status="failed", error=str(exc))
        db.update_story(story_id, status="failed")
        return {"posted": False, "reason": str(exc)}
    except instagram.InstagramPublishError as exc:
        # A login / session problem: the story stays approved for the next cycle.
        db.kv_set(LAST_ERROR_KEY, f"{db.now()} {exc}")
        logger.error("pipeline.publish_failed story=%s error=%s", story_id, exc)
        return {"posted": False, "reason": str(exc)}
    _record_post(story_id, draft, image, caption, status="posted", result=result)
    db.update_story(story_id, status="posted")
    db.kv_set(LAST_POST_KEY, datetime.now(UTC).isoformat())
    logger.info("pipeline.posted story=%s via=%s writer=%s", story_id, result["delivered"], draft.writer)
    return {"posted": True, "story_id": story_id, "writer": draft.writer, **result}


def _record_post(story_id: int, draft: writer.Draft, image: Any, caption: str, *, status: str,
                 result: dict[str, str] | None = None, error: str = "") -> None:  # fmt: skip
    result = result or {}
    db.execute(
        "INSERT INTO posts (story_id, headline, caption, image_path, writer, delivered, media_id, permalink, "
        "status, error, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        story_id, draft.headline, caption, str(image), draft.writer, result.get("delivered", settings.IG_DELIVERY),
        result.get("media_id", ""), result.get("permalink", ""), status, error, db.now(),
    )  # fmt: skip


# --------------------------------------------------------------------------- cycle


def run_cycle(*, post: bool = True) -> dict[str, Any]:
    if not LOCK.acquire(blocking=False):
        return {"skipped": "a cycle is already running"}
    try:
        summary: dict[str, Any] = {"started_at": db.now()}
        try:
            summary["scan"] = scan()
            summary["review"] = review()
            if post:
                summary["post"] = post_next()
        except Exception as exc:  # noqa: BLE001 - the scheduler must survive any one cycle
            logger.exception("pipeline.cycle_failed")
            summary["error"] = f"{type(exc).__name__}: {exc}"
            db.kv_set(LAST_ERROR_KEY, f"{db.now()} {summary['error']}")
        summary["finished_at"] = db.now()
        db.kv_set(LAST_CYCLE_KEY, json.dumps(summary))
        return summary
    finally:
        LOCK.release()
