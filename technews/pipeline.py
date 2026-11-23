"""One cycle of the newsroom: scan -> review -> write -> check -> card -> post.

    scan()       the News Scanner agent reads every feed, keeps the tech stories, scores them
    review()     the Reviewer agent approves only the top candidates
    post_next()  the best approved story is written, checked by the reviewer, rendered and
                 posted, if the pacing allows (daily cap, gap between posts, Instagram pause)

``run_cycle`` runs all three under a lock, so the scheduler and a manual trigger never
overlap.
"""

from __future__ import annotations

import io
import json
import logging
import random
import shutil
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo

from technews import brands, card, db, flyers, instagram, linkedin, motion, notify, rules, sources
from technews.agents import designer, editor, reviewer, scanner, team, writer
from technews.config import settings

logger = logging.getLogger(__name__)

LOCK = threading.Lock()
LAST_POST_KEY = "pipeline:last_post_at"
LAST_CYCLE_KEY = "pipeline:last_cycle"
LAST_ERROR_KEY = "pipeline:last_error"
CYCLE_LEASE_KEY = "pipeline:cycle_lease"


def _cutoff(hours: float) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours)).isoformat(timespec="seconds")


EVENT_HOURS: Final = 36  # reports of one incident (company + outage / breach / exploit) this close together are one story
#: Reasons a story is never reopened, however many outlets pick it up.
FINAL_REASONS: Final = ("rejected by the editor", "already posted", "brand rules", "draft expired")


def _another_look(story: dict[str, Any], before: int, now: int) -> bool:
    """Whether a story turned down earlier deserves a second review now that more outlets carry it: the first
    confirmation of a "single unverified source", and again at 3 and at 6 outlets."""
    if story["status"] not in ("rejected", "low") or str(story["review_reason"] or "").startswith(FINAL_REASONS):
        return False
    if story["review_reason"] == "single unverified source" and before < 2 <= now:
        return True
    return before < 3 <= now or before < 6 <= now


# --------------------------------------------------------------------------- scan


def scan() -> dict[str, int]:
    """Fetch, group, drop what is already stored, and have the scanner score the rest."""
    items = sources.fetch_all(timedelta(hours=settings.SCAN_MAX_AGE_HOURS))
    stories = sources.group(items)
    known = db.rows("SELECT id, key, title, titles, outlets, status, review_reason, published_at FROM stories WHERE found_at >= ?", _cutoff(48))
    fresh: list[sources.Story] = []
    for story in stories:
        match = next((k for k in known if k["key"] == story.key or any(
            sources.same_story(t, title) for t in story.titles for title in [k["title"], *k["titles"][:10]])), None)  # fmt: skip
        if match is None:  # the same incident told in other words ("Outlook down" / "Microsoft outage"), close in time
            near = _cutoff(EVENT_HOURS)
            match = next((k for k in known if k["published_at"] >= near and any(
                sources.same_event(t, title) for t in story.titles for title in [k["title"], *k["titles"][:10]])), None)  # fmt: skip
        if match is None:
            fresh.append(story)
            continue
        # A story we already have, now told by more outlets: more buzz for the reviewer.
        outlets = list(dict.fromkeys([*match["outlets"], *story.outlets]))
        if len(outlets) > len(match["outlets"]):
            titles = list(dict.fromkeys([*match["titles"], *story.titles]))[:20]
            changes: dict[str, Any] = {"outlets": json.dumps(outlets), "titles": json.dumps(titles)}
            if _another_look(match, len(match["outlets"]), len(outlets)):
                # turned down when few had it; now many do, so the reviewer reads it again, as current news
                changes |= {"status": "new", "review_reason": "", "published_at": story.published_at.isoformat(timespec="seconds")}
                logger.info("pipeline.rereview story=%s outlets=%d", match["id"], len(outlets))
            db.update_story(match["id"], **changes)
            match["outlets"], match["titles"] = outlets, titles
    scans: list[Any] = []
    if fresh:
        with team.run("scanner") as info:
            scans = scanner.scan(fresh)
            info["summary"] = f"Scored {len(fresh)} new stories, {sum(1 for s in scans if s.tech)} about tech"
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
        "SELECT * FROM stories WHERE status = 'new' AND published_at >= ? "
        "ORDER BY category IN ('cloud', 'devops', 'ai') DESC, scan_score DESC LIMIT 30",
        _cutoff(settings.SCAN_MAX_AGE_HOURS),
    )
    reviews: list[Any] = []
    if candidates:
        with team.run("reviewer") as info:
            reviews = reviewer.select(candidates, min_score=settings.REVIEW_MIN_SCORE)
            info["summary"] = f"Read {len(candidates)} candidates, approved {sum(r.approve for r in reviews)}"
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
    found = db.rows("SELECT COUNT(*) AS n FROM posts WHERE status = 'posted' AND created_at >= ? AND brand_id = ?",
                    midnight.isoformat(timespec="seconds"), settings.BRAND_ID)  # fmt: skip
    return int(found[0]["n"])


def pacing(*, breaking: bool = False) -> tuple[bool, str]:
    """Whether a post may go out now, and why not. Breaking news has a little room past the daily cap and a short gap."""
    if (left := instagram.paused()) > 0:
        return False, f"Instagram pause, {left / 60:.0f} min left"
    if posts_today() >= settings.POSTS_PER_DAY + (BREAKING_EXTRA if breaking else 0):
        return False, f"daily cap of {settings.POSTS_PER_DAY} reached"
    last = db.kv_get(LAST_POST_KEY + brands.suffix())
    if last:
        gap = min(BREAKING_GAP_MINUTES, settings.MIN_POST_GAP_MINUTES) if breaking else settings.MIN_POST_GAP_MINUTES
        wait = datetime.fromisoformat(last) + timedelta(minutes=gap) - datetime.now(UTC)
        if wait.total_seconds() > 0:
            return False, f"next post in {wait.total_seconds() / 60:.0f} min (gap)"
    return True, "ok"


def next_is_flyer() -> bool:
    """A flyer goes out once FLYER_AFTER_NEWS news posts have gone since the last one."""
    if not settings.FLYERS_ENABLED:
        return False
    recent = db.rows("SELECT flyer FROM posts WHERE status IN ('posted', 'pending') AND brand_id = ? ORDER BY id DESC LIMIT ?",
                     settings.BRAND_ID, settings.FLYER_AFTER_NEWS)
    return (len(recent) == settings.FLYER_AFTER_NEWS and not any(r["flyer"] for r in recent)
            and bool(flyers.listing(enabled_only=True)))


