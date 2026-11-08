"""The **Quality editor** agent: reads a draft before you do and flags what a careful editor would. It adds warnings
to the post in Approvals; it never blocks or changes anything by itself."""

from __future__ import annotations

import logging
import re
from typing import Any, Final

from technews import rules
from technews.agents import llm, team

logger = logging.getLogger(__name__)

HYPE: Final = ("you won't believe", "shocking", "mind-blowing", "game-changer", "game changer", "destroys", "insane", "secret they", "this changes everything")
SYSTEM: Final = (
    "You are the QUALITY EDITOR of an Instagram tech-news page. Read the DRAFT against its STORY. List up to 3 real problems: a claim the story "
    "does not support, an overstated or hyped statement, a missing 'says/reportedly' on an unproven claim, or risky wording about a named company or person. "
    'Reply with JSON {"issues": ["short sentence", ...]} and an empty list when the draft is fine.'
)


def heuristics(headline: str, points: list[str], caption: str) -> list[str]:
    out, text = [], " ".join([headline, *points, caption])
    lowered = text.lower()
    if hype := next((h for h in HYPE if h in lowered), ""):
        out.append(f"Hype wording: '{hype}'. Say what happened instead.")
    words = re.findall(r"[A-Za-z]{3,}", headline)
    if words and sum(w.isupper() for w in words) / len(words) > 0.5:
        out.append("The headline is mostly capitals.")
    if text.count("!") > 2:
        out.append("Several exclamation marks make the post sound less credible.")
    if len(headline.split()) > 14:
        out.append("The headline is long for a card; shorten it.")
    return out


def review(headline: str, points: list[str], caption: str, story: dict[str, Any]) -> list[str]:
    """Warnings for the editor (empty when the draft looks fine)."""
    if not team.enabled("editor"):
        return []
    with team.run("editor") as info:
        out = [*rules.warnings(headline, points, story), *heuristics(headline, points, caption)]
        if llm.available():
            system = SYSTEM + (f"\nYOUR PLAYBOOK (rules learned from the editor):\n{book}" if (book := team.playbook("editor")) else "")
            user = f"STORY: {story.get('title', '')}\n{story.get('summary', '')}\n\nDRAFT headline: {headline}\npoints: {points}\ncaption: {caption[:600]}"
            try:
                reply = llm.chat_json(system=system, user=user, max_tokens=300, temperature=0.1)
                out += [str(i).strip()[:160] for i in (reply.data.get("issues") or [])[:3] if str(i).strip()]
            except llm.LLMError as exc:
                logger.warning("editor.llm_failed error=%s", str(exc)[:160])
        unique = list(dict.fromkeys(out))[:5]
        info["summary"] = f"{len(unique)} note(s) on '{headline[:50]}'" if unique else f"clean: '{headline[:50]}'"
        return unique
