"""The newsroom team: who the agents are, what each has learned, and what each has done.

Agents never retrain a model. They *learn* the way a new editor does: every decision you make (approve, reject, edit
a headline, change a layout, rewrite) is recorded, the trainer turns the pattern into short written rules (a
playbook), and the writer, designer and editor agents read their playbook before every job. You can read, change
or switch off any playbook; with auto-learn off, new rules wait for your OK.
"""

from __future__ import annotations

import json
import logging
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from technews import db
from technews.config import settings

logger = logging.getLogger(__name__)

ROSTER: Final[dict[str, dict[str, Any]]] = {
    "scanner": {"name": "Scanner", "role": "Reads the feeds and picks the stories worth a look.", "switch": False, "learns": False},
    "reviewer": {"name": "Reviewer", "role": "Scores each story, checks it against more than one source, and checks every draft. Learns from what you approve, reject and mark as missed.", "switch": False, "learns": False},
    "writer": {"name": "Content writer", "role": "Writes the headline, key points and caption in your page's voice.", "switch": True, "learns": True},
    "creator": {"name": "Idea creator", "role": "Turns your idea or a link into three flyer options for your business, using only what you give it.", "switch": True, "learns": True},
    "designer": {"name": "Designer", "role": "Chooses the layout for each news card and checks it reads well.", "switch": True, "learns": True},
    "editor": {"name": "Quality editor", "role": "Reads each draft before you do and flags weak claims, hype and risky wording.", "switch": True, "learns": True},
    "scout": {"name": "Source scout", "role": "Keeps the news sources alive: finds new feeds, tests them, and switches off dead or noisy ones.", "switch": True, "learns": False},
    "analyst": {"name": "Performance analyst", "role": "Studies what is approved, edited and (optionally) liked, and says what works.", "switch": True, "learns": False},
    "trainer": {"name": "Trainer", "role": "Turns your decisions into written rules the other agents follow.", "switch": True, "learns": False},
}
AUTO_LEARN_KEY: Final = "agents:auto_learn"
PLAYBOOK_MAX: Final = 1500


def enabled(agent: str) -> bool:
    return (db.kv_get(f"agent:{agent}:on") or "1") == "1"


def set_enabled(agent: str, on: bool) -> None:
    if agent not in ROSTER or not ROSTER[agent]["switch"]:
        raise ValueError("That agent cannot be switched off")
    db.kv_set(f"agent:{agent}:on", "1" if on else "0")


def auto_learn() -> bool:
    return (db.kv_get(AUTO_LEARN_KEY) or "0") == "1"


def set_auto_learn(on: bool) -> None:
    db.kv_set(AUTO_LEARN_KEY, "1" if on else "0")


# --------------------------------------------------------------------------- playbooks


def playbook(agent: str) -> str:
    found = db.rows("SELECT text FROM playbooks WHERE agent = ? AND brand_id = ? ORDER BY id DESC LIMIT 1", agent, settings.BRAND_ID)
    return found[0]["text"] if found else ""


def set_playbook(agent: str, text: str, by: str = "you") -> None:
    if agent not in ROSTER or not ROSTER[agent]["learns"]:
        raise ValueError("That agent has no playbook")
    text = text.strip()[:PLAYBOOK_MAX]
    db.execute("INSERT INTO playbooks (agent, brand_id, text, at, by) VALUES (?,?,?,?,?)", agent, settings.BRAND_ID, text, db.now(), by)


def playbook_history(agent: str, limit: int = 5) -> list[dict[str, Any]]:
    return db.rows("SELECT text, at, by FROM playbooks WHERE agent = ? AND brand_id = ? ORDER BY id DESC LIMIT ?", agent, settings.BRAND_ID, limit)


# --------------------------------------------------------------------------- what the team did


@contextmanager
def run(agent: str) -> Iterator[dict[str, Any]]:
    """Time one job and record it. The caller fills ``info["summary"]``; an exception is recorded and re-raised."""
    info: dict[str, Any] = {"summary": ""}
    started = time.monotonic()
    _busy(agent, True)
    try:
        yield info
    except Exception as exc:
        _log(agent, False, f"{type(exc).__name__}: {exc}"[:300], started)
        raise
    else:
        _log(agent, True, str(info["summary"])[:300], started)
    finally:
        _busy(agent, False)


