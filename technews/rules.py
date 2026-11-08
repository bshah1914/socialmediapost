"""Brand rules the newsroom checks before a story becomes a post: words you never want on the page, and numbers in
a draft that the source story does not contain."""

from __future__ import annotations

import re
from typing import Any

from technews.config import settings

NUMBER = re.compile(r"\d[\d,.]*\s?(?:%|[kKmMbB]\b|million|billion|thousand)?")


def blocked_words() -> list[str]:
    return [w.strip().lower() for w in re.split(r"[,\n]", settings.BLOCKED_WORDS) if w.strip()]


def blocked(*texts: str) -> str:
    """The first blocked word found in the texts ("" when none)."""
    haystack = " ".join(texts).lower()
    for word in blocked_words():
        if re.search(rf"(?<!\w){re.escape(word)}(?!\w)", haystack):
            return word
    return ""


def _digits(text: str) -> set[str]:
    return {re.sub(r"\D", "", m.group()) for m in NUMBER.finditer(text) if re.sub(r"\D", "", m.group())}


def unverified_numbers(draft_texts: list[str], story: dict[str, Any]) -> list[str]:
    """Numbers the draft states that the story's title and summary do not (the writer may have made them up)."""
    source = _digits(f"{story.get('title', '')} {story.get('summary', '')}")
    found = sorted(_digits(" ".join(draft_texts)) - source)
    return [n for n in found if len(n) > 1]


def warnings(headline: str, points: list[str], story: dict[str, Any]) -> list[str]:
    out = []
    if (bad := unverified_numbers([headline, *points], story)):
        out.append("These numbers are not in the source story, check them: " + ", ".join(bad[:5]))
    if (word := blocked(headline, *points)):
        out.append(f"Contains a blocked word: {word}")
    return out
