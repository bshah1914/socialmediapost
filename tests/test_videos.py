"""A business's own recorded video becomes a Reel (or a photo post) waiting for approval."""

import io
import json
import subprocess

import pytest

from conftest import client_as, make_brand
from technews import brands, db, motion, pipeline, videos
from technews.config import settings

pytestmark = pytest.mark.skipif(not motion.available(), reason="needs ffmpeg")


def _clip(path, size, *, audio=True, seconds=3):
    cmd = [motion.ffmpeg(), "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc=size={size}:rate=30"]
    if audio:
        cmd += ["-f", "lavfi", "-i", "sine=frequency=440"]
    cmd += ["-t", str(seconds), "-c:v", "libx264", "-pix_fmt", "yuv420p"] + (["-c:a", "aac"] if audio else []) + [str(path)]
    subprocess.run(cmd, check=True, capture_output=True)
    return path


@pytest.fixture(scope="session")
def clips(tmp_path_factory):
    d = tmp_path_factory.mktemp("clips")
    return {"landscape": _clip(d / "land.mp4", "640x360"), "silent": _clip(d / "silent.mp4", "640x360", audio=False),
            "portrait": _clip(d / "port.mp4", "360x640")}


@pytest.fixture
def clip_landscape(clips):
    return clips["landscape"]


@pytest.fixture
def clip_silent(clips):
    return clips["silent"]


@pytest.fixture
def clip_portrait(clips):
    return clips["portrait"]


def _post_of(v):
    return db.rows("SELECT post_id FROM videos WHERE id = ?", v["id"])[0]["post_id"]


def _reel_file(v):
    post = db.rows("SELECT * FROM posts WHERE id = ?", _post_of(v))[0]
    with brands.use(post["brand_id"]):
        return settings.CARDS_DIR / json.loads(post["meta"])["reel"]["video"]


# --------------------------------------------------------------------------- reading and checking an upload


def test_probe_reads_landscape_clip(clip_landscape):
    info = videos.probe(clip_landscape)
    assert info["width"] == 640 and info["height"] == 360 and 2.9 < info["seconds"] < 3.1 and info["has_audio"]


def test_probe_sees_no_audio(clip_silent):
    assert videos.probe(clip_silent)["has_audio"] is False


def test_renamed_text_file_rejected_and_removed():
    with pytest.raises(videos.VideoError, match="not a video"):
        videos.save_upload(io.BytesIO(b"hello, this is text"), "fake.mp4")
    assert not any(videos.folder().iterdir())
    assert db.rows("SELECT id FROM videos") == []


def test_wrong_suffix_and_too_big(monkeypatch):
    with pytest.raises(videos.VideoError, match="MP4 or MOV"):
        videos.save_upload(io.BytesIO(b"x"), "clip.avi")
    monkeypatch.setattr(videos, "MAX_BYTES", 10)
    with pytest.raises(videos.VideoError, match="over 300 MB"):
        videos.save_upload(io.BytesIO(b"x" * 11), "clip.mp4")
    assert not any(videos.folder().iterdir())


def test_upload_saved_in_business_folder(clip_portrait):
    bid = make_brand("Bakery")
    with brands.use(bid):
        v = videos.save_upload(clip_portrait.open("rb"), "my clip.MOV")
    assert v["brand_id"] == bid and v["status"] == "uploaded" and v["original_name"] == "my clip.MOV"
    assert (brands.folder(bid) / "videos" / v["stored"]).exists()


def test_over_three_minutes_needs_trim():
    with pytest.raises(videos.VideoError, match="3 minutes"):
        videos.Options(start=0, end=200).check({"seconds": 240})
    with pytest.raises(videos.VideoError, match="3 minutes"):
        videos.Options().check({"seconds": 240})  # no trim: the whole video is too long
    assert videos.Options(start=30, end=200).check({"seconds": 240}).end == 200


def test_trim_clamped_to_the_video():
    o = videos.Options(start=-5, end=99).check({"seconds": 10})
    assert o.start == 0 and o.end == 10
    with pytest.raises(videos.VideoError, match="end after it starts"):
        videos.Options(start=8, end=3).check({"seconds": 10})


# --------------------------------------------------------------------------- the ffmpeg command


def _cmd(clip, tmp_path, info, **opts):
    return " ".join(videos.build_command(clip, tmp_path / "o.mp4", videos.Options(**opts), info, logo=None, title_png=None, music=None))


def test_landscape_gets_blurred_fill(clip_landscape, tmp_path):
    cmd = _cmd(clip_landscape, tmp_path, {"width": 640, "height": 360, "has_audio": True, "seconds": 3})
    assert "boxblur" in cmd and "1080" in cmd and "1920" in cmd and "-r 30" in cmd and "+faststart" in cmd and "libx264" in cmd and "yuv420p" in cmd


def test_portrait_is_cropped_not_blurred(clip_portrait, tmp_path):
    cmd = _cmd(clip_portrait, tmp_path, {"width": 360, "height": 640, "has_audio": True, "seconds": 3})
    assert "boxblur" not in cmd and "crop=1080:1920" in cmd


