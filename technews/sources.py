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
from urllib.parse import urlparse

import feedparser
import requests

from technews import db

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
    "The Register": "https://www.theregister.com/headlines.atom",
    "InfoQ Cloud": "https://feed.infoq.com/cloud-computing/",
    "AWS News Blog": "https://aws.amazon.com/blogs/aws/feed/",
    "Google Cloud Blog": "https://cloudblog.withgoogle.com/rss/",
    "Azure Blog": "https://azure.microsoft.com/en-us/blog/feed/",
    "The New Stack": "https://thenewstack.io/feed/",
    "GitHub Blog": "https://github.blog/feed/",
    "Docker Blog": "https://www.docker.com/blog/feed/",
    "CNCF": "https://www.cncf.io/feed/",
    "Kubernetes Blog": "https://kubernetes.io/feed.xml",
    "HashiCorp Blog": "https://www.hashicorp.com/blog/feed.xml",
}

GOOGLE_NEWS: Final = "https://news.google.com/rss/search"
BLOCKED: Final = (401, 403, 429)  # the answers that mean "not you", not "broken"
SEARCHES: Final[tuple[str, ...]] = (
    "AWS announces OR launches",
    "Microsoft Azure announces OR outage",
    "Google Cloud GCP announces",
    "DevOps OR DevSecOps",
    "Kubernetes OR Terraform OR Docker vulnerability OR release",
    "GitHub Actions OR GitLab OR Jenkins",
    "AWS OR Azure OR \"Google Cloud\" announces",
    "cloud outage",
    "cloud security breach misconfiguration",
    "Kubernetes OR serverless OR \"cloud computing\"",
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

#: Outlets whose word is enough on its own; anything else wants a second outlet. The names of
#: the feeds you follow count too (see ``trusted_outlets``).
TRUSTED_EXTRA: Final = frozenset(
    name.lower()
    for name in ("Reuters", "Bloomberg", "The Record", "CNBC", "BBC", "The Guardian",
                 "Associated Press", "Engadget", "ZDNET", "The Register", "CyberScoop", "Axios",
                 "The New Stack", "SiliconANGLE", "CRN", "DataCenterDynamics", "ET Datacenters", "InfoWorld",
                 "Network World", "Computerworld", "CIO", "TechRadar", "VentureBeat", "Data Center Knowledge",
                 "Google Cloud Press Corner", "Cloudflare", "Microsoft Azure", "Amazon Web Services",
                 "DevOps.com", "Container Journal", "InfoQ", "GitLab", "GitHub", "Kubernetes", "Docker",
                 "Cloud Native Now", "CSO Online", "www.csoonline.com", "Docker Blog", "AWS News Blog", "AWS Blog",
                 "Microsoft Azure Blog", "Google Cloud Blog", "Cloud Native Computing Foundation", "CNCF",
                 "HashiCorp", "The Hacker News", "BleepingComputer", "Dark Reading", "SecurityWeek", "TechCrunch",
                 "The Verge", "Ars Technica", "Wired", "CyberSecurityNews", "Hackread", "The Cloudflare Blog",
                 "Platform Engineering", "DevOps Digest", "TechTarget", "Cloud Computing News", "Forbes", "Fortune",
                 "SiliconANGLE & theCUBE", "Techzine Global", "The Next Platform", "The Stack")
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


# --------------------------------------------------------------------------- the editable list


_trusted_cache: frozenset[str] | None = None


def seed() -> None:
    """The first time, the built-in feeds and searches go into the database; after that it is yours."""
    if db.kv_get("sources:seeded"):
        return
    for name, url in FEEDS.items():
        db.execute("INSERT OR IGNORE INTO sources (kind, name, value) VALUES ('feed', ?, ?)", name, url)
    for query in SEARCHES:
        db.execute("INSERT OR IGNORE INTO sources (kind, name, value) VALUES ('search', '', ?)", query)
    db.kv_set("sources:seeded", "1")


def listing() -> list[dict]:
    seed()
    return [{**r, "enabled": bool(r["enabled"])} for r in db.rows("SELECT * FROM sources ORDER BY kind, name, id")]


def add(kind: str, name: str, value: str) -> dict:
    global _trusted_cache
    seed()
    name, value = name.strip(), value.strip()
    if kind == "feed":
        if not re.match(r"https?://\S+$", value):
            raise ValueError("The feed address must start with http:// or https://")
        if not name:
            raise ValueError("Give the feed a name (it shows on the card)")
    elif kind == "search":
        name = ""
        if not 2 <= len(value) <= 120:
            raise ValueError("The search needs 2 to 120 characters")
    else:
        raise ValueError("kind must be feed or search")
    try:
        new_id = db.execute("INSERT INTO sources (kind, name, value) VALUES (?,?,?)", kind, name[:60], value)
    except Exception as exc:  # noqa: BLE001 - the UNIQUE (kind, value) rule
        raise ValueError("That one is already in the list") from exc
    _trusted_cache = None
    return next(r for r in listing() if r["id"] == new_id)


def set_enabled(source_id: int, enabled: bool) -> bool:
    global _trusted_cache
    seed()
    found = db.rows("SELECT id FROM sources WHERE id = ?", source_id)
    if found:
        db.execute("UPDATE sources SET enabled = ? WHERE id = ?", int(enabled), source_id)
        _trusted_cache = None
    return bool(found)


def remove(source_id: int) -> bool:
    global _trusted_cache
    seed()
    found = db.rows("SELECT id FROM sources WHERE id = ?", source_id)
    if found:
        db.execute("DELETE FROM sources WHERE id = ?", source_id)
        _trusted_cache = None
    return bool(found)


def probe(url: str) -> dict:
    """Fetch one feed to show it works: how many items and the first titles."""
    items = _fetch_feed("probe", url)
    return {"count": len(items), "titles": [i.title for i in items[:3]]}


def trusted_outlets() -> frozenset[str]:
    global _trusted_cache
    if _trusted_cache is None:
        seed()
        _trusted_cache = TRUSTED_EXTRA | {r["name"].lower() for r in db.rows(
            "SELECT name FROM sources WHERE kind = 'feed' AND enabled = 1")}  # fmt: skip
    return _trusted_cache


def _get(url: str, **params: str) -> str:
    r = requests.get(url, params=params or None, headers={"User-Agent": UA}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.text


def _fetch_feed(source: str, url: str) -> list[FeedItem]:
    try:
        return parse_feed(source, _get(url))
    except requests.HTTPError as exc:
        if exc.response is None or exc.response.status_code not in BLOCKED:
            raise
        logger.info("sources.feed_blocked %s: reading it through Google News instead", source)
        return _via_google_news(source, url)


def _via_google_news(source: str, url: str) -> list[FeedItem]:
    """Some sites refuse a server's address (Cloudflare bot checks). Their stories are still in Google News."""
    host = (urlparse(url).hostname or "").removeprefix("www.")
    text = _get(GOOGLE_NEWS, q=f"site:{host} when:2d", hl="en-US", gl="US", ceid="US:en")
    items = parse_feed("", text, google=True)
    for item in items:
        item.source = source[:40]  # shows under the feed's own name
    return sorted(items, key=lambda i: i.published_at, reverse=True)[:15]


def _fetch_search(query: str) -> list[FeedItem]:
    text = _get(GOOGLE_NEWS, q=f"{query} when:1d", hl="en-US", gl="US", ceid="US:en")
    return sorted(parse_feed("", text, google=True), key=lambda i: i.published_at, reverse=True)[:12]


def fetch_all(max_age: timedelta) -> list[FeedItem]:
    """Recent items from every feed and search, fetched in parallel; a source that fails is
    skipped."""
    cutoff = datetime.now(UTC) - max_age
    active = [r for r in listing() if r["enabled"]]
    jobs = [(f"feed {r['name']}", _fetch_feed, (r["name"], r["value"]), r["id"]) for r in active if r["kind"] == "feed"]
    jobs += [(f"search {r['value']!r}", _fetch_search, (r["value"],), r["id"]) for r in active if r["kind"] == "search"]
    out: list[FeedItem] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(fn, *args): (label, sid) for label, fn, args, sid in jobs}
        for future in as_completed(futures):
            label, sid = futures[future]
            try:
                got = future.result()
                out += [i for i in got if i.published_at >= cutoff]
                _health(sid, True, len(got), "")
            except requests.RequestException as exc:
                logger.warning("sources.fetch_failed %s error=%s", label, exc)
                _health(sid, False, 0, str(exc))
    return out


def _health(source_id: int, ok: bool, items: int, error: str) -> None:
    """Note how a source answered, so the scout can tell a quiet feed from a dead one. Never raises."""
    try:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        old = db.rows("SELECT * FROM source_health WHERE source_id = ?", source_id)
        if ok:
            db.execute("INSERT INTO source_health (source_id, ok_at, first_fail_at, fails, items, error) VALUES (?,?,'',0,?,'') "
                       "ON CONFLICT(source_id) DO UPDATE SET ok_at = excluded.ok_at, first_fail_at = '', fails = 0, items = excluded.items, error = ''",
                       source_id, now, items)  # fmt: skip
        else:
            first = (old[0]["first_fail_at"] if old and old[0]["fails"] else "") or now
            db.execute("INSERT INTO source_health (source_id, ok_at, first_fail_at, fails, items, error) VALUES (?,'',?,1,0,?) "
                       "ON CONFLICT(source_id) DO UPDATE SET first_fail_at = ?, fails = source_health.fails + 1, error = excluded.error",
                       source_id, first, error[:200], first)  # fmt: skip
    except Exception:  # noqa: BLE001
        logger.exception("source health note failed")


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
    return len(outlets) >= 2 or any(o.lower() in trusted_outlets() for o in outlets)
