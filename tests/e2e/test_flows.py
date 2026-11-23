"""Every new feature, used the way a person would, in a real browser."""

import json
import re

import pytest
from PIL import Image

from e2e.helpers import db, make_clip

pytestmark = pytest.mark.e2e


def _api(page, method, url, body=None):
    r = page.request.fetch(url, method=method, data=json.dumps(body) if body is not None else None,
                           headers={"Content-Type": "application/json"} if body is not None else {})  # fmt: skip
    return r.status, (r.json() if r.headers.get("content-type", "").startswith("application/json") else None)


def _business(page, name, **fields):
    status, out = _api(page, "POST", "/api/brands", {"name": name, **fields})
    assert status == 200, out
    return out["id"]


def test_admin_onboards_business(sign_in, tmp_path):
    page = sign_in()
    page.goto("/#settings")
    card = page.locator("#onboard")
    card.get_by_label("Business name").fill("Bakery Pune")
    card.get_by_label("What it does, who it is for, how it talks").fill("A family bakery in Pune since 1998. Sourdough and cakes to order.")
    card.get_by_role("button", name="Save and continue").click()
    page.wait_for_selector('[data-obpane="2"]:not([hidden])')
    card.get_by_role("button", name="Save and continue").click()
    page.wait_for_selector('[data-obpane="3"]:not([hidden])')
    logo = tmp_path / "logo.png"
    Image.new("RGBA", (64, 64), "#f5a524").save(logo)
    card.get_by_label("Logo (PNG, up to 2 MB)").set_input_files(str(logo))
    card.get_by_role("button", name="Save and continue").click()
    page.wait_for_selector('[data-obpane="4"]:not([hidden])')
    card.get_by_label("Trial run: make the posts but never put them on Instagram").check()
    card.get_by_role("button", name="Save and continue").click()
    page.wait_for_selector('[data-obpane="5"]:not([hidden])')
    card.get_by_role("button", name="Save and continue").click()
    page.wait_for_selector('[data-obpane="6"]:not([hidden])')
    card.get_by_label("Hashtags (up to 5 are used)").fill("#bakery #pune #sourdough")
    card.get_by_role("button", name="Save and continue").click()
    page.wait_for_selector('[data-obpane="7"]:not([hidden])')
    checks = page.locator("#ob-checks")
    checks.get_by_text("done").first.wait_for()
    assert "needed" not in checks.inner_text()
    card.get_by_role("button", name="Switch on").click()
    page.get_by_text("Switched on.").wait_for()
    page.wait_for_load_state("load")
    page.goto("/#settings")
    assert "Bakery Pune" in page.locator("#brand-sw").inner_text()
    page.locator("#biz-health tbody").get_by_text("Bakery Pune").wait_for()
    row = page.locator("#biz-health tbody tr", has_text="Bakery Pune")
    assert "Trial run" in row.inner_text() and "Templates" in row.inner_text()


def test_owner_sees_only_own_business(sign_in, server):
    admin = sign_in()
    bid = _business(admin, "Salon Owner Test", about="A hair salon in Surat that opens at nine", hashtags="#salon", kinds="flyers,ideas,videos")
    status, _ = _api(admin, "POST", "/api/users", {"username": "salonowner", "password": "Owner@12345", "role": "owner", "brand_id": bid})
    assert status == 200
    _, flyers = _api(admin, "GET", "/api/flyers")
    flyer_id = (flyers["flyers"] if isinstance(flyers, dict) else flyers)[0]["id"]
    status, queued = _api(admin, "POST", f"/api/flyers/{flyer_id}/queue", {})
    assert status == 200, queued
    pending = db(server).execute("SELECT id, headline FROM posts WHERE status = 'pending' AND brand_id = 1 ORDER BY id DESC").fetchone()
    owner = sign_in("salonowner", "Owner@12345")
    body = owner.content()
    assert owner.locator("#brand-sw").count() == 0
    assert owner.locator('#nav a[href="#settings"]').count() == 0 and owner.locator('#nav a[href="#sources"]').count() == 0
    assert pending[1] not in body and "Salon Owner Test" in body
    assert _api(owner, "POST", f"/api/posts/{pending[0]}/approve", {})[0] == 404
    assert _api(owner, "GET", "/api/settings")[0] == 403
    assert _api(owner, "POST", "/api/brand", {"id": 1})[0] == 403


