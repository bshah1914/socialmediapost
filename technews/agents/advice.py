"""What the agents suggest, in context: on each post waiting for approval, and on every page of the dashboard.

Every suggestion is ``{"agent", "title", "detail", "tone", "action"}``. ``tone`` is info, warn or good. ``action``
(optional) is something the page can do for you in one click: ``headline``, ``layout``, ``schedule``, ``edit``,
``go`` or ``setting``. Nothing is changed without that click. The rules need no AI; with a model, the writer
adds headline alternatives.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Final
from zoneinfo import ZoneInfo

from technews import config, db, flyers, pipeline, users
from technews.agents import analyst, designer, editor, llm, scout, team
from technews.config import settings

logger = logging.getLogger(__name__)

LAYOUT_NAMES: Final = dict(flyers.LAYOUTS)


def _s(agent: str, title: str, detail: str = "", tone: str = "info", action: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"agent": agent, "name": team.ROSTER[agent]["name"], "title": title, "detail": detail, "tone": tone, "action": action}


# --------------------------------------------------------------------------- one post


def _shorter(headline: str) -> list[str]:
    """Plain rule-based alternatives: the first clause, and a number-first version."""
    out = []
    first = re.split(r"[:;,.—–-]\s", headline)[0].strip()
    if 2 <= len(first.split()) < len(headline.split()):
        out.append(first)
    words = headline.split()
    if len(words) > 9:
        out.append(" ".join(words[:8]).rstrip(",;:") + "...")
    num = re.search(r"\d[\d,.]*\s?(?:%|[kKmMbB]\b|million|billion)?", headline)
    if num and not headline.startswith(num.group().strip()):
        out.append(f"{num.group().strip()}: " + headline.replace(num.group(), "").strip(" ,.-")[:70])
    return [h for h in dict.fromkeys(out) if h and h != headline][:2]


def _headline_ideas(post: dict[str, Any], story: dict[str, Any]) -> list[str]:
    ideas = _shorter(post["headline"])
    if llm.available() and story:
        try:
            reply = llm.chat_json(
                system="You are the CONTENT WRITER of an Instagram tech-news page. Give 2 alternative card headlines (2-10 words, Title Case) that are as punchy as "
                       "possible but use only facts from the story. Follow the playbook if one is given. Reply with JSON {\"headlines\": [..]}."
                       + (f"\nPLAYBOOK:\n{team.playbook('writer')}" if team.playbook("writer") else ""),
                user=f"STORY: {story.get('title')}\n{story.get('summary', '')}\nCURRENT HEADLINE: {post['headline']}", max_tokens=200, temperature=0.7)  # fmt: skip
            ideas = [str(h).strip() for h in (reply.data.get("headlines") or []) if str(h).strip()][:2] or ideas
        except llm.LLMError as exc:
            logger.warning("advice.writer_llm_failed error=%s", str(exc)[:120])
    return ideas


def best_slot(*, skip: set[str] | None = None) -> tuple[str, str]:
    """The next good time to post: the analyst's busiest hours (or mid-morning, lunch and evening), a free slot."""
    tz = ZoneInfo(settings.TIMEZONE)
    hours = (analyst.latest().get("best_hours") or []) + [10, 13, 18]
    taken = {r["scheduled_at"][:13] for r in db.rows("SELECT scheduled_at FROM posts WHERE status = 'scheduled' AND brand_id = ?", settings.BRAND_ID)}
    now = datetime.now(tz)
    for day in range(0, 8):
        for h in dict.fromkeys(int(x) for x in hours):
            slot = (now + timedelta(days=day)).replace(hour=h, minute=0, second=0, microsecond=0)
            utc = slot.astimezone(ZoneInfo("UTC")).isoformat(timespec="seconds")[:13]
            if slot > now + timedelta(minutes=30) and utc not in taken and slot.strftime("%Y-%m-%dT%H:%M") not in (skip or set()):
                return slot.strftime("%Y-%m-%dT%H:%M"), slot.strftime("%a %d %b, %H:%M")
    return "", ""


