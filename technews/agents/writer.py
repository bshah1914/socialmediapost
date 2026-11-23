"""The **Writer** agent: turns an approved story into the Instagram post, i.e. the card's
headline and 2-3 key points, plus the caption and hashtags. The reviewer checks every draft
(``reviewer.check_draft``); a draft that fails goes back once with the reviewer's notes,
then the post falls back to a template built from the headline itself.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Final

from technews.agents import llm, reviewer, team
from technews.config import settings

logger = logging.getLogger(__name__)

SYSTEM: Final = (
    "You are the WRITER agent of an Instagram page about the IT / tech world. You turn one news "
    "story into a post: an image card plus a caption. Style: clear, punchy, smart; like a tech "
    "creator explaining to a friend, never clickbait that the story can't back up.\n"
    "RULES: use ONLY facts and numbers that appear in the STORY, numbers in digits copied as they "
    "are; never invent a number, a date, a victim count or a cause. Do NOT add anything from your own memory: no CVE IDs, version numbers, years, prices or names the STORY does not contain. A thin story makes a short post, not an embellished one. A claim stays a claim ('says', "
    "'claims', 'reportedly'). For a hack, breach, scam or vulnerability, include one simple step "
    "users can take to stay safe (in general terms) if it fits. Credit the outlet in the caption.\n"
    'Reply with ONE JSON object: {"headline": "2-10 words for the card, punchy, Title Case", '
    '"points": ["2-3 key facts for the card, each at most 16 words"], '
    '"caption": "the Instagram caption: a hook line, 2-4 short lines on what happened and why it '
    'matters, a question to the reader, then \'Source: <outlet>\'. 1-3 emojis max.", '
    '"hashtags": ["5 tags without #"]}'
)


@dataclass(frozen=True, slots=True)
class Draft:
    headline: str
    points: list[str]
    caption: str
    hashtags: list[str]
    writer: str  # the model, or "template"
    notes: list[str] = field(default_factory=list)  # the reviewer's last notes, if it fell back

    def full_caption(self) -> str:
        from technews import brand

        tags = " ".join(f"#{t}" for t in self.hashtags[: settings.HASHTAG_LIMIT])
        sign_off = f"🔷 {brand.NAME} · follow @{settings.IG_HANDLE} for the tech news that matters"
        return f"{self.caption.strip()}\n\n{sign_off}\n\n{tags}".strip()[:2200]


DEFAULT_TAGS: Final[dict[str, list[str]]] = {
    "cloud": ["cloudcomputing", "cloud", "aws", "azure", "technews"],
    "devops": ["devops", "kubernetes", "cloudnative", "sre", "technews"],
    "hack": ["cybersecurity", "hacking", "infosec", "technews", "cyberattack"],
    "breach": ["databreach", "cybersecurity", "privacy", "technews", "infosec"],
    "vulnerability": ["cybersecurity", "zeroday", "infosec", "technews", "patchnow"],
    "scam": ["scamalert", "cybersecurity", "onlinesafety", "technews", "phishing"],
    "ai": ["ai", "artificialintelligence", "technews", "machinelearning", "tech"],
    "outage": ["outage", "technews", "tech", "breakingnews", "internet"],
}


def _tags(raw: Any, category: str) -> list[str]:
    tags = [re.sub(r"[^A-Za-z0-9_]", "", str(t)) for t in (raw or []) if str(t).strip()]
    tags = [t for t in tags if t][: settings.HASHTAG_LIMIT]
    return tags or DEFAULT_TAGS.get(category, ["technews", "tech", "technology", "innovation", "techupdate"])


def _story_prompt(story: dict[str, Any], angle: str) -> str:
    facts = {
        "headline": story["title"],
        "summary": story.get("summary") or "",
        "other_headlines": story.get("titles", [])[1:6],
        "outlets": story["outlets"][:6],
        "category": story["category"],
        "why_it_matters (editor's angle)": angle,
    }
    return "STORY:\n" + json.dumps(facts, ensure_ascii=False, indent=1)


def write(story: dict[str, Any], *, angle: str = "") -> Draft:
    """The approved draft for ``story``. Never raises."""
    with team.run("writer") as info:
        draft = _write(story, angle)
        info["summary"] = f"{draft.writer}: {draft.headline[:70]}"
    return draft


def _write(story: dict[str, Any], angle: str) -> Draft:
    notes: list[str] = []
    if llm.available():
        user = _story_prompt(story, angle)
        if team.enabled("writer") and (context := team.writer_context()):
            user = context + "\n\n" + user
        for _attempt in range(2):
            prompt = user + ("\n\nTHE REVIEWER REJECTED YOUR LAST DRAFT. Fix every note:\n- " + "\n- ".join(notes) if notes else "")
            try:
                reply = llm.chat_json(system=SYSTEM, user=prompt, max_tokens=1200, temperature=0.6)
            except llm.LLMError as exc:
                logger.warning("writer.llm_failed error=%s", str(exc)[:200])
                break
            notes = reviewer.check_draft(reply.data, story)
            if not notes:
                d = reply.data
                return Draft(str(d["headline"]).strip(), [str(p).strip() for p in d["points"]],
                             str(d["caption"]).strip(), _tags(d.get("hashtags"), story["category"]), reply.model)  # fmt: skip
            logger.info("writer.draft_rejected notes=%s", notes)
    return template(story, angle=angle, notes=notes)


def template(story: dict[str, Any], *, angle: str = "", notes: list[str] | None = None) -> Draft:
    """A plain post from the story's own words: always true, never fancy."""
    title = story["title"]
    summary = re.split(r"(?<=[.!?])\s+", story.get("summary") or "")
    points = [s for s in summary if 4 <= len(s.split()) <= 18][:2] or [f"Reported by {', '.join(story['outlets'][:3])}"]
    if len(points) < 2:
        points.append(f"Covered by {len(story['outlets'])} outlet(s)")
    lines = [f"⚡ {title}", ""]
    if angle:
        lines += [angle, ""]
    lines += ["What do you think? Tell us below 👇", "", f"Source: {story['source']}"]
    return Draft(title, points, "\n".join(lines), _tags(None, story["category"]), "template", notes or [])


