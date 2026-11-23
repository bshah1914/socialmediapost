"""The **Idea creator** agent (feature N): you give it an idea, a link, or both; it writes three different flyers for
your business from only what you gave it, draws them, checks them, and sends the one you pick to Approvals.

Nothing is invented: the copy may use only facts from your idea and the page, and the quality editor flags any number
that is not in them. A link is fetched safely (public web addresses only, a small download, no private networks)."""

from __future__ import annotations

import base64
import difflib
import io
import ipaddress
import logging
import re
import socket
from html.parser import HTMLParser
from typing import Any, Final
from urllib.parse import urljoin, urlparse

import requests

from technews import brands, db, flyers, pipeline
from technews.agents import designer, editor, llm, team
from technews.config import settings

logger = logging.getLogger(__name__)

GOALS: Final = {"promote": "Promote a service or offer", "announce": "Announce news or an update", "educate": "Teach something useful", "trust": "Build trust (proof, partners, how we work)"}
TONES: Final = {"professional": "Professional and clear", "friendly": "Friendly and simple", "bold": "Bold and punchy"}
CTA: Final = {"promote": "Talk to our engineers", "announce": "Read the details", "educate": "Learn more", "trust": "See how we work"}
MAX_PAGE_BYTES: Final = 1_000_000


# --------------------------------------------------------------------------- reading a link, safely


