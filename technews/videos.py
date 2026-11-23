"""A business's own recorded video, made ready for Instagram: upload, check, trim, fill to 1080x1920, logo and title,
sound, then a Reel (or a photo post) waiting for approval.

Processing runs as a job in the background, one at a time, stored in the database so a restart loses nothing: a job
left "working" for longer than ``STALE_MINUTES`` goes back to the queue, and after ``MAX_ATTEMPTS`` it is marked failed
with the reason. Every job runs as the business the video belongs to."""

from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
import time
import uuid
from dataclasses import asdict, dataclass, fields, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, Any, Final

from technews import brands, db, motion
from technews.config import settings

logger = logging.getLogger(__name__)

MAX_BYTES: Final = 314_572_800  # 300 MB
MAX_SECONDS: Final = 180  # Instagram's Reel limit
SUFFIXES: Final = (".mp4", ".mov")
SOUNDS: Final = ("original", "music", "both")
STALE_MINUTES: Final = 15
MAX_ATTEMPTS: Final = 2
W, H = 1080, 1920
TITLE_SECONDS: Final = 2.5
_lock = threading.Lock()


class VideoError(ValueError):
    """Something about the video that the person can fix; the message says what."""


def folder() -> Path:
    path = brands.folder(settings.BRAND_ID) / "videos"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _num(x: float) -> str:
    return f"{x:g}"


# --------------------------------------------------------------------------- reading a video


def probe(path: Path) -> dict[str, Any]:
    """Length, size (as shown, after any phone rotation) and whether it has sound. Raises VideoError."""
    exe = motion.ffmpeg()
    if not exe:
        raise VideoError("Videos need ffmpeg on the server.")
    out = subprocess.run([exe, "-hide_banner", "-i", str(path)], capture_output=True, text=True, errors="replace", timeout=60).stderr
    size = re.search(r"Stream #[^\n]*Video:[^\n]*?,\s*(\d{2,5})x(\d{2,5})[\s,\[]", out)
    if not size:
        raise VideoError("That file is not a video we can read.")
    found = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", out)
    seconds = int(found[1]) * 3600 + int(found[2]) * 60 + float(found[3]) if found else 0.0
    width, height = int(size[1]), int(size[2])
    if re.search(r"rotat(e\s*:\s*-?|ion of -?)(90|270)", out):  # filmed upright on a phone: ffmpeg turns it when decoding
        width, height = height, width
    return {"seconds": round(seconds, 2), "width": width, "height": height, "has_audio": bool(re.search(r"Stream #[^\n]*Audio:", out))}


def save_upload(stream: IO[bytes], filename: str) -> dict[str, Any]:
    """Keep an uploaded video in the business's folder and note what it is. Nothing is kept when it fails. Raises VideoError."""
    suffix = Path(filename or "").suffix.lower()
    if suffix not in SUFFIXES:
        raise VideoError("Upload an MP4 or MOV video.")
    stored = f"{uuid.uuid4().hex}{suffix}"
    path = folder() / stored
    try:
        size = 0
        with path.open("wb") as out:
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_BYTES:
                    raise VideoError("The video is over 300 MB.")
                out.write(chunk)
        info = probe(path)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    vid = db.execute("INSERT INTO videos (brand_id, at, stored, original_name, seconds, width, height, has_audio, status) VALUES (?,?,?,?,?,?,?,?,?)",
                     settings.BRAND_ID, db.now(), stored, str(filename)[:200], info["seconds"], info["width"], info["height"],
                     int(info["has_audio"]), "uploaded")  # fmt: skip
    return get(vid)


def get(video_id: int) -> dict[str, Any]:
    found = db.rows("SELECT * FROM videos WHERE id = ?", video_id)
    if not found:
        raise VideoError("That video is gone.")
    return found[0]


def listing(limit: int = 30) -> list[dict[str, Any]]:
    return db.rows("SELECT * FROM videos WHERE brand_id = ? ORDER BY id DESC LIMIT ?", settings.BRAND_ID, limit)


