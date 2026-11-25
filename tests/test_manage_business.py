"""Managing a business after it is set up: change everything, switch it off and on, and delete it for good."""

import sqlite3

import pytest

from conftest import client_as, make_brand
from technews import brands, db, users
from technews.config import settings


def _logo(bid):
    from PIL import Image

    Image.new("RGBA", (4, 4), "#123456").save(brands.logo_path(bid), "PNG")


def _ready_brand(name="Bakery", **fields):
    bid = make_brand(name, about="Family bakery in Pune since 1998", hashtags="#bread", delivery="dry_run", enabled=False, **fields)
    _logo(bid)
    return bid


def _fill(bid, tag):
    """One row of everything a business owns, so a delete can be checked table by table."""
    now = db.now()
    pid = db.execute("INSERT INTO posts (story_id, headline, caption, image_path, writer, delivered, status, created_at, brand_id) "
                     "VALUES (0, ?, 'c', 'x.jpg', 'w', 'dry_run', 'pending', ?, ?)", f"post {tag}", now, bid)  # fmt: skip
    db.execute("INSERT INTO metrics (post_id, at) VALUES (?, ?)", pid, now)
    db.execute("INSERT INTO notes (post_id, \"user\", text, at) VALUES (?, 'u', 'n', ?)", pid, now)
    db.execute("INSERT INTO flyers (id, kicker, headline, points, cta, caption, brand_id) VALUES (?, 'k', 'h', '[]', 'c', 'c', ?)", f"f-{tag}", bid)
    db.execute("INSERT INTO ideas (brand_id, at, idea) VALUES (?, ?, 'i')", bid, now)
    db.execute("INSERT INTO videos (brand_id, at, stored, status) VALUES (?, ?, 'v.mp4', 'done')", bid, now)
    for table in ("agent_runs", "playbooks", "proposals"):
        extra = {"agent_runs": ("", ""), "playbooks": (", text", ", 't'"), "proposals": (", text", ", 't'")}[table]
        db.execute(f"INSERT INTO {table} (agent, brand_id, at{extra[0]}) VALUES ('a', ?, ?{extra[1]})", bid, now)  # noqa: S608
    db.execute("INSERT INTO feedback (at, brand_id, kind) VALUES (?, ?, 'approved')", now, bid)
    db.execute("INSERT INTO followers (brand_id, day, count, at) VALUES (?, '2026-11-01', 5, ?)", bid, now)
    db.kv_set("pipeline:last_post_at" + brands.suffix(bid), now)
    return pid


# --------------------------------------------------------------------------- editing


def test_save_cannot_switch_on_a_business_that_is_not_ready():
    bid = make_brand("Bakery", enabled=False)
    with pytest.raises(ValueError, match="Not ready: "):
        brands.save(bid, {"name": "Bakery", "enabled": True})
    assert brands.get(bid)["enabled"] == 0


def test_save_without_enabled_keeps_it_as_it_was():
    bid = make_brand("Bakery", enabled=False)
    brands.save(bid, {"name": "Bakery Two"})
    assert brands.get(bid)["enabled"] == 0 and brands.get(bid)["name"] == "Bakery Two"


def test_a_partial_save_keeps_the_fields_it_does_not_name():
    bid = make_brand("Bakery", handle="@bakery", hashtags="#bread", email="a@b.test", site="b.test", categories="ai", posts_per_day=3, nvidia_key="nvapi-x")
    brands.save(bid, {"name": "Bakery", "clear_nvidia_key": True})
    b = brands.get(bid)
    assert (b["handle"], b["hashtags"], b["email"], b["site"], b["categories"], b["posts_per_day"]) == ("bakery", "#bread", "a@b.test", "b.test", "ai", 3)
    assert b["nvidia_key"] == ""


def test_save_switches_a_ready_business_on():
    bid = _ready_brand()
    brands.save(bid, {"name": "Bakery", "enabled": True})
    assert brands.get(bid)["enabled"] == 1


def test_switch_off_a_business():
    bid = _ready_brand()
    brands.switch_on(bid)
    admin = client_as("boss", "Admin@1234")
    assert admin.post(f"/api/brands/{bid}/disable").status_code == 200
    assert brands.get(bid)["enabled"] == 0


def test_cloudtrio_can_be_switched_off_and_back_on():
    brands.seed()
    admin = client_as("boss", "Admin@1234")
    assert admin.post("/api/brands/1/disable").status_code == 200 and brands.get(1)["enabled"] == 0
    brands.save(1, {"name": "CloudTrio"})  # saving its name keeps it off
    assert brands.get(1)["enabled"] == 0
    assert not any(b["id"] == 1 for b in brands.active())
    out = admin.post("/api/brands/1/enable")  # it runs on the general settings: trial run counts as ready
    assert out.status_code == 200, out.get_json()
    assert brands.get(1)["enabled"] == 1