def for_post(post_id: int) -> list[dict[str, Any]]:
    found = db.rows("SELECT * FROM posts WHERE id = ?", post_id)
    if not found:
        return []
    post = found[0]
    meta = pipeline.draft_meta(post)
    key = f"advice:post:{post_id}:" + hashlib.md5((post["headline"] + post["caption"][:80] + str(meta.get("layout"))).encode()).hexdigest()[:10]
    if cached := db.kv_get(key):
        return json.loads(cached)
    story = db.story(post["story_id"]) if post["story_id"] else None
    out: list[dict[str, Any]] = []
    # the quality editor
    notes = meta.get("warnings") or []
    if team.enabled("editor"):
        extra = [n for n in editor.heuristics(post["headline"], meta.get("points", []), post["caption"]) if n not in notes]
        for n in [*notes, *extra][:4]:
            out.append(_s("editor", n, "Fix it in Edit design, or approve if you are happy with it.", "warn"))
        if not notes and not extra:
            out.append(_s("editor", "Reads clean", "No hype, no numbers the source lacks, no blocked words.", "good"))
    # the content writer
    if not post["flyer"] and story and team.enabled("writer"):
        for h in _headline_ideas(post, story):
            out.append(_s("writer", f"Try the headline: {h}", "A tighter version of the same facts.", "info", {"kind": "headline", "value": h, "label": "Use this headline"}))
        if len(post["caption"]) > 700:
            out.append(_s("writer", "The caption is long", "Instagram readers drop off after about 125 characters: a hook, two facts, a question.", "info"))
    # the designer
    if team.enabled("designer"):
        if post["flyer"]:
            snap = (meta.get("flyer") or {}).get("layout", "classic")
            if snap == "classic":
                pick = designer.candidates("other", post["headline"])[0]
                out.append(_s("designer", f"This flyer uses the Classic layout. Try {LAYOUT_NAMES.get(pick, pick)}", "A different look keeps the page from feeling repetitive.", "info", {"kind": "edit", "label": "Open the studio"}))
        else:
            current = meta.get("layout", "")
            for lay in [c for c in designer.candidates(meta.get("category", "other"), post["headline"]) if c != current][:2]:
                out.append(_s("designer", f"Try the {LAYOUT_NAMES.get(lay, lay)} layout", "Suits this topic and what you have chosen before.", "info", {"kind": "layout", "value": lay, "label": "Switch layout"}))
            if not current:
                out.append(_s("designer", "This card uses the original look", "The designer normally picks a layout; one was not chosen for this post.", "info"))
    # the performance analyst: when to send
    if team.enabled("analyst"):
        value, label = best_slot()
        if value:
            out.append(_s("analyst", f"Best time to post: {label}", "Based on when your posts usually go out and the free slots in the calendar.", "info", {"kind": "schedule", "value": value, "label": "Use this time"}))
        waiting = pipeline.pending_count()
        if waiting >= 3:
            out.append(_s("analyst", f"{waiting} posts are waiting", "Approve or reject the oldest first so stories do not go stale.", "warn"))
    # the trainer: the rules that apply
    rules = [ln.strip("-• ").strip() for a in ("writer", "designer", "editor") for ln in team.playbook(a).splitlines() if ln.strip()][:2]
    if rules:
        out.append(_s("trainer", "Your rules for this post", " ".join(rules), "info"))
    db.kv_set(key, json.dumps(out), ttl=600)
    return out


# --------------------------------------------------------------------------- one page


def _overview() -> list[dict[str, Any]]:
    out = []
    waiting = pipeline.pending_count()
    if waiting:
        out.append(_s("analyst", f"{waiting} post{'s' if waiting != 1 else ''} waiting for you", "The sooner you decide, the fresher the news is when it goes out.", "warn", {"kind": "go", "value": "approvals", "label": "Review"}))
    elif pipeline.posts_today() == 0:
        out.append(_s("analyst", "Nothing posted yet today", "Run a cycle, or check Stories for approved news waiting to be drafted.", "info", {"kind": "go", "value": "stories", "label": "Open stories"}))
    for tip in (analyst.latest().get("tips") or [])[:2]:
        out.append(_s("analyst", tip))
    if team.proposals():
        out.append(_s("trainer", f"{len(team.proposals())} new rule suggestion(s) to look at", "Rules learned from your decisions.", "info", {"kind": "go", "value": "agents", "label": "See rules"}))
    health = [x for x in _settings() if x["tone"] == "warn"][:1]
    return out + health


