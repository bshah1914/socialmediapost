"""Animated marketing flyers: the flyer told as a short story, with its words moving, cut on the music's beat.

    intro    the logo draws itself: the mark is uncovered from its centre, a ring spreads out, the name follows
    hook     the kicker chip pops, the headline builds word by word, a marker sweeps behind the words that sell
    points   each benefit arrives in turn, its tick mark drawing itself
    offer    the big number counts up ("USD 200,000"), or a stamp lands; skipped when there is neither
    cta      the logo, the call to action pulsing on the beat, the email and website typing out, "Follow @..."
             and the small print

Four presets set the character; every scene change falls on a beat of the track:

    bold       punchy words, an accent wipe between scenes, the background pulsing on the beat
    corporate  calm rise and fade, the calm track
    tech       typewriter headline with a cursor, monospace labels, a moving grid
    offer      punchy words, the stamp and the count-up, a light sweep

Everything is drawn from the flyer itself (its words, its layout's colours and the logo), so a changed flyer
makes a changed video. Frames go to ffmpeg exactly as in :mod:`technews.motion`."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Final

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from technews import brand, card
from technews.config import settings

W: Final = 1080
H: Final = 1920
FPS: Final = 30
MARGIN: Final = 84
SAFE_BOTTOM: Final = 1480  # Instagram lays the caption and buttons over the bottom of a Reel
PRESETS: Final = (
    ("bold", "Bold"), ("corporate", "Corporate"), ("tech", "Tech"), ("offer", "Offer"),
    ("minimal", "Minimal"), ("neon", "Neon"), ("magazine", "Magazine"),
    ("impact", "Impact"), ("story", "Story"), ("data", "Data"),
    ("bounce", "Bounce"), ("spring", "Spring"), ("scale", "Scale"),
    ("elegant", "Elegant"), ("pulse", "Pulse"), ("matrix", "Matrix"),
)
PRESET_IDS: Final = frozenset(k for k, _ in PRESETS)
TRACK_BPM: Final = {"upbeat": 112, "calm": 60}
SELLING: Final = frozenset("free save saving savings cut cuts credits credit discount discounts partner zero no lower cheaper faster "
                           "secure bill bills offer offers instant guaranteed".split())  # fmt: skip
MONO_FONTS: Final = ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", "C:/Windows/Fonts/consolab.ttf", "C:/Windows/Fonts/consola.ttf")

RGB = tuple[int, int, int]


@dataclass(frozen=True, slots=True)
class Preset:
    words: str   # punch | rise | type | slam
    wipe: bool   # an accent band between scenes (else the scene slides up and fades)
    pulse: bool  # the background brightens a little on every beat
    grid: bool
    stamp: bool  # a stamp for the offer scene when there is no number
    sweep: bool  # a band of light passes over the hook
    glow: bool = False      # neon glow halo blurred behind each word
    big_hook: bool = False  # oversized headline font for the hook scene
    slow: bool = False      # word timing multiplied by 1.6 (story pace)


STYLE: Final = {
    "bold":      Preset("punch", True,  True,  False, False, True),
    "corporate": Preset("rise",  False, False, False, False, False),
    "tech":      Preset("type",  True,  False, True,  False, False),
    "offer":     Preset("punch", False, True,  False, True,  True),
    "minimal":   Preset("rise",  False, False, False, False, False),
    "neon":      Preset("punch", True,  True,  True,  False, True,  glow=True),
    "magazine":  Preset("punch", False, False, False, False, True,  big_hook=True),
    "impact":    Preset("slam",  True,  True,  False, False, False),
    "story":     Preset("rise",  False, False, False, False, False, slow=True),
    "data":      Preset("type",  False, False, True,  True,  False),
    # --- new animation options ---
    "bounce":    Preset("drop",  True,  True,  False, False, False),                      # words drop & bounce in
    "spring":    Preset("wave",  False, True,  False, False, True),                       # words spring up elastically
    "scale":     Preset("zoom",  True,  True,  False, False, True),                       # words scale up with a sweep
    "elegant":   Preset("fade",  False, False, False, False, False, big_hook=True),       # soft, oversized, luxurious
    "pulse":     Preset("drop",  False, True,  False, True,  False, glow=True),           # bouncy glow + stamp
    "matrix":    Preset("zoom",  False, False, True,  False, False, glow=True),           # tech grid + glowing zoom
}


# --------------------------------------------------------------------------- easing and drawing helpers


def _clip(t: float) -> float:
    return min(1.0, max(0.0, t))


def ease(t: float) -> float:
    return 1 - (1 - _clip(t)) ** 3


def ease_back(t: float) -> float:
    """Ease out with a small overshoot (a pop)."""
    t = _clip(t)
    c = 1.70158
    return 1 + (c + 1) * (t - 1) ** 3 + c * (t - 1) ** 2


def ease_bounce(t: float) -> float:
    """Ease out with a bounce landing."""
    t = _clip(t)
    n, d = 7.5625, 2.75
    if t < 1 / d:
        return n * t * t
    if t < 2 / d:
        t -= 1.5 / d
        return n * t * t + 0.75
    if t < 2.5 / d:
        t -= 2.25 / d
        return n * t * t + 0.9375
    t -= 2.625 / d
    return n * t * t + 0.984375


def ease_elastic(t: float) -> float:
    """Ease out with a springy settle."""
    t = _clip(t)
    if t in (0.0, 1.0):
        return t
    return 2 ** (-10 * t) * math.sin((t * 10 - 0.75) * (2 * math.pi / 3)) + 1


def span(t: float, start: float, length: float) -> float:
    return _clip((t - start) / length) if length > 0 else 1.0


@lru_cache(maxsize=64)
def _mono(size: int) -> ImageFont.FreeTypeFont:
    for path in MONO_FONTS:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return card._font(size, bold=True)


def font(size: int, *, bold: bool = True, mono: bool = False) -> ImageFont.FreeTypeFont:
    return _mono(size) if mono else card._font(size, bold=bold)


def sprite(text: str, fnt: Any, color: RGB) -> Image.Image:
    """``text`` as a tight transparent image."""
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    left, _, right, _ = probe.textbbox((0, 0), text, font=fnt)
    ascent, descent = fnt.getmetrics()
    img = Image.new("RGBA", (max(1, right - left + 4), ascent + descent + 4), (0, 0, 0, 0))
    ImageDraw.Draw(img).text((2 - left, 2), text, font=fnt, fill=color + (255,))
    return img


def faded(img: Image.Image, alpha: float) -> Image.Image:
    if alpha >= 0.999:
        return img
    out = img.copy()
    out.putalpha(img.getchannel("A").point(lambda a: int(a * max(0.0, alpha))))
    return out


def put(frame: Image.Image, img: Image.Image, x: float, y: float, *, alpha: float = 1.0, scale: float = 1.0) -> None:
    """Paste ``img`` with its top-left at (x, y) (scaled about its centre)."""
    if alpha <= 0.01:
        return
    if abs(scale - 1) > 0.01:
        w, h = max(1, int(img.width * scale)), max(1, int(img.height * scale))
        x, y = x - (w - img.width) / 2, y - (h - img.height) / 2
        img = img.resize((w, h), Image.BILINEAR)
    img = faded(img, alpha)
    frame.paste(img, (int(x), int(y)), img)


def rounded(size: tuple[int, int], fill: RGB, radius: int, *, alpha: int = 255) -> Image.Image:
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(img).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius=radius, fill=fill + (alpha,))
    return img


def wrap(text: str, fnt: Any, width: int) -> list[list[str]]:
    """Words broken into lines that fit ``width``."""
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    lines: list[list[str]] = [[]]
    for word in text.split():
        trial = " ".join([*lines[-1], word])
        if lines[-1] and probe.textlength(trial, font=fnt) > width:
            lines.append([word])
        else:
            lines[-1].append(word)
    return [line for line in lines if line]


# --------------------------------------------------------------------------- what the flyer says


NUMBER: Final = re.compile(r"(USD|INR|Rs\.?|₹|\$|€)?\s?(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s?(%|x|\+|k|K)?")


def figure(*texts: str) -> tuple[str, float, str, str] | None:
    """The number to count up: (prefix, value, suffix, how it was written), e.g. ("USD ", 200000, "", "200,000").
    A year, or a small bare number, is not worth counting."""
    for text in texts:
        for m in NUMBER.finditer(text):
            prefix, raw, suffix = (m.group(1) or "").strip(), m.group(2), m.group(3) or ""
            value = float(raw.replace(",", ""))
            if value <= 1 or (not prefix and not suffix and (1900 <= value <= 2100 or value < 100)):
                continue
            return (prefix + (" " if prefix[:1].isalpha() else ""), value, suffix, raw)
    return None


def highlights(headline: str, chosen: list[str] | None = None) -> set[int]:
    """Which words of the headline get the marker: the ones asked for, else numbers and selling words, else the last two."""
    words = headline.split()
    clean = [re.sub(r"[^\w%$₹,.]", "", w).lower().strip(".,") for w in words]
    if chosen:
        wanted = {c.lower().strip() for c in chosen if c.strip()}
        picked = {i for i, w in enumerate(clean) if w in wanted or any(w and w in c.split() for c in wanted)}
        if picked:
            return picked
    picked = {i for i, w in enumerate(clean) if re.search(r"\d", w) or w in SELLING}
    picked |= {i - 1 for i in picked if i > 0 and clean[i - 1] in ("usd", "inr", "rs") and re.search(r"\d", clean[i])}  # "USD 200,000"
    if picked:
        return picked
    return {len(words) - 2, len(words) - 1} if len(words) > 3 else set()


# --------------------------------------------------------------------------- the background


class Backdrop:
    """The layout's gradient drifting slowly, floating shapes in two depths, an optional grid and beat pulse."""

    def __init__(self, top: RGB, bottom: RGB, line: RGB, accent: RGB, preset: Preset, beat: float) -> None:
        tall = H + 600
        grad = Image.new("RGB", (1, tall))
        for y in range(tall):
            k = 0.5 - 0.5 * math.cos(2 * math.pi * y / tall)  # top -> bottom -> top, so the drift never shows a seam
            grad.putpixel((0, y), tuple(int(top[i] + (bottom[i] - top[i]) * k) for i in range(3)))
        self.grad, self.preset, self.beat, self.accent = grad.resize((W, tall)), preset, beat, accent
        self.shapes = []
        for i, (x, y, size, depth) in enumerate(((120, 300, 210, 0.4), (860, 520, 300, 0.7), (300, 1250, 260, 0.55), (930, 1500, 180, 0.35),
                                                  (560, 880, 420, 0.25), (80, 1700, 150, 0.6))):  # fmt: skip
            self.shapes.append((self._shape(i, size, line), x, y, depth))
        self.glow = Image.new("RGBA", (W, 900), (0, 0, 0, 0))
        ImageDraw.Draw(self.glow).ellipse((-200, 0, W + 200, 900), fill=accent + (38,))
        self.glow = self.glow.filter(ImageFilter.GaussianBlur(120))

    @staticmethod
    def _shape(i: int, size: int, color: RGB) -> Image.Image:
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        r = size / 2 - 4
        if i % 2 == 0:  # a hexagon, like the badge in the flyers
            pts = [(size / 2 + r * math.cos(math.pi / 6 + k * math.pi / 3), size / 2 + r * math.sin(math.pi / 6 + k * math.pi / 3)) for k in range(6)]
            d.polygon(pts, outline=color + (90,), width=4)
        else:  # a cloud outline
            d.ellipse((size * 0.08, size * 0.42, size * 0.48, size * 0.82), outline=color + (80,), width=4)
            d.ellipse((size * 0.3, size * 0.22, size * 0.78, size * 0.7), outline=color + (80,), width=4)
            d.ellipse((size * 0.55, size * 0.42, size * 0.94, size * 0.82), outline=color + (80,), width=4)
        return img

    def frame(self, t: float) -> Image.Image:
        drift = int((t * 22) % 600)
        out = self.grad.crop((0, drift, W, drift + H)).copy()
        out.paste(self.glow, (0, int(-250 + 60 * math.sin(t * 0.6))), self.glow)
        if self.preset.grid:
            d = ImageDraw.Draw(out)
            step, shift = 90, int(t * 30) % 90
            for x in range(0, W + step, step):
                d.line((x, 0, x, H), fill=tuple(min(255, c + 14) for c in out.getpixel((x if x < W else W - 1, 10))), width=1)
            for y in range(-step + shift, H, step):
                d.line((0, y, W, y), fill=tuple(min(255, c + 14) for c in out.getpixel((10, max(0, min(H - 1, y))))), width=1)
        for img, x, y, depth in self.shapes:
            yy = (y - t * 40 * depth) % (H + 400) - 200
            out.paste(img, (int(x - img.width / 2 + 25 * math.sin(t * depth + x)), int(yy)), img)
        if self.preset.pulse and self.beat:
            phase = (t % self.beat) / self.beat
            lift = max(0.0, 1 - phase * 4) * 0.06
            if lift > 0.005:
                out = Image.blend(out, Image.new("RGB", (W, H), self.accent), lift)
        return out