def test_admin_with_cloudtrio_off_opens_the_next_business():
    brands.seed()
    bid = make_brand("Bakery")
    brands.switch_off(1)
    page = client_as("boss", "Admin@1234").get("/")
    assert page.status_code == 200 and "<title>Bakery" in page.get_data(as_text=True)


def test_admin_still_gets_in_with_every_business_off():
    brands.seed()
    brands.switch_off(1)
    admin = client_as("boss", "Admin@1234")
    page = admin.get("/")
    assert page.status_code == 200 and 'id="businesses"' in page.get_data(as_text=True)


def test_nothing_runs_for_cloudtrio_while_it_is_off(monkeypatch):
    from technews import pipeline

    brands.seed()
    brands.switch_off(1)
    called = []
    monkeypatch.setattr(pipeline, "scan", lambda: called.append("scan") or {})
    monkeypatch.setattr(pipeline, "review", lambda: called.append("review") or {})
    monkeypatch.setattr(pipeline, "post_next", lambda: called.append("post") or {})
    db.kv_release(pipeline.CYCLE_LEASE_KEY)
    out = pipeline.run_cycle()
    assert called == [] and out.get("brands") == {}, out  # no business that is on posts news: no scanning, no AI review
    bid = make_brand("Bakery")  # on, but posts no news: still no scanning
    db.kv_release(pipeline.CYCLE_LEASE_KEY)
    out = pipeline.run_cycle()
    assert called == ["post"] and list(out["brands"]) == ["Bakery"], out


def test_disconnect_instagram_forgets_the_saved_login():
    bid = make_brand("Bakery")
    brands.session_path(bid).write_text("{}")
    admin = client_as("boss", "Admin@1234")
    assert admin.post(f"/api/brands/{bid}/disconnect").status_code == 200
    assert not brands.session_path(bid).exists()
    assert admin.post("/api/brands/1/disconnect").status_code == 400  # CloudTrio's login lives under Settings


def test_logo_can_be_seen_by_its_own_people_only():
    bid = _ready_brand()
    other = make_brand("Salon")
    admin = client_as("boss", "Admin@1234")
    got = admin.get(f"/api/brands/{bid}/logo.png")
    assert got.status_code == 200 and got.mimetype == "image/png"
    assert admin.get(f"/api/brands/{other}/logo.png").status_code == 404  # none uploaded yet
    owner = client_as("salonowner", "Pass@1234", "owner", other)
    assert owner.get(f"/api/brands/{bid}/logo.png").status_code == 404


def test_new_routes_are_for_admins():
    bid = _ready_brand()
    brands.switch_on(bid)
    owner = client_as("baker", "Pass@1234", "owner", bid)
    assert owner.post(f"/api/brands/{bid}/disable").status_code == 403
    assert owner.post(f"/api/brands/{bid}/disconnect").status_code == 403
    assert owner.get(f"/api/brands/{bid}/footprint").status_code == 403
    assert owner.delete(f"/api/brands/{bid}", json={"confirm": "Bakery"}).status_code == 403


# --------------------------------------------------------------------------- deleting


def test_footprint_counts_what_would_go():
    bid = make_brand("Bakery")
    _fill(bid, "a")
    users.create("baker", "Pass@1234", "owner", bid)
    got = client_as("boss", "Admin@1234").get(f"/api/brands/{bid}/footprint").get_json()
    assert got == {"name": "Bakery", "posts": 1, "flyers": 1, "videos": 1, "ideas": 1, "people": 1, "enabled": True}


def test_delete_needs_it_switched_off_and_the_name_typed():
    bid = make_brand("Bakery")
    with pytest.raises(ValueError, match="Switch it off first"):
        brands.delete(bid, confirm="Bakery")
    brands.switch_off(bid)
    with pytest.raises(ValueError, match="Type the business name"):
        brands.delete(bid, confirm="bakry")
    assert brands.get(bid) is not None


def test_cloudtrio_can_never_be_deleted():
    brands.seed()
    with pytest.raises(ValueError, match="cannot be deleted"):
        brands.delete(1, confirm="CloudTrio")


def test_delete_waits_for_a_video_being_made():
    bid = make_brand("Bakery", enabled=False)
    db.execute("INSERT INTO videos (brand_id, at, stored, status) VALUES (?, ?, 'v.mp4', 'working')", bid, db.now())
    with pytest.raises(ValueError, match="video is being made"):
        brands.delete(bid, confirm="Bakery")


