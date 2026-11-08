"""The **Performance analyst** agent: studies what happens to the posts (approved, rejected, edited, at what hour,
in which layout) and, when you switch it on in Settings, what the audience does (likes and comments). It writes a
short report with plain recommendations, saved for the dashboard and read by the trainer."""

from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from datetime import datetime
from statistics import mean
from typing import Any, Final
from zoneinfo import ZoneInfo

from technews import brands, db
from technews.agents import llm, team
from technews.config import settings

logger = logging.getLogger(__name__)

REPORT_KEY: Final = "analyst:report"


def _posts(days: int = 30) -> list[dict[str, Any]]:
    since = (datetime.now(ZoneInfo("UTC")).timestamp() - days * 86400)
    cutoff = datetime.fromtimestamp(since, ZoneInfo("UTC")).isoformat(timespec="seconds")
    return db.rows("SELECT id, headline, status, created_at, flyer, meta FROM posts WHERE brand_id = ? AND created_at >= ? ORDER BY id DESC LIMIT 500",
                   settings.BRAND_ID, cutoff)  # fmt: skip


def _meta(post: dict[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(post.get("meta") or "{}") or {}
    except ValueError:
        return {}


def compute() -> dict[str, Any]:
    """The numbers, without any AI."""
    posts, fb = _posts(), team.recent_feedback(300)
    kinds = Counter(r["kind"] for r in fb)
    decided = kinds["approved"] + kinds["rejected"]
    tz = ZoneInfo(settings.TIMEZONE)
    hours = Counter(datetime.fromisoformat(p["created_at"]).astimezone(tz).hour for p in posts if p["status"] == "posted")
    layouts, per_cat = Counter(), defaultdict(lambda: Counter())
    for r in fb:
        if r["kind"] in ("approved", "rejected"):
            per_cat[r["detail"].get("category", "?")][r["kind"]] += 1
            if r["detail"].get("layout"):
                layouts[r["detail"]["layout"]] += 1
    cats = {c: {"approved": v["approved"], "rejected": v["rejected"]} for c, v in per_cat.items() if c and c != "?"}
    metrics = db.rows("SELECT m.likes, m.comments, p.flyer, p.meta FROM metrics m JOIN posts p ON p.id = m.post_id WHERE p.brand_id = ?", settings.BRAND_ID)
    by_layout: dict[str, list[int]] = defaultdict(list)
    for m in metrics:
        try:
            by_layout[(json.loads(m["meta"] or "{}") or {}).get("layout") or "original"].append(int(m["likes"]))
        except ValueError:
            continue
    return {
        "posted": sum(p["status"] == "posted" for p in posts), "waiting": sum(p["status"] in ("pending", "scheduled") for p in posts),
        "decisions": decided, "approval_rate": round(kinds["approved"] / decided, 2) if decided else None,
        "edit_rate": round(kinds["edited"] / max(1, decided), 2), "categories": cats,
        "layouts": dict(layouts.most_common(6)), "best_hours": [h for h, _ in hours.most_common(3)],
        "likes": {"posts": len(metrics), "average": round(mean(int(m["likes"]) for m in metrics), 1) if metrics else None,
                  "by_layout": {k: round(mean(v), 1) for k, v in by_layout.items() if v}},
    }  # fmt: skip


def tips(data: dict[str, Any]) -> list[str]:
    out = []
    if data["decisions"] < 5:
        out.append("Not enough decisions yet. Approve or reject a few more posts and the agents will start to learn your taste.")
    else:
        if data["approval_rate"] is not None and data["approval_rate"] < 0.6:
            out.append(f"You approve {int(data['approval_rate'] * 100)}% of drafts. The writer and reviewer are still off target; the trainer will tighten the rules.")
        if data["edit_rate"] > 0.5:
            out.append("You edit more than half of the drafts. Check the writer's playbook: it should now reflect your edits.")
        weak = [c for c, v in data["categories"].items() if v["rejected"] >= 2 and v["rejected"] > v["approved"]]
        if weak:
            out.append("You usually reject these topics: " + ", ".join(weak) + ". Consider removing their sources or tags.")
        strong = [c for c, v in data["categories"].items() if v["approved"] >= 3 and v["approved"] > 2 * v["rejected"]]
        if strong:
            out.append("You approve these topics most: " + ", ".join(strong) + ". The scanner can favour them.")
    if data["best_hours"]:
        out.append("Most posts go out around " + ", ".join(f"{h:02d}:00" for h in data["best_hours"]) + ". Schedule important posts then.")
    if data["likes"]["by_layout"]:
        best = max(data["likes"]["by_layout"].items(), key=lambda kv: kv[1])
        out.append(f"The {best[0]} layout gets the most likes ({best[1]} on average).")
    return out


def report() -> dict[str, Any]:
    """Compute, add an AI summary when a model is available, and save for the dashboard."""
    if not team.enabled("analyst"):
        return latest()
    with team.run("analyst") as info:
        data = compute()
        data["tips"] = tips(data)
        data["summary"] = ""
        if llm.available() and data["decisions"] >= 5:
            try:
                reply = llm.chat_json(system="You are the PERFORMANCE ANALYST of an Instagram tech-news page. In two plain sentences, say what the numbers show and the single most useful change. "
                                      'Reply with JSON {"summary": "..."}.', user=json.dumps(data, default=str)[:3000], max_tokens=200, temperature=0.3)  # fmt: skip
                data["summary"] = str(reply.data.get("summary", ""))[:400]
            except llm.LLMError as exc:
                logger.warning("analyst.llm_failed error=%s", str(exc)[:160])
        data["at"] = db.now()
        db.kv_set(REPORT_KEY + brands.suffix(), json.dumps(data, default=str))
        info["summary"] = f"{data['posted']} posted, {data['decisions']} decisions, {len(data['tips'])} tip(s)"
        return data


def latest() -> dict[str, Any]:
    try:
        return json.loads(db.kv_get(REPORT_KEY + brands.suffix()) or "{}")
    except ValueError:
        return {}


def results() -> dict[str, Any]:
    """What the audience did, from the likes and comments read from Instagram: the best posts, flyers against news
    cards, and the hours that did best. Plain numbers, no AI."""
    rows = db.rows("SELECT m.likes, m.comments, p.id, p.headline, p.created_at, p.flyer, p.permalink FROM metrics m JOIN posts p ON p.id = m.post_id "
                   "WHERE p.brand_id = ? AND p.status = 'posted' ORDER BY p.id DESC LIMIT 300", settings.BRAND_ID)  # fmt: skip
    tz = ZoneInfo(settings.TIMEZONE)
    kinds: dict[str, list[int]] = {"news": [], "flyer": []}
    hours: dict[int, list[int]] = defaultdict(list)
    for r in rows:
        score = int(r["likes"]) + int(r["comments"])
        kinds["flyer" if r["flyer"] else "news"].append(score)
        hours[datetime.fromisoformat(r["created_at"]).astimezone(tz).hour].append(score)
    best = sorted(({"hour": h, "average": round(mean(v), 1), "posts": len(v)} for h, v in hours.items()), key=lambda x: (-x["average"], -x["posts"]))
    top = sorted(rows, key=lambda r: -(int(r["likes"]) + int(r["comments"])))[:5]
    return {
        "collecting": settings.COLLECT_METRICS and settings.IG_DELIVERY == "instagram",
        "posts": len(rows),
        "top": [{"id": r["id"], "headline": r["headline"], "likes": int(r["likes"]), "comments": int(r["comments"]), "kind": "flyer" if r["flyer"] else "news",
                 "link": r["permalink"]} for r in top],
        "by_kind": {k: {"posts": len(v), "average": round(mean(v), 1) if v else None} for k, v in kinds.items()},
        "best_hours": best[:3] if len(rows) >= 5 else [],  # fewer posts than this say nothing about the time of day
    }


def collect_metrics(limit: int = 30) -> int:
    """The audience numbers of the recent posts (see :mod:`technews.analytics`)."""
    from technews import analytics

    return analytics.collect(limit)
