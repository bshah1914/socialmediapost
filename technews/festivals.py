"""Indian festival flyers.

The festival dates are fetched from a public Indian holiday calendar (a free .ics feed, no account or key), kept for
a few days, and matched against the list of festivals below. A few days before each one, a greeting flyer is drawn in
the festival's colours and sent to Approvals like any other post. Nothing is posted without your approval, and every
festival can be switched off.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import requests

from technews import brands, db, flyers, pipeline
from technews.agents import team
from technews.config import settings

logger = logging.getLogger(__name__)

FEED_URL: Final = "https://calendar.google.com/calendar/ical/en.indian%23holiday%40group.v.calendar.google.com/public/basic.ics"
CACHE_KEY: Final = "festivals:cache"
REFRESH_DAYS: Final = 3
DEFAULT_LEAD: Final = 2
MAX_FEED_BYTES: Final = 3_000_000

# key, how the feed names it (regex), name, headline, one line on what it means, colours (top, bottom, text, accent)
CATALOG: Final[tuple[dict[str, Any], ...]] = (
    {"key": "newyear", "match": r"^New Year's Day$", "name": "New Year", "headline": "Happy New Year", "meaning": "Here is to a year of new ideas, steady growth and good news", "c": ("0b1d4a", "1d0f4f", "f4f7ff", "ffc83d")},
    {"key": "sankranti", "match": r"^(Makar Sankranti|Pongal)$", "name": "Makar Sankranti and Pongal", "headline": "Happy Makar Sankranti and Pongal", "meaning": "Wishing you a harvest of good fortune and a bright start to the sun's new journey", "c": ("a34a00", "5a1f00", "fff6e6", "ffd23f")},
    {"key": "vasant", "match": r"^Vasant Panchami$", "name": "Vasant Panchami", "headline": "Happy Vasant Panchami", "meaning": "May knowledge and learning bloom for you and your team this spring", "c": ("c79300", "7a4a00", "fffbe8", "fff0a8")},
    {"key": "republic", "match": r"^Republic Day$", "name": "Republic Day", "headline": "Happy Republic Day", "meaning": "Proud of a country that builds, innovates and keeps growing together", "c": ("0a3a1d", "0b1f5c", "ffffff", "ff9933")},
    {"key": "shivratri", "match": r"^Maha Shivaratri$", "name": "Maha Shivratri", "headline": "Happy Maha Shivratri", "meaning": "May the night bring you calm, strength and a fresh focus", "c": ("0d1b3f", "2a1052", "eef2ff", "9ec5ff")},
    {"key": "holi", "match": r"^Holi$", "name": "Holi", "headline": "Happy Holi", "meaning": "May your days be as bright and colourful as the festival", "c": ("5b1a8f", "c2185b", "ffffff", "ffe14d")},
    {"key": "ugadi", "match": r"^Ugadi$", "name": "Ugadi and Gudi Padwa", "headline": "Happy Ugadi and Gudi Padwa", "meaning": "A new year of new beginnings, good health and success", "c": ("1c6b3a", "0b3d2a", "f5fff4", "ffd23f")},
    {"key": "eid", "match": r"^Ramzan Id$", "name": "Eid ul-Fitr", "headline": "Eid Mubarak", "meaning": "Wishing you peace, joy and togetherness with the people you love", "c": ("0b4a3f", "0a2a3a", "f2fffb", "f2d27a")},
    {"key": "ramnavami", "match": r"^Rama Navami$", "name": "Ram Navami", "headline": "Happy Ram Navami", "meaning": "May the day bring you courage, goodness and peace", "c": ("b34700", "6b1d00", "fff4e6", "ffd23f")},
    {"key": "mahavir", "match": r"^Mahavir Jayanti$", "name": "Mahavir Jayanti", "headline": "Happy Mahavir Jayanti", "meaning": "Wishing you peace, kindness and a mindful day", "c": ("b8860b", "6b4a00", "fffbea", "fff3b0")},
    {"key": "goodfriday", "match": r"^Good Friday$", "name": "Good Friday", "headline": "Wishing you a peaceful Good Friday", "meaning": "A day for reflection, kindness and hope", "c": ("1f2a44", "0d1220", "f2f5ff", "c9d6ff")},
    {"key": "baisakhi", "match": r"^Vaisakhi$", "name": "Baisakhi", "headline": "Happy Baisakhi", "meaning": "Wishing you a season of good harvest, energy and joy", "c": ("c26a00", "7a3a00", "fff7e6", "ffe14d")},
    {"key": "buddha", "match": r"^Buddha Purnima$", "name": "Buddha Purnima", "headline": "Happy Buddha Purnima", "meaning": "May the day bring you calm, clarity and compassion", "c": ("a86a00", "5a3a00", "fffaf0", "ffe8a3")},
    {"key": "bakrid", "match": r"^Bakrid$", "name": "Eid ul-Adha", "headline": "Eid ul-Adha Mubarak", "meaning": "Wishing you and your family blessings, generosity and peace", "c": ("0b4a3f", "0a2a3a", "f2fffb", "f2d27a")},
    {"key": "rathyatra", "match": r"^Rath Yatra$", "name": "Rath Yatra", "headline": "Happy Rath Yatra", "meaning": "May the journey ahead be smooth, blessed and full of joy", "c": ("b34700", "5a1f00", "fff4e6", "ffd23f")},
    {"key": "independence", "match": r"^Independence Day$", "name": "Independence Day", "headline": "Happy Independence Day", "meaning": "Proud of the freedom to build, to dream and to grow together", "c": ("0b3d1f", "0a2a6b", "ffffff", "ff9933")},
    {"key": "onam", "match": r"^Onam$", "name": "Onam", "headline": "Happy Onam", "meaning": "May your home be full of flowers, feasts and good fortune", "c": ("b8860b", "1c6b3a", "fffdf0", "fff3b0")},
    {"key": "rakhi", "match": r"^Raksha Bandhan$", "name": "Raksha Bandhan", "headline": "Happy Raksha Bandhan", "meaning": "Celebrating the bond that protects and supports us all", "c": ("c2185b", "6a1b9a", "fff5fa", "ffe14d")},
    {"key": "janmashtami", "match": r"^Janmashtami$", "name": "Janmashtami", "headline": "Happy Janmashtami", "meaning": "May the day fill your home with joy, wisdom and peace", "c": ("1a3fa0", "0b1a55", "f2f6ff", "ffd23f")},
    {"key": "ganesh", "match": r"^Ganesh Chaturthi$", "name": "Ganesh Chaturthi", "headline": "Happy Ganesh Chaturthi", "meaning": "May Ganpati remove every obstacle and bring you wisdom and success", "c": ("c2410c", "7a1f00", "fff4e6", "ffd23f")},
    {"key": "gandhi", "match": r"^Mahatma Gandhi Jayanti$", "name": "Gandhi Jayanti", "headline": "Remembering Gandhi Jayanti", "meaning": "Honouring a life of truth, simplicity and peace", "c": ("1f2a44", "0d1220", "f5f7ff", "ff9933")},
    {"key": "navratri", "match": r"^First Day of Sharad Navratri$", "name": "Navratri", "headline": "Happy Navratri", "meaning": "Nine nights of energy, devotion and celebration. May they bring you strength", "c": ("a3123a", "4a0a5c", "fff5f7", "ffd23f")},
    {"key": "dussehra", "match": r"^Dussehra$", "name": "Dussehra", "headline": "Happy Dussehra", "meaning": "The victory of good over evil. May it bring you success in all you start", "c": ("b34700", "5a0a1f", "fff4e6", "ffd23f")},
    {"key": "dhanteras", "derive": ("diwali", -2), "name": "Dhanteras", "headline": "Happy Dhanteras", "meaning": "Wishing you wealth, health and a prosperous year ahead", "c": ("4a0f2e", "1f0a3d", "fff6e0", "ffc83d")},
    {"key": "diwali", "match": r"^Diwali/Deepavali$", "name": "Diwali", "headline": "Happy Diwali", "meaning": "May the festival of lights brighten your home and your business", "c": ("5a0f2e", "2b0a52", "fff6e0", "ffb627")},
    {"key": "govardhan", "match": r"^Govardhan Puja$", "name": "Govardhan Puja and Gujarati New Year", "headline": "Happy Govardhan Puja and Gujarati New Year", "meaning": "Wishing you gratitude, abundance and a prosperous new year", "c": ("a34a00", "5a1f00", "fff4e6", "ffd23f")},
    {"key": "bhaidooj", "match": r"^Bhai Duj$", "name": "Bhai Dooj", "headline": "Happy Bhai Dooj", "meaning": "Celebrating the love and care between brothers and sisters", "c": ("c2185b", "6a1b9a", "fff5fa", "ffe14d")},
    {"key": "chhath", "match": r"^Chhat Puja", "name": "Chhath Puja", "headline": "Happy Chhath Puja", "meaning": "Wishing you health, strength and the blessings of the rising sun", "c": ("c26a00", "7a2a00", "fff7e6", "ffe14d")},
    {"key": "gurpurab", "match": r"^Guru Nanak Jayanti$", "name": "Guru Nanak Jayanti", "headline": "Happy Guru Nanak Jayanti", "meaning": "May his message of service, honesty and equality guide us all", "c": ("1a3fa0", "0b1a55", "f2f6ff", "ffd23f")},
    {"key": "christmas", "match": r"^Christmas$", "name": "Christmas", "headline": "Merry Christmas", "meaning": "Wishing you warmth, joy and time with the people who matter", "c": ("0b3d2a", "5a0a14", "f5fff4", "ffd23f")},
    {"key": "teachers", "fixed": (9, 5), "name": "Teachers' Day", "headline": "Happy Teachers' Day", "meaning": "Thank you to every teacher and mentor who helped us learn and grow", "c": ("1a3fa0", "0b1a55", "f2f6ff", "ffd23f")},
    {"key": "lohri", "fixed": (1, 13), "name": "Lohri", "headline": "Happy Lohri", "meaning": "May the bonfire bring warmth, good harvest and happiness to your home", "c": ("b34700", "5a1500", "fff4e6", "ffd23f")},
    {"key": "uttarayan", "fixed": (1, 14), "name": "Uttarayan", "headline": "Happy Uttarayan", "meaning": "May your goals fly high like kites in a clear sky", "c": ("0a6bb3", "6a1b9a", "f2f9ff", "ffd23f")},
    {"key": "bihu", "fixed": (4, 14), "name": "Bohag Bihu", "headline": "Happy Bohag Bihu", "meaning": "Wishing you a new year of joy, music and good harvest", "c": ("b3261e", "5a0a1f", "fff5f0", "ffd23f")},
    {"key": "durgapuja", "derive": ("dussehra", -2), "name": "Durga Puja", "headline": "Happy Durga Puja", "meaning": "May the goddess bring you strength, courage and joy", "c": ("a3123a", "4a0a5c", "fff5f7", "ffd23f")},
    {"key": "engineers", "fixed": (9, 15), "name": "Engineers' Day", "headline": "Happy Engineers' Day", "meaning": "Salute to everyone who builds, fixes and keeps the world running", "c": ("0a2a6b", "081a3a", "eef4ff", "2fd4c4")},
)
BY_KEY: Final = {c["key"]: c for c in CATALOG}

#: The greeting in Hindi and in Gujarati, for the festivals people write it for. Without one, the flyer stays in English.
L10N: Final[dict[str, tuple[str, str]]] = {
    "newyear": ("नव वर्ष की हार्दिक शुभकामनाएँ", "નવા વર્ષની હાર્દિક શુભેચ્છાઓ"),
    "sankranti": ("मकर संक्रांति और पोंगल की हार्दिक शुभकामनाएँ", "મકરસંક્રાંતિ અને પોંગલની હાર્દિક શુભેચ્છાઓ"),
    "uttarayan": ("उत्तरायण की हार्दिक शुभकामनाएँ", "ઉત્તરાયણની હાર્દિક શુભેચ્છાઓ"),
    "lohri": ("लोहड़ी की हार्दिक शुभकामनाएँ", "લોહરીની હાર્દિક શુભેચ્છાઓ"),
    "bihu": ("बिहू की हार्दिक शुभकामनाएँ", "બિહુની હાર્દિક શુભેચ્છાઓ"),
    "republic": ("गणतंत्र दिवस की हार्दिक शुभकामनाएँ", "પ્રજાસત્તાક દિનની હાર્દિક શુભેચ્છાઓ"),
    "shivratri": ("महाशिवरात्रि की हार्दिक शुभकामनाएँ", "મહાશિવરાત્રિની હાર્દિક શુભેચ્છાઓ"),
    "holi": ("होली की हार्दिक शुभकामनाएँ", "હોળીની હાર્દિક શુભેચ્છાઓ"),
    "ugadi": ("उगादी और गुड़ी पड़वा की शुभकामनाएँ", "ઉગાદી અને ગુડી પડવાની શુભેચ્છાઓ"),
    "eid": ("ईद मुबारक", "ઈદ મુબારક"),
    "ramnavami": ("रामनवमी की हार्दिक शुभकामनाएँ", "રામનવમીની હાર્દિક શુભેચ્છાઓ"),
    "baisakhi": ("बैसाखी की हार्दिक शुभकामनाएँ", "બૈસાખીની હાર્દિક શુભેચ્છાઓ"),
    "independence": ("स्वतंत्रता दिवस की हार्दिक शुभकामनाएँ", "સ્વતંત્રતા દિવસની હાર્દિક શુભેચ્છાઓ"),
    "onam": ("ओणम की हार्दिक शुभकामनाएँ", "ઓણમની હાર્દિક શુભેચ્છાઓ"),
    "rakhi": ("रक्षाबंधन की हार्दिक शुभकामनाएँ", "રક્ષાબંધનની હાર્દિક શુભેચ્છાઓ"),
    "janmashtami": ("जन्माष्टमी की हार्दिक शुभकामनाएँ", "જન્માષ્ટમીની હાર્દિક શુભેચ્છાઓ"),
    "ganesh": ("गणेश चतुर्थी की हार्दिक शुभकामनाएँ", "ગણેશ ચતુર્થીની હાર્દિક શુભેચ્છાઓ"),
    "navratri": ("नवरात्रि की हार्दिक शुभकामनाएँ", "નવરાત્રિની હાર્દિક શુભેચ્છાઓ"),
    "durgapuja": ("दुर्गा पूजा की हार्दिक शुभकामनाएँ", "દુર્ગા પૂજાની હાર્દિક શુભેચ્છાઓ"),
    "dussehra": ("दशहरा की हार्दिक शुभकामनाएँ", "દશેરાની હાર્દિક શુભેચ્છાઓ"),
    "dhanteras": ("धनतेरस की हार्दिक शुभकामनाएँ", "ધનતેરસની હાર્દિક શુભેચ્છાઓ"),
    "diwali": ("दीपावली की हार्दिक शुभकामनाएँ", "દિવાળીની હાર્દિક શુભેચ્છાઓ"),
    "govardhan": ("गोवर्धन पूजा की हार्दिक शुभकामनाएँ", "નૂતન વર્ષાભિનંદન"),
    "bhaidooj": ("भाई दूज की हार्दिक शुभकामनाएँ", "ભાઈ બીજની હાર્દિક શુભેચ્છાઓ"),
    "chhath": ("छठ पूजा की हार्दिक शुभकामनाएँ", "છઠ પૂજાની હાર્દિક શુભેચ્છાઓ"),
    "gurpurab": ("गुरु नानक जयंती की हार्दिक शुभकामनाएँ", "ગુરુ નાનક જયંતીની હાર્દિક શુભેચ્છાઓ"),
    "christmas": ("क्रिसमस की हार्दिक शुभकामनाएँ", "નાતાલની હાર્દિક શુભેચ્છાઓ"),
}
LANGUAGES: Final = {"en": "English", "hi": "English and Hindi", "gu": "English and Gujarati"}
FIXED_DATES: Final = {"newyear": (1, 1), "republic": (1, 26), "independence": (8, 15), "gandhi": (10, 2), "christmas": (12, 25)}  # when the feed cannot be reached


# --------------------------------------------------------------------------- the calendar feed


def parse_ics(text: str) -> list[tuple[str, str]]:
    """(YYYY-MM-DD, name) for every all-day event in an .ics file."""
    text = re.sub(r"\r?\n[ \t]", "", text)  # folded lines
    out: list[tuple[str, str]] = []
    for block in text.split("BEGIN:VEVENT")[1:]:
        start = re.search(r"^DTSTART[^:\n]*:(\d{4})(\d{2})(\d{2})", block, re.M)
        name = re.search(r"^SUMMARY[^:\n]*:(.+)$", block, re.M)
        if start and name:
            out.append((f"{start[1]}-{start[2]}-{start[3]}", name[1].strip().replace("\\,", ",").replace("\\'", "'")))
    return out


def refresh(force: bool = False) -> dict[str, Any]:
    """Fetch the festival dates if the copy we hold is older than a few days. A failure keeps the old copy."""
    cached = _cache()
    if cached["events"] and not force:
        try:
            if datetime.now(UTC) - datetime.fromisoformat(cached["at"]) < timedelta(days=REFRESH_DAYS):
                return {"ok": True, "fetched": False, "events": len(cached["events"])}
        except ValueError:
            pass
    try:
        r = requests.get(FEED_URL, headers={"User-Agent": "cloudtrio-newsroom"}, timeout=20)
        r.raise_for_status()
        if len(r.content) > MAX_FEED_BYTES:
            raise ValueError("the calendar is larger than expected")
        events = parse_ics(r.text)
        low, high = (date.today() - timedelta(days=3)).isoformat(), (date.today() + timedelta(days=500)).isoformat()
        kept = [[d, n] for d, n in events if low <= d <= high and any(re.search(c["match"], n) for c in CATALOG if "match" in c)]
        if not kept:
            raise ValueError("no festivals found in the calendar")
    except (requests.RequestException, ValueError) as exc:
        logger.warning("festivals.fetch_failed %s", exc)
        return {"ok": False, "fetched": False, "error": str(exc)[:160], "events": len(cached["events"])}
    db.kv_set(CACHE_KEY, json.dumps({"at": datetime.now(UTC).isoformat(timespec="seconds"), "events": kept}))
    return {"ok": True, "fetched": True, "events": len(kept)}


def _cache() -> dict[str, Any]:
    try:
        data = json.loads(db.kv_get(CACHE_KEY) or "{}")
    except ValueError:
        data = {}
    return {"at": data.get("at", ""), "events": data.get("events", [])}


# --------------------------------------------------------------------------- what is coming


def auto() -> bool:
    return (db.kv_get("festivals:auto") or "1") == "1"


def lead_days() -> int:
    try:
        return max(0, min(14, int(db.kv_get("festivals:lead") or DEFAULT_LEAD)))
    except ValueError:
        return DEFAULT_LEAD


def language() -> str:
    lang = db.kv_get("festivals:lang") or "en"
    return lang if lang in LANGUAGES else "en"


def set_language(lang: str) -> None:
    if lang not in LANGUAGES:
        raise ValueError("Pick English, English and Hindi, or English and Gujarati")
    db.kv_set("festivals:lang", lang)


def wording(key: str) -> dict[str, str]:
    """The headline and message for a festival: the ones you edited, or the built-in ones."""
    c = BY_KEY[key]
    out = {"headline": c["headline"], "meaning": c["meaning"]}
    try:
        out.update({k: v for k, v in json.loads(db.kv_get(f"festival:{key}:text") or "{}").items() if k in out and isinstance(v, str) and v.strip()})
    except ValueError:
        pass
    return out


def set_wording(key: str, headline: str, meaning: str) -> dict[str, str]:
    """Save your own wording for a festival (blank puts the built-in one back)."""
    if key not in BY_KEY:
        raise ValueError("Unknown festival")
    headline, meaning = headline.strip(), meaning.strip()
    if len(headline) > 60 or len(meaning) > 140:
        raise ValueError("The headline can be 60 characters and the message 140")
    db.kv_set(f"festival:{key}:text", json.dumps({"headline": headline, "meaning": meaning}) if headline or meaning else "")
    return wording(key)


def enabled(key: str) -> bool:
    return (db.kv_get(f"festival:{key}:off") or "") != "1"


def set_enabled(key: str, on: bool) -> None:
    if key not in BY_KEY:
        raise ValueError("Unknown festival")
    db.kv_set(f"festival:{key}:off", "" if on else "1")


def _made_key(key: str, year: int) -> str:
    return f"festival:{settings.BRAND_ID}:{key}:{year}"


def dates(today: date | None = None, days: int = 400) -> list[tuple[date, str]]:
    """Every festival (date, key) from today to ``days`` ahead: from the feed, worked out, or fixed."""
    today = today or date.today()
    end = today + timedelta(days=days)
    found: dict[tuple[date, str], None] = {}
    for stamp, name in _cache()["events"]:
        for c in CATALOG:
            if "match" in c and re.search(c["match"], name):
                found[(date.fromisoformat(stamp), c["key"])] = None
    for c in CATALOG:
        if "derive" in c:  # Dhanteras falls two days before Diwali
            base, shift = c["derive"]
            for d, k in list(found):
                if k == base:
                    found[(d + timedelta(days=shift), c["key"])] = None
    for year in (today.year, today.year + 1):
        for key, (month, day) in {**{c["key"]: c["fixed"] for c in CATALOG if "fixed" in c}, **FIXED_DATES}.items():
            if not any(k == key and d.year == year for d, k in found):
                found[(date(year, month, day), key)] = None
    return sorted((d, k) for d, k in found if today <= d <= end)


def upcoming(days: int = 120) -> list[dict[str, Any]]:
    today = date.today()
    out = []
    for d, key in dates(today, days):
        made = db.kv_get(_made_key(key, d.year))
        out.append({"key": key, "name": BY_KEY[key]["name"], "date": d.isoformat(), "days": (d - today).days, "on": enabled(key),
                    "post_id": json.loads(made).get("post_id") if made else 0, "derived": "derive" in BY_KEY[key]})  # fmt: skip
    return out


# --------------------------------------------------------------------------- making the flyer


def flyer_for(key: str, year: int) -> flyers.Flyer:
    c = BY_KEY[key]
    brand = settings.BRAND_NAME
    top, bottom, text, accent = (f"#{h}" for h in c["c"])
    words = wording(key)
    lang = language()
    regional = L10N[key][("hi", "gu").index(lang)] if lang in ("hi", "gu") and key in L10N else ""
    points = (words["meaning"], regional or "Thank you for being part of our journey", f"Warm wishes from the whole {brand} team")
    caption = f"{words['headline']}! {words['meaning']}.\n\n" + (f"{regional}\n\n" if regional else "") + f"Warm wishes from all of us at {brand}."
    return flyers.Flyer(
        f"fest-{key}-{year}" + (f"-b{settings.BRAND_ID}" if settings.BRAND_ID != 1 else ""), c["name"].upper()[:30], words["headline"], points,
        f"Warm wishes from {brand}"[:40], "", caption, False, "classic", {"bg": top, "bg2": bottom, "text": text, "accent": accent})  # fmt: skip


def preview(key: str, year: int | None = None) -> Path:
    """Draw the flyer as it would be made now, without saving it or sending it anywhere."""
    if key not in BY_KEY:
        raise ValueError("Unknown festival")
    settings.CARDS_DIR.mkdir(parents=True, exist_ok=True)
    return flyers.render(flyer_for(key, year or date.today().year), settings.CARDS_DIR / f"preview-{key}-b{settings.BRAND_ID}.jpg")


def make(key: str, year: int | None = None, *, force: bool = False) -> dict[str, Any]:
    """Draw one festival's flyer and send it to Approvals. Does nothing if it was already made this year, unless forced."""
    if key not in BY_KEY:
        raise ValueError("Unknown festival")
    year = year or date.today().year
    if not force and db.kv_get(_made_key(key, year)):
        return {"made": False, "reason": "already made"}
    with team.run("designer") as info:
        flyer = flyers.save({**{f: getattr(flyer_for(key, year), f) for f in ("id", "kicker", "headline", "points", "cta", "note", "caption", "layout", "style")}, "enabled": False})
        tag = re.sub(r"[^A-Za-z]", "", BY_KEY[key]["name"])
        result = pipeline.draft_flyer(flyer, tags=f"#{tag} #Happy{tag} #IndianFestivals #{re.sub(r'[^A-Za-z]', '', settings.BRAND_NAME)}")
        info["summary"] = f"Drew the {BY_KEY[key]['name']} flyer"
    db.kv_set(_made_key(key, year), json.dumps({"post_id": result["post_id"], "at": db.now()}))
    return {"made": True, "post_id": result["post_id"]}