def pick_flyer() -> flyers.Flyer:
    """The flyer posted least recently (one never posted comes first), so they rotate."""
    last = {r["flyer"]: r["id"] for r in db.rows("SELECT flyer, MAX(id) AS id FROM posts WHERE flyer != '' AND brand_id = ? GROUP BY flyer", settings.BRAND_ID)}
    return min(flyers.listing(enabled_only=True), key=lambda f: last.get(f.id, 0))


def publish_flyer(flyer: flyers.Flyer | None = None) -> dict[str, Any]:
    flyer = flyer or pick_flyer()
    image = flyers.render(flyer)
    caption = f"{flyer.caption}\n\n{flyers.hashtags()}"
    try:
        result = instagram.publish_photo(image, caption)
    except instagram.InstagramRestrictedError as exc:
        _record_flyer(flyer, image, caption, status="failed", error=str(exc))
        return {"posted": False, "reason": str(exc)}
    except instagram.InstagramPublishError as exc:
        db.kv_set(LAST_ERROR_KEY, f"{db.now()} {exc}")
        logger.error("pipeline.flyer_failed flyer=%s error=%s", flyer.id, exc)
        return {"posted": False, "reason": str(exc)}
    shared = _share_on_linkedin(flyer, image, caption)
    _record_flyer(flyer, image, caption, status="posted", result=result, linkedin=shared)
    db.kv_set(LAST_POST_KEY + brands.suffix(), datetime.now(UTC).isoformat())
    logger.info("pipeline.posted flyer=%s via=%s linkedin=%s", flyer.id, result["delivered"], shared or "off")
    return {"posted": True, "flyer": flyer.id, "linkedin": shared, **result}


def _share_on_linkedin(flyer: flyers.Flyer, image: Any, caption: str) -> str:
    """The flyer goes to LinkedIn too. A LinkedIn problem never undoes the Instagram post."""
    if not settings.LINKEDIN_ENABLED:
        return ""
    try:
        result = linkedin.publish_photo(image, caption, alt=f"{flyer.kicker}: {flyer.headline}")
    except linkedin.LinkedInError as exc:
        db.kv_set(LAST_ERROR_KEY, f"{db.now()} LinkedIn: {exc}")
        logger.error("pipeline.linkedin_failed flyer=%s error=%s", flyer.id, exc)
        return f"failed: {exc}"[:300]
    return result["permalink"] or result["delivered"]


def _record_flyer(flyer: flyers.Flyer, image: Any, caption: str, *, status: str,
                  result: dict[str, str] | None = None, error: str = "", linkedin: str = "") -> None:  # fmt: skip
    result = result or {}
    db.execute(
        "INSERT INTO posts (story_id, headline, caption, image_path, writer, delivered, media_id, permalink, "
        "status, error, created_at, flyer, linkedin, brand_id) VALUES (0,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        flyer.headline, caption, str(image), "flyer", result.get("delivered", settings.IG_DELIVERY),
        result.get("media_id", ""), result.get("permalink", ""), status, error, db.now(), flyer.id, linkedin, settings.BRAND_ID,
    )  # fmt: skip


# --------------------------------------------------------------------------- approval


def needs_approval(*, flyer: bool) -> bool:
    return settings.APPROVAL_MODE == "all" or (settings.APPROVAL_MODE == "flyers" and flyer)


def pending_count(*, flyer: bool | None = None) -> int:
    """Drafts waiting for approval: all of them, or only flyers (``flyer=True``) or only news (``flyer=False``)."""
    kind = "" if flyer is None else " AND flyer != ''" if flyer else " AND flyer = ''"
    return int(db.rows(f"SELECT COUNT(*) AS n FROM posts WHERE status = 'pending' AND brand_id = ?{kind}", settings.BRAND_ID)[0]["n"])  # noqa: S608 - fixed text


def expire_stale_drafts() -> list[str]:
    """Withdraw news drafts that have waited longer than NEWS_DRAFT_MAX_HOURS: the news is old by then, and a full
    queue would stop every newer story. Flyers and reposts wait as long as you like. Returns the withdrawn headlines."""
    hours = settings.NEWS_DRAFT_MAX_HOURS
    if hours <= 0:
        return []
    gone: list[str] = []
    with APPROVE_LOCK:
        for p in db.rows("SELECT * FROM posts WHERE status = 'pending' AND flyer = '' AND story_id > 0 AND created_at < ? AND brand_id = ?",
                         _cutoff(hours), settings.BRAND_ID):  # fmt: skip
            if draft_meta(p).get("repost_of"):
                continue
            db.execute("UPDATE posts SET status = 'expired', error = ? WHERE id = ? AND status = 'pending'",
                       f"Withdrawn: it waited over {hours} hours for approval, so the news was no longer fresh.", p["id"])  # fmt: skip
            db.update_story(p["story_id"], status="rejected", review_reason="draft expired")
            gone.append(p["headline"])
    if gone:
        logger.info("pipeline.drafts_expired count=%d", len(gone))
        notify.send(notify.title("old news drafts withdrawn"),
                    f"{len(gone)} news draft(s) waited over {hours} hours for approval and were withdrawn, so newer stories can come "
                    "through:\n- " + "\n- ".join(gone[:8]))  # fmt: skip
    return gone


def _insert_draft(story_id: int, headline: str, caption: str, image: Any, writer_name: str, flyer_id: str = "", meta: str = "",
                  *, breaking: bool = False, for_reel: bool = False) -> int:  # fmt: skip
    """Save a waiting post and tell the editor. ``for_reel``: the caller makes the Reel and sends the one alert itself."""
    post_id = _save_draft(story_id, headline, caption, image, writer_name, flyer_id, meta)
    if for_reel:
        return post_id
    media, kind = Path(str(image)), "flyer" if flyer_id else "breaking" if breaking else "news"
    if reel_wanted():
        out = make_reel(post_id)
        if out["ok"]:
            media, kind = settings.CARDS_DIR / out["video"], f"animated {kind}"
    notify.draft_ready(headline, kind, media, post_id=post_id)  # in the background
    return post_id


