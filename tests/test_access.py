"""Admins see every business; everyone else sees and changes only their own."""

from pathlib import Path

import pytest

from conftest import SOURDOUGH, client_as, make_brand
from technews import brands, db, flyers, pipeline, users


def _flyer_post(brand_id=None):
    """A waiting flyer post for this business (business #1 when None); returns its id."""
    if brand_id is None:
        return pipeline.draft_flyer()["post_id"]
    with brands.use(brand_id):
        flyers.save(SOURDOUGH)
        return pipeline.draft_flyer()["post_id"]


def test_owner_only_sees_own_business():
    b2 = make_brand("Bakery")
    b3 = make_brand("Salon")
    c = client_as("baker", "Pass@1234", "owner", b2)
    assert c.post("/api/brand", json={"id": b3}).status_code == 403
    assert c.post("/api/brand", json={"id": 1}).status_code == 403
    assert c.post("/api/brand", json={"id": b2}).status_code == 200


def test_owner_cannot_touch_other_post():
    b2 = make_brand("Bakery")
    pid = _flyer_post()  # business 1's post
    c = client_as("baker", "Pass@1234", "owner", b2)
    assert c.post(f"/api/posts/{pid}/approve").status_code == 404
    assert c.post(f"/api/posts/{pid}/reject").status_code == 404
    assert db.rows("SELECT status FROM posts WHERE id = ?", pid)[0]["status"] == "pending"


def test_owner_approves_own_post():
    b2 = make_brand("Bakery")
    pid = _flyer_post(b2)
    c = client_as("baker", "Pass@1234", "owner", b2)
    assert c.post(f"/api/posts/{pid}/approve").status_code == 200
    assert db.rows("SELECT status FROM posts WHERE id = ?", pid)[0]["status"] == "posted"


def test_admin_acts_only_on_the_business_open():
    b2 = make_brand("Bakery")
    pid = _flyer_post(b2)
    admin = client_as("boss", "Admin@1234")
    assert admin.post(f"/api/posts/{pid}/reject").status_code == 404  # CloudTrio is open
    assert admin.post("/api/brand", json={"id": b2}).status_code == 200
    assert admin.post(f"/api/posts/{pid}/reject").status_code == 200


def test_owner_blocked_from_admin_pages():
    c = client_as("baker", "Pass@1234", "owner", make_brand("Bakery"))
    for url in ("/api/settings", "/api/users", "/api/brands", "/api/backups", "/api/sources", "/api/scout", "/api/security/secrets"):
        assert c.get(url).status_code == 403, url


def test_owner_cannot_change_another_business():
    b2 = make_brand("Bakery")
    c = client_as("baker", "Pass@1234", "owner", b2)
    assert c.post("/api/brands/1/connect", json={}).status_code == 404
    assert c.post("/api/brands", json={"name": "Mine now"}).status_code == 403


def test_owner_edits_own_profile_only():
    b2 = make_brand("Bakery")
    c = client_as("baker", "Pass@1234", "owner", b2)
    assert c.post("/api/brand/profile", json={"about": "Bread since 1998", "nvidia_key": "nvapi-x"}).status_code == 200
    assert brands.get(b2)["about"] == "Bread since 1998" and brands.get(b2)["nvidia_key"] == ""
    assert brands.get(1)["about"] == ""


def test_viewer_cannot_approve():
    b2 = make_brand("Bakery")
    pid = _flyer_post(b2)
    v = client_as("looker", "Pass@1234", "viewer", b2)
    assert v.post(f"/api/posts/{pid}/approve").status_code == 403


def test_switched_off_business_locks_its_people_out():
    b2 = make_brand("Bakery")
    c = client_as("baker", "Pass@1234", "owner", b2)
    brands.save(b2, {"name": "Bakery", "enabled": False})
    assert c.get("/api/posts").status_code == 403


