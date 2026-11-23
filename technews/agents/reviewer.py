"""The **Reviewer** agent, the page's editor-in-chief. Two jobs:

1. ``select``: reads the scanner's best candidates side by side and approves only the top
   stories (impact, buzz, freshness, credibility), each with a score and the angle to post.
   A story from a single unknown outlet is never approved: a rumour stays off the page.
2. ``check_draft``: approves the writer's post before it goes out. Every number must come
   from the story itself (a model invents numbers when it is allowed to), no hype or
   banned phrases, and the lengths fit the card.

Without the NVIDIA model the selection falls back to the scanner's score plus outlet count.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Final

from technews import sources
from technews.agents import llm, team
from technews.agents.scanner import PRIORITY

logger = logging.getLogger(__name__)

PRIORITY_BOOST: Final = 2.0  # cloud and DevOps news come first
PRIORITY_BAR_DROP: Final = 1.0  # and clear a lower bar than other news

SYSTEM: Final = (
    "You are the REVIEWER agent, editor-in-chief of an Instagram page about the IT / tech world "
    "(hacks, breaches, AI, big tech, launches, outages, viral tech). The scanner sends you "
    "candidate stories. Your page posts only the TOP stories, a few a day, so be strict. Score "
    "each 0-10 on: impact (how many people or how much money it touches), buzz (would people "
    "share or comment; more outlets = more buzz), freshness, and credibility (named reputable "
    "outlets; a single unknown source is a rumour). verdict 'post' only for stories that are "
    "clearly big; 'skip' for minor updates, niche vendor news, sponsored or promotional "
    "content, opinion pieces, and anything already old. When two candidates are the same "
    "story, post at most one. CLOUD, DEVOPS AND AI NEWS ARE THE FIRST PRIORITY: a candidate with category cloud, ai "
    "or devops wins ties and is posted before anything else; for these, a major provider's (AWS, "
    "Azure, GCP) or tool's own announcement, release, earnings, outage or big deal is real news, "
    "not a press release. AN OUTAGE, BREACH OR EXPLOITED ZERO-DAY AT A MAJOR SERVICE (Microsoft 365 / Azure / Outlook / "
    "Teams, AWS, Google, Cloudflare, GitHub, OpenAI, Meta, Apple, CrowdStrike and the like) that hits many users is ALWAYS "
    "a top story: score it 9 or 10 and post it, also when the headline is about its cause or its impact. "
    "angle: one line on why it matters to an ordinary tech user (what "
    "it means for them, or what to do). reason: at most 15 words.\n"
    'Reply with ONE JSON object: {"reviews": [{"id": 12, "verdict": "post", "score": 9, '
    '"angle": "...", "reason": "..."}]} with one entry per candidate.'
)


@dataclass(frozen=True, slots=True)
class Review:
    story_id: int
    approve: bool
    score: float
    angle: str
    reason: str


def _prompt(candidates: list[dict[str, Any]]) -> str:
    rows = [
        {
            "id": c["id"],
            "headline": c["title"],
            "category": c["category"],
            "scanner_score": c["scan_score"],
            "outlets": c["outlets"][:8],
            "published_at": c["published_at"],
            "summary": c["summary"][:400],
        }
        for c in candidates
    ]
    return "CANDIDATES:\n" + json.dumps(rows, ensure_ascii=False, indent=1)


def select(candidates: list[dict[str, Any]], *, min_score: float) -> list[Review]:
    """One Review per candidate, best first. Never raises."""
    if not candidates:
        return []
    by_id = {c["id"]: c for c in candidates}
    reviews: dict[int, Review] = {}
    if llm.available():
        try:
            learned = team.reviewer_context()
            reply = llm.chat_json(system=SYSTEM, user=_prompt(candidates) + (f"\n\nWHAT THE EDITOR TAUGHT YOU:\n{learned}" if learned else ""),
                                  max_tokens=3000, temperature=0.1)  # fmt: skip
            for row in reply.data.get("reviews") or []:
                try:
                    sid = int(row["id"])
                    score = max(0.0, min(10.0, float(row.get("score") or 0)))
                    if sid in by_id and by_id[sid]["category"] in PRIORITY:
                        score = min(10.0, score + PRIORITY_BOOST)
                except (KeyError, TypeError, ValueError):
                    continue
                if sid in by_id:
                    reviews[sid] = Review(sid, str(row.get("verdict")).lower() == "post" and score >= _bar(by_id[sid], min_score),
                                          score, str(row.get("angle") or "")[:240],
                                          str(row.get("reason") or "")[:200])  # fmt: skip
            logger.info("reviewer.select model=%s candidates=%d", reply.model, len(candidates))
        except llm.LLMError as exc:
            logger.warning("reviewer.llm_failed error=%s", str(exc)[:200])
    for c in candidates:
        if c["id"] not in reviews:
            reviews[c["id"]] = fallback_review(c, min_score=min_score)
    # The rule the model can't overrule: a single unknown outlet is a rumour.
    for sid, r in reviews.items():
        if r.approve and not sources.trusted(by_id[sid]):
            reviews[sid] = Review(sid, False, r.score, r.angle, "single unverified source")
    return sorted(reviews.values(), key=lambda r: (r.approve, r.score), reverse=True)


def _bar(c: dict[str, Any], min_score: float) -> float:
    """The score a candidate needs to be approved: cloud and DevOps news clear a lower bar."""
    return min_score - PRIORITY_BAR_DROP if c["category"] in PRIORITY else min_score


def fallback_review(c: dict[str, Any], *, min_score: float) -> Review:
    score = min(10.0, float(c["scan_score"]) + min(2.0, (len(c["outlets"]) - 1) * 0.5))
    if c["category"] in PRIORITY:
        score = min(10.0, score + PRIORITY_BOOST)
    return Review(c["id"], score >= _bar(c, min_score), score, "", f"rule review: scan {c['scan_score']}, {len(c['outlets'])} outlets")


# --------------------------------------------------------------------------- draft check

BANNED: Final = ("guaranteed", "100% safe", "click the link", "link in bio to win", "giveaway",
                 "you won't believe", "shocking truth", "must buy", "buy now")  # fmt: skip
SMALL_OK: Final = {str(i) for i in range(0, 11)}
SMALL_WORDS: Final = dict(zip("zero one two three four five six seven eight nine ten".split(),
                              map(str, range(11)), strict=True))  # fmt: skip
NUMBER_WORDS: Final = re.compile(
    r"\b(eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|"
    r"forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|million|billion|trillion)\b",
    re.I,
)


def numbers(text: str) -> set[str]:
    text = re.sub(r"\b(" + "|".join(SMALL_WORDS) + r")\b", lambda m: SMALL_WORDS[m.group(1).lower()], text, flags=re.I)
    found = set()
    for raw in re.findall(r"\d[\d,]*(?:\.\d+)?", text):
        n = raw.replace(",", "")
        if "." in n:
            n = n.rstrip("0").rstrip(".")
        found.add(n)
    return found


def facts_text(story: dict[str, Any]) -> str:
    return " ".join([story["title"], story.get("summary") or "", *story.get("titles", [])])


def check_draft(draft: dict[str, Any], story: dict[str, Any]) -> list[str]:
    """What is wrong with the writer's draft (empty: approved)."""
    problems = []
    headline = str(draft.get("headline") or "")
    points = draft.get("points") if isinstance(draft.get("points"), list) else []
    caption = str(draft.get("caption") or "")
    if not 2 <= len(headline.split()) <= 10:
        problems.append("headline must be 2-10 words")
    if not 2 <= len(points) <= 3:
        problems.append("points must be a list of 2-3 short facts")
    if any(len(str(p).split()) > 18 for p in points):
        problems.append("each point at most 18 words")
    if not 15 <= len(caption.split()) <= 140:
        problems.append("caption must be 15-140 words")
    text = " ".join([headline, caption, *map(str, points)])
    invented = numbers(text) - numbers(facts_text(story)) - SMALL_OK
    if invented:
        problems.append(f"numbers not in the story: {sorted(invented)} (use only the story's numbers)")
    spelled = {m.group(0).lower() for m in NUMBER_WORDS.finditer(text)} - {"million", "billion", "trillion"}
    if spelled:
        problems.append(f"write numbers in digits copied from the story, not {sorted(spelled)}")
    low = text.lower()
    problems += [f'banned phrase "{b}"' for b in BANNED if b in low]
    return problems