# --------------------------------------------------------------------------- animated posts (Reels)


def reel_of(post: dict[str, Any]) -> dict[str, Any]:
    reel = draft_meta(post).get("reel")
    return reel if isinstance(reel, dict) and reel.get("video") else {}


def reel_wanted() -> bool:
    """Whether the next draft should be a Reel: up to REELS_PER_WEEK in 7 days, at most one a day."""
    if settings.REELS_PER_WEEK <= 0 or not motion.available():
        return False
    week = (datetime.now(UTC) - timedelta(days=7)).isoformat(timespec="seconds")
    day = (datetime.now(UTC) - timedelta(hours=20)).isoformat(timespec="seconds")
    recent = db.rows("SELECT created_at FROM posts WHERE brand_id = ? AND created_at >= ? AND status IN ('pending', 'scheduled', 'posted') "
                     "AND meta LIKE ?", settings.BRAND_ID, week, '%"reel": {%')  # fmt: skip
    return len(recent) < settings.REELS_PER_WEEK and not any(r["created_at"] >= day for r in recent)


def _draft_flyer(post: dict[str, Any]) -> flyers.Flyer | None:
    """The flyer a draft was made from, as it stands in the draft (with any edits)."""
    snap = draft_meta(post).get("flyer")
    if isinstance(snap, dict):
        try:
            return flyers.parse({**snap, "id": post["flyer"], "caption": post["caption"] or "-"})
        except ValueError:
            pass
    return flyers.get(post["flyer"])


#: animated-text presets (kinetic typography), best for the lively "words move" look
TEXT_PRESETS: Final = ("bold", "neon", "magazine", "impact", "corporate", "tech", "offer", "story", "data", "minimal",
                       "bounce", "spring", "scale", "elegant", "pulse", "matrix")
#: colour themes to rotate for news reels, so consecutive ones look different
NEWS_THEMES: Final = ("classic", "aurora", "stage", "neon", "cosmic", "gilded", "retro", "blueprint", "horizon", "impact")


def _recent_reel(field: str, limit: int = 5) -> list[str]:
    """What the last few reels used for ``field`` (style/theme), newest first — to avoid repeats."""
    seen: list[str] = []
    for r in db.rows("SELECT meta FROM posts WHERE brand_id = ? AND meta LIKE '%\"reel\": {%' ORDER BY id DESC LIMIT ?",
                     settings.BRAND_ID, limit):
        try:
            v = (json.loads(r["meta"]).get("reel") or {}).get(field)
        except (ValueError, TypeError):
            v = None
        if v:
            seen.append(str(v))
    return seen


def _rotate(options: tuple[str, ...], avoid: list[str]) -> str:
    """Pick an option not among the recently-used ones (random, for variety)."""
    fresh = [o for o in options if o not in avoid] or list(options)
    return random.choice(fresh)


def _news_flyer(post: dict[str, Any], meta: dict[str, Any], theme: str) -> flyers.Flyer:
    """A synthetic flyer built from a news draft, so the animated-text engine can animate the story."""
    story = db.story(post["story_id"]) or {}
    category = meta.get("category") or story.get("category", "other")
    source = meta.get("source") or story.get("source", "")
    points = tuple(str(p) for p in (meta.get("points") or []) if str(p).strip())
    return flyers.Flyer(
        id=flyers.NEWS_ID, kicker=card.LABEL.get(category, "TECH NEWS"), headline=post["headline"],
        points=points or (post["headline"],), cta=settings.REEL_CTA or "Follow for more",
        note=f"Source: {source}" if source else "", caption=post["caption"],
        layout=theme if theme in flyers.LAYOUT_IDS else "classic",
    )  # fmt: skip


def make_reel(post_id: int, *, style: str = "", track: str = "", frame: str = "", theme: str = "",
              highlight: list[str] | None = None, number: str | None = None, badge: str | None = None) -> dict[str, Any]:  # fmt: skip
    """Turn a waiting post into a Reel, or redo its Reel.

    News and flyers both get the lively animated-text look by default; a *simple* movement
    (slide/zoom/…) is used only when one is explicitly chosen. The animation preset and colour
    theme rotate so consecutive reels look different."""
    found = db.rows("SELECT * FROM posts WHERE id = ? AND status = 'pending'", post_id)
    if not found:
        return {"ok": False, "reason": "that post is not waiting for approval"}
    post = found[0]
    is_flyer = bool(post["flyer"])
    meta = draft_meta(post)
    before = meta.get("reel") if isinstance(meta.get("reel"), dict) else {}
    wanted = settings.REEL_STYLE if settings.REEL_STYLE in motion.STYLE_IDS else ""
    style = style if style in motion.STYLE_IDS else (before.get("style") or wanted)
    track = track if track in motion.TRACK_IDS else (before.get("track") or settings.REEL_MUSIC)
    frame = frame if frame in motion.FRAME_IDS else (before.get("frame") or "default")
    theme = theme if theme in flyers.LAYOUT_IDS else (before.get("theme") or "")
    highlight = [h.strip() for h in (highlight if highlight is not None else before.get("highlight") or []) if str(h).strip()][:6]
    number = (number if number is not None else before.get("number") or "").strip()[:30]
    badge = (badge if badge is not None else before.get("badge") or "").strip()[:40]
    # no explicit animation chosen → rotate a fresh animated-text preset for variety
    if not style:
        style = _rotate(TEXT_PRESETS, _recent_reel("style"))
    out = settings.CARDS_DIR / f"reel-{post_id}-{int(time.time() * 1000)}.mp4"
    try:
        if style in motion.SIMPLE_IDS:  # a simple card movement, only when explicitly picked
            image = settings.CARDS_DIR / Path(str(post["image_path"]).replace("\\", "/")).name
            text = " ".join([post["headline"], *((meta.get("flyer") or {}).get("points") or [])])
            video, cover = motion.render(image, out, style=style, track=track, frame=frame,
                                         headline=text if style == "countup" else post["headline"])
        else:  # animated-text (kinetic typography) — flyers and news alike
            if is_flyer:
                flyer = _draft_flyer(post)
                if flyer is None:
                    return {"ok": False, "reason": "The flyer of this draft no longer exists."}
                if theme:  # a colour chosen in the UI overrides the flyer's own palette for the reel
                    flyer = replace(flyer, layout=theme, style={})
            else:
                theme = theme or _rotate(NEWS_THEMES, _recent_reel("theme"))
                flyer = _news_flyer(post, meta, theme)
            video, cover, style = motion.render_flyer(flyer, out, preset=style or "auto", track=track, frame=frame,
                                                       highlight=highlight, number=number, badge=badge)
    except (motion.MotionError, OSError) as exc:
        logger.warning("pipeline.reel_failed post=%s error=%s", post_id, str(exc)[:200])
        return {"ok": False, "reason": str(exc)[:300]}
    meta["reel"] = {"style": style, "track": track, "frame": frame, "theme": theme, "video": video.name, "cover": cover.name,
                    "highlight": highlight, "number": number, "badge": badge}
    db.execute("UPDATE posts SET meta = ? WHERE id = ?", json.dumps(meta), post_id)
    return {"ok": True, "post_id": post_id, **meta["reel"]}