def test_owner_sees_no_other_business_anywhere():
    unique = {**SOURDOUGH, "id": "cloudtrio-secret", "headline": "Zebra launch 7731 for CloudTrio only"}  # text found nowhere else
    pid = pipeline.draft_flyer(flyers.get(flyers.save(unique).id))["post_id"]
    secret = db.rows("SELECT headline, image_path FROM posts WHERE id = ?", pid)[0]
    c = client_as("baker", "Pass@1234", "owner", make_brand("Bakery"))
    for rule in c.application.url_map.iter_rules():
        if "GET" in rule.methods and not rule.arguments and rule.endpoint not in ("static", "login_google", "login_google_callback"):
            body = c.get(rule.rule).get_data(as_text=True)
            assert secret["headline"] not in body, rule.rule
    assert c.get("/cards/" + Path(secret["image_path"]).name).status_code == 404
    own = client_as("ct-owner", "Pass@1234", "owner", 1)  # the check can see a leak: CloudTrio's own people do see it
    assert secret["headline"] in own.get("/").get_data(as_text=True)
    assert own.get("/cards/" + Path(secret["image_path"]).name).status_code == 200


def test_approver_becomes_owner_of_cloudtrio():
    users.create("first", "Admin@1234", "admin")
    uid = db.execute("INSERT INTO users (username, hash, role, created_at) VALUES ('old', 'x', 'approver', ?)", db.now())
    users.migrate()
    assert users.get(uid)["role"] == "owner" and users.get(uid)["brand_id"] == 1


def test_owner_needs_a_business():
    with pytest.raises(ValueError, match="Pick the business"):
        users.create("nobody", "Pass@1234", "owner", 0)
    with pytest.raises(ValueError, match="Pick the business"):
        users.create("nobody", "Pass@1234", "owner", 99)


def test_admin_adds_person_to_the_open_business():
    b2 = make_brand("Bakery")
    admin = client_as("boss", "Admin@1234")
    admin.post("/api/brand", json={"id": b2})
    uid = admin.post("/api/users", json={"username": "baker", "password": "Pass@1234", "role": "owner"}).get_json()["id"]
    assert users.get(uid)["brand_id"] == b2
    uid2 = admin.post("/api/users", json={"username": "salon", "password": "Pass@1234", "role": "viewer", "brand_id": 1}).get_json()["id"]
    assert users.get(uid2)["brand_id"] == 1


def test_owner_page_hides_admin_sections_and_other_businesses():
    make_brand("Salon")
    b2 = make_brand("Bakery")
    owner = client_as("baker", "Pass@1234", "owner", b2).get("/").get_data(as_text=True)
    assert 'href="#settings"' not in owner and 'href="#sources"' not in owner and 'href="#stories"' not in owner
    assert "Salon" not in owner and 'id="brand-sw"' not in owner and "Run full cycle" not in owner
    assert "<b>Bakery</b>" in owner
    admin = client_as("boss", "Admin@1234").get("/").get_data(as_text=True)
    assert 'href="#settings"' in admin and 'href="#stories"' in admin and 'id="brand-sw"' in admin and "Salon" in admin


def test_admin_changes_a_person_to_owner_of_the_open_business():
    b2 = make_brand("Bakery")
    admin = client_as("boss", "Admin@1234")
    uid = users.create("second", "Admin@1234", "admin")
    admin.post("/api/brand", json={"id": b2})
    r = admin.post(f"/api/users/{uid}", json={"role": "owner"})
    assert r.status_code == 200, r.get_json()
    assert users.get(uid)["role"] == "owner" and users.get(uid)["brand_id"] == b2


def test_businesses_have_their_own_section_for_admins():
    import re as _re

    admin = client_as("boss", "Admin@1234").get("/").get_data(as_text=True)
    assert 'href="#businesses"' in admin
    section = _re.search(r'<section id="businesses".*?</section>\s*(?=<!--|<section)', admin, _re.S).group(0)
    assert 'id="onboard"' in section and 'id="biz-health"' in section and 'id="br-list"' in section
    settings_part = _re.search(r'<section id="settings".*?</section>\s*(?=<!--|<section|</main>)', admin, _re.S).group(0)
    assert 'id="onboard"' not in settings_part and 'id="biz-health"' not in settings_part
    owner = client_as("baker", "Pass@1234", "owner", make_brand("Bakery")).get("/").get_data(as_text=True)
    assert 'href="#businesses"' not in owner and 'id="onboard"' not in owner