def run() -> dict[str, Any]:
    """The daily job: refresh the dates, then make the flyer for every festival that is within the lead time."""
    status = refresh()
    if not auto():
        return {**status, "made": []}
    made: list[str] = []
    for brand in brands.active():
        with brands.use(brand):
            today = date.today()
            for d, key in dates(today, lead_days()):
                if enabled(key) and not db.kv_get(_made_key(key, d.year)):
                    try:
                        if make(key, d.year)["made"]:
                            made.append(f"{BY_KEY[key]['name']} ({brand['name']})")
                    except Exception:  # noqa: BLE001 - one festival must not stop the others
                        logger.exception("festivals.make_failed key=%s", key)
    if made:
        logger.info("festivals.made %s", made)
    return {**status, "made": made}


def remind() -> list[str]:
    """Twice a day: a flyer for today or tomorrow that nobody has approved yet is time-sensitive, so say so (once a day each)."""
    from technews import notify

    told: list[str] = []
    for brand in brands.active():
        with brands.use(brand):
            today = date.today()
            for d, key in dates(today, 1):
                made = db.kv_get(_made_key(key, d.year))
                post_id = json.loads(made).get("post_id") if made else 0
                if not post_id or not enabled(key):
                    continue
                found = db.rows("SELECT status FROM posts WHERE id = ?", post_id)
                sent_key = f"festival:{brand['id']}:{key}:{d.year}:reminded:{today.isoformat()}"
                if found and found[0]["status"] == "pending" and not db.kv_get(sent_key):
                    when = "today" if d == today else "tomorrow"
                    notify.send(notify.title(f"approve the {BY_KEY[key]['name']} flyer"), f"{BY_KEY[key]['name']} is {when} and its flyer is still waiting for your approval. {notify.link()}")
                    db.kv_set(sent_key, "1", ttl=2 * 86400)
                    told.append(f"{BY_KEY[key]['name']} ({brand['name']})")
    return told
