"""Every new feature, used the way a person would, in a real browser."""

import json
import re

import pytest
from PIL import Image

from e2e.helpers import db, make_clip

pytestmark = pytest.mark.e2e


def _api(page, method, url, body=None):
    r = page.request.fetch(url, method=method, data=json.dumps(body) if body is not None else None, timeout=120_000,
                           headers={"Content-Type": "application/json"} if body is not None else {})  # fmt: skip
    return r.status, (r.json() if r.headers.get("content-type", "").startswith("application/json") else None)


def _business(page, name, **fields):
    status, out = _api(page, "POST", "/api/brands", {"name": name, **fields})
    assert status == 200, out
    return out["id"]


def test_admin_onboards_business(sign_in, tmp_path):
    page = sign_in()
    page.goto("/#businesses")
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
    page.locator("#ob-msg").get_by_text("Switched on.").wait_for()
    page.wait_for_load_state("load")
    page.goto("/#businesses")
    assert "Bakery Pune" in page.locator("#brand-sw").inner_text()
    page.locator("#biz-health tbody").get_by_text("Bakery Pune").wait_for()
    row = page.locator("#biz-health tbody tr", has_text="Bakery Pune")
    assert "Trial run" in row.inner_text() and "Templates" in row.inner_text()


def test_owner_sees_only_own_business(sign_in, server):
    admin = sign_in()
    bid = _business(admin, "Salon Owner Test", about="A hair salon in Surat that opens at nine", hashtags="#salon", kinds="flyers,ideas,videos")
    status, _ = _api(admin, "POST", "/api/users", {"username": "salonowner", "password": "Owner@12345", "role": "owner", "brand_id": bid})
    assert status == 200
    status, saved = _api(admin, "POST", "/api/flyers", {"kicker": "PRIVATE", "headline": "Zebra launch 7731 for CloudTrio only", "points": "One",
                                                        "cta": "Ask us", "note": "x", "caption": "CloudTrio only"})  # text found nowhere else
    assert status == 200, saved
    flyer_id = (saved.get("flyer") or saved).get("id") if isinstance(saved, dict) else None
    if not flyer_id:
        _, flyers = _api(admin, "GET", "/api/flyers")
        flyer_id = next(f["id"] for f in (flyers["flyers"] if isinstance(flyers, dict) else flyers) if f["headline"].startswith("Zebra launch"))
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
    page.locator("#create").get_by_label("Animation", exact=True).select_option(label="Simple: slide in")
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
    status, out = _api(page, "POST", f"/api/stories/{story_id}/post", {})  # the editor's "post it now": straight out (dry run: the outbox)
    assert status == 200 and out["posted"], out
    assert list((server["data"] / "outbox").glob(f"*story-{story_id}*.jpg"))
    _, flyers = _api(page, "GET", "/api/flyers")
    flyer = (flyers["flyers"] if isinstance(flyers, dict) else flyers)[1]
    assert _api(page, "POST", f"/api/flyers/{flyer['id']}/queue", {})[0] == 200
    page.goto("/#approvals")
    page.reload()
    page.locator("#approvals").get_by_text(flyer["headline"]).first.wait_for()
    page.goto("/#posts")
    page.locator("#posts").get_by_text("AWS launches a new region in Spain").first.wait_for()