def drop_reel(post_id: int) -> dict[str, Any]:
    found = db.rows("SELECT * FROM posts WHERE id = ? AND status = 'pending'", post_id)
    if not found:
        return {"ok": False, "reason": "that post is not waiting for approval"}
    meta = draft_meta(found[0])
    meta.pop("reel", None)
    db.execute("UPDATE posts SET meta = ? WHERE id = ?", json.dumps(meta), post_id)
    return {"ok": True, "post_id": post_id}


def set_reel(post_id: int, on: bool, *, style: str = "", track: str = "", frame: str = "", theme: str = "",
             highlight: list[str] | None = None, number: str | None = None, badge: str | None = None) -> dict[str, Any]:  # fmt: skip
    with _as_post_brand(post_id):
        return make_reel(post_id, style=style, track=track, frame=frame, theme=theme, highlight=highlight, number=number, badge=badge) if on else drop_reel(post_id)


def _publish_ig(post: dict[str, Any], image: Path, text: str) -> dict[str, str]:
    """A draft goes to Instagram: as a Reel when it was made into one, else as a picture."""
    reel = reel_of(post)
    if reel and (settings.CARDS_DIR / reel["video"]).exists():
        return instagram.publish_reel(settings.CARDS_DIR / reel["video"], settings.CARDS_DIR / reel["cover"], text)
    return instagram.publish_photo(image, text)


def _save_draft(story_id: int, headline: str, caption: str, image: Any, writer_name: str, flyer_id: str, meta: str = "") -> int:
    return db.execute(
        "INSERT INTO posts (story_id, headline, caption, image_path, writer, delivered, status, created_at, flyer, meta, brand_id) "
        "VALUES (?,?,?,?,?,?,'pending',?,?,?,?)",
        story_id, headline, caption, str(image), writer_name, settings.IG_DELIVERY, db.now(), flyer_id, meta, settings.BRAND_ID,
    )  # fmt: skip


def draft_story(story_id: int, *, breaking: bool = False) -> dict[str, Any]:
    """Write and render a story, then hold it for approval (flagged as breaking news when it is)."""
    story = db.story(story_id)
    if story is None or story["status"] != "approved":
        return {"posted": False, "reason": "story is not approved"}
    draft = writer.write(story, angle=story["angle"])
    layout = designer.choose(story, draft.headline, draft.points)
    image = _render_news(story_id, story, draft, layout["layout"])
    warnings = editor.review(draft.headline, draft.points, draft.full_caption(), story) if team.enabled("editor") else rules.warnings(draft.headline, draft.points, story)
    meta = json.dumps({"points": draft.points, "category": story["category"], "source": story["source"], "warnings": warnings,
                       "layout": layout["layout"], "designer": layout["reason"], **({"breaking": True} if breaking else {})})  # fmt: skip
    post_id = _insert_draft(story_id, draft.headline, draft.full_caption(), image, draft.writer, meta=meta, breaking=breaking)
    db.update_story(story_id, status="pending")
    logger.info("pipeline.drafted story=%s post=%s", story_id, post_id)
    return {"posted": False, "pending": True, "post_id": post_id, "reason": "waiting for approval"}


def _render_news(story_id: int, story: dict[str, Any], draft: writer.Draft, layout: str) -> Any:
    """The news card: in the designer's layout, or the original card when there is none."""
    if layout:
        settings.CARDS_DIR.mkdir(parents=True, exist_ok=True)
        return flyers.render_news(layout, label=card.LABEL.get(story["category"], "TECH NEWS"), headline=draft.headline, points=draft.points,
                                  source=story["source"], path=settings.CARDS_DIR / f"story-{story_id}.jpg")  # fmt: skip
    return card.render(story_id=story_id, category=story["category"], headline=draft.headline, points=draft.points, source=story["source"])


def draft_video(video_id: int, mp4: Path, cover: Path) -> int:
    """A business's own video, made into a Reel (``mp4`` with ``cover``), held for approval. Returns the post id."""
    from technews import videos

    row = videos.get(video_id)
    opts = videos.Options.from_dict(json.loads(row["options"] or "{}"))
    headline = (opts.title_text or opts.about or Path(row["original_name"]).stem or "Our video").strip()[:120]
    caption = f"{opts.about.strip() or headline}\n\n{flyers.hashtags()}"
    meta = json.dumps({"reel": {"video": mp4.name, "cover": cover.name, "style": "upload"}, "video_id": video_id})
    post_id = _save_draft(0, headline, caption, cover, "video", "", meta)
    db.execute("UPDATE videos SET post_id = ? WHERE id = ?", post_id, video_id)
    notify.draft_ready(headline, "video reel", mp4, post_id=post_id)
    return post_id


def draft_flyer(flyer: flyers.Flyer | None = None, *, tags: str | None = None, for_reel: bool = False) -> dict[str, Any]:
    flyer = flyer or pick_flyer()
    caption = f"{flyer.caption}\n\n{flyers.hashtags() if tags is None else tags}"
    post_id = _insert_draft(0, flyer.headline, caption, flyers.render(flyer), "flyer", flyer.id, json.dumps({"flyer": snapshot(flyer)}), for_reel=for_reel)
    logger.info("pipeline.drafted flyer=%s post=%s", flyer.id, post_id)
    return {"posted": False, "pending": True, "post_id": post_id, "reason": "waiting for approval"}


