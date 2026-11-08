"""The **Designer** agent: picks the layout for each news card. It starts from what suits the topic, leans towards
layouts you switched *to* and away from layouts you switched *from*, and reads its playbook. With an AI model it
chooses among the best candidates; without one it follows the same rules."""

from __future__ import annotations

import logging
import re
from collections import Counter
from typing import Any, Final

from technews import flyers
from technews.agents import llm, team

logger = logging.getLogger(__name__)

SUITS: Final[dict[str, list[str]]] = {
    "cloud": ["blueprint", "aurora", "stage"], "devops": ["console", "blueprint", "bento"], "ai": ["aurora", "stage", "bento"],
    "hack": ["stage", "console", "impact"], "breach": ["impact", "stage", "console"], "vulnerability": ["impact", "console", "stage"],
    "scam": ["impact", "horizon", "stage"], "outage": ["impact", "stage", "blueprint"], "bigtech": ["studio", "bento", "aurora"],
    "launch": ["horizon", "studio", "bento"], "viral": ["horizon", "impact", "bento"], "policy": ["studio", "blueprint", "classic"],
    "other": ["studio", "bento", "aurora", "stage"],
}  # fmt: skip
HAS_NUMBER = re.compile(r"\d[\d,.]*\s?(?:%|[kKmMbB]\b|million|billion)")


def _learned() -> tuple[Counter[str], Counter[str]]:
    to, away = Counter(), Counter()
    for r in team.recent_feedback(200):
        if r["kind"] == "layout_changed":
            to[r["detail"].get("to", "")] += 1
            away[r["detail"].get("from", "")] += 1
    return to, away


def candidates(category: str, headline: str) -> list[str]:
    """Layouts best first: the topic's own, a big-number layout when the headline has a figure, then what you prefer."""
    order = list(SUITS.get(category, SUITS["other"]))
    if HAS_NUMBER.search(headline) and "impact" not in order[:1]:
        order.insert(0, "impact")
    to, away = _learned()
    scored = {lay: (len(order) - i) + 2.0 * to[lay] - 2.5 * away[lay] for i, lay in enumerate(order)}
    for lay, n in to.items():  # a layout you keep choosing can win even outside the topic's list
        if lay in flyers.LAYOUT_IDS and lay not in scored and n >= 3:
            scored[lay] = 1.0 + 2.0 * n
    return sorted(scored, key=lambda k: -scored[k])


def choose(story: dict[str, Any], headline: str, points: list[str]) -> dict[str, str]:
    """``{"layout": id or "", "reason": ..., "by": ...}``. An empty layout means the original card."""
    if not team.enabled("designer"):
        return {"layout": "", "reason": "designer is off", "by": "off"}
    with team.run("designer") as info:
        ranked = candidates(story.get("category", "other"), headline)
        pick, reason, by = ranked[0], "suits the topic and your past choices", "rules"
        if llm.available():
            system = ("You are the DESIGNER agent of an Instagram news page. Pick ONE layout id for this news card from the candidates, "
                      "for clarity and impact. Reply with JSON: {\"layout\": id, \"reason\": \"at most 12 words\"}.")
            if book := team.playbook("designer"):
                system += "\nYOUR PLAYBOOK (rules learned from the editor):\n" + book
            try:
                reply = llm.chat_json(system=system, user=f"category: {story.get('category')}\nheadline: {headline}\npoints: {points}\ncandidates: {ranked}\n",
                                      max_tokens=120, temperature=0.3)  # fmt: skip
                if reply.data.get("layout") in ranked:
                    pick, reason, by = str(reply.data["layout"]), str(reply.data.get("reason", ""))[:120], reply.model
            except llm.LLMError as exc:
                logger.warning("designer.llm_failed error=%s", str(exc)[:160])
        info["summary"] = f"{pick} for {story.get('category', 'other')}: {reason}"
        return {"layout": pick, "reason": reason, "by": by}
