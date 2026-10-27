"""The CloudTrio brand: three linked nodes (the trio) forming a network inside a rounded
cloud-blue badge. Drawn with Pillow at 4x and scaled down, so it is crisp at any size.

    python -m technews.brand    # writes technews/static/logo.png, logo-mark.png, profile.png
"""

from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path
from typing import Final

from PIL import Image, ImageDraw, ImageFilter

STATIC: Final = Path(__file__).resolve().parent / "static"
NAME: Final = "CloudTrio"
TAGLINE: Final = "TECH · HACKS · AI"
INDIGO, CYAN = (94, 92, 230), (50, 200, 255)
NODE: Final = (255, 255, 255)
SPARK: Final = (255, 214, 10)  # the "breaking" node


def _gradient(size: int, a: tuple[int, int, int], b: tuple[int, int, int]) -> Image.Image:
    """A diagonal gradient from top-left ``a`` to bottom-right ``b``."""
    small = Image.new("RGB", (64, 64))
    px = small.load()
    for y in range(64):
        for x in range(64):
            t = (x + y) / 126
            px[x, y] = tuple(int(c1 + (c2 - c1) * t) for c1, c2 in zip(a, b, strict=True))
    return small.resize((size, size), Image.BICUBIC)


@lru_cache(maxsize=8)
def mark(size: int) -> Image.Image:
    """The square logo mark (RGBA)."""
    s = size * 4
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    badge = Image.new("L", (s, s), 0)
    ImageDraw.Draw(badge).rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * 0.24), fill=255)
    img.paste(_gradient(s, INDIGO, CYAN), (0, 0), badge)

    draw = ImageDraw.Draw(img)
    cx, cy, r = s / 2, s * 0.53, s * 0.25
    # Three nodes on a triangle, top one first; the bottom-right one is the "spark".
    nodes = [(cx + r * math.cos(math.radians(a)), cy + r * math.sin(math.radians(a))) for a in (-90, 150, 30)]
    width = int(s * 0.045)
    for i, p in enumerate(nodes):  # the links
        q = nodes[(i + 1) % 3]
        draw.line([p, q], fill=(240, 246, 255), width=width)
    hub = (cx, cy)
    for p in nodes:  # spokes to the hub
        draw.line([p, hub], fill=(205, 228, 255), width=width // 2)
    draw.ellipse([cx - s * 0.04, cy - s * 0.04, cx + s * 0.04, cy + s * 0.04], fill=(240, 246, 255))
    for i, (x, y) in enumerate(nodes):
        rad = s * (0.085 if i else 0.095)
        color = SPARK if i == 2 else NODE
        glow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        ImageDraw.Draw(glow).ellipse([x - rad * 1.7, y - rad * 1.7, x + rad * 1.7, y + rad * 1.7], fill=(*color, 90))
        img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(s * 0.02)))
        draw.ellipse([x - rad, y - rad, x + rad, y + rad], fill=color)
    return img.resize((size, size), Image.LANCZOS)


def _font(size: int, *, bold: bool = True):
    from technews.card import _font

    return _font(size, bold=bold)


def lockup(height: int, *, text_color: tuple[int, int, int] = (240, 244, 255)) -> Image.Image:
    """The mark with the wordmark and tagline to its right (RGBA)."""
    m = mark(height)
    name_font, tag_font = _font(int(height * 0.5)), _font(int(height * 0.2), bold=False)
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    text_w = max(probe.textlength(NAME, font=name_font), probe.textlength(TAGLINE, font=tag_font))
    img = Image.new("RGBA", (height + int(height * 0.28) + int(text_w) + 4, height), (0, 0, 0, 0))
    img.alpha_composite(m)
    draw = ImageDraw.Draw(img)
    x = height + int(height * 0.28)
    draw.text((x, int(height * 0.05)), NAME, font=name_font, fill=text_color)
    draw.text((x, int(height * 0.68)), TAGLINE, font=tag_font, fill=CYAN)
    return img


def profile(size: int = 1080) -> Image.Image:
    """The Instagram profile picture: the mark centred on the page's dark background
    (Instagram crops it to a circle)."""
    img = Image.new("RGB", (size, size), (10, 14, 26))
    m = mark(int(size * 0.62))
    img.paste(m, ((size - m.width) // 2, (size - m.height) // 2), m)
    return img


def export() -> list[Path]:
    STATIC.mkdir(parents=True, exist_ok=True)
    out = [STATIC / "logo-mark.png", STATIC / "logo.png", STATIC / "profile.png"]
    mark(512).save(out[0])
    lockup(240).save(out[1])
    profile().save(out[2])
    return out


if __name__ == "__main__":
    for path in export():
        print(path)