def snapshot(flyer: flyers.Flyer) -> dict[str, Any]:
    """What a flyer draft was built from, so the editor can reopen it exactly as it is."""
    return {"kicker": flyer.kicker, "headline": flyer.headline, "points": list(flyer.points), "cta": flyer.cta,
            "note": flyer.note, "layout": flyer.layout, "style": flyer.style}  # fmt: skip


def draft_meta(post: dict[str, Any]) -> dict[str, Any]:
    try:
        meta = json.loads(post.get("meta") or "{}")
    except ValueError:
        return {}
    return meta if isinstance(meta, dict) else {}


def _news_fields(data: dict[str, Any]) -> tuple[str, list[str], str]:
    """The card's headline, points and tag, checked. Raises ValueError with what to fix."""
    headline = str(data.get("headline") or "").strip()
    if not headline:
        raise ValueError("Headline is required")
    if len(headline) > 120:
        raise ValueError("Headline is too long (max 120 characters)")
    points = data.get("points") or []
    if isinstance(points, str):
        points = points.splitlines()
    points = [str(p).strip() for p in points if str(p).strip()]
    if not 1 <= len(points) <= 3:
        raise ValueError("Add 1 to 3 key points, one per line")
    if any(len(p) > 140 for p in points):
        raise ValueError("Each point must be under 140 characters")
    category = str(data.get("category") or "other")
    if category not in card.LABEL:
        raise ValueError("Pick one of the listed tags")
    return headline, points, category


def edit_draft(post_id: int, data: dict[str, Any], *, preview: bool = False) -> dict[str, Any]:
    with _as_post_brand(post_id):
        return _edit_draft(post_id, data, preview=preview)


def _edit_draft(post_id: int, data: dict[str, Any], *, preview: bool = False) -> dict[str, Any]:
    """Change the design of a post that is waiting for approval: a flyer's layout, colours and words, or a
    news card's headline, points and tag. ``preview`` draws it without saving. Raises ValueError for bad input."""
    found = db.rows("SELECT * FROM posts WHERE id = ? AND status = 'pending'", post_id)
    if not found:
        return {"ok": False, "reason": "that post is not waiting for approval"}
    post = found[0]
    stamp = int(time.time() * 1000)  # a new file name each time, so nobody is shown the old picture
    buf = io.BytesIO() if preview else None
    if post["flyer"]:
        flyer = flyers.parse({**data, "id": post["flyer"], "caption": data.get("caption") or post["caption"] or "-"})
        target = buf or settings.CARDS_DIR / f"flyer-draft-{post_id}-{stamp}.jpg"
        settings.CARDS_DIR.mkdir(parents=True, exist_ok=True)
        flyers.render(flyer, target)
        headline, meta = flyer.headline, {"flyer": snapshot(flyer)}
    else:
        headline, points, category = _news_fields(data)
        story = db.story(post["story_id"]) or {}
        source = draft_meta(post).get("source") or story.get("source", "")
        settings.CARDS_DIR.mkdir(parents=True, exist_ok=True)
        target = buf or settings.CARDS_DIR / f"story-{post['story_id']}-{stamp}.jpg"
        layout = str(data.get("layout") or "")
        if layout and layout not in flyers.LAYOUT_IDS:
            raise ValueError("Pick one of the listed layouts")
        if layout:
            flyers.render_news(layout, label=card.LABEL[category], headline=headline, points=points, source=source, path=target)
        else:
            card.render(story_id=post["story_id"], category=category, headline=headline, points=points, source=source, path=target)
        meta = {"points": points, "category": category, "source": source, "layout": layout, "warnings": draft_meta(post).get("warnings", []),
                "designer": draft_meta(post).get("designer", "")}  # fmt: skip
    if buf is not None:
        return {"ok": True, "jpeg": buf.getvalue()}
    before = draft_meta(post)
    meta.update({k: before[k] for k in ("repost_of", "replaces", "reel") if k in before})
    db.execute("UPDATE posts SET headline = ?, image_path = ?, meta = ? WHERE id = ?", headline, str(target), json.dumps(meta), post_id)
    if before.get("reel"):
        make_reel(post_id)
    if not post["flyer"]:
        team.feedback("edited", post_id, headline_before=post["headline"], headline_after=headline, points_changed=before.get("points") != meta.get("points"),
                      category=meta.get("category", ""))  # fmt: skip
        if (before.get("layout") or "") != (meta.get("layout") or ""):
            team.feedback("layout_changed", post_id, **{"from": before.get("layout", ""), "to": meta.get("layout", ""), "category": meta.get("category", "")})
    return {"ok": True, "image": Path(str(target)).name, "headline": headline, "meta": meta}


def rewrite_draft(post_id: int) -> dict[str, Any]:
    with _as_post_brand(post_id):
        return _rewrite_draft(post_id)


def _rewrite_draft(post_id: int) -> dict[str, Any]:
    """A fresh headline, points and caption for a news draft, from the writer. Nothing is saved."""
    found = db.rows("SELECT * FROM posts WHERE id = ? AND status = 'pending'", post_id)
    story = db.story(found[0]["story_id"]) if found and found[0]["story_id"] else None
    if story is None:
        return {"ok": False, "reason": "only a news post can be rewritten"}
    draft = writer.write(story, angle=story["angle"])
    return {"ok": True, "headline": draft.headline, "points": draft.points, "caption": draft.full_caption()}


APPROVE_LOCK = threading.Lock()


def _as_post_brand(post_id: int) -> Any:
    """Run as the brand a post belongs to, whichever brand the dashboard is showing."""
    found = db.rows("SELECT brand_id FROM posts WHERE id = ?", post_id)
    return brands.use(found[0]["brand_id"] if found else brands.DEFAULT_ID)


def approve(post_id: int, caption: str | None = None) -> dict[str, Any]:
    with _as_post_brand(post_id):
        return _approve(post_id, caption)


def _remember(kind: str, post: dict[str, Any], **extra: Any) -> None:
    meta = draft_meta(post)
    team.feedback(kind, post["id"], headline=post["headline"], category=meta.get("category", ""), layout=meta.get("layout", ""),
                  warnings=meta.get("warnings", []), **extra)  # fmt: skip


