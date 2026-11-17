"""The **Designer** agent: picks the layout for each news card. It starts from what suits the topic, leans towards
layouts you switched *to* and away from layouts you switched *from*, and reads its playbook. With an AI model it
chooses among the best candidates; without one it follows the same rules."""

from __future__ import annotations

import json
import logging
import random
import re
from collections import Counter
from typing import Any, Final

from technews import flyers
from technews.agents import llm, team

logger = logging.getLogger(__name__)

#: Each topic draws from a varied pool spanning the classic and the newer designs, so consecutive
#: cards in the same category do not all look the same.
SUITS: Final[dict[str, list[str]]] = {
    "cloud":         ["blueprint", "aurora", "wave", "cosmic", "stage", "blur"],
    "devops":        ["console", "circuit", "blueprint", "stacked", "bento", "slash"],
    "ai":            ["aurora", "neon", "cosmic", "stage", "blur", "retro"],
    "hack":          ["impact", "slash", "gilded", "console", "frame", "stage"],
    "breach":        ["impact", "slash", "gilded", "console", "frame"],
    "vulnerability": ["impact", "console", "slash", "circuit", "stage"],
    "scam":          ["impact", "horizon", "slash", "gilded", "stage"],
    "outage":        ["impact", "slash", "stage", "blueprint", "console"],
    "bigtech":       ["studio", "bento", "minimal", "frame", "stacked", "aurora"],
    "launch":        ["horizon", "wave", "studio", "retro", "split", "bento"],
    "viral":         ["horizon", "retro", "neon", "impact", "slash", "bento"],
    "policy":        ["studio", "minimal", "blueprint", "frame", "classic"],
    "other":         ["studio", "bento", "aurora", "wave", "minimal", "cosmic", "stage"],
}  # fmt: skip
HAS_NUMBER = re.compile(r"\d[\d,.]*\s?(?:%|[kKmMbB]\b|million|billion)")


def _learned() -> tuple[Counter[str], Counter[str]]:
    to, away = Counter(), Counter()
    for r in team.recent_feedback(200):
        if r["kind"] == "layout_changed":
            to[r["detail"].get("to", "")] += 1
            away[r["detail"].get("from", "")] += 1
    return to, away


def _recent_layouts(n: int = 6) -> list[str]:
    """Layouts the last few news cards used, newest first — so we can avoid repeating them."""
    from technews import db
    from technews.config import settings

    out: list[str] = []
    for r in db.rows("SELECT meta FROM posts WHERE brand_id = ? AND (flyer = '' OR flyer IS NULL) ORDER BY id DESC LIMIT ?",
                     settings.BRAND_ID, n):
        try:
            lay = json.loads(r["meta"] or "{}").get("layout")
        except (ValueError, TypeError):
            lay = None
        if lay:
            out.append(str(lay))
    return out


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
        # drop whatever the last few cards used, so the design keeps changing
        recent = _recent_layouts()
        fresh = [lay for lay in ranked if lay not in recent[:4]] or ranked
        # rules pick: rotate among the strongest fresh options rather than always the top one
        pick, reason, by = random.choice(fresh[:4]), "a fresh look that suits the topic", "rules"
        if llm.available():
            system = ("You are the DESIGNER agent of an Instagram news page. Pick ONE layout id for this news card from the candidates, "
                      "for clarity and impact. Prefer variety — do not always pick the same one. Reply with JSON: {\"layout\": id, \"reason\": \"at most 12 words\"}.")
            if book := team.playbook("designer"):
                system += "\nYOUR PLAYBOOK (rules learned from the editor):\n" + book
            try:
                reply = llm.chat_json(system=system, user=f"category: {story.get('category')}\nheadline: {headline}\npoints: {points}\ncandidates: {fresh}\nrecently used (avoid): {recent[:4]}\n",
                                      max_tokens=120, temperature=0.5)  # fmt: skip
                if reply.data.get("layout") in fresh:
                    pick, reason, by = str(reply.data["layout"]), str(reply.data.get("reason", ""))[:120], reply.model
            except llm.LLMError as exc:
                logger.warning("designer.llm_failed error=%s", str(exc)[:160])
        info["summary"] = f"{pick} for {story.get('category', 'other')}: {reason}"
        return {"layout": pick, "reason": reason, "by": by}