# --------------------------------------------------------------------------- the film


class Film:
    def __init__(self, flyer: Any, *, preset: str = "bold", bpm: float = 112, handle: str = "", highlight: list[str] | None = None,
                 number: str = "", badge: str = "") -> None:  # fmt: skip
        from technews import flyers

        th = flyers._theme(flyer, flyers.BASES.get(flyer.layout, flyers.DARK))
        self.fg, self.muted, self.accent, self.on_accent, self.line = th.fg, th.muted, th.accent, th.on_accent, th.line
        self.flyer, self.p = flyer, STYLE.get(preset, STYLE["bold"])
        self.mono = self.p.words == "type"
        self.beat = 60 / max(40.0, float(bpm))
        self.handle = handle.lstrip("@")
        self.bg = Backdrop(th.top, th.bottom, th.line, th.accent, self.p, self.beat)
        self.figure = figure(number) if number else figure(flyer.headline, *flyer.points, flyer.cta)
        self.badge = badge or (flyer.cta if self.p.stamp and not self.figure else "")
        self.hl = highlights(flyer.headline, highlight)
        self._layout()
        q = lambda sec: max(1, round(sec / self.beat)) * self.beat  # noqa: E731 - every scene ends on a beat
        self.scenes: list[tuple[str, float]] = [("intro", q(1.7)), ("hook", q(max(3.6, self.words_end + 1.6))), ("points", q(1.0) + q(1.6) * len(self.points))]
        if self.figure or self.badge:
            self.scenes.append(("offer", q(2.6)))
        self.scenes.append(("cta", q(4.4)))
        self.length = sum(d for _, d in self.scenes)
        # the grid thumbnail: the fully-built headline, after the words land but before the
        # scene wipe (which starts 0.3s before the hook ends) — else the cover is a half-wiped panel
        self.cover_at = self.scenes[0][1] + self.scenes[1][1] - 0.55

    # ---- prepared once
    def _layout(self) -> None:
        f = self.flyer
        width = W - 2 * MARGIN
        sizes = (160, 148, 136, 128, 116, 104, 94, 84, 76) if self.p.big_hook else (128, 116, 104, 94, 84, 76)
        max_lines = 3 if self.p.big_hook else 4
        for size in sizes:
            hf = font(size, mono=False)
            lines = wrap(f.headline, hf, width)
            if len(lines) <= max_lines:
                break
        self.hfont, self.hsize = hf, size
        self.words: list[dict[str, Any]] = []
        probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
        space = probe.textlength(" ", font=hf)
        line_h = int(size * 1.16)
        block = line_h * len(lines)
        top = int(H * 0.45 - block / 2)
        self.asc, self.desc = hf.getmetrics()
        n = 0
        for row, line in enumerate(lines):
            widths = [probe.textlength(w, font=hf) for w in line]
            x = MARGIN
            for word, wd in zip(line, widths):
                lit = n in self.hl
                self.words.append({"text": word, "img": sprite(word, hf, self.fg), "lit": sprite(word, hf, self.on_accent) if lit else None, "x": x,
                                   "y": top + row * line_h, "w": int(wd), "h": line_h, "n": n, "on": lit})  # fmt: skip
                x += wd + space
                n += 1
        self.head_bottom = top + block
        at, self.word_at = 0.35, []
        for w in self.words:  # the typewriter takes as long as the letters; the others keep a steady rhythm
            self.word_at.append(at)
            at += 0.035 * len(w["text"]) + 0.05 if self.p.words == "type" else 0.11
        self.words_end = at + 0.4
        if self.p.slow:
            self.word_at = [v * 1.6 for v in self.word_at]
            self.words_end *= 1.6
        kf = font(40, mono=self.mono)
        k = sprite(f.kicker.upper(), kf, self.on_accent)
        self.chip = rounded((k.width + 64, k.height + 24), self.accent, 34)
        self.chip.paste(k, (32, 12), k)
        self.chip_y = top - self.chip.height - 56
        pf, step = font(62, bold=True), int(62 * 1.22)
        self.points, total = [], 0
        for text in f.points:
            lines = [" ".join(line) for line in wrap(text, pf, width - 120)]
            img = Image.new("RGBA", (width - 120, step * len(lines) + 12), (0, 0, 0, 0))
            for i, line in enumerate(lines):
                piece = sprite(line, pf, self.fg)
                img.paste(piece, (0, i * step), piece)
            self.points.append({"img": img})
            total += img.height + 80
        y = int(H * 0.47 - total / 2) + 40
        for pt in self.points:
            pt["y"] = y
            y += pt["img"].height + 80
        self.points_top = self.points[0]["y"] if self.points else 600
        self.small_kicker = sprite(f.kicker.upper(), font(40, mono=self.mono), self.accent)
        cf = font(64)
        c = sprite(f.cta, cf, self.on_accent)
        while c.width > W - 2 * MARGIN - 120 and cf.size > 40:
            cf = font(cf.size - 4)
            c = sprite(f.cta, cf, self.on_accent)
        self.button = rounded((c.width + 120, c.height + 50), self.accent, 70)
        self.button.paste(c, (60, 25), c)
        self.lockup = brand.lockup(120, text_color=self.fg).convert("RGBA")
        self.mark = brand.mark(300).convert("RGBA")
        self.name = sprite(brand.NAME if settings.BRAND_ID == 1 else "", font(84), self.fg) if settings.BRAND_ID == 1 else None
        self.contact = [line for line in (settings.BRAND_EMAIL, settings.BRAND_SITE) if line]
        self.follow = sprite(f"Follow @{self.handle}" if self.handle else settings.REEL_CTA, font(50, bold=False), self.muted)
        self.note = [" ".join(line) for line in wrap(f.note, font(32, bold=False), width)] if f.note else []

    # ---- the scenes; ``lt`` is the time inside the scene, ``d`` its length, ``out`` how far it has left (0..1)
    def intro(self, fr: Image.Image, lt: float, d: float, out: float) -> None:
        a = 1 - out
        grow = ease(span(lt, 0.0, 0.7))
        cx, cy, size = W // 2, int(H * 0.42), self.mark.width
        mask = Image.new("L", self.mark.size, 0)
        r = grow * size * 0.75
        ImageDraw.Draw(mask).ellipse((size / 2 - r, size / 2 - r, size / 2 + r, size / 2 + r), fill=int(255 * a))
        mark = self.mark.copy()
        mark.putalpha(Image.composite(self.mark.getchannel("A"), Image.new("L", mark.size, 0), mask))
        put(fr, mark, cx - size / 2, cy - size / 2, scale=0.85 + 0.15 * ease_back(span(lt, 0, 0.8)))
        ring = span(lt, 0.35, 0.9)
        if 0 < ring < 1:
            d_ = ImageDraw.Draw(fr)
            rr = size * 0.55 + ring * 260
            col = tuple(int(self.accent[i] * (1 - ring) + fr.getpixel((cx, max(0, int(cy - rr))))[i] * ring) for i in range(3))
            d_.ellipse((cx - rr, cy - rr, cx + rr, cy + rr), outline=col, width=max(1, int(10 * (1 - ring))))
        if self.name is not None:
            reveal = ease(span(lt, 0.6, 0.6))
            if reveal > 0:
                shown = self.name.crop((0, 0, max(1, int(self.name.width * reveal)), self.name.height))
                put(fr, shown, cx - self.name.width / 2, cy + size / 2 + 50, alpha=a)

    def _word(self, fr: Image.Image, w: dict[str, Any], t: float, a: float, lift: int) -> None:
        start = self.word_at[w["n"]]
        if self.p.words == "type":  # the typewriter: letters appear, a cursor runs ahead
            prog = span(t, start, 0.035 * len(w["text"]))
            if prog <= 0:
                return
            shown = w["img"].crop((0, 0, max(1, int(w["img"].width * prog)), w["img"].height))
            put(fr, shown, w["x"], w["y"] - lift, alpha=a)
            if prog < 1:
                ImageDraw.Draw(fr).rectangle((w["x"] + shown.width + 4, w["y"] - lift + 14, w["x"] + shown.width + 14, w["y"] - lift + w["h"] - 18), fill=self.accent)
            return
        k = span(t, start, 0.45)
        if k <= 0:
            return
        if self.p.words == "slam":
            # word slams in full-screen large then shrinks to its position
            scale = max(1.0, 6.0 - 5.0 * ease_back(k))
            put(fr, w["img"], w["x"], w["y"] - lift, alpha=a * ease(k * 3), scale=scale)
        elif self.p.words == "punch":
            put(fr, w["img"], w["x"], w["y"] - lift, alpha=a * ease(k * 2), scale=1 + 0.45 * (1 - ease_back(k)))
        elif self.p.words == "drop":  # falls from above and bounces into place
            put(fr, w["img"], w["x"], w["y"] - lift - 220 * (1 - ease_bounce(k)), alpha=a * ease(k * 3))
        elif self.p.words == "wave":  # springs up from below with an elastic settle
            put(fr, w["img"], w["x"], w["y"] - lift + 90 * (1 - ease_elastic(k)), alpha=a * ease(k * 2.5))
        elif self.p.words == "zoom":  # scales up smoothly from small with a fade
            put(fr, w["img"], w["x"], w["y"] - lift, alpha=a * ease(k * 2), scale=0.4 + 0.6 * ease(k))
        elif self.p.words == "fade":  # a soft, elegant fade with a gentle lift
            put(fr, w["img"], w["x"], w["y"] - lift + 24 * (1 - ease(k)), alpha=a * ease(k * 1.4))
        else:  # rise
            put(fr, w["img"], w["x"], w["y"] - lift + 50 * (1 - ease(k)), alpha=a * ease(k))

    def hook(self, fr: Image.Image, lt: float, d: float, out: float) -> None:
        a, lift = 1 - out, int(160 * out) if not self.p.wipe else 0
        pop = ease_back(span(lt, 0.0, 0.45))
        if pop > 0:
            put(fr, self.chip, MARGIN, self.chip_y - lift, alpha=a * min(1.0, pop * 2), scale=0.6 + 0.4 * pop)
        words_end = self.words_end
        marker = span(lt, words_end, 0.5)
        d_ = ImageDraw.Draw(fr)
        if self.p.glow:  # neon: blurred glow halo behind each visible word
            for w in self.words:
                if span(lt, self.word_at[w["n"]], 0.45) > 0:
                    glow_img = w["img"].filter(ImageFilter.GaussianBlur(22))
                    put(fr, faded(glow_img, 0.5 * (1 - out)), w["x"] - 11, w["y"] - lift - 11)
        lit = [w for w in self.words if w["on"]]
        for w in lit:  # the marker goes under the words, so it is drawn first
            if marker > 0:
                x0, y0 = w["x"] - 12, w["y"] - lift + 2 + int(self.asc * 0.16)
                d_.rounded_rectangle((x0, y0, x0 + (w["w"] + 24) * ease(marker), w["y"] - lift + 2 + self.asc + int(self.desc * 0.55)), radius=12, fill=self.accent)
        for w in self.words:
            self._word(fr, w, lt, a, lift)
            if w["on"] and marker > 0:
                cut = w["lit"].crop((0, 0, max(1, int((w["w"] + 12) * ease(marker))), w["lit"].height))
                put(fr, cut, w["x"], w["y"] - lift, alpha=a)
        if self.p.sweep and 0.2 < span(lt, words_end + 0.5, 0.8) < 1:
            k = span(lt, words_end + 0.5, 0.8)
            band = Image.new("RGBA", (220, H), (255, 255, 255, 0))
            ImageDraw.Draw(band).rectangle((0, 0, 220, H), fill=(255, 255, 255, 36))
            band = band.filter(ImageFilter.GaussianBlur(40)).rotate(-14, expand=True)
            put(fr, band, -400 + k * (W + 600), -300, alpha=a)

    def _tick(self, size: int, prog: float) -> Image.Image:
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        ring = ease(_clip(prog / 0.6))
        if ring > 0:
            d.arc((4, 4, size - 4, size - 4), -90, -90 + 360 * ring, fill=self.accent + (255,), width=6)
        tick = _clip((prog - 0.45) / 0.55)
        if tick > 0:
            a, b, c = (size * 0.28, size * 0.53), (size * 0.44, size * 0.68), (size * 0.73, size * 0.36)
            first = min(1.0, tick * 2)
            d.line((a, (a[0] + (b[0] - a[0]) * first, a[1] + (b[1] - a[1]) * first)), fill=self.accent + (255,), width=7)
            if tick > 0.5:
                second = (tick - 0.5) * 2
                d.line((b, (b[0] + (c[0] - b[0]) * second, b[1] + (c[1] - b[1]) * second)), fill=self.accent + (255,), width=7)
        return img

    def points_scene(self, fr: Image.Image, lt: float, d: float, out: float) -> None:
        a, lift = 1 - out, int(160 * out) if not self.p.wipe else 0
        put(fr, self.small_kicker, MARGIN, self.points_top - 130 - lift, alpha=a * ease(span(lt, 0, 0.4)))
        slot = (d - self.beat * max(1, round(1.0 / self.beat))) / max(1, len(self.points))
        for i, pt in enumerate(self.points):
            start = 0.35 + i * slot
            k = span(lt, start, 0.55)
            if k <= 0:
                continue
            put(fr, self._tick(84, span(lt, start, 0.7)), MARGIN - 6, pt["y"] - lift + 2, alpha=a)
            img = pt["img"]
            shown = img.crop((0, 0, max(1, int(img.width * ease(k))), img.height))
            put(fr, shown, MARGIN + 110 + 60 * (1 - ease(k)), pt["y"] - lift, alpha=a * ease(k))

    def offer(self, fr: Image.Image, lt: float, d: float, out: float) -> None:
        a = 1 - out
        if self.figure:
            prefix, value, suffix, raw = self.figure
            now = value * ease(span(lt, 0.15, d * 0.55))
            shown = f"{now:,.0f}" if "," in raw or value >= 1000 else (f"{now:.1f}" if "." in raw else f"{now:.0f}")
            text = f"{prefix}{shown}{suffix}"
            size = 220 if len(text) <= 6 else (170 if len(text) <= 9 else 130)
            big = sprite(text, font(size), self.accent)
            land = ease_back(span(lt, 0.0, 0.5))
            put(fr, big, (W - big.width) / 2, H * 0.40 - big.height / 2, alpha=a * min(1.0, land * 2), scale=0.7 + 0.3 * land)
            label = sprite(self.flyer.kicker.title() if len(self.flyer.kicker) < 30 else "", font(56, bold=False), self.fg)
            put(fr, label, (W - label.width) / 2, H * 0.40 + big.height / 2 + 30, alpha=a * ease(span(lt, 0.5, 0.5)))
        else:
            bf = font(66)
            lines = [" ".join(line) for line in wrap(self.badge, bf, 700)]
            body = Image.new("RGBA", (800, 90 * len(lines) + 120), (0, 0, 0, 0))
            dd = ImageDraw.Draw(body)
            dd.rounded_rectangle((6, 6, 794, body.height - 6), radius=40, outline=self.accent + (255,), width=10)
            for i, line in enumerate(lines):
                s = sprite(line, bf, self.accent)
                body.paste(s, ((800 - s.width) // 2, 60 + i * 90), s)
            stamp = body.rotate(-8, expand=True, resample=Image.BICUBIC)
            land = span(lt, 0.1, 0.35)
            shake = 8 * math.sin(lt * 60) * (1 - span(lt, 0.45, 0.3)) if land >= 1 else 0
            put(fr, stamp, (W - stamp.width) / 2 + shake, H * 0.42 - stamp.height / 2, alpha=a * min(1.0, land * 1.5), scale=2.2 - 1.2 * ease(land))

    def cta(self, fr: Image.Image, lt: float, d: float, out: float) -> None:
        cf = font(56, mono=self.mono)
        block = self.lockup.height + 110 + self.button.height + 90 + 80 * len(self.contact) + 90
        y = (SAFE_BOTTOM - 120 - block) / 2 + 120
        put(fr, self.lockup, (W - self.lockup.width) / 2, y + 30 * (1 - ease(span(lt, 0, 0.5))), alpha=ease(span(lt, 0.0, 0.5)))
        y += self.lockup.height + 110
        pop = ease_back(span(lt, 0.25, 0.5))
        beat_pulse = 1 + 0.05 * max(0.0, 1 - ((lt % self.beat) / self.beat) * 3) if lt > 0.8 else 1
        if pop > 0:
            put(fr, self.button, (W - self.button.width) / 2, y, alpha=min(1.0, pop * 2), scale=(0.5 + 0.5 * pop) * beat_pulse)
        y += self.button.height + 90
        for i, line in enumerate(self.contact):
            typed = span(lt, 0.9 + i * 0.6, 0.045 * len(line))
            if typed > 0:
                full = sprite(line, cf, self.fg)
                part = full.crop((0, 0, max(1, int(full.width * typed)), full.height))
                put(fr, part, (W - full.width) / 2, y)
            y += 80
        put(fr, self.follow, (W - self.follow.width) / 2, y + 20, alpha=ease(span(lt, 2.0, 0.6)))
        for i, line in enumerate(self.note):
            small = sprite(line, font(32, bold=False), self.muted)
            put(fr, small, (W - small.width) / 2, SAFE_BOTTOM - 20 + i * 44, alpha=ease(span(lt, 1.2, 0.6)))

    # ---- the whole film
    def frame(self, t: float) -> Image.Image:
        fr = self.bg.frame(t)
        start = 0.0
        draw = {"intro": self.intro, "hook": self.hook, "points": self.points_scene, "offer": self.offer, "cta": self.cta}
        for i, (name, d) in enumerate(self.scenes):
            if start <= t < start + d or (i == len(self.scenes) - 1 and t >= start):
                lt = t - start
                last = i == len(self.scenes) - 1
                leave = 0.0 if last else span(lt, d - 0.35, 0.35)
                draw[name](fr, lt, d, leave if not self.p.wipe else 0.0)
                if self.p.wipe and not last and lt > d - 0.3:  # the accent band covers the cut
                    k = span(lt, d - 0.3, 0.3)
                    ImageDraw.Draw(fr).rectangle((0, 0, int(W * ease(k)), H), fill=self.accent)
                if self.p.wipe and i > 0 and lt < 0.3:
                    k = span(lt, 0, 0.3)
                    ImageDraw.Draw(fr).rectangle((int(W * ease(k)), 0, W, H), fill=self.accent)
                break
            start += d
        return fr

    def frames(self):  # noqa: ANN201 - a generator of frames
        for n in range(int(self.length * FPS)):
            yield self.frame(n / FPS)


def auto_preset(flyer: Any) -> str:
    """A preset to suit the flyer: numbers/credits → offer, AI/LLM → neon, cloud tooling → tech, else bold."""
    text = " ".join([flyer.kicker, flyer.headline, *flyer.points]).lower()
    if figure(flyer.headline, *flyer.points) or re.search(r"\b(credits?|discount|free|offer)\b", text):
        return "offer"
    if re.search(r"\b(ai|llm|gpt|claude|gemini|openai|anthropic|neural|model)\b", text):
        return "neon"
    if re.search(r"\b(devops|kubernetes|terraform|ci/cd|pipeline|automation|monitoring|security)\b", text):
        return "tech"
    return "bold"
