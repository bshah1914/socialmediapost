"""The post image: a 1080x1350 (4:5) card with the category, the headline, the key points
and the source."""

from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

from technews.config import settings

W, H = 1080, 1350
PAD: Final = 80
BG_TOP, BG_BOTTOM = (10, 14, 26), (22, 30, 58)
TEXT, MUTED = (240, 244, 255), (150, 160, 190)
ACCENT: Final[dict[str, tuple[int, int, int]]] = {
    "cloud": (64, 200, 255), "devops": (48, 209, 188), "hack": (255, 69, 58), "breach": (255, 69, 58), "vulnerability": (255, 159, 10),
    "scam": (255, 159, 10), "outage": (255, 214, 10), "ai": (94, 92, 230),
    "bigtech": (10, 132, 255), "launch": (48, 209, 88), "viral": (255, 55, 95),
    "policy": (100, 210, 255), "other": (10, 132, 255),
}  # fmt: skip
LABEL: Final[dict[str, str]] = {"cloud": "CLOUD", "devops": "DEVOPS", "hack": "CYBER ATTACK", "breach": "DATA BREACH", "vulnerability": "SECURITY ALERT",
                                "scam": "SCAM ALERT", "outage": "OUTAGE", "ai": "AI", "bigtech": "BIG TECH",
                                "launch": "LAUNCH", "viral": "VIRAL", "policy": "TECH POLICY", "other": "TECH NEWS"}  # fmt: skip

BOLD_FONTS: Final = ("/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/Library/Fonts/Arial Bold.ttf",
                     "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
                     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "DejaVuSans-Bold.ttf",
                     "C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf")  # fmt: skip
REGULAR_FONTS: Final = ("/System/Library/Fonts/Supplemental/Arial.ttf", "/Library/Fonts/Arial.ttf",
                        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
                        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "DejaVuSans.ttf",
                        "C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/arial.ttf")  # fmt: skip


#: Fonts for the Indian scripts. They do not hold the Latin letters, so a piece of text is drawn in one of them only when
#: it is itself in that script (the greeting line); everything else keeps the usual font. Complex scripts need Pillow's
#: text shaping (libraqm), which the server image has.
SCRIPT_FONTS: Final[dict[str, tuple[tuple[str, ...], tuple[str, ...]]]] = {
    "deva": (("/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf", "C:/Windows/Fonts/Nirmala.ttc"),
             ("/usr/share/fonts/truetype/noto/NotoSansDevanagari-Bold.ttf", "C:/Windows/Fonts/NirmalaB.ttc")),
    "gujr": (("/usr/share/fonts/truetype/noto/NotoSansGujarati-Regular.ttf", "C:/Windows/Fonts/Nirmala.ttc"),
             ("/usr/share/fonts/truetype/noto/NotoSansGujarati-Bold.ttf", "C:/Windows/Fonts/NirmalaB.ttc")),
}  # fmt: skip


def script_of(text: str) -> str:
    """"deva" or "gujr" when the text holds Devanagari or Gujarati letters, else "" (Latin)."""
    if any("\u0900" <= ch <= "\u097f" for ch in text):
        return "deva"
    if any("\u0a80" <= ch <= "\u0aff" for ch in text):
        return "gujr"
    return ""


@lru_cache(maxsize=64)
def _script_font(script: str, size: int, bold: bool) -> ImageFont.FreeTypeFont | None:
    for path in SCRIPT_FONTS[script][1 if bold else 0]:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return None


def _for(text: object, font: Any) -> Any:
    """The font to draw ``text`` with: the script's own font when the text is in an Indian script, else ``font``."""
    if not isinstance(font, ImageFont.FreeTypeFont) or not isinstance(text, str) or not (script := script_of(text)):
        return font
    bold = "bold" in str(getattr(font, "path", "")).lower() or str(getattr(font, "path", "")).lower().endswith("bd.ttf")
    return _script_font(script, int(font.size), bold) or font


_draw_text, _draw_length, _draw_bbox = ImageDraw.ImageDraw.text, ImageDraw.ImageDraw.textlength, ImageDraw.ImageDraw.textbbox