def test_delete_removes_everything_of_that_business_and_nothing_else():
    keep = make_brand("Salon")
    gone = make_brand("Bakery")
    keep_post = _fill(keep, "keep")
    gone_post = _fill(gone, "gone")
    users.create("salonowner", "Pass@1234", "owner", keep)
    users.create("baker", "Pass@1234", "owner", gone)
    users.create("bakerview", "Pass@1234", "viewer", gone)
    (brands.folder(gone) / "cards").mkdir()
    (brands.folder(gone) / "cards" / "x.jpg").write_bytes(b"x")
    brands.session_path(gone).write_text("{}")
    brands.switch_off(gone)

    out = brands.delete(gone, confirm="  bakery ")

    assert out["backup"].startswith("newsroom-") and (settings.DATA_DIR / "backups" / out["backup"]).exists()
    assert out["removed"]["posts"] == 1 and out["removed"]["people"] == 2
    assert brands.get(gone) is None and not (settings.DATA_DIR / "brands" / str(gone)).exists()
    assert users.by_name("baker") is None and users.by_name("bakerview") is None
    assert db.kv_get("pipeline:last_post_at" + brands.suffix(gone)) is None
    for table in ("posts", "flyers", "ideas", "videos", "agent_runs", "feedback", "playbooks", "proposals", "followers"):
        assert db.rows(f"SELECT 1 FROM {table} WHERE brand_id = ?", gone) == [], table  # noqa: S608
        assert db.rows(f"SELECT 1 FROM {table} WHERE brand_id = ?", keep) != [], table  # noqa: S608
    assert db.rows("SELECT 1 FROM metrics WHERE post_id = ?", gone_post) == [] and db.rows("SELECT 1 FROM notes WHERE post_id = ?", gone_post) == []
    assert db.rows("SELECT 1 FROM metrics WHERE post_id = ?", keep_post) != [] and users.by_name("salonowner") is not None
    assert db.kv_get("pipeline:last_post_at" + brands.suffix(keep)) is not None and brands.get(keep) is not None


def test_delete_covers_every_table_that_belongs_to_a_business():
    """A table added later with a brand_id column must be added to the delete too."""
    if db.postgres():
        pytest.skip("checked on SQLite")
    conn = sqlite3.connect(settings.DB_PATH)
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    owned = {t for t in tables if any(c[1] == "brand_id" for c in conn.execute(f"PRAGMA table_info({t})"))}
    conn.close()
    assert owned - {"users"} == set(brands.OWNED_TABLES)


def test_delete_from_the_console():
    bid = make_brand("Bakery")
    _fill(bid, "a")
    admin = client_as("boss", "Admin@1234")
    assert admin.post("/api/brand", json={"id": bid}).status_code == 200  # the admin has it open
    assert admin.delete(f"/api/brands/{bid}", json={"confirm": "Bakery"}).status_code == 400  # still on
    admin.post(f"/api/brands/{bid}/disable")
    assert admin.delete(f"/api/brands/{bid}", json={"confirm": "nope"}).status_code == 400
    out = admin.delete(f"/api/brands/{bid}", json={"confirm": "Bakery"})
    assert out.status_code == 200, out.get_json()
    assert [b["name"] for b in out.get_json()["brands"]] == ["CloudTrio"] and out.get_json()["backup"]
    assert admin.get("/").status_code == 200  # the admin falls back to CloudTrio
    assert any(r["action"] == "delete business" for r in db.rows("SELECT action FROM audit"))


def test_people_of_a_deleted_business_are_signed_out():
    bid = make_brand("Bakery")
    owner = client_as("baker", "Pass@1234", "owner", bid)
    brands.switch_off(bid)
    brands.delete(bid, confirm="Bakery")
    assert owner.get("/api/posts").status_code == 401


# --------------------------------------------------------------------------- owners edit their own business


def test_owners_get_their_own_business_page():
    bid = _ready_brand()
    brands.switch_on(bid)
    owner = client_as("baker", "Pass@1234", "owner", bid).get("/").get_data(as_text=True)
    assert 'href="#mybusiness"' in owner and 'id="my-about"' in owner and 'id="onboard"' not in owner
    viewer = client_as("bakerview", "Pass@1234", "viewer", bid).get("/").get_data(as_text=True)
    assert 'href="#mybusiness"' not in viewer
    admin = client_as("boss", "Admin@1234").get("/").get_data(as_text=True)
    assert 'href="#mybusiness"' not in admin  # admins use Businesses


def test_owner_saves_their_profile_and_logo():
    bid = _ready_brand()
    brands.switch_on(bid)
    owner = client_as("baker", "Pass@1234", "owner", bid)
    got = owner.post("/api/brand/profile", json={"about": "Sourdough and cakes to order in Pune", "colors": {"bg": "#101010", "accent": "#ff0000"}})
    assert got.status_code == 200 and brands.get(bid)["about"].startswith("Sourdough")
    assert owner.get("/api/brand/profile").get_json()["brand"]["about"].startswith("Sourdough")
    assert "nvidia_key" not in owner.get("/api/brand/profile").get_json()["brand"]
