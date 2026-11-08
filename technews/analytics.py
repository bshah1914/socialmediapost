"""Analytics: what the audience does with the posts, read back from Instagram.

* Every 6 hours, the likes, comments, plays, reach, saves and shares of the posts from the last 7 days
  (reach, saves and shares need a Business or Creator account; without one they stay at 0).
* Once a day, the follower count, so growth shows as a line.
* A new comment is passed on (Telegram and the other alert channels) so someone can answer within the hour.
* Every Monday, a short report of the week goes to the same channels.

Each look is one request per post, kept to recent posts, so the account is not hammered."""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from statistics import mean
from typing import Any, Final
from zoneinfo import ZoneInfo

from technews import brands, db, notify
from technews.config import settings

logger = logging.getLogger(__name__)

RECENT_DAYS: Final = 7  # a post's numbers settle within a week
MAX_POSTS: Final = 30
INSIGHTS_KEY: Final = "analytics:insights"  # "ok" or why Instagram gave no insights
NUMBERS: Final = ("likes", "comments", "plays", "saves", "shares", "reach")


def active() -> bool:
    return settings.COLLECT_METRICS and settings.IG_DELIVERY == "instagram"


def _find(data: Any, *names: str) -> int:
    """The first number found under any of ``names`` anywhere in Instagram's nested answer."""
    if isinstance(data, dict):
        for name in names:
            value = data.get(name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return int(value)
            if isinstance(value, dict) and isinstance(value.get("value"), (int, float)):
                return int(value["value"])
        for value in data.values():
            if isinstance(value, (dict, list)) and (found := _find(value, *names)):
                return found
    elif isinstance(data, list):
        for value in data:
            if found := _find(value, *names):
                return found
    return 0


def _kind(post: dict[str, Any]) -> str:
    try:
        meta = json.loads(post.get("meta") or "{}") or {}
    except ValueError:
        meta = {}
    if isinstance(meta, dict) and meta.get("reel"):
        return "reel"
    return "flyer" if post.get("flyer") else "news"


def _media_numbers(client: Any, media_id: str) -> dict[str, int]:
    media = client.media_info(media_id)
    out = {"likes": int(getattr(media, "like_count", 0) or 0), "comments": int(getattr(media, "comment_count", 0) or 0),
           "plays": int(getattr(media, "play_count", 0) or getattr(media, "view_count", 0) or 0), "saves": 0, "shares": 0, "reach": 0}  # fmt: skip
    try:
        insights = client.insights_media(media_id)
    except Exception as exc:  # noqa: BLE001 - a personal account, or Instagram said no: the basic numbers still count
        db.kv_set(INSIGHTS_KEY + brands.suffix(), str(exc)[:200])
        return out
    db.kv_set(INSIGHTS_KEY + brands.suffix(), "ok")
    out["reach"] = _find(insights, "reach_count", "reach", "accounts_reached")
    out["saves"] = _find(insights, "save_count", "saved_count", "saves")
    out["shares"] = _find(insights, "share_count", "shares_count", "shares")
    out["plays"] = out["plays"] or _find(insights, "play_count", "video_view_count", "plays")
    return out


def collect(limit: int = MAX_POSTS) -> int:
    """Read the numbers of the recent posts; returns how many were updated. A new comment is passed on."""
    if not active():
        return 0
    from technews import instagram
    from technews.agents import team

    since = (datetime.now(UTC) - timedelta(days=RECENT_DAYS)).isoformat(timespec="seconds")
    posts = db.rows("SELECT id, media_id, headline, permalink FROM posts WHERE brand_id = ? AND status = 'posted' AND media_id != '' "
                    "AND media_id NOT LIKE 'dry:%' AND created_at >= ? ORDER BY id DESC LIMIT ?", settings.BRAND_ID, since, limit)  # fmt: skip
    if not posts:
        return 0
    done = 0
    with team.run("analyst") as info:
        client = instagram.get_client()
        for post in posts:
            try:
                numbers = _media_numbers(client, post["media_id"])
            except Exception as exc:  # noqa: BLE001 - one bad post must not stop the rest
                logger.warning("analytics.post_failed post=%s error=%s", post["id"], str(exc)[:120])
                continue
            before = db.rows("SELECT comments FROM metrics WHERE post_id = ?", post["id"])
            db.execute("DELETE FROM metrics WHERE post_id = ?", post["id"])
            db.execute("INSERT INTO metrics (post_id, likes, comments, plays, saves, shares, reach, at) VALUES (?,?,?,?,?,?,?,?)",
                       post["id"], *(numbers[k] for k in NUMBERS), db.now())  # fmt: skip
            if before and numbers["comments"] > int(before[0]["comments"]):
                new = numbers["comments"] - int(before[0]["comments"])
                notify.send(f"CloudTrio: {new} new comment{'s' if new > 1 else ''} on Instagram",
                            f"{post['headline']}\n\nAnswer within the hour to keep the post moving.\n{post['permalink']}")  # fmt: skip
            done += 1
        info["summary"] = f"read the numbers of {done} post(s)"
    return done


def followers_today() -> bool:
    day = datetime.now(ZoneInfo(settings.TIMEZONE)).date().isoformat()
    return bool(db.rows("SELECT 1 FROM followers WHERE brand_id = ? AND day = ?", settings.BRAND_ID, day))


def snapshot_followers() -> int | None:
    """Today's follower count (one request a day)."""
    if not active():
        return None
    from technews import instagram

    client = instagram.get_client()
    user = client.user_info(client.user_id)
    count = int(getattr(user, "follower_count", 0) or 0)
    day = datetime.now(ZoneInfo(settings.TIMEZONE)).date().isoformat()
    db.execute("DELETE FROM followers WHERE brand_id = ? AND day = ?", settings.BRAND_ID, day)
    db.execute("INSERT INTO followers (brand_id, day, count, following, media, at) VALUES (?,?,?,?,?,?)", settings.BRAND_ID, day, count,
               int(getattr(user, "following_count", 0) or 0), int(getattr(user, "media_count", 0) or 0), db.now())  # fmt: skip
    logger.info("analytics.followers count=%s", count)
    return count


def _score(m: dict[str, Any]) -> int:
    """What a post earned: every like, comment, save and share counts once."""
    return sum(int(m[k] or 0) for k in ("likes", "comments", "saves", "shares"))


def summary(days: int = 30) -> dict[str, Any]:
    """Everything the Analytics page shows, for the last ``days`` days. Plain numbers, no AI."""
    tz = ZoneInfo(settings.TIMEZONE)
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
    growth = db.rows("SELECT day, count FROM followers WHERE brand_id = ? AND day >= ? ORDER BY day",
                     settings.BRAND_ID, (datetime.now(tz).date() - timedelta(days=days)).isoformat())  # fmt: skip
    rows = db.rows("SELECT p.id, p.headline, p.created_at, p.flyer, p.meta, p.permalink, m.likes, m.comments, m.plays, m.saves, m.shares, m.reach "
                   "FROM posts p LEFT JOIN metrics m ON m.post_id = p.id WHERE p.brand_id = ? AND p.status = 'posted' AND p.created_at >= ? "
                   "ORDER BY p.created_at DESC", settings.BRAND_ID, since)  # fmt: skip
    measured = [r for r in rows if r["likes"] is not None]
    by_kind: dict[str, list[int]] = defaultdict(list)
    hours: dict[int, list[int]] = defaultdict(list)
    weekdays: dict[str, list[int]] = defaultdict(list)
    for r in measured:
        at = datetime.fromisoformat(r["created_at"]).astimezone(tz)
        by_kind[_kind(r)].append(_score(r))
        hours[at.hour].append(_score(r))
        weekdays[at.strftime("%a")].append(_score(r))
    posts = [{"id": r["id"], "headline": r["headline"], "kind": _kind(r), "link": r["permalink"], "at": r["created_at"],
              "measured": r["likes"] is not None, "score": _score(r) if r["likes"] is not None else None,
              **{k: int(r[k] or 0) for k in NUMBERS}} for r in rows]  # fmt: skip
    total = {k: sum(p[k] for p in posts) for k in NUMBERS}
    first, last = (growth[0]["count"], growth[-1]["count"]) if growth else (None, None)
    reach = total["reach"]
    return {
        "days": days, "active": active(), "insights": db.kv_get(INSIGHTS_KEY + brands.suffix()) or "",
        "followers": {"now": last, "change": (last - first) if growth else None, "series": [{"day": g["day"], "count": int(g["count"])} for g in growth]},
        "posted": len(rows), "measured": len(measured), "total": total,
        "engagement_rate": round(100 * sum(_score(r) for r in measured) / reach, 1) if reach else None,
        "by_kind": {k: {"posts": len(v), "average": round(mean(v), 1)} for k, v in sorted(by_kind.items())},
        "best_hours": sorted(({"hour": h, "average": round(mean(v), 1), "posts": len(v)} for h, v in hours.items()),
                             key=lambda x: (-x["average"], -x["posts"]))[:3] if len(measured) >= 5 else [],
        "best_days": sorted(({"day": d, "average": round(mean(v), 1), "posts": len(v)} for d, v in weekdays.items()),
                            key=lambda x: (-x["average"], -x["posts"]))[:3] if len(measured) >= 5 else [],
        "top": sorted((p for p in posts if p["measured"]), key=lambda p: -(p["score"] or 0))[:5],
        "posts": posts[:60],
    }  # fmt: skip


def weekly_text() -> str:
    """The Monday report, in a few plain lines."""
    s = summary(7)
    hour = lambda h: f"{(h + 11) % 12 + 1} {'am' if h < 12 else 'pm'}"  # noqa: E731
    lines = [f"Posts this week: {s['posted']} ({s['measured']} measured)"]
    if s["followers"]["now"] is not None:
        change = s["followers"]["change"] or 0
        lines.append(f"Followers: {s['followers']['now']} ({'+' if change >= 0 else ''}{change} this week)")
    t = s["total"]
    lines.append(f"Likes {t['likes']}, comments {t['comments']}, saves {t['saves']}, shares {t['shares']}, reach {t['reach']}, plays {t['plays']}")
    if s["top"]:
        lines.append(f"Best post: {s['top'][0]['headline']} (score {s['top'][0]['score']}) {s['top'][0]['link']}")
    if len(s["by_kind"]) > 1:
        best = max(s["by_kind"].items(), key=lambda kv: kv[1]["average"])
        lines.append(f"Best format: {best[0]} ({best[1]['average']} per post on average)")
    if s["best_hours"]:
        lines.append(f"Best hour: {hour(s['best_hours'][0]['hour'])}")
    if not s["measured"]:
        lines.append("No numbers yet: switch on reading likes and comments in Settings, Agents.")
    return "\n".join(lines)


def weekly_report() -> dict[str, str]:
    if settings.IG_DELIVERY != "instagram" or not notify.channels():
        return {}
    return notify.send("CloudTrio: your Instagram week", weekly_text() + (f"\n\n{settings.PUBLIC_URL.rstrip('/')}/#analytics" if settings.PUBLIC_URL else ""))