def _public(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            return False
    return bool(infos)


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title, self.desc, self.parts, self._skip, self._in_title = "", "", [], 0, False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag in ("script", "style", "noscript", "svg", "nav", "footer", "header"):
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "meta" and (a.get("name") in ("description",) or a.get("property") in ("og:description",)) and not self.desc:
            self.desc = (a.get("content") or "").strip()
        elif tag == "meta" and a.get("property") == "og:title" and not self.title:
            self.title = (a.get("content") or "").strip()

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript", "svg", "nav", "footer", "header"):
            self._skip = max(0, self._skip - 1)
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        text = " ".join(data.split())
        if self._in_title and not self.title:
            self.title = text
        elif text and not self._skip and len(text) > 25:
            self.parts.append(text)


def fetch_page(url: str) -> dict[str, str]:
    """Title, description and main text of a public web page. Raises ValueError with what went wrong."""
    url = url.strip()
    for _hop in range(4):
        parts = urlparse(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("The link must start with http:// or https://")
        if not _public(parts.hostname):
            raise ValueError("That address is not a public website.")
        try:
            resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0 CloudTrioNewsroom/1.0"}, timeout=10, allow_redirects=False, stream=True)
        except requests.RequestException as exc:
            raise ValueError(f"The page could not be opened ({type(exc).__name__}).") from exc
        if resp.is_redirect and resp.headers.get("location"):
            url = urljoin(url, resp.headers["location"])
            continue
        if resp.status_code >= 400:
            raise ValueError(f"The page answered with an error ({resp.status_code}).")
        body = resp.raw.read(MAX_PAGE_BYTES, decode_content=True)
        parser = _Text()
        parser.feed(body.decode(resp.encoding or "utf-8", errors="ignore"))
        text = " ".join(parser.parts)[:4000]
        if not (parser.title or parser.desc or text):
            raise ValueError("The page has no readable text.")
        return {"url": url, "title": parser.title[:200], "description": parser.desc[:400], "text": text}
    raise ValueError("The link redirects too many times.")


# --------------------------------------------------------------------------- the brief and the copy


def _business() -> str:
    offers = "; ".join(f"{f.kicker.title()}: {f.headline}" for f in flyers.listing()[:7])
    if settings.BRAND_ABOUT:
        return (f"{settings.BRAND_NAME}. What it does: {settings.BRAND_ABOUT.rstrip('.')}. Website {settings.BRAND_SITE}, contact {settings.BRAND_EMAIL}. "
                f"What the business offers today: {offers or 'not listed'}.")
    return f"{settings.BRAND_NAME}. Website {settings.BRAND_SITE}, contact {settings.BRAND_EMAIL}. What the business offers today: {offers or 'not listed'}."


def _clamp(v: dict[str, Any]) -> dict[str, Any]:
    pts = [str(p).strip()[:120] for p in (v.get("points") or []) if str(p).strip()][:4]
    return {"kicker": str(v.get("kicker", "")).strip()[:30].upper() or "NEW", "headline": str(v.get("headline", "")).strip()[:120],
            "points": pts, "cta": str(v.get("cta", "")).strip()[:40] or "Talk to our engineers",
            "note": str(v.get("note", "")).strip()[:140], "caption": str(v.get("caption", "")).strip()[:1500], "angle": str(v.get("angle", "")).strip()[:40]}  # fmt: skip


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if len(s.split()) >= 4]


def fallback(idea: str, page: dict[str, str], goal: str) -> list[dict[str, Any]]:
    """One plain flyer from the words given (used when no AI model is available)."""
    base = idea.strip() or page.get("description") or page.get("title") or "New from us"
    sents = _sentences(" ".join([idea, page.get("description", ""), page.get("title", "")]))
    headline = " ".join((page.get("title") or (sents[0] if sents else base)).split()[:9]).rstrip(".,:;")
    points = [" ".join(s.split()[:16]) for s in sents[1:4]] or [" ".join(base.split()[:16])]
    return [_clamp({"kicker": goal.upper() if goal != "promote" else "WHAT WE DO", "headline": headline, "points": points, "cta": CTA.get(goal, "Talk to our engineers"),
                    "note": "Free consultation. No obligation.", "caption": f"{base[:300]}\n\n{CTA.get(goal, 'Talk to our engineers')}: {settings.BRAND_EMAIL}", "angle": "your words"})]  # fmt: skip


def _ask(idea: str, page: dict[str, str], goal: str, tone: str) -> list[dict[str, Any]]:
    system = (
        "You are the IDEA CREATOR of a business Instagram page. Turn the user's IDEA and/or the PAGE into 3 DIFFERENT marketing flyers for the BUSINESS. "
        "Use ONLY facts that appear in the IDEA or the PAGE. Never invent prices, discounts, numbers, client names, awards or guarantees; if you make a claim, keep it modest. "
        "Each variant has its own angle (for example: the problem, the benefit, how it works, the offer). No hashtags in the caption. Copy must be plain, specific, and sound like a person, not an advert.\n"
        f"GOAL: {GOALS.get(goal, goal)}. TONE: {TONES.get(tone, tone)}.\nBUSINESS: {_business()}\n"
        'Reply with JSON {"variants": [{"angle": "2 words", "kicker": "up to 3 words", "headline": "up to 9 words", "points": ["3 points, each up to 14 words"], '
        '"cta": "up to 4 words", "note": "small print or condition, up to 100 characters", "caption": "hook line, 2 short lines, then a call to action with the contact"}]}'
    )
    if book := team.playbook("creator"):
        system += "\nYOUR PLAYBOOK (rules learned from the editor, follow them):\n" + book
    user = f"IDEA:\n{idea or '(none)'}\n\nPAGE:\n" + (f"{page.get('title', '')}\n{page.get('description', '')}\n{page.get('text', '')[:2500]}" if page else "(none)")
    reply = llm.chat_json(system=system, user=user, max_tokens=1800, temperature=0.8)
    return [_clamp(v) for v in (reply.data.get("variants") or []) if isinstance(v, dict)][:3]


def _thumb(data: dict[str, Any]) -> str:
    buf = io.BytesIO()
    flyers.render(flyers.parse({**data, "enabled": True}), buf)
    from PIL import Image

    img = Image.open(io.BytesIO(buf.getvalue()))
    img.thumbnail((540, 675))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=80)
    return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode()


