"""The **Trainer** agent: turns your decisions into written rules for the writer, designer and quality editor.

It reads the recent feedback (what you approved, rejected and edited, which layouts you switched to) and the
analyst's report, drafts a short playbook for each learning agent, and proposes it. With auto-learn on the rules
apply at once; otherwise they wait for your OK in Agents. Every version is kept, so you can go back."""

from __future__ import annotations

import logging
from collections import Counter
from statistics import median
from typing import Any, Final

from technews.agents import analyst, llm, team

logger = logging.getLogger(__name__)

MIN_DECISIONS: Final = 5


def _words(text: str) -> int:
    return len(str(text).split())


def lessons() -> dict[str, list[str]]:
    """The rules that follow from the decisions, without any AI."""
    fb = team.recent_feedback(300)
    approved = [r["detail"] for r in fb if r["kind"] == "approved"]
    rejected = [r["detail"] for r in fb if r["kind"] == "rejected"]
    edits = [r["detail"] for r in fb if r["kind"] == "edited"]
    writer, designer, editor = [], [], []
    if approved:
        lens = [_words(d.get("headline", "")) for d in approved if d.get("headline")]
        if lens:
            writer.append(f"Headlines the editor approves are about {int(median(lens))} words. Aim for that.")
    shortened = [e for e in edits if e.get("headline_before") and _words(e.get("headline_after", "")) < _words(e["headline_before"])]
    if len(edits) >= 3 and len(shortened) / len(edits) >= 0.6:
        writer.append("The editor usually shortens headlines when editing. Write shorter, plainer headlines.")
    if sum(1 for d in approved if d.get("caption_edited")) >= max(3, len(approved) // 2):
        writer.append("The editor often rewrites the caption. Keep captions short: a hook, two facts, a question.")
    if rejected:
        cats = Counter(d.get("category", "") for d in rejected if d.get("category"))
        if cats and cats.most_common(1)[0][1] >= 2:
            writer.append(f"Stories about {cats.most_common(1)[0][0]} are often rejected. Be extra careful with their wording.")
    switched = Counter((r["detail"].get("category", ""), r["detail"].get("to", "")) for r in fb if r["kind"] == "layout_changed")
    for (cat, lay), n in switched.most_common(3):
        if n >= 2 and lay:
            designer.append(f"For {cat or 'any'} stories the editor prefers the {lay} layout.")
    away = Counter(r["detail"].get("from", "") for r in fb if r["kind"] == "layout_changed")
    for lay, n in away.most_common(2):
        if n >= 3 and lay:
            designer.append(f"The editor often changes the {lay} layout. Use it less.")
    flagged_rejects = [d for d in rejected if d.get("warnings")]
    if len(flagged_rejects) >= 2:
        editor.append("Drafts you reject usually carried a warning. Flag numbers and unsupported claims even more firmly.")
    return {"writer": writer, "designer": designer, "editor": editor}


def _merge(agent: str, new: list[str]) -> str:
    """Keep the existing rules, add the new ones that are not there yet."""
    old = [ln.strip().lstrip("-• ").strip() for ln in team.playbook(agent).splitlines() if ln.strip()]
    merged = old + [n for n in new if n not in old]
    return "\n".join(f"- {line}" for line in merged[-12:])


def learn() -> dict[str, Any]:
    """Study the decisions and propose playbooks. Returns what was proposed."""
    if not team.enabled("trainer"):
        return {"skipped": "the trainer is off"}
    with team.run("trainer") as info:
        counts = team.feedback_counts()
        decided = counts.get("approved", 0) + counts.get("rejected", 0)
        if decided < MIN_DECISIONS:
            info["summary"] = f"waiting for more decisions ({decided} of {MIN_DECISIONS})"
            return {"skipped": info["summary"]}
        found = lessons()
        report = analyst.latest() or analyst.compute()
        made: dict[str, int] = {}
        for agent, new in found.items():
            text = _merge(agent, new)
            if llm.available() and new:
                try:
                    reply = llm.chat_json(
                        system="You are the TRAINER of a team of AI agents running an Instagram tech-news page. Rewrite the playbook for the "
                               f"{agent.upper()} agent as at most 10 short rules, plain and specific, keeping the useful old rules and adding the new lessons. "
                               'Reply with JSON {"rules": ["..."]}.',
                        user=f"CURRENT PLAYBOOK:\n{team.playbook(agent) or '(empty)'}\n\nNEW LESSONS:\n- " + "\n- ".join(new) + f"\n\nPERFORMANCE: {report.get('summary', '')}",
                        max_tokens=500, temperature=0.2)  # fmt: skip
                    rules = [str(r).strip() for r in (reply.data.get("rules") or []) if str(r).strip()][:10]
                    if rules:
                        text = "\n".join(f"- {r}" for r in rules)
                except llm.LLMError as exc:
                    logger.warning("trainer.llm_failed error=%s", str(exc)[:160])
            if team.propose(agent, text, "; ".join(new)[:280]):
                made[agent] = len(new)
        info["summary"] = ("proposed " + ", ".join(f"{a} ({n})" for a, n in made.items())) if made else "no new rules this time"
        return {"proposed": made}