# --------------------------------------------------------------------------- a business's own video

VIDEO_SYSTEM: Final = (
    "You write the Instagram caption for a business's own video. Use ONLY facts from the LINE about the video and the "
    "BUSINESS profile; never invent prices, numbers, names, awards or promises. Plain, warm, specific words that sound like "
    "a person, not an advert. A hook line, one or two short lines, then a call to action with the contact. No hashtags. "
    'Reply with JSON {"caption": "..."}'
)
_HASHTAG = re.compile(r"(?<![\w&])#\w+")


def video_caption(about: str, title: str = "") -> str:
    """The caption for a video post: from the line given (and the business profile), with the business's hashtags (5 at most).
    Without an AI model, the line itself and the contact."""
    from technews import flyers

    line = (about or title).strip()
    contact = settings.BRAND_EMAIL or settings.BRAND_SITE
    text = f"{line}\n\nTalk to us: {contact}" if contact else line
    if llm.available() and line:
        user = f"BUSINESS: {settings.BRAND_NAME}. {settings.BRAND_ABOUT}\nCONTACT: {contact}\nLINE: {line}" + (f"\nTITLE: {title}" if title and title != line else "")
        try:
            reply = llm.chat_json(system=VIDEO_SYSTEM, user=user, max_tokens=400, temperature=0.6)
            written = _HASHTAG.sub("", str(reply.data.get("caption") or ""))
            written = "\n".join(re.sub(r"[ \t]{2,}", " ", part).strip() for part in written.splitlines()).strip()
            if written:
                text = written[:1800]
        except llm.LLMError as exc:
            logger.warning("writer.video_caption_failed error=%s", str(exc)[:160])
    tags = " ".join([w for w in flyers.hashtags().split() if w.startswith("#")][: settings.HASHTAG_LIMIT])
    return f"{text}\n\n{tags}".strip()
