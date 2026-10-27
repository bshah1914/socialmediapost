"""Where the IT / tech news comes from, and how one story told by many outlets becomes one
story (the duplicate logic is CopyTrading's newsroom ``same_story``).

The sources are tech and security outlets plus Google News keyword searches for the things
that go viral: hacks, breaches, zero-days, outages, big AI launches. Hacker News's front page
(300+ points) catches what the tech crowd is talking about.
"""

from __future__ import annotations

import hashlib
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final

import feedparser
import requests

logger = logging.getLogger(__name__)

UA: Final = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129 Safari/537.36"
TIMEOUT: Final = 20

FEEDS: Final[dict[str, str]] = {
    "The Hacker News": "https://feeds.feedburner.com/TheHackersNews",
    "BleepingComputer": "https://www.bleepingcomputer.com/feed/",
    "Krebs on Security": "https://krebsonsecurity.com/feed/",
    "SecurityWeek": "https://www.securityweek.com/feed/",
    "Dark Reading": "https://www.darkreading.com/rss.xml",
    "TechCrunch": "https://techcrunch.com/feed/",
    "The Verge": "https://www.theverge.com/rss/index.xml",
    "Ars Technica": "https://feeds.arstechnica.com/arstechnica/index",
    "Wired": "https://www.wired.com/feed/rss",
    "Hacker News": "https://hnrss.org/frontpage?points=300",
}

GOOGLE_NEWS: Final = "https://news.google.com/rss/search"
SEARCHES: Final[tuple[str, ...]] = (
    "data breach",
    "ransomware attack",
    "zero-day exploited",
    "hackers cyber attack",
    "outage down worldwide",
    "AI model launch",
    "OpenAI OR Nvidia OR Google OR Microsoft OR Apple OR Meta announces",
    "crypto exchange hacked",
    "viral tech",
)

#: Outlets whose word is enough on its own; anything else wants a second outlet.
TRUSTED: Final = frozenset(
    name.lower()
    for name in (*FEEDS, "Reuters", "Bloomberg", "The Record", "CNBC", "BBC", "The Guardian",
                 "Associated Press", "Engadget", "ZDNET", "The Register", "CyberScoop", "Axios")
)  # fmt: skip


@dataclass(slots=True)
class FeedItem:
    source: str
    url: str
    title: str
    summary: str
    published_at: datetime


@dataclass(slots=True)
class Story:
    title: str
    url: str
    source: str
    summary: str
    published_at: datetime
    outlets: list[str] = field(default_factory=list)
    titles: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return hashlib.sha1(self.url.encode()).hexdigest()[:20]


def _clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", text).strip()


def _when(entry: feedparser.FeedParserDict) -> datetime:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    return datetime(*parsed[:6], tzinfo=UTC) if parsed else datetime.now(UTC)


def parse_feed(source: str, text: str, *, google: bool = False) -> list[FeedItem]:
    items = []
    for entry in feedparser.parse(text).entries:
        title, link = _clean(entry.get("title", "")), entry.get("link", "")
        if not title or not link.startswith("http"):
            continue
        outlet = source
        if google:
            # The outlet is in <source>; the title ends with " - Outlet" (dropped, so one story
            # reads the same from every outlet).
            outlet = _clean((entry.get("source") or {}).get("title", "")) or "Google News"
            if title.endswith(f" - {outlet}"):
                title = title[: -len(outlet) - 3].strip()
        summary = "" if google else _clean(entry.get("summary", ""))[:1200]
        items.append(FeedItem(outlet[:40], link[:600], title[:300], summary, _when(entry)))
    return items


