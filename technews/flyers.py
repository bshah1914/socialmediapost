"""Marketing flyers: 1080x1350 promo cards for CloudTrio's own services, in the same style as
the news cards. The copy comes from cloudtrio.in; anything a cloud provider approves (credits)
or that varies by customer (discounts, savings) carries its condition on the flyer itself.

    python -m technews.flyers          # renders every flyer into data/flyers/
"""

from __future__ import annotations

import colorsys
import io
import json
import math
import re
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Final

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

from technews import brand, db
from technews.card import H, MUTED, PAD, TEXT, W, _font, _wrap
from technews.config import settings

EMAIL: Final = "info@cloudtrio.in"
SITE: Final = "cloudtrio.in"
TEAL: Final = (47, 212, 196)
INK: Final = (8, 26, 54)
NAVY_TOP, NAVY_BOTTOM = (8, 18, 42), (10, 52, 84)


@dataclass(frozen=True, slots=True)
class Flyer:
    id: str
    kicker: str
    headline: str
    points: tuple[str, ...]
    cta: str
    note: str  # the condition that goes with the claim
    caption: str
    enabled: bool = True
    layout: str = "classic"  # one of LAYOUTS
    style: dict = field(default_factory=dict)  # optional colours over the layout: bg, bg2, text, accent (#rrggbb)


FLYERS: Final[tuple[Flyer, ...]] = (
    Flyer("bill", "BILLING DISCOUNTS", "Cut your AWS bill. Keep your setup.",
          ("Partner-rate billing on the spend you already have",
           "Nothing migrates, nothing is re-architected",
           "One consolidated invoice across clouds and licences"),
          "Get a free bill review", "Savings depend on your agreement, spend tier and service mix.",
          "Move your AWS account under our partner agreement and pay partner rates on the spend you already have. "
          f"Send us a recent bill for a free review: {EMAIL}"),
    Flyer("credits", "CLOUD CREDITS", "Up to USD 200,000 in cloud credits",
          ("AWS credit programmes from roughly USD 300 up to USD 200,000",
           "Azure credits for eligible startups and businesses",
           "We prepare and submit your applications with you"),
          "Check what you qualify for", "Credits are approved by the cloud provider and depend on eligibility.",
          "Startup, accelerator-backed or an established business? We help you find and apply for the AWS and "
          f"Azure credit programmes you qualify for. Email {EMAIL}"),
    Flyer("services", "WHAT WE DO", "Cloud, DevOps, security and AI. One team.",
          ("Cloud migration and managed services on AWS, Azure and GCP",
           "DevOps, CI/CD and cloud security with compliance built in",
           "AI/ML solutions and application development"),
          "Talk to our engineers", "Free consultation. No obligation.",
          f"CloudTrio: cloud, DevOps, security and AI under one roof. Talk to our engineers: {EMAIL}"),
    Flyer("partners", "OFFICIAL PARTNER", "One partner for AWS, Azure and Google Cloud",
          ("AWS Advanced Tier Services Partner",
           "Microsoft Azure Solutions Partner",
           "Google Cloud Partner, with Workspace and Gemini licensing"),
          "Move your bill to us", "Billing discounts and support through the partner channel.",
          f"Billing discounts, credit applications and support across AWS, Azure and Google Cloud. {EMAIL}"),
    Flyer("images", "VM IMAGES", "Hardened, ready-to-run cloud VM images",
          ("ReactJS and Node.js on Windows Server",
           "GPU-enabled Linux for AI workloads",
           "DevOps CI/CD runners and database servers"),
          "See the images", "Available for Azure, AWS and Google Cloud.",
          f"Production-ready, preconfigured VM images for Azure, AWS and Google Cloud. Details: {SITE}"),
    Flyer("cloudlunar", "CLOUDLUNAR", "Find the waste in your cloud bill",
          ("25+ optimisation rules across your AWS accounts",
           "AI anomaly detection and cost forecasting",
           "Teams save an average of 40% on cloud spend"),
          "Start free", "Average across customers. Results vary with your environment.",
          f"CloudLunar finds idle and oversized resources and shows what to fix first. Start free at {SITE}"),
    Flyer("licences", "LICENSING", "Microsoft 365, Google Workspace, Zoho and Adobe on one invoice",
          ("Migrate from your old mail server or cPanel hosting",
           "Each user on the plan they actually need",
           "Businesses have seen 30-50% a year off email and productivity licences"),
          "Get a licence review", "Savings vary with your current setup.",
          f"Right-sized licences, renewals tracked, one monthly invoice. Ask us for a review: {EMAIL}"),
)
HASHTAGS: Final = "#cloudcomputing #aws #azure #devops #cloudtrio"  # the default; settings.FLYER_HASHTAGS wins

BY_ID: Final = {f.id: f for f in FLYERS}  # the built-in set; the live set is in the database


# --------------------------------------------------------------------------- the editable set


def hashtags() -> str:
    return settings.FLYER_HASHTAGS


def _row(r: dict) -> Flyer:
    return Flyer(r["id"], r["kicker"], r["headline"], tuple(json.loads(r["points"])), r["cta"], r["note"],
                 r["caption"], bool(r["enabled"]), r["layout"] or "classic", _load_style(r.get("style")))  # fmt: skip


def _seed() -> None:
    """The first time, the built-in flyers go into the database; after that it is yours."""
    if db.kv_get("flyers:seeded"):
        return
    for i, f in enumerate(FLYERS):
        db.execute("INSERT OR IGNORE INTO flyers (id, kicker, headline, points, cta, note, caption, enabled, position) "
                   "VALUES (?,?,?,?,?,?,?,1,?)",
                   f.id, f.kicker, f.headline, json.dumps(list(f.points)), f.cta, f.note, f.caption, i)  # fmt: skip
    db.kv_set("flyers:seeded", "1")


def listing(*, enabled_only: bool = False) -> list[Flyer]:
    _seed()
    found = [_row(r) for r in db.rows("SELECT * FROM flyers WHERE brand_id = ? ORDER BY position, id", settings.BRAND_ID)]
    return [f for f in found if f.enabled] if enabled_only else found


def get(flyer_id: str) -> Flyer | None:
    _seed()
    found = db.rows("SELECT * FROM flyers WHERE id = ?", flyer_id)
    return _row(found[0]) if found else None


def parse(data: dict) -> Flyer:
    """A flyer from the editor's fields, checked. Raises ValueError with what to fix."""

    def text(key: str, label: str, limit: int, *, required: bool = True) -> str:
        value = str(data.get(key) or "").strip()
        if required and not value:
            raise ValueError(f"{label} is required")
        if len(value) > limit:
            raise ValueError(f"{label} is too long (max {limit} characters)")
        return value

    points = data.get("points") or []
    if isinstance(points, str):
        points = points.splitlines()
    points = [str(p).strip() for p in points if str(p).strip()]
    if not 1 <= len(points) <= 4:
        raise ValueError("Add 1 to 4 points, one per line")
    if any(len(p) > 120 for p in points):
        raise ValueError("Each point must be under 120 characters")
    headline = text("headline", "Headline", 120)
    layout = str(data.get("layout") or "classic")
    if layout not in LAYOUT_IDS:
        raise ValueError("Pick one of the listed layouts")
    style = clean_style(data.get("style"))
    slug = re.sub(r"[^a-z0-9]+", "-", str(data.get("id") or headline).lower()).strip("-")[:30] or "flyer"
    return Flyer(slug, text("kicker", "Tag line", 30).upper(), headline, tuple(points), text("cta", "Button text", 40),
                 text("note", "Condition note", 140, required=False), text("caption", "Caption", 1500),
                 bool(data.get("enabled", True)), layout, style)  # fmt: skip