def _approve(post_id: int, caption: str | None = None) -> dict[str, Any]:
    """The editor's OK: post the draft to Instagram (and a flyer to LinkedIn) now. The gap and
    daily cap are skipped, as for any human override; Instagram's pause is not."""
    with APPROVE_LOCK:
        found = db.rows("SELECT * FROM posts WHERE id = ?", post_id)
        if not found or found[0]["status"] != "pending":
            return {"posted": False, "reason": "that post is not waiting for approval"}
        post = found[0]
        if (left := instagram.paused()) > 0:
            return {"posted": False, "reason": f"Instagram pause, {left / 60:.0f} min left"}
        text = (caption or post["caption"]).strip()
        _remember("approved", post, caption_edited=bool(caption and caption.strip() != post["caption"].strip()))
        image = settings.CARDS_DIR / Path(str(post["image_path"]).replace("\\", "/")).name  # moved machines keep working
        try:
            result = _publish_ig(post, image, text)
        except instagram.InstagramRestrictedError as exc:
            db.execute("UPDATE posts SET status = 'failed', error = ? WHERE id = ?", str(exc), post_id)
            if post["story_id"]:
                db.update_story(post["story_id"], status="failed")
            return {"posted": False, "reason": str(exc)}
        except instagram.InstagramPublishError as exc:  # a login problem: it stays waiting
            db.kv_set(LAST_ERROR_KEY, f"{db.now()} {exc}")
            return {"posted": False, "reason": str(exc)}
        flyer = flyers.get(post["flyer"])
        shared = _share_on_linkedin(flyer, image, text) if flyer else ""
        db.execute(
            "UPDATE posts SET status = 'posted', caption = ?, delivered = ?, media_id = ?, permalink = ?, "
            "linkedin = ?, created_at = ? WHERE id = ?",
            text, result["delivered"], result["media_id"], result["permalink"], shared, db.now(), post_id,
        )  # fmt: skip
        if post["story_id"]:
            db.update_story(post["story_id"], status="posted")
        db.kv_set(LAST_POST_KEY + brands.suffix(), datetime.now(UTC).isoformat())
        logger.info("pipeline.approved post=%s via=%s linkedin=%s", post_id, result["delivered"], shared or "off")
        out = {"posted": True, "post_id": post_id, "linkedin": shared, **result}
        if old := draft_meta(post).get("replaces"):
            gone = _take_down(int(old))
            out["replaced"] = gone if gone == "deleted" else f"the old post is still up: {gone}"
        return out


def reject(post_id: int) -> dict[str, Any]:
    with APPROVE_LOCK:
        found = db.rows("SELECT * FROM posts WHERE id = ? AND status = 'pending'", post_id)
        if not found:
            return {"posted": False, "reason": "that post is not waiting for approval"}
        db.execute("UPDATE posts SET status = 'rejected' WHERE id = ?", post_id)
        _remember("rejected", found[0])
        if found[0]["story_id"] and not draft_meta(found[0]).get("repost_of"):
            db.update_story(found[0]["story_id"], status="rejected", review_reason="rejected by the editor")
        return {"rejected": True, "post_id": post_id}


# --------------------------------------------------------------------------- after posting


def _posted(post_id: int) -> dict[str, Any] | None:
    found = db.rows("SELECT * FROM posts WHERE id = ? AND status = 'posted'", post_id)
    return found[0] if found else None


def edit_posted_caption(post_id: int, caption: str) -> dict[str, Any]:
    """Change the caption of a post that is already up (Instagram only; LinkedIn keeps its text)."""
    caption = caption.strip()
    if not caption:
        return {"ok": False, "reason": "The caption cannot be empty."}
    if len(caption) > 2200:
        return {"ok": False, "reason": "Instagram captions are at most 2,200 characters."}
    with _as_post_brand(post_id), APPROVE_LOCK:
        post = _posted(post_id)
        if not post:
            return {"ok": False, "reason": "Only a post that is up on Instagram can have its caption changed."}
        try:
            instagram.edit_caption(post["media_id"], caption)
        except instagram.InstagramPublishError as exc:
            return {"ok": False, "reason": str(exc)}
        db.execute("UPDATE posts SET caption = ? WHERE id = ?", caption, post_id)
        logger.info("pipeline.caption_changed post=%s", post_id)
        return {"ok": True, "post_id": post_id}


def _take_down(post_id: int) -> str:
    """Delete a posted post from Instagram and mark it; returns "deleted" or why not."""
    post = _posted(post_id)
    if not post:
        return "it is not up on Instagram"
    try:
        instagram.delete_media(post["media_id"])
    except instagram.InstagramPublishError as exc:
        return str(exc)
    db.execute("UPDATE posts SET status = 'deleted', error = ? WHERE id = ?", f"deleted from Instagram {db.now()[:16]}", post_id)
    logger.info("pipeline.deleted post=%s", post_id)
    return "deleted"


def delete_posted(post_id: int) -> dict[str, Any]:
    with _as_post_brand(post_id), APPROVE_LOCK:
        gone = _take_down(post_id)
    return {"ok": gone == "deleted", "post_id": post_id, **({} if gone == "deleted" else {"reason": gone})}


def repost(post_id: int, *, replace: bool = False) -> dict[str, Any]:
    """Put a copy of a post back into Approvals, to fix its design or words and post it again.
    With ``replace`` the old post is taken down from Instagram once the new one is up."""
    with _as_post_brand(post_id):
        found = db.rows("SELECT * FROM posts WHERE id = ?", post_id)
        if not found or found[0]["status"] in ("pending", "scheduled"):
            return {"ok": False, "reason": "That post is already waiting in Approvals."}
        post = found[0]
        old = settings.CARDS_DIR / Path(str(post["image_path"]).replace("\\", "/")).name
        if not old.exists():
            return {"ok": False, "reason": "The picture of that post is no longer on the server."}
        image = old.with_name(f"{old.stem}-re{datetime.now(UTC).strftime('%H%M%S')}{old.suffix}")
        shutil.copyfile(old, image)
        meta = draft_meta(post)
        meta["repost_of"] = post_id
        if replace and post["status"] == "posted":
            meta["replaces"] = post_id
        else:
            meta.pop("replaces", None)
        new_id = _save_draft(post["story_id"], post["headline"], post["caption"], image, post["writer"], post["flyer"], json.dumps(meta))
        logger.info("pipeline.reposted post=%s as=%s replace=%s", post_id, new_id, bool(meta.get("replaces")))
        return {"ok": True, "post_id": new_id, "replaces": bool(meta.get("replaces"))}


