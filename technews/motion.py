"""Animated posts: a news card or flyer turned into a short vertical Reel (1080x1920) with an instrumental track.

The picture the newsroom already designed is the star. Around it: a blurred, darkened copy of it as the
background, the brand at the top, a closing line ("Follow for ...") at the bottom, and one of four movements:

    slide     the card rises into place, then drifts slowly closer
    zoom      the card settles from close-up to full view
    reveal    the card is uncovered from the top down, then a light sweep passes over it
    countup   a number from the headline counts up first ("40%"), then the card slides in

The music is CloudTrio's own: two instrumental tracks are generated here (so there is nothing to license and
Instagram does not mute the Reel), or a royalty-free track you upload in Settings. Frames are drawn with Pillow
and encoded by ffmpeg (H.264 + AAC, as Instagram wants)."""

from __future__ import annotations

import logging
import math
import random
import re
import shutil
import struct
import subprocess
import wave
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

from PIL import Image, ImageDraw, ImageFilter

from technews import brand, card
from technews.config import settings

logger = logging.getLogger(__name__)

W: Final = 1080
H: Final = 1920
FPS: Final = 30
SIMPLE: Final = (
    ("slide", "Simple: slide in"), ("zoom", "Simple: zoom"),
    ("reveal", "Simple: reveal"), ("countup", "Simple: count up"),
    ("drift", "Simple: drift in"), ("split", "Simple: split reveal"),
    ("focus", "Simple: focus in"), ("float", "Simple: float"),
)
SIMPLE_IDS: Final = frozenset(k for k, _ in SIMPLE)
#: The animated-text presets (technews.kinetic) first, then the picture simply moving.
STYLES: Final = (
    ("bold", "Bold (animated text)"), ("corporate", "Corporate (animated text)"),
    ("tech", "Tech (animated text)"), ("offer", "Offer (animated text)"),
    ("minimal", "Minimal (animated text)"), ("neon", "Neon (animated text)"),
    ("magazine", "Magazine (animated text)"), ("impact", "Impact (animated text)"),
    ("story", "Story (animated text)"), ("data", "Data (animated text)"),
    *SIMPLE,
)
STYLE_IDS: Final = frozenset(k for k, _ in STYLES)
TRACKS: Final = (("upbeat", "Upbeat (built in)"), ("calm", "Calm (built in)"), ("custom", "Your uploaded track"), ("none", "No music"))
TRACK_IDS: Final = frozenset(k for k, _ in TRACKS)
MUSIC_TYPES: Final = (".mp3", ".m4a", ".aac", ".wav", ".ogg")


class MotionError(RuntimeError):
    pass