# --------------------------------------------------------------------------- what to make of it


@dataclass(frozen=True)
class Options:
    start: float = 0.0
    end: float = 0.0  # 0 = to the end
    sound: str = "original"  # original | music | both
    logo: bool = True
    title: bool = True
    title_text: str = ""
    about: str = ""  # a line about the video, for the caption
    as_post: bool = False  # a photo post from one frame instead of a Reel
    cover_at: float | None = None
    track: str = ""  # music track ("" = the business's usual)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> Options:
        data = data or {}
        names = {f.name for f in fields(cls)}
        clean: dict[str, Any] = {}
        for k, v in data.items():
            if k not in names or v is None:
                continue
            if k in ("start", "end"):
                clean[k] = float(v or 0)
            elif k == "cover_at":
                clean[k] = float(v)
            elif k in ("logo", "title", "as_post"):
                clean[k] = bool(v)
            else:
                clean[k] = str(v)[:300 if k == "about" else 80]
        return cls(**clean)

    def check(self, info: dict[str, Any]) -> Options:
        """These options fitted to the video: the trim kept inside it, at most 3 minutes. Raises VideoError."""
        length = float(info.get("seconds") or 0)
        start = max(0.0, min(self.start, length))
        end = length if self.end <= 0 or self.end > length else self.end
        if end <= start:
            raise VideoError("The video must end after it starts.")
        if end - start > MAX_SECONDS + 0.05:
            raise VideoError("Trim the video to 3 minutes or less.")
        cover = None if self.cover_at is None else min(max(self.cover_at, start), end)
        return replace(self, start=round(start, 2), end=round(end, 2), sound=self.sound if self.sound in SOUNDS else "original", cover_at=cover)