def test_idea_to_reel(sign_in):
    page = sign_in()
    page.goto("/#create")
    page.get_by_label("Your idea or notes").fill("We now offer a free cloud cost review for startups. We find idle servers and oversized databases.")
    page.get_by_label("Reel (animated, with music)").check()
    page.get_by_label("Animation").select_option(label="Simple: slide in")
    page.get_by_role("button", name="Create 3 options").click()
    send = page.get_by_role("button", name="Send to Approvals").first
    send.wait_for(timeout=60_000)
    send.click()
    page.wait_for_url(re.compile(r".*#reels$"), timeout=120_000)
    page.wait_for_load_state("load")
    page.locator("#reels video").first.wait_for(timeout=30_000)


def test_video_upload_to_reel(sign_in, server, tmp_path):
    page = sign_in()
    page.goto("/#videos")
    page.get_by_label("Video file").set_input_files(str(make_clip(tmp_path / "office.mp4")))
    page.locator("#vd-edit").wait_for(state="visible", timeout=60_000)
    page.get_by_label("End (seconds)").fill("2")
    page.get_by_label("End (seconds)").dispatch_event("change")
    page.get_by_label("About this video (the caption is written from this line only)").fill("Our new office in Pune is open")
    page.get_by_label("Title", exact=True).fill("New office")
    page.locator("#vd-covers button").nth(2).click()
    page.get_by_role("button", name="Make Reel").click()
    page.get_by_text("Done. The Reel is waiting in Reels.").first.wait_for(timeout=120_000)
    post_id, meta, caption = db(server).execute("SELECT id, meta, caption FROM posts WHERE writer = 'video' ORDER BY id DESC").fetchone()
    assert json.loads(meta)["reel"]["video"].startswith("video-") and "Our new office in Pune is open" in caption
    page.goto("/#reels")
    page.reload()
    page.locator("#reels video").first.wait_for(timeout=30_000)
    status, out = _api(page, "POST", f"/api/posts/{post_id}/approve", {})
    assert status == 200 and out["posted"], out
    assert list((server["data"] / "outbox").glob("*video-*.mp4"))


def test_cloudtrio_still_drafts_news_and_flyers(sign_in, server):
    page = sign_in()
    con = db(server)
    now = "2099-01-01T00:00:00+00:00"
    story_id = con.execute("INSERT INTO stories (key, title, url, source, published_at, found_at, category, status, review_score, summary) "
                           "VALUES ('e2e', 'AWS launches a new region in Spain', 'https://x.test/e2e', 'AWS News Blog', ?, ?, 'cloud', 'approved', 9, "
                           "'AWS opened a new cloud region in Spain with three zones.')", (now, now)).lastrowid  # fmt: skip
    con.commit()
    status, out = _api(page, "POST", f"/api/stories/{story_id}/post", {})
    assert status == 200, out
    _, flyers = _api(page, "GET", "/api/flyers")
    flyer = (flyers["flyers"] if isinstance(flyers, dict) else flyers)[1]
    assert _api(page, "POST", f"/api/flyers/{flyer['id']}/queue", {})[0] == 200
    page.goto("/#approvals")
    page.reload()
    approvals = page.locator("#approvals")
    approvals.get_by_text("AWS launches a new region in Spain").first.wait_for()
    approvals.get_by_text(flyer["headline"]).first.wait_for()


@pytest.mark.parametrize("view", ["create", "videos", "approvals", "settings"])
def test_phone_width(sign_in, view):
    page = sign_in(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
    page.goto(f"/#{view}")
    page.wait_for_timeout(300)
    width = page.evaluate("document.documentElement.scrollWidth")
    assert width <= 390, f"{view} scrolls sideways on a phone: {width}px"