def _text(self: ImageDraw.ImageDraw, xy: Any, text: Any, *args: Any, font: Any = None, **kw: Any) -> Any:
    return _draw_text(self, xy, text, *args, font=_for(text, font), **kw)


def _length(self: ImageDraw.ImageDraw, text: Any, font: Any = None, *args: Any, **kw: Any) -> Any:
    return _draw_length(self, text, _for(text, font), *args, **kw)


def _bbox(self: ImageDraw.ImageDraw, xy: Any, text: Any, font: Any = None, *args: Any, **kw: Any) -> Any:
    return _draw_bbox(self, xy, text, _for(text, font), *args, **kw)


ImageDraw.ImageDraw.text, ImageDraw.ImageDraw.textlength, ImageDraw.ImageDraw.textbbox = _text, _length, _bbox  # type: ignore[method-assign]


def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    for path in BOLD_FONTS if bold else REGULAR_FONTS:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, width: int) -> list[str]:
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if draw.textlength(trial, font=font) <= width or not line:
            line = trial
        else:
            lines.append(line)
            line = word
    return [*lines, line] if line else lines


def _fit_headline(draw: ImageDraw.ImageDraw, text: str) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    """The biggest size at which the headline fits in 4 lines."""
    for size in (90, 80, 72, 64, 58, 52):
        font = _font(size, bold=True)
        lines = _wrap(draw, text, font, W - 2 * PAD)
        if len(lines) <= 4:
            return font, lines
    return font, lines[:4]


def render(*, story_id: int, category: str, headline: str, points: list[str], source: str, path: Any = None) -> Any:
    img = Image.new("RGB", (W, H))
    draw = ImageDraw.Draw(img)
    for y in range(H):  # vertical gradient
        t = y / H
        draw.line([(0, y), (W, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(BG_TOP, BG_BOTTOM, strict=True)))
    accent = ACCENT.get(category, ACCENT["other"])
    draw.rectangle([0, 0, W, 14], fill=accent)

    # Brand lockup + date, then the category pill
    from technews import brand

    logo = brand.lockup(84)
    img.paste(logo, (PAD, 60), logo)
    date = datetime.now(ZoneInfo(settings.TIMEZONE)).strftime("%d %b %Y")
    draw.text((W - PAD, 102), date, font=_font(32), fill=MUTED, anchor="rm")
    label_font = _font(34, bold=True)
    label = LABEL.get(category, "TECH NEWS")
    lw = draw.textlength(label, font=label_font)
    draw.rounded_rectangle([PAD, 190, PAD + lw + 48, 250], radius=30, fill=accent)
    draw.text((PAD + 24, 220), label, font=label_font, fill=(10, 10, 10), anchor="lm")

    # Headline
    font, lines = _fit_headline(draw, headline)
    y = 300
    for line in lines:
        draw.text((PAD, y), line, font=font, fill=TEXT)
        y += int(font.size * 1.18)

    # Key points
    y += 40
    draw.line([(PAD, y), (PAD + 160, y)], fill=accent, width=6)
    y += 50
    point_font = _font(40)
    for point in points[:3]:
        wrapped = _wrap(draw, point, point_font, W - 2 * PAD - 50)[:3]
        draw.ellipse([PAD, y + 14, PAD + 18, y + 32], fill=accent)
        for line in wrapped:
            if y > H - 220:
                break
            draw.text((PAD + 50, y), line, font=point_font, fill=TEXT)
            y += 54
        y += 26

    # Footer
    draw.line([(PAD, H - 150), (W - PAD, H - 150)], fill=(60, 70, 100), width=2)
    draw.text((PAD, H - 95), f"Source: {source}"[:48], font=_font(32), fill=MUTED, anchor="lm")
    draw.text((W - PAD, H - 95), f"@{settings.IG_HANDLE}", font=_font(34, bold=True), fill=accent, anchor="rm")

    if path is None:
        settings.CARDS_DIR.mkdir(parents=True, exist_ok=True)
        path = settings.CARDS_DIR / f"story-{story_id}.jpg"
    img.save(path, "JPEG", quality=92)
    return path