def generate(idea: str, url: str = "", goal: str = "promote", tone: str = "professional") -> dict[str, Any]:
    """Three drawn, checked flyer options. Raises ValueError for bad input."""
    idea, url = idea.strip()[:2000], url.strip()
    if not idea and not url:
        raise ValueError("Type an idea or paste a link first.")
    if not team.enabled("creator"):
        raise ValueError("The Idea creator is switched off (Agents).")
    if not brands.has("ideas"):
        raise ValueError("Ideas are switched off for this business.")
    goal, tone = (goal if goal in GOALS else "promote"), (tone if tone in TONES else "professional")
    with team.run("creator") as info:
        page: dict[str, str] = {}
        if url:
            page = fetch_page(url)
        variants, by = [], "rules"
        if llm.available():
            try:
                variants, by = _ask(idea, page, goal, tone), "ai"
            except llm.LLMError as exc:
                logger.warning("creator.llm_failed error=%s", str(exc)[:160])
        if not variants:
            variants, by = fallback(idea, page, goal), "rules"
        existing = [f.headline for f in flyers.listing()] + [r["headline"] for r in db.rows("SELECT headline FROM posts WHERE brand_id = ? ORDER BY id DESC LIMIT 40", settings.BRAND_ID)]
        source = {"title": (idea or page.get("title", ""))[:200], "summary": f"{idea} {page.get('title', '')} {page.get('description', '')} {page.get('text', '')[:2000]}"}
        out, layouts_used = [], []
        for i, v in enumerate(variants):
            ranked = [c for c in designer.candidates("other", v["headline"]) if c not in layouts_used] or designer.candidates("other", v["headline"])
            v["layout"] = ranked[0]
            layouts_used.append(v["layout"])
            palette = flyers.palettes_for(" ".join([v["headline"], *v["points"]]))[i % 3] if i else None
            v["style"] = {k: palette[k] for k in ("bg", "bg2", "text", "accent")} if palette else {}
            try:
                flyers.parse({**v, "enabled": True})
            except ValueError as exc:
                logger.warning("creator.variant_invalid error=%s", exc)
                continue
            warnings = editor.review(v["headline"], v["points"], v["caption"], source)
            similar = max(existing, key=lambda h: difflib.SequenceMatcher(None, h.lower(), v["headline"].lower()).ratio(), default="")
            if similar and difflib.SequenceMatcher(None, similar.lower(), v["headline"].lower()).ratio() > 0.8:
                warnings.append(f"Very similar to an earlier post: '{similar[:60]}'.")
            out.append({**v, "warnings": warnings, "preview": _thumb(v)})
        if not out:
            raise ValueError("The copy did not fit a flyer. Try a clearer idea.")
        idea_id = db.execute("INSERT INTO ideas (brand_id, at, idea, url, goal, tone, status, flyer_id, post_id) VALUES (?,?,?,?,?,?,?,?,?)",
                             settings.BRAND_ID, db.now(), idea[:600], url[:300], goal, tone, "generated", "", 0)  # fmt: skip
        info["summary"] = f"{len(out)} option(s) from {'a link and ' if url else ''}{'your idea' if idea else 'the link'} ({by})"
        return {"idea_id": idea_id, "variants": out, "ai": by == "ai", "page": {"title": page.get("title", ""), "url": page.get("url", "")} if page else None}


def send(idea_id: int, variant: dict[str, Any], *, rotate: bool = False) -> dict[str, Any]:
    """Save the chosen variant as a flyer (in the rotation only if asked) and hold a post for approval. Raises ValueError."""
    clean = {**_clamp(variant), "layout": variant.get("layout") or "classic", "style": variant.get("style") or {}, "enabled": rotate}
    saved = flyers.save(clean)
    result = pipeline.draft_flyer(flyers.get(saved.id))
    db.execute("UPDATE ideas SET status = 'sent', flyer_id = ?, post_id = ? WHERE id = ? AND brand_id = ?", saved.id, result["post_id"], idea_id, settings.BRAND_ID)
    team.feedback("idea_sent", result["post_id"], headline=saved.headline, layout=saved.layout)
    return {"flyer_id": saved.id, "post_id": result["post_id"]}


def history(limit: int = 8) -> list[dict[str, Any]]:
    return db.rows("SELECT id, at, idea, url, goal, status, flyer_id, post_id FROM ideas WHERE brand_id = ? ORDER BY id DESC LIMIT ?", settings.BRAND_ID, limit)