BREAKING_HOURS: Final = 8  # a breaking story is this fresh at most
BREAKING_EXTRA: Final = 2  # breaking news may go past the daily cap by this many posts
BREAKING_GAP_MINUTES: Final = 20  # and needs only this long since the last post


def is_breaking(story: dict[str, Any]) -> bool:
    """Big, confirmed and fresh: an outage, breach or exploit at a big company that 3+ outlets report and the reviewer
    scored 8+, or any story 4+ outlets report that the reviewer scored 9+."""
    if not settings.BREAKING_NEWS or story["published_at"] < _cutoff(BREAKING_HOURS):
        return False
    score, outlets = float(story.get("review_score") or 0), len(story.get("outlets") or [])
    return (bool(sources.event(story["title"])) and outlets >= 3 and score >= 8) or (outlets >= 4 and score >= 9)


def _postable(*, breaking: bool = False) -> dict[str, Any] | None:
    """The next approved story to post (only a breaking one with ``breaking``). Stories this page already covered, or
    that the brand rules block, are set aside on the way."""
    approved = db.rows(
        "SELECT id, title, category, summary, outlets, review_score, published_at FROM stories WHERE status = 'approved' AND published_at >= ? "
        "ORDER BY category IN ('cloud', 'devops', 'ai') DESC, review_score DESC, published_at DESC",
        _cutoff(settings.SCAN_MAX_AGE_HOURS),
    )  # fmt: skip
    posted = [r["title"] for r in db.rows("SELECT title FROM stories WHERE status IN ('posted', 'pending') AND found_at >= ?", _cutoff(48))]
    tags = brands.categories(brands.current())
    for story in approved:
        if tags and story["category"] not in tags:  # another brand's story
            continue
        if breaking and not is_breaking(story):
            continue
        # One subject told in different words (GMO "warns users" / "refuses demands") is
        # posted once: the page already covered it.
        if any(sources.same_subject(story["title"], p) or sources.related(story["title"], p) for p in posted):
            db.update_story(story["id"], status="rejected", review_reason="already posted this subject")
            continue
        if word := rules.blocked(story["title"], story.get("summary") or ""):
            db.update_story(story["id"], status="rejected", review_reason=f"brand rules: blocked word '{word}'")
            continue
        return story
    return None


def _post_story(story_id: int, *, breaking: bool = False) -> dict[str, Any]:
    return draft_story(story_id, breaking=breaking) if needs_approval(flyer=False) else publish_story(story_id)


def post_next() -> dict[str, Any]:
    expire_stale_drafts()
    news_on = brands.has("news")
    flyers_on = brands.has("flyers") and settings.FLYERS_ENABLED
    if not news_on and not flyers_on:  # a business that only posts what it makes itself (ideas, videos)
        return {"posted": False, "reason": "this business makes no automatic posts"}
    # Breaking news does not wait for the gap between posts or for the queue of drafts.
    if news_on and pacing(breaking=True)[0] and (hot := _postable(breaking=True)):
        logger.info("pipeline.breaking story=%s", hot["id"])
        return {**_post_story(hot["id"], breaking=True), "breaking": True}
    ok, why = pacing()
    if not ok:
        return {"posted": False, "reason": why}
    if not news_on and not flyers.listing(enabled_only=True):
        return {"posted": False, "reason": "no flyers in the rotation"}
    flyer_turn = next_is_flyer() if news_on else True  # a business without news posts a flyer every turn
    # The queue is per kind: a flyer waiting for approval never holds up the news, nor news a flyer.
    if needs_approval(flyer=flyer_turn) and (waiting := pending_count(flyer=flyer_turn)) >= settings.APPROVAL_QUEUE_MAX:
        return {"posted": False, "reason": f"{waiting} {'flyers' if flyer_turn else 'news posts'} waiting for your approval"}
    if flyer_turn:
        return draft_flyer() if needs_approval(flyer=True) else publish_flyer()
    story = _postable()
    return _post_story(story["id"]) if story else {"posted": False, "reason": "no approved story waiting"}


def draft_missed(story_id: int) -> dict[str, Any]:
    """The editor asked for a story the newsroom passed over (the "possibly missed" alert): make it into a draft now,
    and remember it, so the reviewer learns what this page wants."""
    story = db.story(story_id)
    if story is None:
        return {"posted": False, "reason": "That story is gone."}
    if story["status"] in ("pending", "posted"):
        return {"posted": False, "reason": "That story is already " + ("waiting for approval." if story["status"] == "pending" else "posted.")}
    team.feedback("missed", 0, headline=story["title"], category=story["category"])
    db.update_story(story_id, status="approved", review_reason="picked by the editor (missed story)")
    with brands.use(brands.DEFAULT_ID):
        return _post_story(story_id, breaking=is_breaking({**story, "review_score": max(8.0, float(story.get("review_score") or 0))}))


def publish_story(story_id: int) -> dict[str, Any]:
    """Write, check, render and post one story (the pacing is the caller's)."""
    story = db.story(story_id)
    if story is None:
        return {"posted": False, "reason": f"no story {story_id}"}
    if story["status"] == "posted":
        return {"posted": False, "reason": "already posted"}
    draft = writer.write(story, angle=story["angle"])
    image = _render_news(story_id, story, draft, designer.choose(story, draft.headline, draft.points)["layout"])
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
    db.kv_set(LAST_POST_KEY + brands.suffix(), datetime.now(UTC).isoformat())
    logger.info("pipeline.posted story=%s via=%s writer=%s", story_id, result["delivered"], draft.writer)
    return {"posted": True, "story_id": story_id, "writer": draft.writer, **result}


def _record_post(story_id: int, draft: writer.Draft, image: Any, caption: str, *, status: str,
                 result: dict[str, str] | None = None, error: str = "") -> None:  # fmt: skip
    result = result or {}
    db.execute(
        "INSERT INTO posts (story_id, headline, caption, image_path, writer, delivered, media_id, permalink, "
        "status, error, created_at, brand_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        story_id, draft.headline, caption, str(image), draft.writer, result.get("delivered", settings.IG_DELIVERY),
        result.get("media_id", ""), result.get("permalink", ""), status, error, db.now(), settings.BRAND_ID,
    )  # fmt: skip