def _busy(agent: str, on: bool) -> None:
    """Mark an agent as working. Kept in the database so the website sees jobs done by the worker."""
    try:
        db.kv_set(f"agent:{agent}:busy", db.now() if on else "")
    except Exception:  # noqa: BLE001 - a status marker must never break a job
        logger.exception("busy marker failed")


def _log(agent: str, ok: bool, summary: str, started: float) -> None:
    try:
        db.execute("INSERT INTO agent_runs (agent, brand_id, at, ok, summary, ms) VALUES (?,?,?,?,?,?)",
                   agent, settings.BRAND_ID, db.now(), int(ok), summary, int((time.monotonic() - started) * 1000))  # fmt: skip
    except Exception:  # noqa: BLE001 - the log must never break a job
        logger.exception("agent run log failed")


def activity(limit: int = 25) -> list[dict[str, Any]]:
    return db.rows("SELECT agent, at, ok, summary, ms FROM agent_runs WHERE brand_id = ? ORDER BY id DESC LIMIT ?", settings.BRAND_ID, limit)


DUTIES: Final[dict[str, dict[str, str]]] = {
    "scanner": {"works_on": "New stories from your sources", "when": "Every newsroom cycle"},
    "reviewer": {"works_on": "Candidate stories and every finished draft", "when": "Every newsroom cycle, and before a post is made"},
    "writer": {"works_on": "Headlines, key points and captions", "when": "When a story or idea is turned into a post"},
    "creator": {"works_on": "Ideas and links from the Create page", "when": "When you ask for options"},
    "designer": {"works_on": "The layout of each news card", "when": "When a post is drafted"},
    "editor": {"works_on": "Drafts waiting for your approval", "when": "When a post is drafted"},
    "scout": {"works_on": "The list of news sources", "when": "Nightly, or when you press Find sources"},
    "analyst": {"works_on": "Approval rates, edits and likes", "when": "On request, and in the 6-hourly metrics job"},
    "trainer": {"works_on": "Your decisions, turned into playbook rules", "when": "Nightly at 03:30, or when you press Learn"},
}
BUSY_FOR_MINUTES: Final = 10


