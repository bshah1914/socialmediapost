"""From the upload screen to Approvals: upload, make, caption, cover, approve."""

import io
import json

import pytest
from PIL import Image

from conftest import client_as, make_brand
from technews import brands, db, motion, pipeline, videos
from technews.agents import llm, writer
from technews.config import settings

pytestmark = pytest.mark.skipif(not motion.available(), reason="needs ffmpeg")


def _upload(c, clip, name="office.mp4"):
    r = c.post("/api/videos", data={"file": (clip.open("rb"), name)}, content_type="multipart/form-data")
    assert r.status_code == 200, r.get_json()
    return r.get_json()["video"]["id"]


def _post_of(vid):
    return db.rows("SELECT * FROM posts WHERE id = (SELECT post_id FROM videos WHERE id = ?)", vid)[0]


def test_upload_to_reel_end_to_end(clip_landscape):
    c = client_as("boss", "Admin@1234")
    vid = _upload(c, clip_landscape)
    r = c.post(f"/api/videos/{vid}/make", json={"about": "Our new Pune office", "logo": False, "title": False})
    assert r.status_code == 200 and r.get_json()["video"]["status"] == "queued"
    assert videos.run_next()
    post = _post_of(vid)
    assert post["status"] == "pending" and pipeline.reel_of(post)["video"].startswith("video-")
    assert "Our new Pune office" in post["caption"] and post["caption"].count("#") <= 5
    row = c.get(f"/api/videos/{vid}").get_json()["video"]
    assert row["status"] == "done" and row["post_id"] == post["id"]


def test_make_post_from_video(clip_portrait):
    c = client_as("boss", "Admin@1234")
    vid = _upload(c, clip_portrait)
    c.post(f"/api/videos/{vid}/make", json={"about": "Fresh bread", "as_post": True, "cover_at": 1.5})
    assert videos.run_next()
    post = _post_of(vid)
    assert "reel" not in json.loads(post["meta"]) and json.loads(post["meta"])["video_id"] == vid
    assert Image.open(settings.CARDS_DIR / post["image_path"].replace("\\", "/").split("/")[-1]).size == (1080, 1350)


def test_videos_off_for_business(clip_portrait):
    bid = make_brand("Bakery", kinds="flyers")
    c = client_as("baker", "Pass@1234", "owner", bid)
    r = c.post("/api/videos", data={"file": (clip_portrait.open("rb"), "a.mp4")}, content_type="multipart/form-data")
    assert r.status_code == 403 and "switched off" in r.get_json()["error"]


def test_bad_upload_says_why():
    c = client_as("boss", "Admin@1234")
    r = c.post("/api/videos", data={"file": (io.BytesIO(b"not a video"), "a.mp4")}, content_type="multipart/form-data")
    assert r.status_code == 400 and "not a video" in r.get_json()["error"]
    assert c.post("/api/videos", data={}, content_type="multipart/form-data").status_code == 400


def test_too_big_upload_says_why(clip_portrait):
    c = client_as("boss", "Admin@1234")
    c.application.config["MAX_CONTENT_LENGTH"] = 1000
    r = c.post("/api/videos", data={"file": (clip_portrait.open("rb"), "a.mp4")}, content_type="multipart/form-data")
    assert r.status_code == 413 and "300 MB" in r.get_json()["error"]


def test_approve_marks_video_posted(clip_portrait):
    c = client_as("boss", "Admin@1234")
    vid = _upload(c, clip_portrait)
    c.post(f"/api/videos/{vid}/make", json={"about": "Fresh bread", "logo": False, "title": False})
    videos.run_next()
    post = _post_of(vid)
    assert c.post(f"/api/posts/{post['id']}/approve").get_json()["posted"]
    assert db.rows("SELECT posted_at FROM videos WHERE id = ?", vid)[0]["posted_at"]
    assert list(settings.OUTBOX_DIR.glob("*video-*.mp4"))


def test_owner_cannot_reach_another_business_video(clip_portrait):
    c = client_as("boss", "Admin@1234")
    vid = _upload(c, clip_portrait)  # CloudTrio's
    owner = client_as("baker", "Pass@1234", "owner", make_brand("Bakery"))
    for url in (f"/api/videos/{vid}", f"/api/videos/{vid}/source", f"/api/videos/{vid}/frame.jpg?at=1"):
        assert owner.get(url).status_code == 404, url
    assert owner.post(f"/api/videos/{vid}/make", json={}).status_code == 404
    assert owner.get("/api/videos").get_json()["videos"] == []


def test_source_and_frame_for_the_trim_player(clip_portrait):
    c = client_as("boss", "Admin@1234")
    vid = _upload(c, clip_portrait)
    src = c.get(f"/api/videos/{vid}/source", headers={"Range": "bytes=0-99"})
    assert src.status_code == 206 and len(src.data) == 100
    frame = c.get(f"/api/videos/{vid}/frame.jpg?at=1")
    assert frame.status_code == 200 and frame.mimetype == "image/jpeg"


def test_pick_another_cover(clip_portrait):
    c = client_as("boss", "Admin@1234")
    vid = _upload(c, clip_portrait)
    c.post(f"/api/videos/{vid}/make", json={"logo": False, "title": False})
    videos.run_next()
    before = pipeline.reel_of(_post_of(vid))["cover"]
    r = c.post(f"/api/videos/{vid}/cover", json={"at": 0.4})
    assert r.status_code == 200
    after = pipeline.reel_of(_post_of(vid))["cover"]
    assert after != before and (settings.CARDS_DIR / after).exists()


def test_trim_too_long_refused_at_make(clip_portrait, monkeypatch):
    c = client_as("boss", "Admin@1234")
    vid = _upload(c, clip_portrait)
    db.execute("UPDATE videos SET seconds = 400 WHERE id = ?", vid)
    r = c.post(f"/api/videos/{vid}/make", json={})
    assert r.status_code == 400 and "3 minutes" in r.get_json()["error"]


def test_video_caption_without_ai_uses_only_given_words():
    bid = make_brand("Bakery", about="Family bakery in Pune since 1998", hashtags="#bread #pune #bakery #sourdough #cake #extra", email="hi@bakery.test")
    with brands.use(bid):
        text = writer.video_caption("Our sourdough comes out at 6am", "Sunday bake")
    assert text.startswith("Our sourdough comes out at 6am") and "hi@bakery.test" in text
    assert text.count("#") == 5


def test_video_caption_with_ai_drops_its_hashtags(monkeypatch):
    bid = make_brand("Bakery", about="Family bakery in Pune since 1998", hashtags="#bread", nvidia_key="nvapi-b")
    seen = []

    def fake(*, system, user, **kw):
        seen.append(user)
        return llm.ChatResult({"caption": "Warm bread at 6am. #yum #wow\n\nCome by."}, "m", 1)

    monkeypatch.setattr(llm, "chat_json", fake)
    with brands.use(bid):
        text = writer.video_caption("Our sourdough comes out at 6am", "")
    assert "#yum" not in text and text.startswith("Warm bread at 6am.") and text.rstrip().endswith("#bread")
    assert "Family bakery in Pune since 1998" in seen[0] and "Our sourdough comes out at 6am" in seen[0]