# --------------------------------------------------------------------------- cycle


def run_cycle(*, post: bool = True) -> dict[str, Any]:
    if not LOCK.acquire(blocking=False):
        return {"skipped": "a cycle is already running"}
    if not db.kv_claim(CYCLE_LEASE_KEY, 1200):  # another process (the scheduler or the web app) is running one
        LOCK.release()
        return {"skipped": "a cycle is already running"}
    try:
        summary: dict[str, Any] = {"started_at": db.now()}
        db.kv_set(LAST_ERROR_KEY, "")  # an old error is not current; anything this cycle hits is saved again
        try:
            summary["scan"] = scan()
            summary["review"] = review()
            if post:
                brands.seed()
                results = {}
                for brand in brands.active():
                    with brands.use(brand):
                        results[brand["name"]] = post_next()
                summary["post"] = results.get(brands.get(brands.DEFAULT_ID)["name"]) or next(iter(results.values()), {})
                summary["brands"] = results
        except Exception as exc:  # noqa: BLE001 - the scheduler must survive any one cycle
            logger.exception("pipeline.cycle_failed")
            summary["error"] = f"{type(exc).__name__}: {exc}"
            db.kv_set(LAST_ERROR_KEY, f"{db.now()} {summary['error']}")
        summary["finished_at"] = db.now()
        db.kv_set(LAST_CYCLE_KEY, json.dumps(summary))
        return summary
    finally:
        db.kv_release(CYCLE_LEASE_KEY)
        LOCK.release()


# --------------------------------------------------------------------------- the calendar: posts that go out at a chosen time


def _utc(local_text: str) -> datetime:
    """A time typed in the dashboard (local, like 2026-11-06T10:30) as UTC."""
    try:
        naive = datetime.fromisoformat(local_text)
    except ValueError:
        raise ValueError("Pick a date and time") from None
    return (naive.replace(tzinfo=ZoneInfo(settings.TIMEZONE)) if naive.tzinfo is None else naive).astimezone(UTC)


def schedule(post_id: int, when_local: str, caption: str | None = None) -> dict[str, Any]:
    """Hold an approved post until ``when_local`` (the page's time zone). Raises ValueError for a bad time."""
    when = _utc(when_local)
    if when < datetime.now(UTC) + timedelta(minutes=1):
        raise ValueError("Pick a time in the future")
    if when > datetime.now(UTC) + timedelta(days=60):
        raise ValueError("Schedule up to 60 days ahead")
    with APPROVE_LOCK:
        found = db.rows("SELECT * FROM posts WHERE id = ? AND status = 'pending'", post_id)
        if not found:
            return {"scheduled": False, "reason": "that post is not waiting for approval"}
        text = (caption or found[0]["caption"]).strip()
        db.execute("UPDATE posts SET status = 'scheduled', scheduled_at = ?, caption = ? WHERE id = ?",
                   when.isoformat(timespec="seconds"), text, post_id)  # fmt: skip
    return {"scheduled": True, "post_id": post_id, "at": when.isoformat(timespec="seconds")}


def unschedule(post_id: int) -> dict[str, Any]:
    with APPROVE_LOCK:
        found = db.rows("SELECT id FROM posts WHERE id = ? AND status = 'scheduled'", post_id)
        if not found:
            return {"scheduled": False, "reason": "that post is not scheduled"}
        db.execute("UPDATE posts SET status = 'pending', scheduled_at = '' WHERE id = ?", post_id)
    return {"scheduled": False, "post_id": post_id}


def publish_due() -> list[dict[str, Any]]:
    """Send every scheduled post whose time has come. Run by the scheduler once a minute."""
    due = db.rows("SELECT id FROM posts WHERE status = 'scheduled' AND scheduled_at != '' AND scheduled_at <= ? ORDER BY scheduled_at",
                  db.now())  # fmt: skip
    out = []
    for row in due:
        post_id = row["id"]
        if not db.kv_claim(f"publish:{post_id}", 300):
            continue
        db.execute("UPDATE posts SET status = 'pending' WHERE id = ? AND status = 'scheduled'", post_id)
        result = approve(post_id)
        if not result.get("posted"):
            reason = str(result.get("reason", ""))
            if "pause" in reason:  # try again next minute
                db.execute("UPDATE posts SET status = 'scheduled' WHERE id = ?", post_id)
            else:
                db.execute("UPDATE posts SET error = ? WHERE id = ?", reason[:300], post_id)
                notify.send(notify.title("a scheduled post did not go out", notify.brand_of(post_id)), f"Post {post_id}: {reason}", brand_id=notify.brand_of(post_id))
            logger.warning("pipeline.scheduled_failed post=%s reason=%s", post_id, reason)
        out.append({"post_id": post_id, **result})
    return out


def calendar(month: str) -> list[dict[str, Any]]:
    """The current brand's posted, scheduled and waiting posts for a month (``YYYY-MM``, the page's time zone)."""
    tz = ZoneInfo(settings.TIMEZONE)
    try:
        first = datetime.strptime(month, "%Y-%m").replace(tzinfo=tz)
    except ValueError:
        raise ValueError("Month looks like 2026-11") from None
    nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    lo, hi = (x.astimezone(UTC).isoformat(timespec="seconds") for x in (first, nxt))
    found = db.rows(
        "SELECT id, headline, status, created_at, scheduled_at, flyer FROM posts WHERE brand_id = ? AND status IN ('posted','scheduled','pending') "
        "AND ((status = 'scheduled' AND scheduled_at >= ? AND scheduled_at < ?) OR (status != 'scheduled' AND created_at >= ? AND created_at < ?)) "
        "ORDER BY id", settings.BRAND_ID, lo, hi, lo, hi)  # fmt: skip
    items = []
    for p in found:
        moment = datetime.fromisoformat(p["scheduled_at"] if p["status"] == "scheduled" else p["created_at"]).astimezone(tz)
        items.append({"id": p["id"], "headline": p["headline"], "status": p["status"], "flyer": bool(p["flyer"]),
                      "day": moment.date().isoformat(), "time": moment.strftime("%H:%M")})  # fmt: skip
    return sorted(items, key=lambda i: (i["day"], i["time"]))