def _get(url: str, **params: str) -> str:
    r = requests.get(url, params=params or None, headers={"User-Agent": UA}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.text


def _fetch_feed(source: str, url: str) -> list[FeedItem]:
    return parse_feed(source, _get(url))


def _fetch_search(query: str) -> list[FeedItem]:
    text = _get(GOOGLE_NEWS, q=f"{query} when:1d", hl="en-US", gl="US", ceid="US:en")
    return sorted(parse_feed("", text, google=True), key=lambda i: i.published_at, reverse=True)[:12]


def fetch_all(max_age: timedelta) -> list[FeedItem]:
    """Recent items from every feed and search, fetched in parallel; a source that fails is
    skipped."""
    cutoff = datetime.now(UTC) - max_age
    jobs = [(f"feed {name}", _fetch_feed, (name, url)) for name, url in FEEDS.items()]
    jobs += [(f"search {q!r}", _fetch_search, (q,)) for q in SEARCHES]
    out: list[FeedItem] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(fn, *args): label for label, fn, args in jobs}
        for future in as_completed(futures):
            try:
                out += [i for i in future.result() if i.published_at >= cutoff]
            except requests.RequestException as exc:
                logger.warning("sources.fetch_failed %s error=%s", futures[future], exc)
    return out


# --------------------------------------------------------------------------- same story

STOP_WORDS: Final = frozenset(
    "a an the of to in on for and or as at by with from is are was be its it this that how what "
    "why will can could may says said after over amid new today us your you just now".split()
)
#: Words every tech headline uses: sharing them says nothing about being the same story.
GENERIC: Final = frozenset(
    "ai tech app apps data hack hackers hacked attack attacks cyber security breach users user "
    "update launch launches company report reports million billion first big top week year "
    "warns warning flaw flaws bug bugs exploit exploited vulnerability new latest".split()
)


def words(title: str) -> frozenset[str]:
    return frozenset(w for w in re.findall(r"[a-z0-9$%.\-]+", title.lower()) if w not in STOP_WORDS)


def same_story(a: str, b: str) -> bool:
    """Two headlines about one story: they share most of their DISTINCT words (names,
    products, events), not just the words every tech headline has."""
    wa, wb = words(a) - GENERIC, words(b) - GENERIC
    if not wa or not wb:
        return False
    shared = len(wa & wb)
    return shared >= 2 and shared / min(len(wa), len(wb)) >= 0.4


def names(title: str) -> frozenset[str]:
    """The names in a headline (companies, products, people), lowercased: its capitalised
    words after the first, or only its acronyms (GMO, CISA) when the headline is Title Case."""
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9&'.\-]*", title)
    caps = [t for t in tokens if t[0].isupper()]
    title_case = len(caps) >= 0.7 * max(1, len(tokens))
    found = {t for t in tokens if len(t) >= 2 and t.isupper()}
    if not title_case:
        found |= {t for t in tokens[1:] if t[0].isupper()}
    return frozenset(w.lower().removesuffix("'s").rstrip(".") for w in found) - GENERIC - STOP_WORDS


def same_subject(title: str, other: str) -> bool:
    """Two headlines about one subject: they name the same company / product and share one
    more distinct word ("GMO warns users of ransomware" ~ "GMO refuses ransomware demands")."""
    shared_names = names(title) & names(other)
    if not shared_names:
        return False
    return len((words(title) - GENERIC) & (words(other) - GENERIC) - shared_names) >= 1


def group(items: list[FeedItem]) -> list[Story]:
    """One Story per subject, earliest report first, with every outlet that ran it. The
    story keeps the first item that has a summary (feeds have one, Google News has none)."""
    stories: list[Story] = []
    for item in sorted(items, key=lambda i: i.published_at):
        match = next((s for s in stories if any(same_story(item.title, t) for t in s.titles)), None)
        if match is None:
            stories.append(Story(item.title, item.url, item.source, item.summary, item.published_at,
                                 [item.source], [item.title]))  # fmt: skip
            continue
        if item.source not in match.outlets:
            match.outlets.append(item.source)
        match.titles.append(item.title)
        if not match.summary and item.summary:
            match.title, match.url, match.source, match.summary = item.title, item.url, item.source, item.summary
    return stories


def trusted(story: Story | dict) -> bool:
    outlets = story.outlets if isinstance(story, Story) else story["outlets"]
    return len(outlets) >= 2 or any(o.lower() in TRUSTED for o in outlets)