def build_command(src: Path, out: Path, opts: Options, info: dict[str, Any], *, logo: Path | None, title_png: Path | None,
                  music: Path | None) -> list[str]:  # fmt: skip
    """The ffmpeg command that turns the upload into an Instagram Reel (same encode as the animated posts)."""
    duration = (opts.end or float(info.get("seconds") or 0)) - opts.start
    cmd = [motion.ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-ss", _num(opts.start)]
    if opts.end:
        cmd += ["-to", _num(opts.end)]
    cmd += ["-i", str(src)]
    n = 1
    graph = []
    if int(info.get("width") or 0) > int(info.get("height") or 0):  # filmed sideways: a blurred copy fills the frame behind it
        graph.append(f"[0:v]split=2[bg][fg];[bg]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=20:2[bgb];"
                     f"[fg]scale={W}:-2[fgs];[bgb][fgs]overlay=(W-w)/2:(H-h)/2[v0]")  # fmt: skip
    else:
        graph.append(f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}[v0]")
    last = "v0"
    if logo:
        cmd += ["-i", str(logo)]
        graph.append(f"[{n}:v]scale=160:-1[lg];[{last}][lg]overlay=W-w-48:48[v1]")
        last, n = "v1", n + 1
    if title_png:
        cmd += ["-loop", "1", "-t", _num(TITLE_SECONDS), "-i", str(title_png)]
        graph.append(f"[{n}:v]format=rgba[tt];[{last}][tt]overlay=0:0:enable='between(t,0,{_num(TITLE_SECONDS)})':eof_action=pass[v2]")
        last, n = "v2", n + 1
    graph.append(f"[{last}]fps=30,format=yuv420p,setsar=1[vout]")
    original = bool(info.get("has_audio")) and opts.sound in ("original", "both")
    if music and opts.sound in ("music", "both"):
        cmd += ["-stream_loop", "-1", "-i", str(music)]
        if original:
            graph.append(f"[0:a]volume=1.0[a0];[{n}:a]volume=0.25[a1];[a0][a1]amix=inputs=2:duration=first:dropout_transition=0[aout]")
        else:
            graph.append(f"[{n}:a]volume=0.9[aout]")
        n += 1
    elif original:
        graph.append("[0:a]anull[aout]")
    else:  # no sound to keep: a silent track, so every Reel has audio
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
        graph.append(f"[{n}:a]anull[aout]")
        n += 1
    cmd += ["-filter_complex", ";".join(graph), "-map", "[vout]", "-map", "[aout]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-r", "30", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-movflags", "+faststart", "-t", _num(round(duration, 2)), str(out)]  # fmt: skip
    return cmd


def frames(video_id: int, n: int = 4) -> list[float]:
    """Seconds of ``n`` evenly spaced cover choices inside the trim."""
    row = get(video_id)
    opts = Options.from_dict(json.loads(row["options"] or "{}"))
    start, end = opts.start, opts.end if opts.end > 0 else float(row["seconds"])
    return [round(start + (end - start) * (i + 0.5) / n, 2) for i in range(n)]


def frame_jpg(src: Path, at: float, out: Path, size: tuple[int, int] = (W, H)) -> Path:
    """One frame of a video as a JPEG of ``size`` (cropped to fill). Raises VideoError."""
    w, h = size
    out.parent.mkdir(parents=True, exist_ok=True)
    done = subprocess.run([motion.ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-ss", _num(max(0.0, at)), "-i", str(src), "-frames:v", "1",
                           "-vf", f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}", "-q:v", "3", str(out)],
                          capture_output=True, text=True, errors="replace", timeout=120)  # fmt: skip
    if done.returncode != 0 or not out.exists():
        raise VideoError("A picture could not be taken from the video: " + done.stderr.strip()[-200:])
    return out


def _title_png(text: str, out: Path) -> Path:
    """The title shown for the first seconds: a band in the business's colours, on a transparent 1080x1920 frame."""
    from PIL import Image, ImageDraw

    from technews import kinetic

    colours = brands.colors(brands.current())
    band, ink = colours.get("bg", "#0b1f3a"), colours.get("accent", "#ffffff")
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font = kinetic.font(72, bold=True)
    words, lines, line = text.split(), [], ""
    for word in words:
        trial = f"{line} {word}".strip()
        if draw.textlength(trial, font=font) > W - 180 and line:
            lines.append(line)
            line = word
        else:
            line = trial
    lines = (lines + [line])[:3]
    top = H - 560
    height = 60 + 90 * len(lines)
    rgb = tuple(int(band.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4))
    draw.rounded_rectangle((60, top, W - 60, top + height), radius=28, fill=(*rgb, 225))
    for i, text_line in enumerate(lines):
        draw.text((W // 2, top + 30 + 90 * i + 45), text_line, font=font, fill=ink, anchor="mm")
    img.save(out, "PNG")
    return out


def _logo_file() -> Path | None:
    if settings.BRAND_ID == brands.DEFAULT_ID:  # CloudTrio: its built-in mark
        from technews import brand

        path = settings.CARDS_DIR / "video-logo-1.png"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            brand.mark(256).save(path, "PNG")
        return path
    path = brands.logo_path(settings.BRAND_ID)
    return path if path.exists() else None


# --------------------------------------------------------------------------- the jobs


def queue(video_id: int, opts: Options) -> dict[str, Any]:
    """Ask for this video to be made with these options (checked now, made in the background). Raises VideoError."""
    row = get(video_id)
    checked = opts.check({"seconds": row["seconds"]})
    db.execute("UPDATE videos SET status = 'queued', options = ?, attempts = 0, error = '' WHERE id = ?", json.dumps(asdict(checked)), video_id)
    start_worker()
    return get(video_id)


def start_worker() -> None:
    """Start making queued videos now, in the background (the scheduler also looks every minute)."""
    if not settings.VIDEO_WORKER:
        return

    def drain() -> None:
        try:
            while run_next():
                pass
        except Exception:  # noqa: BLE001 - the scheduler tries again in a minute
            logger.exception("videos.worker_failed")

    threading.Thread(target=drain, daemon=True, name="video-worker").start()


def run_next() -> bool:
    """Make the next queued video. Returns False when there was nothing to do."""
    with _lock:
        stale = (datetime.now(UTC) - timedelta(minutes=STALE_MINUTES)).isoformat(timespec="seconds")
        db.execute("UPDATE videos SET status = 'queued' WHERE status = 'working' AND started_at < ?", stale)
        found = db.rows("SELECT * FROM videos WHERE status = 'queued' AND attempts < ? ORDER BY id LIMIT 1", MAX_ATTEMPTS)
        if not found:
            return False
        row = found[0]
        key = f"video-job:{row['id']}"
        if not db.kv_claim(key, STALE_MINUTES * 60):  # another process has it
            return False
        try:
            attempts = int(row["attempts"]) + 1
            db.execute("UPDATE videos SET status = 'working', attempts = ?, started_at = ? WHERE id = ?", attempts, db.now(), row["id"])
            try:
                with brands.use(int(row["brand_id"])):
                    post_id = _make(row)
            except Exception as exc:  # noqa: BLE001 - recorded on the video, shown on the page
                status = "queued" if attempts < MAX_ATTEMPTS else "failed"
                db.execute("UPDATE videos SET status = ?, error = ? WHERE id = ?", status, str(exc)[:500], row["id"])
                logger.warning("videos.job_failed video=%s attempt=%s error=%s", row["id"], attempts, str(exc)[:200])
                return True
            db.execute("UPDATE videos SET status = 'done', error = '', post_id = ? WHERE id = ?", post_id, row["id"])
            return True
        finally:
            db.kv_release(key)


def _make(row: dict[str, Any]) -> int:
    """Render the Reel for one video row (running as its business) and hold it for approval. Returns the post id."""
    from technews import pipeline

    info = {"seconds": row["seconds"], "width": row["width"], "height": row["height"], "has_audio": bool(row["has_audio"])}
    opts = Options.from_dict(json.loads(row["options"] or "{}")).check(info)
    src = folder() / row["stored"]
    if not src.exists():
        raise VideoError("The uploaded video is no longer on the server.")
    settings.CARDS_DIR.mkdir(parents=True, exist_ok=True)
    out = settings.CARDS_DIR / f"video-{row['id']}-{int(time.time() * 1000)}.mp4"
    title = (opts.title_text or opts.about).strip()
    title_png = _title_png(title[:90], out.with_name(out.stem + "-title.png")) if opts.title and title else None
    music = motion.track_file(opts.track or settings.REEL_MUSIC) if opts.sound in ("music", "both") else None
    cmd = build_command(src, out, opts, info, logo=_logo_file() if opts.logo else None, title_png=title_png, music=music)
    done = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=900)
    if title_png:
        title_png.unlink(missing_ok=True)
    if done.returncode != 0 or not out.exists():
        out.unlink(missing_ok=True)
        raise VideoError("The video could not be made: " + (done.stderr.strip()[-300:] or f"ffmpeg exit {done.returncode}"))
    at = (opts.cover_at if opts.cover_at is not None else (opts.start + opts.end) / 2) - opts.start
    cover = frame_jpg(out, at, out.with_name(out.stem + "-cover.jpg"))
    return pipeline.draft_video(int(row["id"]), out, cover)


def cleanup(days: int = 30) -> int:
    """Delete the uploaded originals of videos posted more than ``days`` ago (the Reel itself is kept). Returns how many."""
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
    removed = 0
    for row in db.rows("SELECT id, brand_id, stored FROM videos WHERE posted_at != '' AND posted_at < ? AND stored != ''", cutoff):
        path = brands.folder(int(row["brand_id"])) / "videos" / row["stored"]
        if path.exists():
            path.unlink()
            removed += 1
        db.execute("UPDATE videos SET stored = '' WHERE id = ?", row["id"])
    return removed