def ffmpeg() -> str:
    """The ffmpeg program ("" when there is none: Reels are then switched off)."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg  # a developer machine without ffmpeg installed

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        return ""


def available() -> bool:
    return bool(ffmpeg())


# --------------------------------------------------------------------------- music


def music_dir() -> Path:
    path = settings.DATA_DIR / "music"
    path.mkdir(parents=True, exist_ok=True)
    return path


def custom_track() -> Path | None:
    for ext in MUSIC_TYPES:
        if (path := music_dir() / f"custom{ext}").exists():
            return path
    return None


def save_custom_track(data: bytes, filename: str) -> Path:
    ext = Path(filename).suffix.lower()
    if ext not in MUSIC_TYPES:
        raise ValueError("Upload an MP3, M4A, AAC, WAV or OGG file")
    if not 10_000 < len(data) <= 15 * 1024 * 1024:
        raise ValueError("The track must be between 10 KB and 15 MB")
    for old in music_dir().glob("custom.*"):
        old.unlink()
    path = music_dir() / f"custom{ext}"
    path.write_bytes(data)
    return path


RATE: Final = 22050
LOOP_SECONDS: Final = 32
NOTE: Final = {n: 440 * 2 ** ((i - 9) / 12) for i, n in enumerate(("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"))}


def _hz(name: str) -> float:
    """``"A3"`` -> 220.0."""
    return NOTE[name[:-1]] * 2 ** (int(name[-1]) - 4)


def _add(buf: list[float], start: float, freq: float, length: float, amp: float, *, attack: float = 0.01, decay: float = 0.0, harmonics: tuple[float, ...] = (1.0,)) -> None:
    """A note into ``buf``: a few sine harmonics, a soft attack, then held (``decay`` 0) or dying away."""
    first, count = int(start * RATE), int(length * RATE)
    step = 2 * math.pi * freq / RATE
    for i in range(count):
        j = first + i
        if j >= len(buf):
            break
        t = i / RATE
        env = min(1.0, t / attack) if attack else 1.0
        env *= math.exp(-t * decay) if decay else min(1.0, (length - t) / 0.25)
        buf[j] += amp * env * sum(h_amp * math.sin(step * i * (k + 1)) for k, h_amp in enumerate(harmonics) if h_amp)


def _compose(track: str) -> list[float]:
    buf = [0.0] * (RATE * LOOP_SECONDS)
    chords = (("C3", "E3", "G3", "B3"), ("A2", "C3", "E3", "G3"), ("F2", "A2", "C3", "E3"), ("G2", "B2", "D3", "F3"))
    if track == "calm":  # 60 bpm: warm held chords and a slow, soft arpeggio
        bar = 4.0
        for n in range(LOOP_SECONDS // int(bar)):
            chord = chords[n % 4]
            for note in chord:
                _add(buf, n * bar, _hz(note), bar, 0.06, attack=0.8, harmonics=(1.0, 0.25, 0.08))
            for k in range(8):
                note = chord[(0, 2, 1, 3, 2, 1, 3, 2)[k]]
                _add(buf, n * bar + k * 0.5, _hz(note) * 2, 0.9, 0.05, decay=4.0, harmonics=(1.0, 0.4))
    else:  # upbeat, 112 bpm: chords on every beat, bass, a soft kick and a hat
        beat = 60 / 112
        bar = 4 * beat
        rng = random.Random(7)
        for n in range(int(LOOP_SECONDS / bar)):
            chord = chords[n % 4]
            for b in range(4):
                at = n * bar + b * beat
                for note in chord:
                    _add(buf, at, _hz(note) * 2, beat * 0.9, 0.028, decay=3.0, harmonics=(1.0, 0.5, 0.2))
                _add(buf, at, _hz(chord[0]) / 2, beat * 0.8, 0.16, decay=2.5, harmonics=(1.0, 0.3))
                first = int(at * RATE)  # kick: a falling sine
                for i in range(int(0.18 * RATE)):
                    if first + i < len(buf):
                        t = i / RATE
                        buf[first + i] += 0.35 * math.exp(-t * 22) * math.sin(2 * math.pi * (50 + 90 * math.exp(-t * 30)) * t)
                first = int((at + beat / 2) * RATE)  # hat on the off-beat: a short burst of noise
                for i in range(int(0.05 * RATE)):
                    if first + i < len(buf):
                        buf[first + i] += 0.05 * math.exp(-i / RATE * 90) * (rng.random() * 2 - 1)
    return buf


def track_file(track: str) -> Path | None:
    """The audio for ``track`` (a built-in one is generated once and kept), or None for no music."""
    if track == "none":
        return None
    if track == "custom":
        return custom_track()
    path = music_dir() / f"cloudtrio-{track}.wav"
    if not path.exists():
        buf = _compose(track)
        peak = max(1e-9, max(abs(x) for x in buf))
        with wave.open(str(path), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(RATE)
            out.writeframes(b"".join(struct.pack("<h", int(32000 * 0.85 * x / peak)) for x in buf))
        logger.info("motion.track_made track=%s", track)
    return path


# --------------------------------------------------------------------------- pictures


def _ease(t: float) -> float:
    """Ease out (cubic), 0..1."""
    t = min(1.0, max(0.0, t))
    return 1 - (1 - t) ** 3


def _between(t: float, start: float, end: float) -> float:
    return _ease((t - start) / (end - start)) if end > start else 1.0


def _background(img: Image.Image) -> Image.Image:
    bg = img.convert("RGB").resize((W, int(img.height * W / img.width)))
    bg = bg.resize((int(bg.width * H / bg.height * 1.15), int(H * 1.15))) if bg.height < H * 1.15 else bg
    left, top = (bg.width - W) // 2, (bg.height - H) // 2
    bg = bg.crop((left, top, left + W, top + H)).filter(ImageFilter.GaussianBlur(38))
    return Image.blend(bg, Image.new("RGB", (W, H), (6, 14, 30)), 0.62)


def number_in(text: str) -> tuple[str, float, str] | None:
    """The number worth counting up in a headline: ("Save ", 40, "%") style; prefers a %, x or + figure."""
    found = list(re.finditer(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s?(%|x\b|\+)?", text))
    if not found:
        return None
    best = next((m for m in found if m.group(2)), found[0])
    value = float(best.group(1).replace(",", ""))
    if value <= 1 or (not best.group(2) and 1900 <= value <= 2100):  # a year is not a result
        return None
    return best.group(1), value, best.group(2) or ""


@lru_cache(maxsize=8)
def _lockup(height: int) -> Image.Image:
    return brand.lockup(height).convert("RGBA")


def _faded(img: Image.Image, alpha: float) -> Image.Image:
    if alpha >= 1:
        return img
    out = img.copy()
    out.putalpha(out.getchannel("A").point(lambda a: int(a * alpha)))
    return out


def _text_layer(text: str, size: int, *, bold: bool = True, color: tuple[int, int, int] = (242, 246, 255), width: int = W - 160) -> Image.Image:
    font = card._font(size, bold=bold)
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    lines = card._wrap(probe, text, font, width)
    line_h = int(size * 1.25)
    layer = Image.new("RGBA", (W, line_h * len(lines) + 10), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    for i, line in enumerate(lines):
        draw.text(((W - draw.textlength(line, font=font)) / 2, i * line_h), line, font=font, fill=color + (255,))
    return layer


def frames(image: Path, *, style: str = "slide", seconds: float = 12, headline: str = "", cta: str = ""):  # noqa: ANN201 - a generator of frames
    """Every frame of the Reel, as RGB images."""
    src = Image.open(image).convert("RGBA")
    base_w = W - 120
    card_img = src.resize((base_w, int(src.height * base_w / src.width)), Image.LANCZOS)
    bg = _background(src)
    logo = _lockup(64)
    cta_layer = _text_layer(cta or settings.REEL_CTA, 46)
    card_top = max(200, (H - card_img.height) // 2 - 40)
    figure = number_in(headline) if style == "countup" else None
    if style == "countup" and figure is None:
        style = "slide"
    intro = 2.4 if figure else 0.0
    accent = brand.TEAL
    label = _text_layer(headline, 44, bold=False, color=(200, 214, 238)) if figure else None
    sheen = Image.new("L", (card_img.width, card_img.height), 0)
    ImageDraw.Draw(sheen).polygon([(0, 0), (140, 0), (40, card_img.height), (-100, card_img.height)], fill=70)
    sheen = sheen.filter(ImageFilter.GaussianBlur(30))
    total = int(seconds * FPS)
    for n in range(total):
        t = n / FPS
        frame = bg.copy().convert("RGBA")
        frame.alpha_composite(_faded(logo, _between(t, 0.1, 0.8)), ((W - logo.width) // 2, 110))
        if figure and t < intro + 0.4:  # the number counts up, then gives way to the card
            fade = 1 - _between(t, intro, intro + 0.4)
            value = figure[1] * _ease(t / (intro - 0.4))
            shown = f"{value:,.0f}" if "," in figure[0] or value >= 1000 else (f"{value:.1f}" if "." in figure[0] else f"{value:.0f}")
            big = _text_layer(f"{shown}{figure[2]}", 260, color=accent)
            frame.alpha_composite(_faded(big, fade), (0, H // 2 - 300))
            frame.alpha_composite(_faded(label, fade * _between(t, 0.3, 1.0)), (0, H // 2 + 20))
        ct = t - intro
        if ct >= 0:
            img, x, y = card_img, (W - card_img.width) // 2, card_top
            if style in ("slide", "countup"):
                y += int((1 - _between(ct, 0.0, 1.0)) * 700)
                img = _faded(card_img, _between(ct, 0.0, 0.6))
                grow = 1 + 0.035 * max(0.0, (ct - 1.0) / max(1.0, seconds - intro - 1.0))
            elif style == "zoom":
                grow = 1.18 - 0.18 * _between(ct, 0.0, 1.6)
                img = _faded(card_img, _between(ct, 0.0, 0.6))
            elif style == "drift":
                # slides in from the right with a slight tilt that straightens on landing
                progress = _between(ct, 0.0, 1.0)
                x = int((W + 150) * (1 - progress) + (W - card_img.width) // 2 * progress)
                angle = 6.0 * (1 - _ease(min(1.0, ct / 1.0)))
                if angle > 0.1:
                    img = card_img.rotate(angle, expand=True, resample=Image.BICUBIC)
                    x = x - (img.width - card_img.width) // 2
                    y = card_top - (img.height - card_img.height) // 2
                img = _faded(img, _between(ct, 0.0, 0.5))
                grow = 1 + 0.02 * max(0.0, (ct - 1.0) / max(1.0, seconds - intro - 1.0))
                if grow != 1:
                    scaled = img.resize((int(img.width * grow), int(img.height * grow)), Image.BILINEAR)
                    x, y = x - (scaled.width - img.width) // 2, y - (scaled.height - img.height) // 2
                    img = scaled
                frame.alpha_composite(img, (x, y))
                continue  # skip the generic grow/composite below
            elif style == "split":
                # background tears apart, card is revealed behind it
                grow = 1.0
                if ct < 1.2:
                    split_progress = _ease(ct / 1.2)
                    offset = int(split_progress * 420)
                    split_frame = Image.new("RGB", (W, H), (6, 14, 30))
                    top_half = bg.crop((0, 0, W, H // 2))
                    bot_half = bg.crop((0, H // 2, W, H))
                    split_frame.paste(top_half, (0, -offset))
                    split_frame.paste(bot_half, (0, H // 2 + offset))
                    card_alpha = _between(ct, 0.4, 1.0)
                    split_frame = split_frame.convert("RGBA")
                    split_frame.alpha_composite(_faded(card_img, card_alpha), (x, y))
                    frame.alpha_composite(split_frame, (0, 0))
                else:
                    frame.alpha_composite(card_img, (x, y))
                continue
            elif style == "focus":
                # starts blurred and sharpens into focus
                blur_r = max(0, int(18 * (1 - _between(ct, 0.0, 1.8))))
                scale = 1.08 - 0.08 * _between(ct, 0.0, 1.8)
                blurred = card_img.filter(ImageFilter.GaussianBlur(blur_r)) if blur_r > 0 else card_img
                blurred = _faded(blurred, _between(ct, 0.0, 0.5))
                scaled_w, scaled_h = int(card_img.width * scale), int(card_img.height * scale)
                if abs(scale - 1) > 0.005:
                    blurred = blurred.resize((scaled_w, scaled_h), Image.BILINEAR)
                sx = (W - blurred.width) // 2
                sy = card_top - (blurred.height - card_img.height) // 2
                frame.alpha_composite(blurred, (sx, sy))
                continue
            elif style == "float":
                # card bobs gently, background drifts in opposite direction
                bob_y = int(14 * math.sin(ct * math.pi * 0.55))
                frame.alpha_composite(card_img, (x, y + bob_y))
                continue
            else:  # reveal
                grow = 1.0
                shown_h = int(card_img.height * _between(ct, 0.2, 1.8))
                img = Image.new("RGBA", card_img.size, (0, 0, 0, 0))
                if shown_h:
                    img.paste(card_img.crop((0, 0, card_img.width, shown_h)), (0, 0))
                if 2.2 <= ct <= 3.2:
                    shift = int((ct - 2.2) * (card_img.width + 300)) - 150
                    band = Image.new("L", card_img.size, 0)
                    band.paste(sheen, (shift, 0))
                    img = img.copy()
                    img.alpha_composite(Image.merge("RGBA", (*[Image.new("L", card_img.size, 255)] * 3, band)))
            if grow != 1:
                img = img.resize((int(card_img.width * grow), int(card_img.height * grow)), Image.BILINEAR)
                x, y = (W - img.width) // 2, y - (img.height - card_img.height) // 2
            frame.alpha_composite(img, (x, y))
        end = seconds - 3.0
        if t >= end:
            rise = _between(t, end, end + 0.8)
            frame.alpha_composite(_faded(cta_layer, rise), (0, H - 300 + int((1 - rise) * 40)))
        yield frame.convert("RGB")


def encode(frames, out: Path, *, seconds: float, track: str, cover_at: float | None = None) -> tuple[Path, Path]:  # noqa: ANN001
    """Frames (RGB images at FPS) and the track into an Instagram-ready MP4. Returns (video, cover); the cover is
    the frame at ``cover_at`` seconds (the last frame when None). Raises MotionError when ffmpeg is missing or fails."""
    exe = ffmpeg()
    if not exe:
        raise MotionError("Animated posts need ffmpeg on the server")
    out.parent.mkdir(parents=True, exist_ok=True)
    cover = out.with_name(out.stem + "-cover.jpg")
    music = track_file(track if track in TRACK_IDS else "upbeat")
    cmd = [exe, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-"]
    if music:
        cmd += ["-stream_loop", "-1", "-i", str(music)]
    cmd += ["-map", "0:v"] + (["-map", "1:a", "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
                               "-af", f"volume=0.9,afade=t=in:d=0.3,afade=t=out:st={seconds - 1.5:.2f}:d=1.5"] if music else [])  # fmt: skip
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-t", f"{seconds:.2f}", str(out)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)  # noqa: S603 - our own command line
    keep, last = int(cover_at * FPS) if cover_at is not None else None, None
    try:
        for n, frame in enumerate(frames):
            proc.stdin.write(frame.tobytes())  # type: ignore[union-attr]
            if keep is None or n == keep:
                last = frame
    except BrokenPipeError:
        pass
    finally:
        if proc.stdin:
            proc.stdin.close()
    err = (proc.stderr.read() if proc.stderr else b"").decode(errors="replace")
    if proc.wait(timeout=300) != 0 or not out.exists():
        raise MotionError(f"ffmpeg could not make the Reel: {err[-300:]}")
    if last is not None:
        last.save(cover, quality=92)
    return out, cover


def render(image: Path, out: Path, *, style: str = "slide", track: str = "upbeat", seconds: float | None = None,
           headline: str = "", cta: str = "") -> tuple[Path, Path]:  # fmt: skip
    """The simple Reel: the finished picture moving. Returns (video, cover)."""
    style = style if style in SIMPLE_IDS else "slide"
    seconds = float(seconds or settings.REEL_SECONDS)
    result = encode(frames(image, style=style, seconds=seconds, headline=headline, cta=cta), out, seconds=seconds, track=track)
    logger.info("motion.rendered file=%s style=%s track=%s", out.name, style, track)
    return result


def render_flyer(flyer: Any, out: Path, *, preset: str = "auto", track: str = "upbeat", highlight: list[str] | None = None,
                 number: str = "", badge: str = "") -> tuple[Path, Path, str]:  # fmt: skip
    """The animated marketing flyer (see :mod:`technews.kinetic`): its words move, cut on the beat.
    Returns (video, cover, the preset used)."""
    from technews import instagram, kinetic

    preset = preset if preset in kinetic.PRESET_IDS else kinetic.auto_preset(flyer)
    film = kinetic.Film(flyer, preset=preset, bpm=kinetic.TRACK_BPM.get(track, 120), handle=instagram.session_user() or settings.IG_HANDLE,
                        highlight=highlight, number=number, badge=badge)  # fmt: skip
    video, cover = encode(film.frames(), out, seconds=film.length, track=track, cover_at=film.cover_at)
    logger.info("motion.rendered_flyer file=%s preset=%s track=%s seconds=%.1f", out.name, preset, track, film.length)
    return video, cover, preset
