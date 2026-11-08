"""The **News Scanner** agent: reads every fresh headline and keeps only IT / tech-world news
(hacks, breaches, vulnerabilities, scams, AI, big tech, launches, outages, viral tech
moments), tagging each with a category and a 0-10 score for how much a tech audience on
Instagram cares right now.

Headlines go to the NVIDIA model in batches; without a key, or when every model fails, a
keyword scorer takes over so the pipeline never stops.
"""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Final

from technews.agents import llm
from technews.sources import Story

logger = logging.getLogger(__name__)

CATEGORIES: Final = ("cloud", "devops", "hack", "breach", "vulnerability", "scam", "ai", "bigtech", "launch", "outage",
                     "viral", "policy", "other")  # fmt: skip
PRIORITY: Final = ("cloud", "devops", "ai")  # the page's first priority: cloud, DevOps and AI news; everything else only fills the gaps
PRIORITY_BOOST: Final = 2.5
BATCH: Final = 10  # small batches answer fast
PARALLEL: Final = 6

SYSTEM: Final = (
    "You are the NEWS SCANNER agent of an Instagram page about the IT / tech world. You read raw "
    "headlines and keep ONLY tech news: cybersecurity (hacks, data breaches, ransomware, zero-days, "
    "scams, crypto hacks), cloud (AWS, Azure, GCP / Google Cloud, Oracle/IBM cloud, Cloudflare, "
    "serverless, cloud security, cloud outages, pricing, services and region launches), devops (CI/CD, "
    "Kubernetes, Docker, Terraform, GitHub/GitLab, Jenkins, SRE, platform engineering, "
    "infrastructure as code, observability), AI (models, launches, controversies), big tech companies, chips, "
    "software and apps, gadget launches, outages of big services, tech regulation, and tech "
    "moments going viral online. CLOUD AND DEVOPS ARE THE PAGE'S FIRST PRIORITY: tag anything about "
    "AWS, Azure, GCP or other cloud providers as category cloud (even a cloud breach or outage), "
    "anything about DevOps tooling and practice as category devops, and score either 2-3 points "
    "higher than an equally big story in another category. NOT tech: politics, sports, entertainment, general business or "
    "markets, unless the story is about technology itself. Deals, discounts, product reviews, "
    "how-to guides, podcasts and opinion pieces are tech=false.\n"
    "For each headline give: tech (true/false); category (one of "
    + ", ".join(CATEGORIES)
    + "); score 0-10 = how much a general tech audience scrolling Instagram would care RIGHT NOW "
    "(10: everyone is talking about it, e.g. a breach of 100M+ users, a global outage, a major AI "
    "release; 7-8: big names or wide impact; 4-6: niche but real; 0-3: minor). More outlets "
    "covering it means more buzz. why: at most 12 words.\n"
    'Reply with ONE JSON object: {"items": [{"i": 0, "tech": true, "category": "breach", '
    '"score": 8, "why": "..."}]} with one entry per headline, same i.'
)


@dataclass(frozen=True, slots=True)
class Scan:
    tech: bool
    category: str
    score: float
    why: str


def _prompt(stories: list[Story]) -> str:
    rows = [
        {"i": i, "headline": s.title, "outlets": len(s.outlets), "summary": s.summary[:280]}
        for i, s in enumerate(stories)
    ]
    return "HEADLINES:\n" + json.dumps(rows, ensure_ascii=False, indent=1)