def test_trim_is_in_the_command(clip_portrait, tmp_path):
    cmd = _cmd(clip_portrait, tmp_path, {"width": 360, "height": 640, "has_audio": True, "seconds": 3}, start=0.5, end=2.5)
    assert "-ss 0.5" in cmd and "-to 2.5" in cmd


# --------------------------------------------------------------------------- the background job


def test_no_audio_gets_silent_track(clip_silent):
    v = videos.save_upload(clip_silent.open("rb"), "silent.mp4")
    videos.queue(v["id"], videos.Options(logo=False, title=False))
    assert videos.run_next()
    out = videos.probe(_reel_file(v))
    assert out["width"] == 1080 and out["height"] == 1920 and out["has_audio"]


def test_job_runs_as_video_brand(clip_portrait):
    bid = make_brand("Bakery", kinds="videos")
    with brands.use(bid):
        v = videos.save_upload(clip_portrait.open("rb"), "p.mp4")
        videos.queue(v["id"], videos.Options(logo=False, title=False))
    assert videos.run_next()  # called outside any business, like the scheduler
    post = db.rows("SELECT * FROM posts WHERE id = ?", _post_of(v))[0]
    assert post["brand_id"] == bid and post["status"] == "pending"
    assert list((brands.folder(bid) / "cards").glob("video-*.mp4"))
    assert db.rows("SELECT status FROM videos WHERE id = ?", v["id"])[0]["status"] == "done"


def test_logo_title_and_music_render(clip_landscape):
    from PIL import Image

    bid = make_brand("Bakery", kinds="videos", colors={"bg": "#112233", "accent": "#ffaa00"})
    Image.new("RGBA", (64, 64), "#ffaa00").save(brands.logo_path(bid), "PNG")
    with brands.use(bid):
        v = videos.save_upload(clip_landscape.open("rb"), "office.mp4")
        videos.queue(v["id"], videos.Options(title_text="Our new office", sound="both", track="calm", end=2))
    assert videos.run_next()
    out = videos.probe(_reel_file(v))
    assert out["width"] == 1080 and 1.8 < out["seconds"] < 2.3 and out["has_audio"]


def test_nothing_queued_returns_false():
    assert videos.run_next() is False


def test_stuck_job_requeued_then_failed(clip_portrait, monkeypatch):
    v = videos.save_upload(clip_portrait.open("rb"), "p.mp4")
    videos.queue(v["id"], videos.Options())
    db.execute("UPDATE videos SET status = 'working', attempts = 1, started_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", v["id"])
    monkeypatch.setattr(videos, "build_command", lambda *a, **k: [motion.ffmpeg(), "-badflag"])
    videos.run_next()
    row = db.rows("SELECT status, attempts, error FROM videos WHERE id = ?", v["id"])[0]
    assert row["status"] == "failed" and row["attempts"] == 2 and row["error"]
    assert _post_of(v) == 0


def test_failed_first_attempt_is_retried(clip_portrait, monkeypatch):
    v = videos.save_upload(clip_portrait.open("rb"), "p.mp4")
    videos.queue(v["id"], videos.Options(logo=False, title=False))
    real = videos.build_command
    monkeypatch.setattr(videos, "build_command", lambda *a, **k: [motion.ffmpeg(), "-badflag"])
    videos.run_next()
    assert db.rows("SELECT status, attempts FROM videos WHERE id = ?", v["id"])[0] == {"status": "queued", "attempts": 1}
    monkeypatch.setattr(videos, "build_command", real)
    assert videos.run_next()
    assert db.rows("SELECT status FROM videos WHERE id = ?", v["id"])[0]["status"] == "done"


def test_cleanup_removes_old_originals_only(clip_portrait):
    old = videos.save_upload(clip_portrait.open("rb"), "old.mp4")
    new = videos.save_upload(clip_portrait.open("rb"), "new.mp4")
    db.execute("UPDATE videos SET posted_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", old["id"])
    assert videos.cleanup(30) == 1
    assert not (videos.folder() / old["stored"]).exists() and (videos.folder() / new["stored"]).exists()


def test_cover_frames_inside_the_trim(clip_portrait):
    v = videos.save_upload(clip_portrait.open("rb"), "p.mp4")
    db.execute("UPDATE videos SET options = ? WHERE id = ?", json.dumps({"start": 1.0, "end": 2.0}), v["id"])
    times = videos.frames(v["id"], 4)
    assert len(times) == 4 and all(1.0 <= t <= 2.0 for t in times)


def test_scheduler_makes_every_queued_video(clip_portrait):
    from technews import scheduler

    for name in ("a.mp4", "b.mp4"):
        v = videos.save_upload(clip_portrait.open("rb"), name)
        videos.queue(v["id"], videos.Options(logo=False, title=False))
    assert scheduler.videos_tick() == 2
    assert {r["status"] for r in db.rows("SELECT status FROM videos")} == {"done"}