def _stories() -> list[dict[str, Any]]:
    out = []
    ready = db.rows("SELECT title, category, review_score FROM stories WHERE status = 'approved' ORDER BY category IN ('cloud','devops','ai') DESC, review_score DESC LIMIT 20")
    if ready:
        top = ready[0]
        out.append(_s("reviewer", f"{len(ready)} approved stor{'ies' if len(ready) != 1 else 'y'} waiting to be drafted", f"Next up: {top['title'][:90]} ({top['category']}, score {top['review_score']}).", "info"))
    reasons = Counter(r["review_reason"].split(":")[0][:60] for r in db.rows("SELECT review_reason FROM stories WHERE status IN ('rejected','low') AND review_reason != '' LIMIT 300"))
    single = sum(n for k, n in reasons.items() if "single unverified source" in k)
    if single >= 5:
        out.append(_s("reviewer", f"{single} stories were rejected as 'single unverified source'", "If one of those outlets is reliable, add it to the trusted list so its stories can pass.", "info"))
    cats = Counter(r["category"] for r in db.rows("SELECT category FROM stories WHERE found_at >= ?", (datetime.now() - timedelta(days=3)).isoformat()))
    if cats and not any(cats[c] for c in ("cloud", "devops", "ai")):
        out.append(_s("scanner", "No cloud, DevOps or AI stories in the last 3 days", "Check that those sources are switched on in Sources.", "warn", {"kind": "go", "value": "sources", "label": "Open sources"}))
    return out


def _flyers() -> list[dict[str, Any]]:
    out, items = [], flyers.listing()
    classic = [f for f in items if f.layout == "classic"]
    if items and len(classic) == len(items):
        out.append(_s("designer", f"All {len(items)} flyers use the Classic layout", "Give each flyer its own look (Aurora, Bento, Blueprint...) so the page does not feel repetitive.", "info"))
    posted = Counter(r["flyer"] for r in db.rows("SELECT flyer FROM posts WHERE status = 'posted' AND flyer != '' AND brand_id = ?", settings.BRAND_ID))
    never = [f.kicker for f in items if f.enabled and not posted[f.id]]
    if never:
        out.append(_s("analyst", f"{len(never)} flyer(s) have never been posted", ", ".join(never[:4]) + ". They are next in the rotation.", "info"))
    for f in items:
        if re.search(r"\d\s?%|\d+x|save", f.headline + " ".join(f.points), re.I) and len(f.note) < 15:
            out.append(_s("editor", f"'{f.kicker}' makes a claim but has no small print", "Add a condition in the note field (for example: savings depend on your plan).", "warn"))
            break
    if items and not any(len(f.headline.split()) <= 6 for f in items):
        out.append(_s("writer", "Every flyer headline is long", "Flyers read best with a headline of six words or fewer.", "info"))
    return out


def _sources() -> list[dict[str, Any]]:
    out = []
    rows = db.rows("SELECT id, kind, name, value, enabled FROM sources")
    week = (datetime.now() - timedelta(days=7)).isoformat()
    seen = Counter(r["source"] for r in db.rows("SELECT source FROM stories WHERE found_at >= ?", week))
    good = Counter(r["source"] for r in db.rows("SELECT source FROM stories WHERE found_at >= ? AND status IN ('approved','posted','pending')", week))
    noisy = [(n, c) for n, c in seen.items() if c >= 10 and good[n] == 0]
    if noisy:
        n, c = max(noisy, key=lambda x: x[1])
        out.append(_s("scanner", f"{n} gave {c} stories this week and none were approved", "Consider switching it off to save review time.", "info", {"kind": "go", "value": "sources", "label": "Open sources"}))
    dead = [r["name"] for r in rows if r["enabled"] and r["kind"] == "feed" and scout.state(r, scout.health(r["id"])) in ("dead", "slow")][:4]
    if dead:
        out.append(_s("scout", f"{len(dead)} feed(s) are failing", ", ".join(d[:40] for d in dead) + ". The scout will switch them off after three days.", "warn"))
    if (n := len(scout.ideas())):
        out.append(_s("scout", f"{n} suggestion(s) waiting in the Source scout", "New feeds that were tested, and feeds worth switching off.", "info"))
    silent = [r["name"] or r["value"] for r in rows if r["enabled"] and r["kind"] == "feed" and not seen.get(r["name"]) and r["name"] not in dead][:4]
    if silent:
        out.append(_s("scanner", f"{len(silent)} feed(s) gave nothing this week", ", ".join(s[:40] for s in silent) + ". They may be quiet or moved.", "info"))
    if not any(r["enabled"] and r["kind"] == "search" for r in rows):
        out.append(_s("scanner", "No news searches are on", "A search for cloud, DevOps and AI news finds stories feeds miss.", "info"))
    return out