def board() -> dict[str, dict[str, Any]]:
    """One tile per agent: is it working, how its last day went, and its last job."""
    now = datetime.now(UTC)
    since = (now - timedelta(hours=24)).isoformat(timespec="seconds")
    runs = db.rows("SELECT agent, at, ok, summary, ms FROM agent_runs WHERE brand_id = ? AND at >= ? ORDER BY id DESC LIMIT 2000", settings.BRAND_ID, since)
    out: dict[str, dict[str, Any]] = {}
    for key, meta in ROSTER.items():
        mine = [r for r in runs if r["agent"] == key]
        last = mine[0] if mine else (db.rows("SELECT at, ok, summary, ms FROM agent_runs WHERE agent = ? AND brand_id = ? ORDER BY id DESC LIMIT 1", key, settings.BRAND_ID) or [None])[0]
        hours = [0] * 24
        for r in mine:
            age = int((now - _when(r["at"])).total_seconds() // 3600)
            if 0 <= age < 24:
                hours[23 - age] += 1
        busy = db.kv_get(f"agent:{key}:busy") or ""
        working = bool(busy) and (now - _when(busy)).total_seconds() < BUSY_FOR_MINUTES * 60
        failed = sum(1 for r in mine if not r["ok"])
        if meta["switch"] and not enabled(key):
            state = "off"
        elif working:
            state = "working"
        elif last and not last["ok"]:
            state = "attention"
        elif last:
            state = "idle"
        else:
            state = "new"
        timed = [r["ms"] for r in mine if r["ms"]]
        out[key] = {**DUTIES[key], "state": state, "jobs": len(mine), "failed": failed, "avg_ms": int(sum(timed) / len(timed)) if timed else 0,
                    "hours": hours, "last": {"at": last["at"], "ok": bool(last["ok"]), "summary": last["summary"]} if last else None}
    return out


def _when(stamp: str) -> datetime:
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return datetime.fromtimestamp(0, UTC)
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def stats(agent: str) -> dict[str, Any]:
    rows = db.rows("SELECT ok, at FROM agent_runs WHERE agent = ? AND brand_id = ? ORDER BY id DESC LIMIT 50", agent, settings.BRAND_ID)
    return {"runs": len(rows), "ok": sum(r["ok"] for r in rows), "last": rows[0]["at"] if rows else ""}


# --------------------------------------------------------------------------- what you decided


def feedback(kind: str, post_id: int, **detail: Any) -> None:
    """Record one decision (approved, rejected, edited, rewritten, scheduled). Never raises."""
    try:
        db.execute("INSERT INTO feedback (at, brand_id, post_id, kind, detail) VALUES (?,?,?,?,?)",
                   db.now(), settings.BRAND_ID, post_id, kind, json.dumps(detail, ensure_ascii=False)[:1500])  # fmt: skip
    except Exception:  # noqa: BLE001
        logger.exception("feedback log failed")


def recent_feedback(limit: int = 200) -> list[dict[str, Any]]:
    out = db.rows("SELECT at, post_id, kind, detail FROM feedback WHERE brand_id = ? ORDER BY id DESC LIMIT ?", settings.BRAND_ID, limit)
    for r in out:
        try:
            r["detail"] = json.loads(r["detail"] or "{}")
        except ValueError:
            r["detail"] = {}
    return out


def feedback_counts() -> dict[str, int]:
    return dict(Counter(r["kind"] for r in recent_feedback()))


def examples(kind: str, key: str = "headline", limit: int = 6) -> list[str]:
    """Recent headlines (or another detail) behind one kind of decision."""
    return [str(r["detail"].get(key)) for r in recent_feedback(120) if r["kind"] == kind and r["detail"].get(key)][:limit]


def reviewer_context() -> str:
    """What the reviewer should know before choosing: the stories the editor said were missed, and what the editor
    approved and turned down lately. This is how the reviewer learns what this page wants."""
    parts = []
    if (missed := examples("missed", limit=8)):
        parts.append("Stories the editor said this page should NOT have missed (score stories like these high):\n- " + "\n- ".join(missed))
    if (good := examples("approved", limit=8)):
        parts.append("Posts the editor APPROVED lately (topics this page wants):\n- " + "\n- ".join(good))
    if (bad := examples("rejected", limit=8)):
        parts.append("Posts the editor REJECTED lately (topics to score lower):\n- " + "\n- ".join(bad))
    return "\n\n".join(parts)


def writer_context() -> str:
    """What the writer should know before writing: your rules, and what you approved and turned down lately."""
    parts = []
    if (book := playbook("writer")):
        parts.append("HOUSE STYLE (rules learned from the editor's decisions, follow them):\n" + book)
    if (good := examples("approved")):
        parts.append("Headlines the editor APPROVED lately (this is the voice):\n- " + "\n- ".join(good))
    if (bad := examples("rejected")):
        parts.append("Headlines the editor REJECTED lately (avoid this style):\n- " + "\n- ".join(bad))
    return "\n\n".join(parts)


# --------------------------------------------------------------------------- proposals from the trainer


def propose(agent: str, text: str, reason: str) -> int:
    """A new playbook from the trainer. Applied at once with auto-learn on; otherwise it waits for you."""
    text = text.strip()[:PLAYBOOK_MAX]
    if not text or text == playbook(agent):
        return 0
    if auto_learn():
        set_playbook(agent, text, by="trainer")
        return db.execute("INSERT INTO proposals (agent, brand_id, text, reason, at, status) VALUES (?,?,?,?,?,?)",
                          agent, settings.BRAND_ID, text, reason[:300], db.now(), "applied")  # fmt: skip
    return db.execute("INSERT INTO proposals (agent, brand_id, text, reason, at, status) VALUES (?,?,?,?,?,?)",
                      agent, settings.BRAND_ID, text, reason[:300], db.now(), "pending")  # fmt: skip


def proposals(status: str = "pending") -> list[dict[str, Any]]:
    return db.rows("SELECT id, agent, text, reason, at FROM proposals WHERE brand_id = ? AND status = ? ORDER BY id DESC LIMIT 20", settings.BRAND_ID, status)


def decide(proposal_id: int, accept: bool) -> bool:
    found = db.rows("SELECT * FROM proposals WHERE id = ? AND status = 'pending'", proposal_id)
    if not found:
        return False
    if accept:
        set_playbook(found[0]["agent"], found[0]["text"], by="trainer (approved)")
    db.execute("UPDATE proposals SET status = ? WHERE id = ?", "applied" if accept else "dismissed", proposal_id)
    return True