@pytest.mark.parametrize("view", ["create", "videos", "approvals", "settings", "businesses"])
def test_phone_width(sign_in, view):
    page = sign_in(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
    page.goto(f"/#{view}")
    page.wait_for_timeout(300)
    width = page.evaluate("document.documentElement.scrollWidth")
    assert width <= 390, f"{view} scrolls sideways on a phone: {width}px"


def test_admin_edits_switches_off_and_deletes_a_business(sign_in):
    page = sign_in()
    page.on("dialog", lambda d: d.accept())
    bid = _business(page, "Gym Delete Test", about="A gym in Baroda that opens at six", hashtags="#gym", kinds="flyers,ideas", delivery="dry_run")
    page.goto("/#businesses")
    page.reload()  # the list was drawn before the business was added
    card = page.locator(f'.brand-card[data-bid="{bid}"]')
    card.locator("summary").click()
    card.locator("[data-bchecks] li").first.wait_for()  # its readiness shows when it opens
    card.get_by_label("What it does, who it is for, how it talks").fill("A gym in Baroda with morning classes for beginners")
    card.get_by_label("Hashtags (up to 5 are used)").fill("#gym #baroda")
    card.get_by_label("Its own recorded videos").check()
    card.get_by_role("button", name="Save changes").click()
    page.locator(f"#bmsg-{bid}").get_by_text("Saved.").wait_for()
    saved = next(b for b in _api(page, "GET", "/api/brands")[1]["brands"] if b["id"] == bid)
    assert saved["about"].startswith("A gym in Baroda with morning") and saved["hashtags"] == "#gym #baroda"
    assert set(saved["kinds"].split(",")) == {"flyers", "ideas", "videos"} and saved["enabled"]

    card = page.locator(f'.brand-card[data-bid="{bid}"]')  # drawn again after the save, still open
    assert card.get_by_role("button", name="Delete…").is_disabled()  # it is on: switch it off first
    card.get_by_role("button", name="Switch off").click()
    page.locator(f"#bmsg-{bid}").get_by_text("Switched off.").wait_for()
    card = page.locator(f'.brand-card[data-bid="{bid}"]')
    card.get_by_role("button", name="Delete…").click()
    card.locator("[data-bfoot]").get_by_text("Posts").wait_for()
    go = card.get_by_role("button", name="Delete for good")
    card.get_by_label("Type Gym Delete Test to confirm").fill("Gym")
    assert go.is_disabled()
    card.get_by_label("Type Gym Delete Test to confirm").fill("gym delete test")
    assert go.is_enabled()
    go.click()
    page.locator("#biz-msg").get_by_text("Gym Delete Test was deleted.").wait_for()
    assert all(b["id"] != bid for b in _api(page, "GET", "/api/brands")[1]["brands"])
    page.wait_for_timeout(2700)  # the page reloads; the business is gone from the sidebar and the table too
    page.locator("#biz-health tbody").get_by_text("CloudTrio").wait_for()
    assert "Gym Delete Test" not in page.locator("#biz-health").inner_text()
    assert page.locator("#brand-sw").count() == 0 or "Gym Delete Test" not in page.locator("#brand-sw").inner_text()


def test_owner_edits_their_own_business(sign_in, tmp_path):
    admin = sign_in()
    bid = _business(admin, "Florist Owner Test", about="A florist in Nashik for weddings", hashtags="#flowers", kinds="flyers,ideas", delivery="dry_run")
    assert _api(admin, "POST", "/api/users", {"username": "floristowner", "password": "Owner@12345", "role": "owner", "brand_id": bid})[0] == 200
    owner = sign_in("floristowner", "Owner@12345")
    assert owner.locator('#nav a[href="#businesses"]').count() == 0
    owner.goto("/#mybusiness")
    about = owner.get_by_label("What it does, who it is for, how it talks")
    owner.wait_for_function("document.getElementById('my-about').value.length > 0")
    assert about.input_value() == "A florist in Nashik for weddings"
    about.fill("A florist in Nashik: wedding stages and bouquets to order")
    logo = tmp_path / "florist.png"
    Image.new("RGBA", (64, 64), "#e91e63").save(logo)
    owner.get_by_label("Logo (PNG, up to 2 MB)").set_input_files(str(logo))
    owner.get_by_role("button", name="Save").click()
    owner.locator("#my-msg").get_by_text("Saved.").wait_for()
    owner.locator("#my-logo-img:not([hidden])").wait_for()
    saved = next(b for b in _api(admin, "GET", "/api/brands")[1]["brands"] if b["id"] == bid)
    assert saved["about"].startswith("A florist in Nashik: wedding stages") and saved["logo"]