def _calendar() -> list[dict[str, Any]]:
    out, tz = [], ZoneInfo(settings.TIMEZONE)
    today = datetime.now(tz).date()
    booked = {i["day"] for m in {today.strftime("%Y-%m"), (today + timedelta(days=7)).strftime("%Y-%m")} for i in pipeline.calendar(m)}
    empty = [(today + timedelta(days=d)) for d in range(1, 8) if (today + timedelta(days=d)).isoformat() not in booked]
    if empty:
        out.append(_s("analyst", f"{len(empty)} of the next 7 days have nothing planned", ", ".join(d.strftime("%a") for d in empty[:5]) + ".", "info", {"kind": "go", "value": "approvals", "label": "Schedule posts"}))
    value, label = best_slot()
    if value:
        out.append(_s("analyst", f"Best free slot: {label}", "From the hours your posts usually go out.", "good"))
    return out


def _posts() -> list[dict[str, Any]]:
    out, rep = [], analyst.latest()
    if rep.get("likes", {}).get("average") is not None:
        out.append(_s("analyst", f"Posts average {rep['likes']['average']} likes", "See the Agents page for the best layout.", "info", {"kind": "go", "value": "agents", "label": "Open agents"}))
    else:
        out.append(_s("analyst", "Switch on likes and comments to see what performs", "Under Agents. It adds a few Instagram requests a day.", "info", {"kind": "go", "value": "agents", "label": "Open agents"}))
    for tip in (rep.get("tips") or [])[:2]:
        out.append(_s("analyst", tip))
    return out


def _settings() -> list[dict[str, Any]]:
    out = []
    go = lambda el: {"kind": "setting", "value": el, "label": "Go to it"}  # noqa: E731
    if not settings.NVIDIA_API_KEY:
        out.append(_s("trainer", "No AI key is set", "The agents use simple built-in rules until an NVIDIA key is saved.", "warn", go("NVIDIA_API_KEY")))
    elif not (settings.LLM2_BASE_URL and settings.LLM2_API_KEY):
        out.append(_s("trainer", "Add a second AI service as a backup", "If NVIDIA is down, writing falls back to templates.", "info", go("LLM2_BASE_URL")))
    from technews import notify

    if not notify.channels():
        out.append(_s("editor", "No alert channel is set", "You will not be told when a post is waiting or the newsroom stops.", "warn", go("NOTIFY_EMAIL_TO")))
    if not settings.BLOCKED_WORDS:
        out.append(_s("editor", "No blocked words yet", "Add names or topics you never want on the page.", "info", go("BLOCKED_WORDS")))
    status = config.secrets_status()
    if not status["key_set"]:
        out.append(_s("trainer", "Passwords are stored as plain text", "Set SECRETS_KEY on the server to protect them.", "warn"))
    if users.two_step_mode() != "required":
        out.append(_s("analyst", "2-step sign-in is not required", "Turn it on for everyone under Team and roles.", "info", go("sec-mode")))
    if not settings.COLLECT_METRICS:
        out.append(_s("analyst", "Likes and comments are not being read", "Switch them on under Agents to learn what performs.", "info", {"kind": "go", "value": "agents", "label": "Open agents"}))
    if settings.POSTS_PER_DAY > 6:
        out.append(_s("analyst", f"Up to {settings.POSTS_PER_DAY} posts a day is a lot for one page", "Most pages do best with 2 to 4 good posts a day.", "info", go("POSTS_PER_DAY")))
    return out


AREAS: Final = {"overview": _overview, "stories": _stories, "flyers": _flyers, "sources": _sources, "calendar": _calendar, "posts": _posts, "settings": _settings}


def for_area(area: str) -> list[dict[str, Any]]:
    fn = AREAS.get(area)
    if fn is None:
        return []
    try:
        return fn()
    except Exception:  # noqa: BLE001 - advice must never break a page
        logger.exception("advice.failed area=%s", area)
        return []