def _parse(data: dict, n: int) -> dict[int, Scan]:
    out = {}
    for row in data.get("items") or []:
        try:
            i = int(row["i"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= i < n:
            category = str(row.get("category") or "other").lower()
            category = category if category in CATEGORIES else "other"
            score = max(0.0, min(10.0, float(row.get("score") or 0)))
            out[i] = Scan(
                tech=bool(row.get("tech")),
                category=category,
                score=min(10.0, score + PRIORITY_BOOST) if category in PRIORITY and score > 0 else score,
                why=str(row.get("why") or "")[:160],
            )
    return out


def _scan_batch(batch: list[Story]) -> list[Scan]:
    parsed: dict[int, Scan] = {}
    if llm.available():
        try:
            reply = llm.chat_json(system=SYSTEM, user=_prompt(batch), max_tokens=1500, temperature=0.1)
            parsed = _parse(reply.data, len(batch))
            logger.info("scanner.batch model=%s n=%d scored=%d ms=%d", reply.model, len(batch), len(parsed), reply.latency_ms)
        except llm.LLMError as exc:
            logger.warning("scanner.llm_failed error=%s", str(exc)[:200])
    return [parsed.get(i) or keyword_scan(s) for i, s in enumerate(batch)]


def scan(stories: list[Story]) -> list[Scan]:
    """One Scan per story, in order; the batches run in parallel. Never raises."""
    batches = [stories[i : i + BATCH] for i in range(0, len(stories), BATCH)]
    with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        return [scan for batch in pool.map(_scan_batch, batches) for scan in batch]


# --------------------------------------------------------------------------- fallback

KEYWORDS: Final[tuple[tuple[str, str, float], ...]] = (
    ("devops", r"\b(devops|devsecops|ci/cd|cicd|kubernetes|k8s|docker|terraform|ansible|jenkins|gitlab|github actions|helm|argo ?cd|gitops|sre|platform engineering|infrastructure as code|observability|prometheus|grafana|opentofu|pulumi)\b", 7),
    ("cloud", r"\b(cloud|aws|azure|gcp|kubernetes|k8s|serverless|cloudflare|data ?cent(er|re)s?|hyperscaler)\b", 7),
    ("breach", r"\b(breach(es|ed)?|leak(s|ed)?|expos(es|ed) (data|records|users|customers)|stolen data)\b", 7),
    ("hack", r"\b(hack(ed|ers?)?|ransomware|cyber ?attack|malware|botnet|ddos|spyware)\b", 7),
    ("vulnerability", r"\b(zero[- ]day|cve-\d+|vulnerabilit(y|ies)|exploit(ed)?|patch(es)?)\b", 6),
    ("scam", r"\b(scam|phishing|fraud|deepfake)\b", 5),
    ("outage", r"\b(outage|down for|went down|global(ly)? down|disruption)\b", 7),
    ("ai", r"\b(ai|openai|chatgpt|gpt-\d|gemini|claude|llm|anthropic|deepseek|copilot|nvidia)\b", 6),
    ("bigtech", r"\b(apple|google|microsoft|meta|amazon|tesla|x\.com|samsung|tiktok|instagram|whatsapp)\b", 5),
    ("launch", r"\b(launch(es|ed)?|unveil(s|ed)?|announc(es|ed)|release(s|d)?)\b", 4),
    ("viral", r"\b(viral|trending|internet (is )?(loses|freaks)|backlash)\b", 6),
)
NOT_TECH: Final = re.compile(r"\b(deal|discount|% off|best .* to buy|review:|podcast|how to|opinion)\b", re.I)


def keyword_scan(story: Story) -> Scan:
    text = f"{story.title} {story.summary[:300]}"
    hits = [(cat, base) for cat, pattern, base in KEYWORDS if re.search(pattern, text, re.I)]
    if not hits or NOT_TECH.search(story.title):
        return Scan(False, "other", 0.0, "keyword scan: no tech signal")
    category, base = max(hits, key=lambda h: h[1])
    buzz = min(3.0, (len(story.outlets) - 1) * 0.75)  # more outlets, more buzz
    priority = [cat for cat, _ in hits if cat in PRIORITY]
    if priority:  # cloud and DevOps outrank whatever else they overlap with
        category, base = ("cloud" if "cloud" in priority else priority[0]), max(base, 7) + PRIORITY_BOOST
    return Scan(True, category, min(10.0, base + buzz), f"keyword scan: {category}, {len(story.outlets)} outlets")