def save(data: dict) -> Flyer:
    """Create or update a flyer. A new one whose id is taken gets a numbered id."""
    flyer = parse(data)
    exists = bool(data.get("id")) and get(flyer.id) is not None
    if not exists:
        taken = {r["id"] for r in db.rows("SELECT id FROM flyers")}  # ids are shared by every brand
        base, n = flyer.id, 2
        while flyer.id in taken:
            flyer, n = Flyer(f"{base}-{n}", *[getattr(flyer, k) for k in
                      ("kicker", "headline", "points", "cta", "note", "caption", "enabled", "layout", "style")]), n + 1  # fmt: skip
    last = db.rows("SELECT COALESCE(MAX(position), -1) AS p FROM flyers WHERE brand_id = ?", settings.BRAND_ID)[0]["p"]
    db.execute(
        "INSERT INTO flyers (id, kicker, headline, points, cta, note, caption, enabled, layout, style, position, brand_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET kicker=excluded.kicker, headline=excluded.headline, points=excluded.points, "
        "cta=excluded.cta, note=excluded.note, caption=excluded.caption, enabled=excluded.enabled, layout=excluded.layout, "
        "style=excluded.style",
        flyer.id, flyer.kicker, flyer.headline, json.dumps(list(flyer.points)), flyer.cta, flyer.note, flyer.caption,
        int(flyer.enabled), flyer.layout, json.dumps(flyer.style), last + 1, settings.BRAND_ID,
    )  # fmt: skip
    return flyer


def delete(flyer_id: str) -> bool:
    if get(flyer_id) is None:
        return False
    db.execute("DELETE FROM flyers WHERE id = ?", flyer_id)
    return True


LAYOUTS: Final = (
    ("classic", "Classic navy"),
    ("aurora", "Aurora glass"),
    ("blueprint", "Blueprint"),
    ("stage", "Spotlight"),
    ("studio", "Light studio"),
    ("bento", "Bento grid"),
    ("horizon", "Sunset horizon"),
    ("console", "Terminal"),
    ("impact", "Big number"),
)
LAYOUT_IDS: Final = frozenset(k for k, _ in LAYOUTS)


# --------------------------------------------------------------------------- drawing

FOOT_TOP: Final = H - 330  # the call to action starts here; nothing above it may reach below FOOT_TOP - 30
ROOM: Final = FOOT_TOP - 30
RGB = tuple[int, int, int]


@dataclass(frozen=True, slots=True)
class Theme:
    top: RGB
    bottom: RGB
    fg: RGB
    muted: RGB
    accent: RGB
    on_accent: RGB
    line: RGB
    logo_text: RGB
    ghost: float = 0.0  # opacity of the large badge behind the copy
    radius: int = 48  # corners of the call to action
    chip: RGB | None = None  # a plate behind the logo, for backgrounds the logo does not suit
    hl: RGB | None = None  # a highlight colour of its own (the paper design's marker)
    site_fg: bool = False  # the web address in the text colour instead of the accent


DARK: Final = Theme(NAVY_TOP, NAVY_BOTTOM, TEXT, MUTED, TEAL, INK, (60, 90, 120), (242, 246, 255), 0.13)
AURORA: Final = Theme((5, 9, 26), (9, 14, 40), (240, 246, 255), (150, 166, 200), (46, 214, 200), (4, 24, 28), (52, 66, 110), (240, 246, 255))
BLUEPRINT: Final = Theme((8, 30, 98), (5, 18, 66), (236, 244, 255), (150, 180, 230), (94, 214, 255), (4, 22, 48), (60, 100, 180),
                         (236, 244, 255), radius=10)  # fmt: skip
STAGE: Final = Theme((4, 5, 10), (12, 14, 26), (247, 248, 255), (138, 146, 176), (138, 150, 255), (10, 12, 40), (40, 46, 76), (247, 248, 255))
STUDIO: Final = Theme((247, 248, 253), (236, 240, 250), (14, 22, 48), (88, 98, 128), (86, 72, 236), (255, 255, 255), (214, 220, 238), (14, 22, 48))
BENTO: Final = Theme((8, 10, 16), (12, 15, 22), (240, 244, 252), (138, 148, 174), (64, 140, 255), (255, 255, 255), (36, 44, 64), (240, 244, 252))
HORIZON: Final = Theme((255, 112, 64), (70, 28, 150), (255, 255, 255), (255, 226, 236), (236, 52, 140), (255, 255, 255), (150, 90, 200),
                       (8, 26, 54), chip=(255, 255, 255))  # fmt: skip
CONSOLE: Final = Theme((6, 10, 10), (9, 16, 15), (226, 236, 230), (128, 150, 142), (110, 255, 190), (4, 30, 20), (34, 54, 48),
                       (226, 236, 230), radius=10)  # fmt: skip
IMPACT: Final = Theme((28, 70, 255), (8, 14, 92), (255, 255, 255), (196, 214, 255), (255, 214, 72), (30, 24, 0), (80, 110, 230), (255, 255, 255))


# --------------------------------------------------------------------------- colours of your own

STYLE_KEYS: Final = ("bg", "bg2", "text", "accent")
HEX: Final = re.compile(r"^#[0-9a-fA-F]{6}$")


def clean_style(raw: object) -> dict[str, str]:
    """Only the four known colours, each ``#rrggbb``; anything else raises ValueError."""
    if not raw:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("Colours must be a set of #rrggbb values")
    out: dict[str, str] = {}
    for key in STYLE_KEYS:
        value = str(raw.get(key) or "").strip()
        if not value:
            continue
        if not HEX.match(value):
            raise ValueError(f"The {key} colour must look like #1a2b3c")
        out[key] = value.lower()
    return out


def _load_style(raw: object) -> dict[str, str]:
    try:
        return clean_style(json.loads(raw) if isinstance(raw, str) and raw else {})
    except ValueError:
        return {}


def _rgb(value: str | None) -> RGB | None:
    return (int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16)) if value else None


def _hex(c: RGB) -> str:
    return "#{:02x}{:02x}{:02x}".format(*c)


def _mix(a: RGB, b: RGB, t: float) -> RGB:
    """``a`` moved ``t`` of the way towards ``b``."""
    return (int(a[0] + (b[0] - a[0]) * t), int(a[1] + (b[1] - a[1]) * t), int(a[2] + (b[2] - a[2]) * t))


def _luminance(c: RGB) -> float:
    def channel(v: int) -> float:
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    return 0.2126 * channel(c[0]) + 0.7152 * channel(c[1]) + 0.0722 * channel(c[2])


def contrast(a: RGB, b: RGB) -> float:
    """WCAG contrast ratio, 1 (none) to 21 (black on white)."""
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _ink_for(c: RGB) -> RGB:
    """Dark or white text, whichever reads better on ``c``."""
    return (12, 18, 32) if contrast(c, (12, 18, 32)) >= contrast(c, (255, 255, 255)) else (255, 255, 255)


def _theme(flyer: Flyer, base: Theme) -> Theme:
    """The layout's own colours, with the flyer's overrides on top."""
    s = flyer.style
    if not s:
        return base
    top = _rgb(s.get("bg")) or base.top
    bottom = _rgb(s.get("bg2")) or (top if s.get("bg") else base.bottom)
    fg = _rgb(s.get("text")) or base.fg
    accent = _rgb(s.get("accent")) or base.accent
    on_accent = _ink_for(accent) if s.get("accent") else base.on_accent
    if not s.get("accent") and contrast(accent, top) < 2.0:  # a background change must not hide the accent
        accent, on_accent = fg, _ink_for(fg)
    moved = bool(s.get("bg") or s.get("text"))
    return replace(base, top=top, bottom=bottom, fg=fg, accent=accent, on_accent=on_accent,
                   muted=_mix(fg, top, 0.4) if moved else base.muted,
                   line=_mix(fg, top, 0.78) if moved else base.line,
                   logo_text=fg if s.get("text") else base.logo_text,
                   hl=accent if s.get("accent") and base.hl else base.hl)  # fmt: skip


