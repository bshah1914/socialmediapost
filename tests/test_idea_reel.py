"""An idea goes straight to the Reels queue: one step, one alert."""

from types import SimpleNamespace

import pytest

from conftest import client_as
from technews import db, notify, pipeline
from technews.agents import creator


@pytest.fixture
def calls(monkeypatch):
    rec = SimpleNamespace(make_reel=[], draft_ready=[])

    def make_reel(post_id, *, style="", track="", **kw):
        rec.make_reel.append((post_id, style, track))
        return {"ok": True, "post_id": post_id, "video": f"reel-{post_id}.mp4", "cover": f"reel-{post_id}-cover.jpg", "style": style}

    monkeypatch.setattr(pipeline, "make_reel", make_reel)
    monkeypatch.setattr(notify, "draft_ready", lambda headline, kind, image, *, wait=False, post_id=0: rec.draft_ready.append(SimpleNamespace(kind=kind, image=image, post_id=post_id)))
    return rec


@pytest.fixture
def calls_failing(calls, monkeypatch):
    monkeypatch.setattr(pipeline, "make_reel", lambda post_id, **kw: calls.make_reel.append((post_id, kw.get("style", ""), kw.get("track", ""))) or {"ok": False, "reason": "Animated posts need ffmpeg on the server"})
    return calls


def _idea():
    return creator.generate("We now offer a free cloud cost review for startups. We find idle servers and oversized databases.")


def test_idea_sent_as_reel_alerts_once(calls):
    idea = _idea()
    out = creator.send(idea["idea_id"], idea["variants"][0], as_reel=True, style="slide", track="calm")
    assert out["reel"] is True and out["reason"] == ""
    assert calls.make_reel == [(out["post_id"], "slide", "calm")]
    assert [c.kind for c in calls.draft_ready] == ["animated flyer"]
    assert calls.draft_ready[0].post_id == out["post_id"] and calls.draft_ready[0].image.name == f"reel-{out['post_id']}.mp4"


def test_reel_failure_keeps_flyer(calls_failing):
    idea = _idea()
    out = creator.send(idea["idea_id"], idea["variants"][0], as_reel=True)
    assert out["reel"] is False and "ffmpeg" in out["reason"]
    assert [c.kind for c in calls_failing.draft_ready] == ["flyer"]
    assert db.rows("SELECT status FROM posts WHERE id = ?", out["post_id"])[0]["status"] == "pending"


def test_reel_ignores_weekly_limit(calls, monkeypatch):
    monkeypatch.setattr(pipeline, "reel_wanted", lambda: False)
    idea = _idea()
    assert creator.send(idea["idea_id"], idea["variants"][0], as_reel=True)["reel"] is True


def test_flyer_choice_unchanged(calls):
    idea = _idea()
    out = creator.send(idea["idea_id"], idea["variants"][0])
    assert out["reel"] is False and calls.make_reel == []
    assert [c.kind for c in calls.draft_ready] == ["flyer"]


def test_create_send_api_takes_reel_choice(calls):
    c = client_as("boss", "Admin@1234")
    idea = c.post("/api/create/generate", json={"idea": "Free cloud cost review for startups, we find idle servers."}).get_json()
    out = c.post("/api/create/send", json={"idea_id": idea["idea_id"], "variant": idea["variants"][0], "as": "reel", "style": "zoom", "track": "upbeat"}).get_json()
    assert out["reel"] is True and calls.make_reel == [(out["post_id"], "zoom", "upbeat")]


def test_real_reel_for_a_new_business_lands_in_its_folder(monkeypatch):
    from conftest import make_brand
    from technews import brands, motion
    from technews.config import settings

    if not motion.available():
        pytest.skip("needs ffmpeg")
    monkeypatch.setattr(settings, "REEL_SECONDS", 3)
    bid = make_brand("Bakery", about="Family bakery in Pune since 1998")
    with brands.use(bid):
        idea = creator.generate("Sourdough every Sunday, baked at 6am.")
        out = creator.send(idea["idea_id"], idea["variants"][0], as_reel=True, style="slide", track="none")
    assert out["reel"] is True, out["reason"]
    post = db.rows("SELECT * FROM posts WHERE id = ?", out["post_id"])[0]
    assert post["brand_id"] == bid
    assert (brands.folder(bid) / "cards" / pipeline.reel_of(post)["video"]).exists()
