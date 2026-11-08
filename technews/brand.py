"""The CloudTrio brand: a hexagon badge holding a cloud with a T cut out of it, and three
circuit traces below (cloud, tech, the trio). The artwork lives in ``logos/`` (SVG sources and
PNGs); the app ships the PNG mark in ``technews/static`` and draws the wordmark with Pillow.

    python -m technews.brand    # re-copies the mark, profile picture and lockup into static/
"""

from __future__ import annotations

import shutil
from functools import lru_cache
from pathlib import Path
from typing import Final

from PIL import Image, ImageDraw

STATIC: Final = Path(__file__).resolve().parent / "static"
LOGOS: Final = Path(__file__).resolve().parent.parent / "logos" / "png"
NAME: Final = "CloudTrio"
TAGLINE: Final = "CLOUD · TECH · AI"
TEAL = (47, 212, 196)
MUTED = (147, 165, 204)


@lru_cache(maxsize=1)
def _mark_source() -> Image.Image:
    return Image.open(STATIC / "logo-mark.png").convert("RGBA")


@lru_cache(maxsize=8)
def mark(size: int) -> Image.Image:
    """The square logo mark (RGBA)."""
    return _mark_source().resize((size, size), Image.LANCZOS)


def _font(size: int, *, bold: bool = True):
    from technews.card import _font

    return _font(size, bold=bold)


def _custom(height: int) -> Image.Image | None:
    """The logo another brand uploaded, scaled to ``height`` (None for the first brand or when none was uploaded)."""
    from technews import brands
    from technews.config import settings

    path = brands.logo_path(settings.BRAND_ID)
    if settings.BRAND_ID == 1 or not path.exists():
        return None
    logo = Image.open(path).convert("RGBA")
    return logo.resize((max(1, int(logo.width * height / logo.height)), height), Image.LANCZOS)


def lockup(height: int, *, text_color: tuple[int, int, int] = (242, 246, 255)) -> Image.Image:
    """The mark with the wordmark and tagline to its right (RGBA), for dark backgrounds."""
    from technews.config import settings

    if settings.BRAND_ID != 1:  # another brand: its own logo (if any) and its name
        logo, name_font = _custom(height), _font(int(height * 0.5))
        probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
        text_w = int(probe.textlength(settings.BRAND_NAME, font=name_font))
        gap = int(height * 0.2) if logo else 0
        img = Image.new("RGBA", ((logo.width if logo else 0) + gap + text_w + 4, height), (0, 0, 0, 0))
        if logo:
            img.alpha_composite(logo)
        ImageDraw.Draw(img).text(((logo.width if logo else 0) + gap, int(height * 0.22)), settings.BRAND_NAME, font=name_font, fill=text_color)
        return img
    m = mark(height)
    name_font, tag_font = _font(int(height * 0.5)), _font(int(height * 0.19), bold=False)
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    cloud_w = probe.textlength("Cloud", font=name_font)
    text_w = max(cloud_w + probe.textlength("Trio", font=name_font), probe.textlength(TAGLINE, font=tag_font))
    gap = int(height * 0.2)
    img = Image.new("RGBA", (height + gap + int(text_w) + 4, height), (0, 0, 0, 0))
    img.alpha_composite(m)
    draw = ImageDraw.Draw(img)
    x = height + gap
    draw.text((x, int(height * 0.1)), "Cloud", font=name_font, fill=text_color)
    draw.text((x + cloud_w, int(height * 0.1)), "Trio", font=name_font, fill=TEAL)
    draw.text((x + 2, int(height * 0.7)), TAGLINE, font=tag_font, fill=MUTED)
    return img


def profile(size: int = 1080) -> Image.Image:
    """The Instagram profile picture (Instagram crops it to a circle)."""
    return Image.open(STATIC / "profile.png").convert("RGB").resize((size, size), Image.LANCZOS)


def export() -> list[Path]:
    """Refresh static/ from the logo package."""
    out = [STATIC / "logo-mark.png", STATIC / "logo.png", STATIC / "profile.png"]
    shutil.copyfile(LOGOS / "cloudtrio-mark.png", out[0])
    shutil.copyfile(LOGOS / "cloudtrio-profile.png", out[2])
    mark.cache_clear()
    _mark_source.cache_clear()
    lockup(240).save(out[1])
    return out


if __name__ == "__main__":
    for path in export():
        print(path)