def _canvas(th: Theme) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGB", (W, H))
    draw = ImageDraw.Draw(img)
    for y in range(H):
        t = y / H
        draw.line([(0, y), (W, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(th.top, th.bottom, strict=True)))
    return img, draw


def _overlay(img: Image.Image, paint: Callable[[ImageDraw.ImageDraw], None]) -> ImageDraw.ImageDraw:
    """Paint translucent shapes (RGBA colours) onto the picture."""
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    paint(ImageDraw.Draw(layer))
    img.paste(Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB"))
    return ImageDraw.Draw(img)


def _ghost(img: Image.Image, th: Theme) -> ImageDraw.ImageDraw:
    ghost = brand.mark(760).copy()
    ghost.putalpha(ghost.getchannel("A").point(lambda a: int(a * th.ghost)))
    img.paste(ghost, (W - 560, H - 700), ghost)
    return ImageDraw.Draw(img)


def _logo(img: Image.Image, th: Theme, y: int = 60, *, x: int = PAD, center: bool = False, height: int = 84,
          text: RGB | None = None) -> ImageDraw.ImageDraw:  # fmt: skip
    logo = brand.lockup(height, text_color=text or th.logo_text)
    if center:
        x = (W - logo.width) // 2
    draw = ImageDraw.Draw(img)
    if th.chip:
        draw.rounded_rectangle([x - 22, y - 14, x + logo.width + 22, y + height + 14], radius=22, fill=th.chip)
    img.paste(logo, (x, y), logo)
    return ImageDraw.Draw(img)


def _pill(draw: ImageDraw.ImageDraw, th: Theme, text: str, y: int, *, x: int = PAD, center: bool = False,
          fill: RGB | None = None, ink: RGB | None = None, radius: int = 29) -> int:  # fmt: skip
    font = _font(32, bold=True)
    w = draw.textlength(text, font=font) + 48
    if center:
        x = int((W - w) / 2)
    draw.rounded_rectangle([x, y, x + w, y + 58], radius=radius, fill=fill or th.accent)
    draw.text((x + 24, y + 29), text, font=font, fill=ink or th.on_accent, anchor="lm")
    return y + 58


@dataclass(frozen=True, slots=True)
class Plan:
    """The headline and the points, sized together so that both fit above the call to action."""

    hfont: ImageFont.FreeTypeFont
    hlines: list[str]
    hheight: int
    pfont: ImageFont.FreeTypeFont
    blocks: list[list[str]]
    step: int


def _plan(draw: ImageDraw.ImageDraw, flyer: Flyer, *, sizes: tuple[int, ...], lines: int, avail: Callable[[int], int],
          points_width: int, extra: int = 24, min_block: int = 0, head_width: int = W - 2 * PAD,
          pfont_fn: Callable[[int], ImageFont.FreeTypeFont] = _font) -> Plan:  # fmt: skip
    """Largest headline whose points still fit: ``avail(headline_height)`` is the room left for them."""
    for floor in (30, 27, 24, 22):  # keep the points readable: shrink the headline before the points
        for size in sizes:
            hfont = _font(size, bold=True)
            wrapped = _wrap(draw, flyer.headline, hfont, head_width)
            if len(wrapped) > lines:
                continue
            hh = len(wrapped) * int(size * 1.16)
            for psize in (38, 34, 31, 30, 28, 27, 25, 24, 22):
                if psize < floor:
                    break
                pfont = pfont_fn(psize)
                blocks = [_wrap(draw, p, pfont, points_width)[:3] for p in flyer.points]
                step = int(psize * 1.32)
                if sum(max(min_block, len(b) * step + extra) for b in blocks) <= avail(hh):
                    return Plan(hfont, wrapped, hh, pfont, blocks, step)
    hfont = _font(sizes[-1], bold=True)
    wrapped = _wrap(draw, flyer.headline, hfont, head_width)[:lines]
    pfont = pfont_fn(22)
    return Plan(hfont, wrapped, len(wrapped) * int(sizes[-1] * 1.16), pfont,
                [_wrap(draw, p, pfont, points_width)[:2] for p in flyer.points], 29)  # fmt: skip


def _title(draw: ImageDraw.ImageDraw, plan: Plan, y: int, fill: RGB, *, center: bool = False, last: RGB | None = None,
           x: int = PAD) -> int:  # fmt: skip
    """Draw the headline; returns the y below it."""
    for i, line in enumerate(plan.hlines):
        colour = last if last and i == len(plan.hlines) - 1 else fill
        if center:
            draw.text((W // 2, y), line, font=plan.hfont, fill=colour, anchor="ma")
        else:
            draw.text((x, y), line, font=plan.hfont, fill=colour)
        y += int(plan.hfont.size * 1.16)
    return y


def _tick(draw: ImageDraw.ImageDraw, x: int, y: int, fill: RGB = TEAL, ink: RGB = INK) -> None:
    draw.ellipse([x, y, x + 44, y + 44], fill=fill)
    draw.line([(x + 11, y + 23), (x + 19, y + 31), (x + 33, y + 14)], fill=ink, width=6, joint="curve")


def _points(draw: ImageDraw.ImageDraw, plan: Plan, y: int, *, fill: RGB, ink: RGB, text: RGB, marker: str = "tick",
            x: int = PAD) -> int:  # fmt: skip
    """The points as a list with a marker (tick, square, dash or num) in front of each."""
    indent = {"tick": 70, "num": 70, "square": 52, "dash": 58}[marker]
    for i, block in enumerate(plan.blocks, 1):
        if marker == "tick":
            _tick(draw, x, y + 2, fill, ink)
        elif marker == "num":
            draw.ellipse([x, y + 2, x + 44, y + 46], fill=fill)
            draw.text((x + 22, y + 25), str(i), font=_font(26, bold=True), fill=ink, anchor="mm")
        elif marker == "square":
            draw.rectangle([x + 4, y + 10, x + 26, y + 32], fill=fill)
        else:
            draw.line([(x, y + plan.step // 2), (x + 36, y + plan.step // 2)], fill=fill, width=5)
        for line in block:
            draw.text((x + indent, y), line, font=plan.pfont, fill=text)
            y += plan.step
        y += 24
    return y


# --- drawing tools for the richer designs: smooth shapes, glows, frosted glass, gradient fills and text

SS = 2  # shapes are drawn at twice the size and shrunk, so their edges are smooth
MONO_FONTS: Final = ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
                     "C:/Windows/Fonts/consola.ttf", "DejaVuSansMono.ttf")  # fmt: skip
MONO_BOLD: Final = ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
                    "C:/Windows/Fonts/consolab.ttf", "DejaVuSansMono-Bold.ttf")  # fmt: skip


def _mono(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    for path in MONO_BOLD if bold else MONO_FONTS:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return _font(size, bold=bold)


class _Layer:
    """Translucent shapes in the picture's own coordinates, drawn smooth and laid over the picture."""

    def __init__(self) -> None:
        self.img = Image.new("RGBA", (W * SS, H * SS), (0, 0, 0, 0))
        self.d = ImageDraw.Draw(self.img)

    @staticmethod
    def _s(points):
        return [v * SS for v in points] if not isinstance(points[0], tuple) else [(x * SS, y * SS) for x, y in points]

    def ellipse(self, box, fill=None, outline=None, width=1) -> None:
        self.d.ellipse(self._s(box), fill=fill, outline=outline, width=width * SS)

    def rrect(self, box, radius, fill=None, outline=None, width=1) -> None:
        self.d.rounded_rectangle(self._s(box), radius=radius * SS, fill=fill, outline=outline, width=width * SS)

    def polygon(self, points, fill=None, outline=None) -> None:
        self.d.polygon(self._s(points), fill=fill, outline=outline)

    def line(self, points, fill, width=1) -> None:
        self.d.line(self._s(points), fill=fill, width=width * SS, joint="curve")

    def arc(self, box, start, end, fill, width=1) -> None:
        self.d.arc(self._s(box), start, end, fill=fill, width=width * SS)

    def apply(self, img: Image.Image, blur: float = 0) -> None:
        small = self.img.resize((W, H), Image.LANCZOS)
        if blur:
            small = small.filter(ImageFilter.GaussianBlur(blur))
        img.paste(Image.alpha_composite(img.convert("RGBA"), small).convert("RGB"))


def _acc2(th: Theme, shift: float = 0.12) -> RGB:
    """A partner of the accent colour, a little round the colour wheel, for gradients and glows."""
    h, lightness, sat = colorsys.rgb_to_hls(*(v / 255 for v in th.accent))
    r, g, b = colorsys.hls_to_rgb((h + shift) % 1.0, lightness, sat)
    return (int(r * 255), int(g * 255), int(b * 255))


def _glow(img: Image.Image, cx: float, cy: float, radius: float, color: RGB, alpha: float, squash: float = 1.0) -> None:
    """A soft round light that fades out at ``radius``."""
    w, h = int(radius * 2), int(radius * 2 * squash)
    g = Image.radial_gradient("L").resize((w, h), Image.BICUBIC)
    mask = g.point(lambda v: int(255 * alpha * max(0.0, 1 - v / 181) ** 2.2))
    img.paste(Image.new("RGB", (w, h), color), (int(cx - w / 2), int(cy - h / 2)), mask)


def _grain(img: Image.Image, amount: float = 0.05) -> None:
    """A little film grain, which keeps big gradients from looking flat."""
    noise = Image.effect_noise(img.size, 48).convert("RGB")
    img.paste(Image.blend(img, ImageChops.overlay(img, noise), amount))


def _rmask(w: int, h: int, radius: int) -> Image.Image:
    m = Image.new("L", (w * 2, h * 2), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, w * 2 - 1, h * 2 - 1], radius=radius * 2, fill=255)
    return m.resize((w, h), Image.LANCZOS)


def _lin(w: int, h: int, mode: str = "h") -> Image.Image:
    """A black-to-white gradient: ``h`` left to right, ``v`` top to bottom, ``d`` corner to corner."""
    row = Image.new("L", (w, 1))
    row.putdata([int(255 * i / max(1, w - 1)) for i in range(w)])
    col = Image.new("L", (1, h))
    col.putdata([int(255 * i / max(1, h - 1)) for i in range(h)])
    horizontal, vertical = row.resize((w, h)), col.resize((w, h))
    return horizontal if mode == "h" else vertical if mode == "v" else Image.blend(horizontal, vertical, 0.5)


def _grad_fill(img: Image.Image, box, radius: int, c1: RGB, c2: RGB, mode: str = "h") -> None:
    x0, y0, x1, y1 = (int(v) for v in box)
    w, h = x1 - x0, y1 - y0
    colour = Image.composite(Image.new("RGB", (w, h), c2), Image.new("RGB", (w, h), c1), _lin(w, h, mode))
    img.paste(colour, (x0, y0), _rmask(w, h, radius))


def _grad_text(img: Image.Image, xy, text: str, font: ImageFont.FreeTypeFont, c1: RGB, c2: RGB, anchor: str = "la") -> None:
    x0, y0, x1, y1 = ImageDraw.Draw(img).textbbox(xy, text, font=font, anchor=anchor)
    w, h = x1 - x0, y1 - y0
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).text((xy[0] - x0, xy[1] - y0), text, font=font, fill=255, anchor=anchor)
    colour = Image.composite(Image.new("RGB", (w, h), c2), Image.new("RGB", (w, h), c1), _lin(w, h, "h"))
    img.paste(colour, (x0, y0), mask)


def _glass(img: Image.Image, box, radius: int = 36, *, tint: RGB = (255, 255, 255), fill: float = 0.07, edge: float = 0.3, blur: float = 22) -> None:
    """A frosted panel: what is behind it, blurred, with a faint tint and a bright edge."""
    x0, y0, x1, y1 = (int(v) for v in box)
    w, h = x1 - x0, y1 - y0
    pane = Image.blend(img.crop((x0, y0, x1, y1)).filter(ImageFilter.GaussianBlur(blur)), Image.new("RGB", (w, h), tint), fill)
    mask = _rmask(w, h, radius)
    img.paste(pane, (x0, y0), mask)
    inner = Image.new("L", (w, h), 0)
    inner.paste(_rmask(w - 4, h - 4, max(1, radius - 2)), (2, 2))
    ring = ImageChops.subtract(mask, inner).point(lambda v: int(v * edge))
    img.paste(Image.new("RGB", (w, h), (255, 255, 255)), (x0, y0), ring)


def _shadow(img: Image.Image, box, radius: int, *, blur: int = 26, dy: int = 16, alpha: float = 0.2, color: RGB = (30, 40, 90)) -> None:
    x0, y0, x1, y1 = (int(v) for v in box)
    pad = blur * 3
    m = Image.new("L", (x1 - x0 + pad * 2, y1 - y0 + pad * 2), 0)
    ImageDraw.Draw(m).rounded_rectangle([pad, pad, pad + x1 - x0, pad + y1 - y0], radius=radius, fill=int(255 * alpha))
    img.paste(Image.new("RGB", m.size, color), (x0 - pad, y0 - pad + dy), m.filter(ImageFilter.GaussianBlur(blur)))


def _hexagon(cx: float, cy: float, r: float, turn: float = 0.0) -> list[tuple[float, float]]:
    return [(cx + r * math.cos(math.radians(60 * i + 30 + turn)), cy + r * math.sin(math.radians(60 * i + 30 + turn))) for i in range(6)]


NEWS_ID: Final = "news"  # a news card drawn through a flyer layout; its note is the source line


def _news_footer(draw: ImageDraw.ImageDraw, th: Theme, flyer: Flyer, *, center: bool, x0: int) -> None:
    """Where a flyer has its button: the source on one side, the page handle on the other."""
    source = _wrap(draw, flyer.note, _font(34, bold=True), W - 2 * PAD)[:1]
    handle = f"@{settings.IG_HANDLE}"
    if center:
        draw.text((W // 2, FOOT_TOP + 40), source[0] if source else "", font=_font(34, bold=True), fill=th.muted, anchor="mm")
        draw.text((W // 2, FOOT_TOP + 124), handle, font=_font(54, bold=True), fill=th.accent, anchor="mm")
    else:
        draw.text((x0, FOOT_TOP + 40), source[0] if source else "", font=_font(34, bold=True), fill=th.muted, anchor="lm")
        draw.text((x0, FOOT_TOP + 124), handle, font=_font(54, bold=True), fill=th.accent, anchor="lm")
    draw.line([(x0, H - 118), (W - PAD, H - 118)], fill=th.line, width=2)
    line = "Daily cloud, DevOps and AI news. Follow for more."
    if center:
        draw.text((W // 2, H - 72), line, font=_font(28), fill=th.muted, anchor="mm")
    else:
        draw.text((x0, H - 72), line, font=_font(28), fill=th.muted, anchor="lm")


def _footer(draw: ImageDraw.ImageDraw, th: Theme, flyer: Flyer, *, center: bool = False, x0: int = PAD, img: Image.Image | None = None,
            grad: tuple[RGB, RGB] | None = None, outline: bool = False) -> None:  # fmt: skip
    """The call to action, the contact line and the condition that goes with the claim."""
    if flyer.id == NEWS_ID:
        return _news_footer(draw, th, flyer, center=center, x0=x0)
    cta_font = _font(40, bold=True)
    cta = f"{flyer.cta}  >"
    cw = int(draw.textlength(cta, font=cta_font))
    left = int((W - (cw + 80)) / 2) if center else x0
    box = [left, FOOT_TOP, left + cw + 80, FOOT_TOP + 96]
    radius = min(th.radius, 48)
    text = th.on_accent
    if grad and img is not None:
        _grad_fill(img, box, radius, grad[0], grad[1])
        text = _ink_for(_mix(grad[0], grad[1], 0.5))
    elif outline:
        draw.rounded_rectangle(box, radius=radius, outline=th.accent, width=3)
        text = th.accent
    else:
        draw.rounded_rectangle(box, radius=radius, fill=th.accent)
    draw.text((left + 40, FOOT_TOP + 48), cta, font=cta_font, fill=text, anchor="lm")
    if center:
        both = f"{settings.BRAND_EMAIL}   ·   {settings.BRAND_SITE}"
        draw.text((W // 2, FOOT_TOP + 150), both, font=_font(36, bold=True), fill=th.fg, anchor="mm")
    else:
        draw.text((x0, FOOT_TOP + 150), settings.BRAND_EMAIL, font=_font(40, bold=True), fill=th.fg, anchor="lm")
        draw.text((W - PAD, FOOT_TOP + 150), settings.BRAND_SITE, font=_font(40, bold=True), fill=th.fg if th.site_fg else th.accent, anchor="rm")
    draw.line([(x0, H - 118), (W - PAD, H - 118)], fill=th.line, width=2)
    for i, line in enumerate(_wrap(draw, flyer.note, _font(26), W - x0 - PAD)[:2]):
        if center:
            draw.text((W // 2, H - 88 + i * 34), line, font=_font(26), fill=th.muted, anchor="mm")
        else:
            draw.text((x0, H - 88 + i * 34), line, font=_font(26), fill=th.muted, anchor="lm")


# --- the nine designs: each takes a flyer and returns the finished image


def _classic(flyer: Flyer) -> Image.Image:
    th = _theme(flyer, DARK)
    img, draw = _canvas(th)
    draw.rectangle([0, 0, W, 14], fill=th.accent)
    draw = _logo(img, th, 60)
    draw = _ghost(img, th)
    plan = _plan(draw, flyer, sizes=(84, 76, 68, 60, 54), lines=4, avail=lambda h: ROOM - (292 + h + 82), points_width=W - 2 * PAD - 80)
    _pill(draw, th, flyer.kicker, 190)
    y = _title(draw, plan, 292, th.fg) + 36
    draw.line([(PAD, y), (PAD + 160, y)], fill=th.accent, width=6)
    _points(draw, plan, y + 46, fill=th.accent, ink=th.on_accent, text=th.fg)
    _footer(draw, th, flyer)
    return img


def _aurora(flyer: Flyer) -> Image.Image:
    """Colour glows behind a frosted-glass panel."""
    th = _theme(flyer, AURORA)
    img, draw = _canvas(th)
    c2, c3 = _acc2(th, 0.13), _acc2(th, 0.32)
    _glow(img, W - 110, 130, 640, th.accent, 0.62)
    _glow(img, 40, 720, 660, c3, 0.5)
    _glow(img, W - 160, 1130, 560, c2, 0.42)
    _grain(img, 0.05)
    draw = _logo(img, th, 58)
    x, width = PAD + 20, W - 2 * PAD - 40
    plan = _plan(draw, flyer, sizes=(80, 72, 64, 58, 52), lines=4, avail=lambda h: (FOOT_TOP - 60) - (338 + h + 30), points_width=width - 64, head_width=width)
    content_end = 312 + plan.hheight + 30 + sum(len(b) * plan.step + 24 for b in plan.blocks)
    _glass(img, (PAD - 28, 190, W - PAD + 28, min(FOOT_TOP - 26, max(content_end + 22, 780))), 46)
    tw = draw.textlength(flyer.kicker, font=_font(28, bold=True))
    _glass(img, (x, 226, x + tw + 66, 280), 27, fill=0.1, edge=0.4, blur=6)
    _glow(img, x + 28, 253, 26, th.accent, 0.9)
    draw.ellipse([x + 22, 247, x + 34, 259], fill=th.accent)
    draw.text((x + 48, 253), flyer.kicker, font=_font(28, bold=True), fill=th.fg, anchor="lm")
    y = _title(draw, plan, 312, th.fg, x=x) + 30
    for block in plan.blocks:
        cy = y + plan.step // 2
        _glow(img, x + 14, cy, 26, th.accent, 0.8)
        draw.ellipse([x + 7, cy - 7, x + 21, cy + 7], fill=th.accent)
        for line in block:
            draw.text((x + 46, y), line, font=plan.pfont, fill=th.fg)
            y += plan.step
        y += 24
    _footer(draw, th, flyer, img=img, grad=(th.accent, c2))
    return img


def _blueprint(flyer: Flyer) -> Image.Image:
    """An engineering drawing: grid, a diagram of clouds joined through the CloudTrio hexagon."""
    th = _theme(flyer, BLUEPRINT)
    img, draw = _canvas(th)
    minor, major = _mix(th.top, th.fg, 0.07), _mix(th.top, th.fg, 0.14)
    for gx in range(0, W + 1, 40):
        draw.line([(gx, 0), (gx, H)], fill=major if gx % 200 == 0 else minor, width=1)
    for gy in range(0, H + 1, 40):
        draw.line([(0, gy), (W, gy)], fill=major if gy % 200 == 0 else minor, width=1)
    _glow(img, 800, 240, 420, th.accent, 0.3)
    hub = (800, 235)
    nodes = [(640, 120), (975, 135), (985, 345), (640, 360), (815, 70)]
    lay = _Layer()
    for nx, ny in nodes:
        lay.line([hub, (nx, ny)], (*th.accent, 150), 2)
    for r in (118, 150):
        for k in range(0, 360, 14 if r == 118 else 20):
            lay.arc((hub[0] - r, hub[1] - r, hub[0] + r, hub[1] + r), k, k + 6, (*th.accent, 120 if r == 118 else 70), 2)
    lay.polygon(_hexagon(*hub, 70), fill=(*_mix(th.top, (0, 0, 0), 0.4), 255), outline=(*th.accent, 255))
    lay.polygon(_hexagon(*hub, 58), outline=(*th.accent, 110))
    for nx, ny in nodes:
        lay.ellipse((nx - 26, ny - 26, nx + 26, ny + 26), fill=(*_mix(th.top, (0, 0, 0), 0.3), 255), outline=(*th.accent, 255), width=3)
        lay.ellipse((nx - 7, ny - 7, nx + 7, ny + 7), fill=(*th.accent, 255))
    lay.apply(img)
    mark = brand.mark(76)
    img.paste(mark, (hub[0] - 38, hub[1] - 38), mark)
    draw = _logo(img, th, 58)
    for cx, cy, sx, sy in ((36, 36, 1, 1), (W - 36, 36, -1, 1), (36, H - 36, 1, -1), (W - 36, H - 36, -1, -1)):
        draw.line([(cx, cy), (cx + 46 * sx, cy)], fill=th.accent, width=3)
        draw.line([(cx, cy), (cx, cy + 46 * sy)], fill=th.accent, width=3)
    font = _font(28, bold=True)
    tw = draw.textlength(flyer.kicker, font=font)
    draw.rectangle([PAD, 196, PAD + tw + 60, 248], outline=th.accent, width=2)
    draw.rectangle([PAD + 16, 216, PAD + 28, 228], fill=th.accent)
    draw.text((PAD + 42, 222), flyer.kicker, font=font, fill=th.accent, anchor="lm")
    plan = _plan(draw, flyer, sizes=(82, 74, 66, 58, 52), lines=4, avail=lambda h: ROOM - (410 + h + 30), points_width=W - 2 * PAD - 56)
    y = _title(draw, plan, 410, th.fg) + 30
    draw.line([(PAD, y - 6), (PAD, y - 6 + sum(len(b) * plan.step + 24 for b in plan.blocks) - 24 + 12)], fill=th.accent, width=3)
    for block in plan.blocks:
        draw.rectangle([PAD + 22, y + plan.step // 2 - 5, PAD + 32, y + plan.step // 2 + 5], fill=th.accent)
        for line in block:
            draw.text((PAD + 52, y), line, font=plan.pfont, fill=th.fg)
            y += plan.step
        y += 24
    _footer(draw, th, flyer)
    return img


def _stage(flyer: Flyer) -> Image.Image:
    """Black stage, one light from above, headline set in a gradient."""
    th = _theme(flyer, STAGE)
    img, draw = _canvas(th)
    _glow(img, W // 2, -40, 900, _mix(th.accent, (255, 255, 255), 0.45), 0.42, squash=1.2)
    _glow(img, W // 2, H + 80, 700, th.accent, 0.16, squash=0.7)
    _grain(img, 0.04)
    draw = _logo(img, th, 58)
    draw.line([(PAD, 224), (PAD + 44, 224)], fill=th.accent, width=3)
    draw.text((PAD + 62, 224), flyer.kicker, font=_font(28, bold=True), fill=th.accent, anchor="lm")
    plan = _plan(draw, flyer, sizes=(112, 102, 92, 82, 72, 64, 56), lines=5, avail=lambda h: ROOM - (272 + h + 36), points_width=W - 2 * PAD - 60)
    y = 272
    for line in plan.hlines:
        _grad_text(img, (PAD, y), line, plan.hfont, th.fg, th.accent)
        y += int(plan.hfont.size * 1.16)
    y += 36
    for block in plan.blocks:
        draw.line([(PAD, y - 12), (W - PAD, y - 12)], fill=th.line, width=1)
        draw.line([(PAD, y + plan.step // 2), (PAD + 30, y + plan.step // 2)], fill=th.accent, width=4)
        for line in block:
            draw.text((PAD + 54, y), line, font=plan.pfont, fill=th.fg)
            y += plan.step
        y += 24
    _footer(draw, th, flyer, outline=True)
    return img


def _studio(flyer: Flyer) -> Image.Image:
    """A light, airy page: soft colour clouds, white cards that float."""
    th = _theme(flyer, STUDIO)
    img, draw = _canvas(th)
    c2 = _acc2(th, 0.1)
    _glow(img, 940, 90, 560, th.accent, 0.26)
    _glow(img, 60, 560, 500, _acc2(th, 0.2), 0.22)
    _glow(img, 900, 1160, 560, (255, 186, 142), 0.3)
    draw = _logo(img, th, 58)
    tw = draw.textlength(flyer.kicker, font=_font(28, bold=True))
    draw.rounded_rectangle([PAD, 190, PAD + tw + 50, 244], radius=27, fill=_mix(th.accent, (255, 255, 255), 0.86))
    draw.text((PAD + 25, 217), flyer.kicker, font=_font(28, bold=True), fill=th.accent, anchor="lm")
    plan = _plan(draw, flyer, sizes=(84, 76, 68, 60, 54), lines=4, avail=lambda h: ROOM - (278 + h + 44), points_width=W - 2 * PAD - 150, extra=58, min_block=108)
    y = _title(draw, plan, 278, th.fg) + 44
    for block in plan.blocks:
        h = max(92, len(block) * plan.step + 40)
        box = (PAD, y, W - PAD, y + h)
        _shadow(img, box, 28, color=_mix(th.accent, (20, 20, 60), 0.5), alpha=0.16)
        draw.rounded_rectangle(box, radius=28, fill=(255, 255, 255), outline=th.line, width=2)
        _grad_fill(img, (PAD + 26, y + (h - 52) // 2, PAD + 78, y + (h - 52) // 2 + 52), 26, th.accent, c2, "d")
        cy = y + h // 2
        draw.line([(PAD + 41, cy), (PAD + 49, cy + 8), (PAD + 63, cy - 8)], fill=(255, 255, 255), width=5, joint="curve")
        ty = y + (h - len(block) * plan.step) // 2
        for line in block:
            draw.text((PAD + 104, ty), line, font=plan.pfont, fill=th.fg)
            ty += plan.step
        y += h + 16
    _footer(draw, th, flyer, img=img, grad=(th.accent, c2))
    return img


def _bento(flyer: Flyer) -> Image.Image:
    """Tiles of different sizes, as in a bento box."""
    th = _theme(flyer, BENTO)
    img, draw = _canvas(th)
    c2 = _acc2(th, 0.14)
    _glow(img, W - 60, 60, 440, th.accent, 0.2)
    _grain(img, 0.035)
    draw = _logo(img, th, 58)
    n, gap = len(flyer.points), 16
    cw = (W - 2 * PAD - gap) // 2
    rows = {1: [[0]], 2: [[0, 1]], 3: [[0], [1, 2]], 4: [[0, 1], [2, 3]]}[n]
    top = 194
    head_ink = _ink_for(_mix(th.accent, c2, 0.5))
    chosen = None
    for hsize in (76, 68, 62, 56, 50, 46):
        hfont = _font(hsize, bold=True)
        hlines = _wrap(draw, flyer.headline, hfont, W - 2 * PAD - 72)
        if len(hlines) > 3:
            continue
        tile_h = 30 + 50 + 16 + len(hlines) * int(hsize * 1.16) + 34
        for psize in (32, 29, 26, 24, 22):
            pfont = _font(psize)
            step = int(psize * 1.32)
            heights = []
            for row in rows:
                width = (W - 2 * PAD if len(row) == 1 else cw) - 56
                heights.append(max(26 + 44 + 14 + len(_wrap(draw, flyer.points[i], pfont, width)[:3]) * step + 26 for i in row))
            if top + tile_h + gap + sum(heights) + gap * (len(rows) - 1) <= ROOM + 16:
                chosen = (hfont, hlines, tile_h, pfont, step, heights)
                break
        if chosen:
            break
    if not chosen:
        hfont = _font(46, bold=True)
        hlines = _wrap(draw, flyer.headline, hfont, W - 2 * PAD - 72)[:3]
        pfont = _font(22)
        chosen = (hfont, hlines, 30 + 50 + 16 + len(hlines) * 53 + 34, pfont, 29, [150] * len(rows))
    hfont, hlines, tile_h, pfont, step, heights = chosen
    _grad_fill(img, (PAD, top, W - PAD, top + tile_h), 40, th.accent, c2, "d")
    draw.rounded_rectangle([PAD + 36, top + 30, PAD + 36 + draw.textlength(flyer.kicker, font=_font(26, bold=True)) + 36, top + 78], radius=24, fill=_mix(th.accent, (0, 0, 0), 0.35))
    draw.text((PAD + 54, top + 54), flyer.kicker, font=_font(26, bold=True), fill=(255, 255, 255), anchor="lm")
    y = top + 30 + 50 + 16
    for line in hlines:
        draw.text((PAD + 36, y), line, font=hfont, fill=head_ink)
        y += int(hfont.size * 1.16)
    y = top + tile_h + gap
    fill, edge = _mix(th.top, th.fg, 0.07), _mix(th.top, th.fg, 0.16)
    for row, rh in zip(rows, heights, strict=True):
        tile_w = W - 2 * PAD if len(row) == 1 else cw
        for col, idx in enumerate(row):
            x = PAD + col * (cw + gap)
            draw.rounded_rectangle([x, y, x + tile_w, y + rh], radius=32, fill=fill, outline=edge, width=2)
            _grad_fill(img, (x + 26, y + 26, x + 70, y + 70), 22, th.accent, c2, "d")
            draw.line([(x + 37, y + 49), (x + 44, y + 56), (x + 58, y + 41)], fill=(255, 255, 255), width=4, joint="curve")
            ty = y + 26 + 44 + 14
            for line in _wrap(draw, flyer.points[idx], pfont, tile_w - 56)[:3]:
                draw.text((x + 28, ty), line, font=pfont, fill=th.fg)
                ty += step
        y += rh + gap
    _footer(draw, th, flyer)
    return img


def _horizon(flyer: Flyer) -> Image.Image:
    """A sunset gradient hero over a dark panel that rises to meet it."""
    th = _theme(flyer, HORIZON)
    img = Image.new("RGB", (W, H))
    draw = ImageDraw.Draw(img)
    for y in range(H):
        t = y / (H * 0.6)
        colour = _mix(th.top, th.accent, min(1.0, t / 0.55)) if t < 0.55 else _mix(th.accent, th.bottom, min(1.0, (t - 0.55) / 0.45))
        draw.line([(0, y), (W, y)], fill=colour)
    lay = _Layer()
    for r, a in ((330, 60), (240, 70), (150, 80)):
        lay.polygon(_hexagon(W - 120, 250, r), outline=(255, 255, 255, a))
    lay.polygon(_hexagon(W - 120, 250, 150), fill=(255, 255, 255, 26))
    lay.polygon(_hexagon(40, 640, 210), outline=(255, 255, 255, 46))
    lay.apply(img)
    _grain(img, 0.04)
    draw = _logo(img, th, 58)
    dark = _mix(th.bottom, (0, 0, 0), 0.78)
    plan = _plan(draw, flyer, sizes=(88, 80, 72, 64, 56), lines=4, avail=lambda h: ROOM - (max(284 + h + 50, 600) + 44), points_width=W - 2 * PAD - 80)
    tw = draw.textlength(flyer.kicker, font=_font(28, bold=True))
    _glass(img, (PAD, 192, PAD + tw + 52, 246), 27, fill=0.16, edge=0.5, blur=4)
    draw.text((PAD + 26, 219), flyer.kicker, font=_font(28, bold=True), fill=th.fg, anchor="lm")
    y = _title(draw, plan, 276, th.fg)
    top = max(y + 50, 600)
    panel = _Layer()
    panel.rrect((-4, top, W + 4, H + 80), 64, fill=(*dark, 255))
    panel.apply(img)
    _points(draw, plan, top + 44, fill=th.accent, ink=_ink_for(th.accent), text=(245, 240, 250))
    _footer(draw, th, flyer, img=img, grad=(th.accent, _mix(th.accent, th.top, 0.7)))
    return img


def _console(flyer: Flyer) -> Image.Image:
    """A terminal window, for the DevOps crowd."""
    th = _theme(flyer, CONSOLE)
    img, draw = _canvas(th)
    _glow(img, 140, 110, 520, th.accent, 0.1)
    _glow(img, W - 60, 1100, 480, _acc2(th, 0.2), 0.08)
    draw = _logo(img, th, 58)
    win = (PAD - 12, 186, W - PAD + 12, FOOT_TOP - 26)
    _shadow(img, win, 26, blur=30, dy=18, alpha=0.5, color=(0, 0, 0))
    draw.rounded_rectangle(win, radius=26, fill=_mix(th.top, th.fg, 0.05), outline=th.line, width=2)
    draw.line([(win[0] + 2, win[1] + 56), (win[2] - 2, win[1] + 56)], fill=th.line, width=2)
    for i, c in enumerate(((255, 95, 86), (255, 189, 46), (39, 201, 63))):
        draw.ellipse([win[0] + 28 + i * 28, win[1] + 20, win[0] + 44 + i * 28, win[1] + 36], fill=c)
    draw.text(((win[0] + win[2]) // 2, win[1] + 29), "cloudtrio ~ newsroom", font=_mono(22), fill=th.muted, anchor="mm")
    x, width = win[0] + 36, win[2] - win[0] - 72
    slug = re.sub(r"[^a-z0-9]+", "-", flyer.kicker.lower()).strip("-")[:34]
    py = win[1] + 56 + 30
    draw.text((x, py), "$", font=_mono(26, bold=True), fill=th.accent)
    draw.text((x + 30, py), f"cloudtrio post --topic {slug}", font=_mono(26), fill=th.muted)
    plan = _plan(draw, flyer, sizes=(78, 70, 64, 58, 52, 46, 42), lines=3, avail=lambda h: (win[3] - 40) - (py + 70 + h + 24), points_width=width - 54, head_width=width, pfont_fn=_mono, extra=22)
    y = _title(draw, plan, py + 62, th.fg, x=x) + 24
    last = (x, y)
    for block in plan.blocks:
        draw.line([(x + 2, y + plan.step // 2), (x + 10, y + plan.step // 2 + 8), (x + 26, y + plan.step // 2 - 9)], fill=th.accent, width=4, joint="curve")
        for line in block:
            draw.text((x + 46, y), line, font=plan.pfont, fill=th.fg)
            last = (x + 46 + draw.textlength(line, font=plan.pfont), y)
            y += plan.step
        y += 22
    draw.rectangle([last[0] + 12, last[1] + 3, last[0] + 12 + plan.pfont.size * 0.6, last[1] + plan.pfont.size * 1.15], fill=th.accent)
    _footer(draw, th, flyer)
    return img


STAT_RE: Final = re.compile(
    r"\d[\d,.]*\s?[-\u2013]\s?\d[\d,.]*\s?%"  # a range of percentages: 30-50%
    r"|(?:USD|INR|US\$|\$|\u20b9|Rs\.?)\s?\d[\d,.]*\s?(?:k|K|M|lakh|crore)?"  # money
    r"|\d[\d,.]*\s?(?:%|\+|x\b|X\b)"  # a percentage, a count with a plus, a multiple
)


def _hero_stat(flyer: Flyer) -> str:
    """The one number worth showing huge (200,000 / 40% / 25+), or an empty string."""
    for text in (flyer.headline, *flyer.points):
        m = STAT_RE.search(text)
        if m and len(m.group(0).strip()) <= 16:
            return m.group(0).strip()
    return ""


def _impact(flyer: Flyer) -> Image.Image:
    """The number that matters, set very large."""
    th = _theme(flyer, IMPACT)
    img, draw = _canvas(th)
    _glow(img, W - 60, 250, 640, th.accent, 0.3)
    _glow(img, 0, 900, 700, _mix(th.top, (120, 60, 255), 0.5), 0.55)
    lay = _Layer()
    for r in range(220, 1000, 130):
        lay.ellipse((W - 40 - r, 260 - r, W - 40 + r, 260 + r), outline=(255, 255, 255, 30), width=2)
    lay.apply(img)
    _grain(img, 0.04)
    draw = _logo(img, th, 58)
    draw = ImageDraw.Draw(img)
    _pill(draw, th, flyer.kicker, 186)
    stat = _hero_stat(flyer)
    y = 268
    if stat:
        for size in (300, 270, 240, 210, 180, 150, 124, 100):
            font = _font(size, bold=True)
            if draw.textlength(stat, font=font) <= W - 2 * PAD:
                break
        _grad_text(img, (PAD - 6, y), stat, font, (255, 255, 255), th.accent)
        y += int(size * 1.12) + 6
        plan = _plan(draw, flyer, sizes=(58, 52, 48, 44, 40), lines=3, avail=lambda h: ROOM - (y + h + 28), points_width=W - 2 * PAD - 80)
    else:
        plan = _plan(draw, flyer, sizes=(120, 108, 96, 84, 74, 64, 56), lines=5, avail=lambda h: ROOM - (y + h + 30), points_width=W - 2 * PAD - 80)
    y = _title(draw, plan, y, th.fg, last=None if stat else th.accent) + 28
    _points(draw, plan, y, fill=th.accent, ink=th.on_accent, text=th.fg)
    _footer(draw, th, flyer)
    return img


_DRAW: Final = {
    "classic": _classic, "aurora": _aurora, "blueprint": _blueprint, "stage": _stage, "studio": _studio,
    "bento": _bento, "horizon": _horizon, "console": _console, "impact": _impact,
}  # fmt: skip


# --------------------------------------------------------------------------- the studio: palettes and advice

BASES: Final = {"classic": DARK, "aurora": AURORA, "blueprint": BLUEPRINT, "stage": STAGE, "studio": STUDIO, "bento": BENTO,
                "horizon": HORIZON, "console": CONSOLE, "impact": IMPACT}  # fmt: skip

#: Ready-made colour sets: background, second background (equal for a flat colour), text, accent.
PALETTES: Final = (
    {"id": "brand", "name": "CloudTrio navy", "bg": "#081a36", "bg2": "#0a3454", "text": "#f0f4ff", "accent": "#2fd4c4"},
    {"id": "violet", "name": "Midnight violet", "bg": "#220e4e", "bg2": "#602cac", "text": "#ffffff", "accent": "#ff5cc8"},
    {"id": "lime", "name": "Black and lime", "bg": "#080808", "bg2": "#080808", "text": "#f5f5f5", "accent": "#c8ff3c"},
    {"id": "gold", "name": "Black and gold", "bg": "#0c0c10", "bg2": "#1c1914", "text": "#f6f0e2", "accent": "#d4af37"},
    {"id": "forest", "name": "Forest mint", "bg": "#062822", "bg2": "#0a4636", "text": "#f0f9f1", "accent": "#78f0b8"},
    {"id": "ocean", "name": "Ocean light", "bg": "#eaf6ff", "bg2": "#cce5fc", "text": "#081e42", "accent": "#0c60b0"},
    {"id": "paper", "name": "Warm paper", "bg": "#faf4e8", "bg2": "#faf4e8", "text": "#121212", "accent": "#e8590c"},
    {"id": "coral", "name": "Coral sunrise", "bg": "#ff6840", "bg2": "#ffa83c", "text": "#ffffff", "accent": "#3c1208"},
    {"id": "alert", "name": "Security red", "bg": "#1a0808", "bg2": "#3a0e12", "text": "#fff1f0", "accent": "#ff4d4f"},
    {"id": "slate", "name": "Slate and amber", "bg": "#1e293b", "bg2": "#0f172a", "text": "#f8fafc", "accent": "#fbbf24"},
    {"id": "royal", "name": "Royal blue", "bg": "#0b1f5c", "bg2": "#1d4ed8", "text": "#ffffff", "accent": "#fcd34d"},
    {"id": "graphite", "name": "Graphite and cyan", "bg": "#111827", "bg2": "#1f2937", "text": "#f9fafb", "accent": "#22d3ee"},
)  # fmt: skip

#: Words in a flyer that point at a palette that suits the subject.
TOPIC_PALETTES: Final = (
    (("security", "breach", "hack", "risk", "threat", "compliance", "soc"), ("alert", "graphite", "slate")),
    (("credit", "discount", "save", "saving", "bill", "cost", "free", "offer", "%"), ("gold", "coral", "paper")),
    (("ai", "ml", "gemini", "model", "intelligence", "agent"), ("violet", "lime", "graphite")),
    (("devops", "kubernetes", "ci/cd", "pipeline", "container", "terraform"), ("graphite", "lime", "slate")),
    (("licence", "license", "microsoft", "workspace", "zoho", "adobe", "365", "office"), ("royal", "ocean", "paper")),
    (("aws", "azure", "google cloud", "gcp", "cloud", "migration", "partner"), ("brand", "ocean", "royal")),
    (("startup", "launch", "new"), ("lime", "coral", "violet")),
)  # fmt: skip

ACTION_WORDS: Final = frozenset(
    "get start book talk check claim see try move ask send request download join reserve learn find explore "
    "compare apply schedule contact view open".split()
)  # fmt: skip


def theme_defaults() -> dict[str, dict[str, str]]:
    """Each layout's own colours, so the studio's pickers start where the layout does."""
    return {k: {"bg": _hex(t.top), "bg2": _hex(t.bottom), "text": _hex(t.fg), "accent": _hex(t.accent)} for k, t in BASES.items()}


def palettes_for(text: str) -> list[dict]:
    """All palettes, the ones that suit this copy first (each marked ``match``)."""
    low = text.lower()
    wanted: list[str] = []
    for words, ids in TOPIC_PALETTES:
        if any(w in low for w in words):
            wanted += [i for i in ids if i not in wanted]
    ranked = [{**p, "match": p["id"] in wanted[:4]} for p in PALETTES]
    return sorted(ranked, key=lambda p: (wanted.index(p["id"]) if p["id"] in wanted[:4] else 99))


def advise(data: dict) -> list[dict]:
    """Checks on the copy and the colours: ``[{"level": good | warn | info, "text": ...}]``."""
    notes: list[dict] = []

    def add(level: str, text: str) -> None:
        notes.append({"level": level, "text": text})

    headline = str(data.get("headline") or "").strip()
    kicker = str(data.get("kicker") or "").strip()
    cta = str(data.get("cta") or "").strip()
    note = str(data.get("note") or "").strip()
    points = data.get("points") or []
    if isinstance(points, str):
        points = points.splitlines()
    points = [str(p).strip() for p in points if str(p).strip()]
    layout = str(data.get("layout") or "classic")

    if headline:
        words = len(headline.split())
        if len(headline) > 80 or words > 13:
            add("warn", f"The headline is {len(headline)} characters. On a phone it reads best under about 60.")
        elif len(headline) <= 60:
            add("good", "The headline is short enough to read at a glance.")
    if len(points) > 3:
        add("info", "Three points are easier to take in than four.")
    elif points:
        add("good", f"{len(points)} point{'s' if len(points) != 1 else ''}, which suits the space.")
    if any(len(pt) > 90 for pt in points):
        add("warn", "A point is longer than 90 characters, so its text will shrink. Trim it to one short line.")
    if len(kicker) > 22:
        add("info", "A shorter tag line (under 22 characters) keeps the tag compact.")
    if cta and cta.split()[0].lower().strip(",.") not in ACTION_WORDS:
        add("info", "Start the button with an action word, such as Get, Book, Check or Talk.")
    if re.search(r"(\d\s*%|usd|\$|\u20b9)", headline + " ".join(points), re.I) and not note:
        add("warn", "A claim with a number needs a condition note, such as 'Savings depend on your spend'.")

    try:
        style = clean_style(data.get("style"))
    except ValueError:
        style = {}
    base = BASES.get(layout, DARK)
    th = _theme(Flyer("x", "", "", (), "", "", "", True, layout, style), base)
    pairs = (("Text on the background", th.fg, th.top, 4.5), ("Text at the bottom of the background", th.fg, th.bottom, 4.5),
             ("The accent on the background", th.accent, th.top, 3.0))  # fmt: skip
    for label, fg, bg, need in pairs:
        ratio = contrast(fg, bg)
        if ratio < need:
            add("warn", f"{label} has low contrast ({ratio:.1f}:1, aim for {need:g}+). Pick a lighter or darker colour.")
    if all(contrast(f, b) >= n for _, f, b, n in pairs):
        add("good", "Colours have good contrast, so the flyer stays readable on a small screen.")
    return notes


def ai_ideas(data: dict) -> dict:
    """Headline alternatives and a layout and palette that suit the copy, from the language model.
    Returns ``{"error": ...}`` rather than raising, since this is a nicety."""
    from technews.agents import llm

    if not llm.available():
        return {"error": "AI ideas need an NVIDIA key (Settings, AI agents)."}
    brief = {k: data.get(k) for k in ("kicker", "headline", "points", "cta")}
    system = (
        "You are a senior social-media designer for CloudTrio, a cloud, DevOps and AI services company. Given a flyer's "
        "copy, reply with one JSON object: {\"headlines\": [three alternative headlines, each under 60 characters, plain "
        "words, no hype or exaggeration], \"layout\": one of " + ", ".join(sorted(LAYOUT_IDS)) + ", \"palette\": one of "
        + ", ".join(p["id"] for p in PALETTES) + ", \"why\": one sentence on why that look suits the message, "
        "\"tips\": [up to three short, specific suggestions to improve the flyer]}."
    )
    try:
        reply = llm.chat_json(system=system, user=json.dumps(brief), max_tokens=700, temperature=0.6)
    except llm.LLMError as exc:
        return {"error": f"The AI did not answer ({str(exc)[:80]}). Try again."}
    raw = reply.data
    palette_ids = {p["id"] for p in PALETTES}
    return {
        "headlines": [str(h).strip()[:120] for h in (raw.get("headlines") or []) if str(h).strip()][:3],
        "layout": raw.get("layout") if raw.get("layout") in LAYOUT_IDS else "",
        "palette": raw.get("palette") if raw.get("palette") in palette_ids else "",
        "why": str(raw.get("why") or "")[:240],
        "tips": [str(t).strip()[:200] for t in (raw.get("tips") or []) if str(t).strip()][:3],
    }


@lru_cache(maxsize=32)
def layout_sample(layout: str, width: int = 360) -> bytes:
    """A small picture of a layout, drawn with sample copy, for the studio's layout gallery."""
    sample = replace(FLYERS[1], layout=layout if layout in LAYOUT_IDS else "classic")
    img = _DRAW[sample.layout](sample).resize((width, int(width * H / W)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=82)
    return buf.getvalue()


def render_news(layout: str, *, label: str, headline: str, points: list[str], source: str, path: Any) -> Any:
    """A news card in one of the flyer layouts. ``path`` may be a file or a buffer."""
    flyer = Flyer(NEWS_ID, label, headline, tuple(points), "", f"Source: {source}"[:60], "", layout=layout if layout in LAYOUT_IDS else "classic")
    _DRAW[flyer.layout](flyer).save(path, "JPEG", quality=92)
    return path


def render(flyer: Flyer, path: Path | None = None) -> Path:
    img = _DRAW.get(flyer.layout, _classic)(flyer)
    if path is None:
        settings.CARDS_DIR.mkdir(parents=True, exist_ok=True)
        path = settings.CARDS_DIR / f"flyer-{flyer.id}.jpg"
    img.save(path, "JPEG", quality=92)
    return path


if __name__ == "__main__":
    for f in listing():
        print(render(f))
