"""The **Source scout** agent: keeps the list of news sources alive.

It watches how each feed behaves (does it answer, how many stories, how many you approve), suggests switching off
feeds that have died or only add noise, and looks for new cloud, DevOps and AI feeds, testing each one before it
is suggested. You approve each change on the Sources page, or switch on "let the scout manage sources" and it adds
a couple of tested feeds and switches off dead ones by itself. It never deletes a source."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from technews import db, sources
from technews.agents import llm, team

logger = logging.getLogger(__name__)

AUTO_KEY: Final = "agents:auto_sources"
DEAD_AFTER: Final = timedelta(hours=72)  # no successful fetch for this long
NOISY_STORIES: Final = 15  # in 14 days, with none of them approved
MAX_AUTO_ADD: Final = 2

#: Feeds worth trying. Each one is fetched before it is suggested, so a moved or blocked feed never reaches you.
CANDIDATES: Final[tuple[tuple[str, str], ...]] = (
    ("Cloudflare Blog", "https://blog.cloudflare.com/rss/"),
    ("Red Hat Blog", "https://www.redhat.com/en/rss/blog"),
    ("Datadog Blog", "https://www.datadoghq.com/blog/index.xml"),
    ("Grafana Blog", "https://grafana.com/blog/index.xml"),
    ("GitLab Blog", "https://about.gitlab.com/atom.xml"),
    ("AWS Security Blog", "https://aws.amazon.com/blogs/security/feed/"),
    ("Microsoft Security Blog", "https://www.microsoft.com/en-us/security/blog/feed/"),
    ("Google Security Blog", "https://security.googleblog.com/feeds/posts/default"),
    ("DevOps.com", "https://devops.com/feed/"),
    ("Cloud Native Now", "https://cloudnativenow.com/feed/"),
    ("InfoWorld Cloud", "https://www.infoworld.com/category/cloud-computing/index.rss"),
    ("SiliconANGLE", "https://siliconangle.com/feed/"),
    ("VentureBeat AI", "https://venturebeat.com/category/ai/feed/"),
    ("Hugging Face Blog", "https://huggingface.co/blog/feed.xml"),
    ("OpenAI News", "https://openai.com/news/rss.xml"),
    ("Stack Overflow Blog", "https://stackoverflow.blog/feed/"),
)  # fmt: skip


def auto() -> bool:
    return (db.kv_get(AUTO_KEY) or "0") == "1"


def set_auto(on: bool) -> None:
    db.kv_set(AUTO_KEY, "1" if on else "0")


# --------------------------------------------------------------------------- what the feeds do


def health(source_id: int) -> dict[str, Any]:
    found = db.rows("SELECT * FROM source_health WHERE source_id = ?", source_id)
    return found[0] if found else {"ok_at": "", "first_fail_at": "", "fails": 0, "items": 0, "error": ""}


def state(row: dict[str, Any], h: dict[str, Any]) -> str:
    """ok | slow (a few failures) | dead (nothing for three days) | new (never fetched)."""
    if not h["ok_at"] and not h["fails"]:
        return "new"
    ref = h["ok_at"] or h["first_fail_at"]
    if ref and datetime.now(UTC) - datetime.fromisoformat(ref) > DEAD_AFTER and h["fails"] > 0:
        return "dead"
    return "slow" if h["fails"] >= 3 else "ok"


def feed_stats(days: int = 14) -> dict[str, dict[str, int]]:
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
    out: dict[str, dict[str, int]] = {}
    for r in db.rows("SELECT source, status FROM stories WHERE found_at >= ?", since):
        s = out.setdefault(r["source"], {"found": 0, "used": 0})
        s["found"] += 1
        s["used"] += r["status"] in ("approved", "posted", "pending")
    return out


def _idea(action: str, name: str, value: str, reason: str, source_id: int = 0) -> int:
    dup = db.rows("SELECT id FROM source_ideas WHERE action = ? AND value = ? AND status IN ('pending', 'applied')", action, value)
    return 0 if dup else db.execute("INSERT INTO source_ideas (action, name, value, reason, source_id, at, status) VALUES (?,?,?,?,?,?,?)",
                                    action, name, value, reason[:300], source_id, db.now(), "pending")  # fmt: skip


def retirements() -> list[dict[str, Any]]:
    """Feeds to switch off, and why."""
    out, stats = [], feed_stats()
    for r in sources.listing():
        if r["kind"] != "feed" or not r["enabled"]:
            continue
        h = health(r["id"])
        if state(r, h) == "dead":
            out.append({"row": r, "reason": f"It has not answered for over 3 days ({h['error'][:80] or 'no reply'})."})
        elif (st := stats.get(r["name"])) and st["found"] >= NOISY_STORIES and st["used"] == 0:
            out.append({"row": r, "reason": f"{st['found']} stories in 14 days and none were approved."})
    return out


# --------------------------------------------------------------------------- new feeds


def _probe(url: str) -> dict[str, Any] | None:
    """The feed, if it answers with something recent: ``{"count", "newest_days", "title"}``."""
    try:
        items = sources.parse_feed("probe", sources._get(url))
    except Exception:  # noqa: BLE001 - any failure means "not usable"
        return None
    if not items:
        return None
    newest = max(i.published_at for i in items)
    days = (datetime.now(UTC) - newest).days
    return {"count": len(items), "newest_days": days, "title": items[0].title} if days <= 14 else None


def _ask_model(known: set[str]) -> list[tuple[str, str]]:
    if not llm.available():
        return []
    try:
        reply = llm.chat_json(
            system="You help an Instagram page about cloud, DevOps, security and AI news find RSS feeds. Name up to 8 well-known, reliable feeds "
                   "(official company blogs and respected news sites) that are NOT in the KNOWN list. Give the real feed URL only if you are sure. "
                   'Reply with JSON {"feeds": [{"name": "...", "url": "https://..."}]}.',
            user="KNOWN: " + ", ".join(sorted(known))[:1500], max_tokens=600, temperature=0.3)  # fmt: skip
    except llm.LLMError as exc:
        logger.warning("scout.llm_failed error=%s", str(exc)[:160])
        return []
    return [(str(f.get("name", "")).strip()[:60], str(f.get("url", "")).strip()) for f in (reply.data.get("feeds") or [])
            if str(f.get("url", "")).startswith("https://") and str(f.get("name", "")).strip()][:8]  # fmt: skip


def discover(limit: int = 4) -> list[dict[str, Any]]:
    """Test feeds that are not on the list yet and return the ones that work, best first."""
    have = {r["value"].rstrip("/") for r in sources.listing()}
    names = {r["name"] for r in sources.listing()}
    dismissed = {r["value"] for r in db.rows("SELECT value FROM source_ideas WHERE action = 'add' AND status = 'dismissed'")}
    tried = [(n, u) for n, u in [*CANDIDATES, *_ask_model(names)] if u.rstrip("/") not in have and n not in names and u not in dismissed]
    found = []
    for name, url in dict.fromkeys(tried):
        if len(found) >= limit:
            break
        if p := _probe(url):
            found.append({"name": name, "url": url, **p})
    return found


# --------------------------------------------------------------------------- the daily job


def run() -> dict[str, Any]:
    """Look at the sources, suggest what to change, and (if you let it) change it."""
    if not team.enabled("scout"):
        return {"skipped": "the scout is off"}
    with team.run("scout") as info:
        added, switched_off, ideas = [], [], 0
        for item in retirements():
            row = item["row"]
            if auto():
                sources.set_enabled(row["id"], False)
                db.execute("INSERT INTO source_ideas (action, name, value, reason, source_id, at, status) VALUES ('disable',?,?,?,?,?,'applied')",
                           row["name"], row["value"], item["reason"], row["id"], db.now())  # fmt: skip
                switched_off.append(row["name"])
            elif _idea("disable", row["name"], row["value"], item["reason"], row["id"]):
                ideas += 1
        for f in discover():
            reason = f"Works: {f['count']} recent items, newest {f['newest_days']} day(s) old. Latest: {f['title'][:70]}"
            if auto() and len(added) < MAX_AUTO_ADD:
                try:
                    sources.add("feed", f["name"], f["url"])
                except ValueError:
                    continue
                db.execute("INSERT INTO source_ideas (action, name, value, reason, source_id, at, status) VALUES ('add',?,?,?,0,?,'applied')", f["name"], f["url"], reason, db.now())
                added.append(f["name"])
            elif _idea("add", f["name"], f["url"], reason):
                ideas += 1
        info["summary"] = (f"added {', '.join(added)}; " if added else "") + (f"switched off {', '.join(switched_off)}; " if switched_off else "") + f"{ideas} new suggestion(s)"
        return {"added": added, "switched_off": switched_off, "suggested": ideas}


def ideas(status: str = "pending") -> list[dict[str, Any]]:
    return db.rows("SELECT id, action, name, value, reason, source_id, at FROM source_ideas WHERE status = ? ORDER BY id DESC LIMIT 30", status)


def decide(idea_id: int, accept: bool) -> bool:
    found = db.rows("SELECT * FROM source_ideas WHERE id = ? AND status = 'pending'", idea_id)
    if not found:
        return False
    idea = found[0]
    if accept:
        if idea["action"] == "add":
            sources.add("feed", idea["name"], idea["value"])
        else:
            sources.set_enabled(idea["source_id"], False)
    db.execute("UPDATE source_ideas SET status = ? WHERE id = ?", "applied" if accept else "dismissed", idea_id)
    team.feedback("source_" + ("accepted" if accept else "dismissed"), 0, action=idea["action"], name=idea["name"])
    return True
