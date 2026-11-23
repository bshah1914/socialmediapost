import io
import os
from datetime import UTC, datetime, timedelta

import pytest

from technews import config, db, pipeline, sources
from technews.agents import llm, reviewer, scanner, writer
from technews.config import settings

from conftest import _reset_postgres


def _client():
    """A test client signed in as the admin."""
    from technews import auth, create_app

    app = create_app()
    auth.set_credentials("brijesh", "Admin@123")
    client = app.test_client()
    assert client.post("/login", data={"username": "brijesh", "password": "Admin@123"}).status_code == 302
    return client


def item(title, source="BleepingComputer", summary="", minutes_ago=30):
    return sources.FeedItem(source, f"https://x.test/{abs(hash(title))}", title, summary,
                            datetime.now(UTC) - timedelta(minutes=minutes_ago))


def test_group_merges_one_story_from_many_outlets():
    stories = sources.group([
        item("Hackers breach Ticketmaster, steal data of 560 million customers", "BleepingComputer"),
        item("Ticketmaster confirms breach affecting 560 million customers", "TechCrunch"),
        item("Apple unveils new M5 MacBook Pro", "The Verge"),
    ])
    assert len(stories) == 2
    ticket = next(s for s in stories if "Ticketmaster" in s.title)
    assert ticket.outlets == ["BleepingComputer", "TechCrunch"]
    assert sources.trusted(ticket)


def test_keyword_scanner_keeps_tech_and_drops_the_rest():
    breach = sources.Story("Massive data breach exposes 10 million users", "u", "s", "", datetime.now(UTC), ["a", "b"])
    deal = sources.Story("Best laptop deal: 40% off today", "u", "s", "", datetime.now(UTC), ["a"])
    cricket = sources.Story("India win the cricket final", "u", "s", "", datetime.now(UTC), ["a"])
    a, b, c = scanner.scan([breach, deal, cricket])
    assert a.tech and a.category == "breach" and a.score >= 7
    assert not b.tech and not c.tech


def test_scanner_parses_model_reply(monkeypatch):
    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "chat_json", lambda **_: llm.ChatResult(
        {"items": [{"i": 0, "tech": True, "category": "launch", "score": 9, "why": "major release"}]}, "m", 1))
    story = sources.Story("OpenAI releases GPT-6", "u", "s", "", datetime.now(UTC), ["a"])
    (s,) = scanner.scan([story])
    assert (s.tech, s.category, s.score) == (True, "launch", 9.0)


STORY = {"id": 1, "title": "Ticketmaster breach exposes 560 million customers", "summary": "ShinyHunters claims "
         "it stole 1.3TB of data.", "titles": [], "outlets": ["BleepingComputer"], "category": "breach",
         "source": "BleepingComputer", "scan_score": 9.0, "published_at": "", "angle": ""}  # fmt: skip


def test_reviewer_rejects_invented_numbers_and_hype():
    draft = {"headline": "Ticketmaster Hit By Huge Breach", "points": ["560 million customers exposed",
             "Hackers want $500,000 ransom"], "caption": "Guaranteed the biggest leak ever. " * 4}  # fmt: skip
    problems = reviewer.check_draft(draft, STORY)
    assert any("500000" in p for p in problems)
    assert any("guaranteed" in p for p in problems)


def test_reviewer_approves_a_faithful_draft():
    draft = {"headline": "Ticketmaster Breach Hits 560M", "points": ["560 million customers exposed",
             "ShinyHunters claims it stole 1.3TB of data"],
             "caption": "Ticketmaster is reportedly breached. ShinyHunters claims 1.3TB of data was taken "
                        "from 560 million customers. Change your password now. Were you affected?\n\nSource: BleepingComputer"}  # fmt: skip
    assert reviewer.check_draft(draft, STORY) == []


def test_reviewer_never_approves_a_single_unknown_source():
    c = {**STORY, "outlets": ["randomblog.biz"], "scan_score": 10.0}
    (r,) = reviewer.select([c], min_score=8)
    assert not r.approve and r.reason == "single unverified source"


def test_writer_falls_back_to_template_without_model():
    d = writer.write(STORY)
    assert d.writer == "template" and d.headline == STORY["title"]
    assert len(d.hashtags) <= settings.HASHTAG_LIMIT and "#databreach" in d.full_caption()


def test_full_cycle_dry_run(monkeypatch):
    items = [
        item("Ticketmaster breach exposes 560 million customers", "BleepingComputer",
             "ShinyHunters claims it stole the data. Ticketmaster has not confirmed the scope."),
        item("Ticketmaster breach: 560 million customers exposed", "TechCrunch"),
        item("Ticketmaster data breach hits 560 million", "Wired"),
        item("India win the cricket final", "Sports Daily"),
    ]
    monkeypatch.setattr(sources, "fetch_all", lambda max_age: items)
    summary = pipeline.run_cycle()
    assert summary["scan"]["new"] == 2 and summary["review"]["approved"] == 1
    assert summary["post"]["posted"] and summary["post"]["delivered"] == "dry_run"
    assert list((settings.OUTBOX_DIR).glob("*.jpg")) and list(settings.OUTBOX_DIR.glob("*.txt"))
    # The second cycle finds nothing new and waits for the gap.
    again = pipeline.run_cycle()
    assert again["scan"]["new"] == 0 and not again["post"]["posted"]


def test_same_subject_in_different_words():
    assert sources.same_subject("Japan’s GMO Internet warns users of impending ransomware attack",
                                "GMO refuses demands as hackers threaten data leak, ransomware attack at noon Tuesday")
    assert not sources.same_subject("Apple unveils new M5 MacBook Pro", "Apple fined in EU over App Store rules")


def test_settings_api_saves_masks_and_guards(monkeypatch, tmp_path):
    from dotenv import dotenv_values

    from technews import config, create_app

    env = tmp_path / ".env"
    env.write_text("IG_PASSWORD=old\n")
    monkeypatch.setattr(config, "ENV_PATH", env)
    monkeypatch.setattr(settings, "SCHEDULER_ENABLED", False)
    for name in ("NVIDIA_API_KEY", "IG_USERNAME", "IG_PASSWORD", "POSTS_PER_DAY"):
        monkeypatch.setattr(settings, name, getattr(settings, name))  # restored after the test
    client = _client()

    r = client.post("/api/settings", json={"NVIDIA_API_KEY": "nvapi-secret-1234", "IG_USERNAME": "@cloudtrio",
                                           "IG_PASSWORD": "", "POSTS_PER_DAY": "3"})  # fmt: skip
    assert r.status_code == 200 and set(r.json["changed"]) == {"NVIDIA_API_KEY", "IG_USERNAME", "POSTS_PER_DAY"}
    assert settings.NVIDIA_API_KEY == "nvapi-secret-1234" and settings.POSTS_PER_DAY == 3
    saved = dotenv_values(env)
    assert saved["IG_USERNAME"] == "cloudtrio" and saved["IG_PASSWORD"] == "old"  # blank secret kept

    shown = client.get("/api/settings").json["settings"]
    assert "secret" not in shown["NVIDIA_API_KEY"] and shown["NVIDIA_API_KEY"].endswith("1234")
    assert client.post("/api/settings", json={"IG_DELIVERY": "tiktok"}).status_code == 400
    # Nobody who has not signed in can change settings.
    anon = client.application.test_client().post("/api/settings", json={"POSTS_PER_DAY": "9"})
    assert anon.status_code == 401 and settings.POSTS_PER_DAY == 3


def test_password_mask_shows_nothing_of_the_password(monkeypatch):
    from technews import config

    monkeypatch.setattr(settings, "IG_PASSWORD", "hunter2-secret-1998")
    assert config.public()["IG_PASSWORD"] == "•" * 8


def test_cloud_and_devops_outrank_other_tech_news():
    def scan(title):
        return scanner.keyword_scan(sources.group([item(title)])[0])

    assert scan("AWS launches new Graviton instances").category == "cloud"
    assert scan("Terraform 2.0 released with a new state engine").category == "devops"
    assert scan("Kubernetes flaw allows root escalation").score >= scan("Hackers breach Sony, 2 million users affected").score


def test_priority_news_clears_a_lower_review_bar():
    base = {"id": 1, "scan_score": 7.2, "outlets": ["TechCrunch"]}
    assert reviewer.fallback_review({**base, "category": "devops"}, min_score=8).approve
    assert not reviewer.fallback_review({**base, "category": "launch"}, min_score=8).approve


def _post(flyer=""):
    db.execute("INSERT INTO posts (story_id, headline, caption, image_path, writer, delivered, status, created_at, flyer) "
               "VALUES (0, 'h', 'c', 'x.jpg', 'w', 'dry_run', 'posted', ?, ?)", db.now(), flyer)


def test_a_flyer_follows_every_two_news_posts(monkeypatch):
    monkeypatch.setattr(settings, "FLYERS_ENABLED", True)
    monkeypatch.setattr(settings, "FLYER_AFTER_NEWS", 2)
    assert not pipeline.next_is_flyer()
    _post(); assert not pipeline.next_is_flyer()
    _post(); assert pipeline.next_is_flyer()
    _post("bill"); assert not pipeline.next_is_flyer()
    _post(); _post(); assert pipeline.next_is_flyer()
    monkeypatch.setattr(settings, "FLYERS_ENABLED", False)
    assert not pipeline.next_is_flyer()


def test_flyers_rotate_and_post_in_dry_run(monkeypatch):
    from technews import flyers

    seen = []
    for _ in range(len(flyers.FLYERS)):
        result = pipeline.publish_flyer()
        assert result["posted"] and result["delivered"] == "dry_run"
        seen.append(result["flyer"])
    assert sorted(seen) == sorted(f.id for f in flyers.FLYERS)


def test_dashboard_renders_news_and_flyer_posts(monkeypatch):
    from technews import create_app

    monkeypatch.setattr(settings, "SCHEDULER_ENABLED", False)
    _post("bill")
    client = _client()
    page = client.get("/")
    assert page.status_code == 200
    assert client.get("/api/posts").status_code == 200
    stats = client.get("/api/insights").get_json()
    assert len(stats["daily"]) == 14 and stats["posted_total"] >= 1
    assert {f["id"] for f in stats["flyers"]} == {f.id for f in __import__("technews.flyers", fromlist=["FLYERS"]).FLYERS}
    assert client.get("/api/flyer/bill.jpg").status_code == 200
    assert client.get("/api/flyer/nope.jpg").status_code == 404


def test_linkedin_flyer_goes_out_live_and_never_blocks_instagram(monkeypatch):
    from technews import flyers, linkedin

    calls = []

    class Resp:
        def __init__(self, status=200, body=None, headers=None):
            self.status_code, self._body, self.headers, self.text = status, body or {}, headers or {}, "boom"
            self.ok = status < 400

        def json(self):
            return self._body

    def fake_post(url, **kw):
        calls.append(url)
        if url.endswith("initializeUpload"):
            return Resp(body={"value": {"uploadUrl": "https://up.example/x", "image": "urn:li:image:1"}})
        assert kw["json"]["commentary"].count("\\_") == 1 and kw["json"]["content"]["media"]["id"] == "urn:li:image:1"
        return Resp(201, headers={"x-restli-id": "urn:li:share:9"})

    monkeypatch.setattr(linkedin.requests, "post", fake_post)
    monkeypatch.setattr(linkedin.requests, "put", lambda url, **kw: Resp())
    for name, value in {"IG_DELIVERY": "instagram", "LINKEDIN_ENABLED": True, "LINKEDIN_ACCESS_TOKEN": "tok",
                        "LINKEDIN_AUTHOR": "urn:li:organization:5"}.items():
        monkeypatch.setattr(settings, name, value)
    image = flyers.render(flyers.FLYERS[0])
    out = linkedin.publish_photo(image, "a_b #cloud")
    assert out["permalink"].endswith("urn:li:share:9/") and len(calls) == 2

    monkeypatch.setattr(linkedin.requests, "post", lambda url, **kw: Resp(401))
    shared = pipeline._share_on_linkedin(flyers.FLYERS[0], image, "x")
    assert shared.startswith("failed:") and "expired" in shared


def test_linkedin_dry_run_writes_the_outbox(monkeypatch):
    from technews import flyers, linkedin

    monkeypatch.setattr(settings, "LINKEDIN_ENABLED", True)
    out = linkedin.publish_photo(flyers.render(flyers.FLYERS[0]), "hello")
    assert out["delivered"] == "dry_run" and any(settings.OUTBOX_DIR.glob("linkedin-*.jpg"))


def test_nothing_posts_until_approved(monkeypatch):
    from technews import instagram

    sent = []
    real = instagram.publish_photo
    monkeypatch.setattr(instagram, "publish_photo", lambda path, caption, **kw: sent.append(caption) or real(path, caption, **kw))
    monkeypatch.setattr(settings, "APPROVAL_MODE", "all")
    monkeypatch.setattr(settings, "APPROVAL_QUEUE_MAX", 2)
    story_id = db.execute("INSERT INTO stories (key, title, url, source, published_at, found_at, category, status, review_score) "
                          "VALUES ('k', 'AWS launches a new region in Spain', 'https://x.test/a', 'AWS News Blog', ?, ?, 'cloud', 'approved', 9)",
                          db.now(), db.now())  # fmt: skip

    first = pipeline.post_next()
    assert first["pending"] and not sent and db.story(story_id)["status"] == "pending"
    assert pipeline.pending_count() == 1

    db.execute("INSERT INTO posts (story_id, headline, caption, image_path, writer, delivered, status, created_at) "
               "VALUES (0,'h','c','x.jpg','w','dry_run','pending',?)", db.now())
    monkeypatch.setattr(settings, "FLYERS_ENABLED", False)  # (a flyer has its own queue)
    assert "news posts waiting for your approval" in pipeline.post_next()["reason"] and not sent  # the news queue is full
    monkeypatch.setattr(settings, "FLYERS_ENABLED", True)

    done = pipeline.approve(first["post_id"], caption="edited caption")
    assert done["posted"] and sent == ["edited caption"] and db.story(story_id)["status"] == "posted"
    row = db.rows("SELECT * FROM posts WHERE id = ?", first["post_id"])[0]
    assert row["status"] == "posted" and row["caption"] == "edited caption"
    assert not pipeline.approve(first["post_id"])["posted"]  # a second click does nothing

    flyer_post = pipeline.draft_flyer()["post_id"]
    assert pipeline.reject(flyer_post)["rejected"] and pipeline.pending_count() == 1
    assert not pipeline.approve(flyer_post)["posted"]


def test_flyers_only_mode_posts_news_straight_away(monkeypatch):
    monkeypatch.setattr(settings, "APPROVAL_MODE", "flyers")
    assert not pipeline.needs_approval(flyer=False) and pipeline.needs_approval(flyer=True)


def test_dashboard_shows_the_approval_queue_and_api_decides(monkeypatch):
    from technews import create_app

    monkeypatch.setattr(settings, "SCHEDULER_ENABLED", False)
    monkeypatch.setattr(settings, "APPROVAL_MODE", "all")
    post_id = pipeline.draft_flyer()["post_id"]
    client = _client()
    page = client.get("/")
    assert page.status_code == 200 and b"Approve &amp; post" in page.data and b'id="post-%d"' % post_id in page.data
    assert client.post(f"/api/posts/{post_id}/approve", json={"caption": "ok"}).status_code == 200
    assert client.post(f"/api/posts/{post_id}/approve").status_code == 409
    assert client.get("/").status_code == 200


def test_admin_token_can_be_rotated_from_the_dashboard(monkeypatch, tmp_path):
    from technews import config, create_app

    env = tmp_path / ".env"
    env.write_text("ADMIN_TOKEN=old\n")
    monkeypatch.setattr(config, "ENV_PATH", env)
    monkeypatch.setattr(settings, "SCHEDULER_ENABLED", False)
    monkeypatch.setattr(settings, "ADMIN_TOKEN", "old")
    client = _client()
    anon = client.application.test_client()  # not signed in: only the token opens the API
    assert anon.post("/api/settings/rotate-token").status_code == 401
    new = anon.post("/api/settings/rotate-token", headers={"X-Admin-Token": "old"}).get_json()["token"]
    assert len(new) >= 30 and f"ADMIN_TOKEN={new}" in env.read_text() and "old" not in env.read_text()
    assert anon.post("/api/settings/rotate-token", headers={"X-Admin-Token": "old"}).status_code == 401
    assert anon.get("/api/settings", headers={"X-Admin-Token": new}).status_code == 200
    assert client.post("/api/settings/rotate-token").status_code == 200  # a signed-in admin can rotate it too


def test_flyers_are_editable_from_the_portal(monkeypatch):
    from technews import create_app, flyers

    monkeypatch.setattr(settings, "SCHEDULER_ENABLED", False)
    client = _client()
    assert len(client.get("/api/flyers").get_json()) == len(flyers.FLYERS)  # seeded from the built-ins

    new = {"kicker": "webinar", "headline": "Free AWS cost webinar", "points": "Live Q&A\nSlides shared\n",
           "cta": "Reserve a seat", "note": "", "caption": "Join us.", "enabled": True}
    made = client.post("/api/flyers", json=new).get_json()["id"]
    assert made == "free-aws-cost-webinar" and flyers.get(made).kicker == "WEBINAR"
    assert client.post("/api/flyers", json={**new, "points": ""}).status_code == 400
    assert client.post("/api/flyers", json={**new, "id": made, "headline": "Renamed"}).get_json()["id"] == made
    assert flyers.get(made).headline == "Renamed" and len(flyers.listing()) == len(flyers.FLYERS) + 1

    assert client.post("/api/flyers/preview", json=new).mimetype == "image/jpeg"
    assert client.post(f"/api/flyers/{made}/queue").get_json()["posted"]  # approval is off in tests
    assert client.get(f"/api/flyer/{made}.jpg").status_code == 200

    for f in flyers.listing():  # switching every flyer off means none is picked
        client.post("/api/flyers", json={"id": f.id, "kicker": f.kicker, "headline": f.headline, "points": list(f.points),
                                         "cta": f.cta, "note": f.note, "caption": f.caption, "enabled": False})
    monkeypatch.setattr(settings, "FLYER_AFTER_NEWS", 1)
    _post()
    assert not pipeline.next_is_flyer()
    assert client.delete(f"/api/flyers/{made}").get_json()["deleted"] and flyers.get(made) is None


def test_news_sources_are_editable_and_drive_the_fetch(monkeypatch):
    from technews import create_app

    monkeypatch.setattr(settings, "SCHEDULER_ENABLED", False)
    client = _client()
    rows = client.get("/api/sources").get_json()
    assert sum(r["kind"] == "feed" for r in rows) == len(sources.FEEDS) and sum(r["kind"] == "search" for r in rows) == len(sources.SEARCHES)

    added = client.post("/api/sources", json={"kind": "feed", "name": "My Cloud Blog", "value": "https://blog.test/feed"}).get_json()
    assert client.post("/api/sources", json={"kind": "feed", "name": "x", "value": "https://blog.test/feed"}).status_code == 400  # duplicate
    assert client.post("/api/sources", json={"kind": "feed", "name": "", "value": "nope"}).status_code == 400
    assert sources.trusted({"outlets": ["My Cloud Blog"]})  # a feed you follow is a trusted outlet

    asked = []
    monkeypatch.setattr(sources, "_fetch_feed", lambda name, url: asked.append(name) or [])
    monkeypatch.setattr(sources, "_fetch_search", lambda q: asked.append(q) or [])
    for r in rows:  # everything off but the new feed
        client.post(f"/api/sources/{r['id']}", json={"enabled": False})
    sources.fetch_all(timedelta(hours=1))
    assert asked == ["My Cloud Blog"]
    client.post(f"/api/sources/{added['id']}", json={"enabled": False})
    assert not sources.trusted({"outlets": ["My Cloud Blog"]})
    assert client.delete(f"/api/sources/{added['id']}").get_json()["deleted"]


def test_newsroom_settings_save_validate_and_apply(monkeypatch, tmp_path):
    from technews import config, create_app, scheduler

    monkeypatch.setattr(config, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(settings, "SCHEDULER_ENABLED", False)
    for name in ("SCAN_INTERVAL_MINUTES", "REVIEW_MIN_SCORE", "FLYERS_ENABLED", "TIMEZONE", "BRAND_EMAIL", "FLYER_AFTER_NEWS"):
        monkeypatch.setattr(settings, name, getattr(settings, name))
    called = []
    monkeypatch.setattr(scheduler, "reschedule", lambda: called.append(1))
    client = _client()
    out = client.post("/api/settings", json={"SCAN_INTERVAL_MINUTES": 15, "REVIEW_MIN_SCORE": "7.5", "FLYERS_ENABLED": False,
                                             "TIMEZONE": "Europe/London", "BRAND_EMAIL": "hello@cloudtrio.in", "FLYER_AFTER_NEWS": 3})
    assert out.status_code == 200 and called
    assert (settings.SCAN_INTERVAL_MINUTES, settings.REVIEW_MIN_SCORE, settings.FLYERS_ENABLED) == (15, 7.5, False)
    saved = (tmp_path / ".env").read_text()
    assert "FLYERS_ENABLED=False" in saved and "REVIEW_MIN_SCORE=7.5" in saved
    for bad in ({"REVIEW_MIN_SCORE": 11}, {"TIMEZONE": "Mars/Base"}, {"BRAND_EMAIL": "nope"}, {"FLYER_AFTER_NEWS": 0}, {"SCAN_MAX_AGE_HOURS": "x"}):
        assert client.post("/api/settings", json=bad).status_code == 400, bad


def test_sign_in_guards_the_portal_and_the_account_can_change(monkeypatch):
    from technews import auth, create_app

    monkeypatch.setattr(settings, "SCHEDULER_ENABLED", False)
    app = create_app()
    auth.set_credentials("brijesh", "Admin@123")
    anon = app.test_client()
    assert anon.get("/health").get_json() == {"ok": True}  # the only open page besides the sign-in
    page = anon.get("/")
    assert page.status_code == 302 and "/login" in page.headers["Location"]
    assert anon.get("/api/posts").status_code == 401 and anon.get("/cards/x.jpg").status_code == 302
    assert anon.get("/login").status_code == 200

    assert anon.post("/login", data={"username": "brijesh", "password": "nope"}).status_code == 401
    assert anon.post("/login", data={"username": "other", "password": "Admin@123"}).status_code == 401
    ok = anon.post("/login", data={"username": "Brijesh", "password": "Admin@123", "next": "//evil.test"})
    assert ok.status_code == 302 and ok.headers["Location"] == "/"  # an outside address is never followed
    assert anon.get("/").status_code == 200 and anon.get("/api/status").status_code == 200

    # change the password: wrong current password first, then the real change
    assert anon.post("/api/account", json={"current_password": "bad", "new_password": "NewPassw0rd!"}).status_code == 403
    assert anon.post("/api/account", json={"current_password": "Admin@123", "new_password": "short"}).status_code == 400
    other = app.test_client()
    other.post("/login", data={"username": "brijesh", "password": "Admin@123"})
    done = anon.post("/api/account", json={"current_password": "Admin@123", "new_username": "cloudtrio-admin", "new_password": "NewPassw0rd!"})
    assert done.status_code == 200 and done.get_json()["user"] == "cloudtrio-admin"
    assert anon.get("/api/posts").status_code == 200  # this browser stays signed in
    assert other.get("/api/posts").status_code == 401  # every other browser is signed out
    fresh = app.test_client()
    assert fresh.post("/login", data={"username": "brijesh", "password": "Admin@123"}).status_code == 401
    assert fresh.post("/login", data={"username": "cloudtrio-admin", "password": "NewPassw0rd!"}).status_code == 302
    assert fresh.post("/logout").status_code == 302 and fresh.get("/api/posts").status_code == 401


def test_too_many_wrong_passwords_lock_the_sign_in_for_a_while(monkeypatch):
    from technews import auth, create_app

    monkeypatch.setattr(settings, "SCHEDULER_ENABLED", False)
    monkeypatch.setattr(auth, "_failures", {})
    app = create_app()
    auth.set_credentials("brijesh", "Admin@123")
    client = app.test_client()
    for _ in range(auth.MAX_FAILURES):
        assert client.post("/login", data={"username": "brijesh", "password": "x"}).status_code == 401
    assert client.post("/login", data={"username": "brijesh", "password": "Admin@123"}).status_code == 429


def test_a_draft_from_another_machine_still_posts(monkeypatch):
    from technews import flyers

    monkeypatch.setattr(settings, "APPROVAL_MODE", "all")
    flyers.render(flyers.get("bill"))  # the image sits in this machine's cards folder
    post_id = db.execute("INSERT INTO posts (story_id, headline, caption, image_path, writer, delivered, status, created_at, flyer) "
                         "VALUES (0,'h','c','C:\\Automation Code\\data\\cards\\flyer-bill.jpg','flyer','dry_run','pending',?,'bill')", db.now())  # fmt: skip
    assert pipeline.approve(post_id)["posted"]


def _no_channels(monkeypatch):
    for name in ("NOTIFY_EMAIL_TO", "SMTP_HOST", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "NOTIFY_WEBHOOK_URL", "WHATSAPP_PROVIDER",
                 "WHATSAPP_TO", "WHATSAPP_APIKEY", "TWILIO_SID", "TWILIO_WHATSAPP_FROM", "SMTP_USER", "SMTP_FROM"):
        monkeypatch.setattr(settings, name, "")


def test_a_new_draft_notifies_every_channel_that_is_set_up(monkeypatch):
    from technews import notify

    _no_channels(monkeypatch)
    assert notify.channels() == [] and notify.draft_ready("x", "news", None) is None and not notify.test()["ok"]

    sent = {"mail": [], "http": []}

    class FakeSMTP:
        def __init__(self, host, port, timeout=0): sent["mail"].append(("connect", host, port))
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def ehlo(self): pass
        def has_extn(self, name): return True
        def starttls(self): sent["mail"].append(("tls",))
        def login(self, u, p): sent["mail"].append(("login", u))
        def send_message(self, msg): sent["mail"].append(("msg", msg["To"], msg["Subject"], len(list(msg.iter_attachments()))))

    class Resp:
        ok, status_code, text = True, 200, ""

    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(notify.requests, "post", lambda url, **kw: sent["http"].append((url, kw)) or Resp())
    monkeypatch.setattr(notify.requests, "get", lambda url, **kw: sent["http"].append((url, kw)) or Resp())
    for name, value in {"NOTIFY_EMAIL_TO": "me@cloudtrio.in, boss@cloudtrio.in", "SMTP_HOST": "smtp.test", "SMTP_USER": "u",
                        "SMTP_PASSWORD": "p", "TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_CHAT_ID": "42",
                        "NOTIFY_WEBHOOK_URL": "https://hooks.test/x", "WHATSAPP_PROVIDER": "callmebot",
                        "WHATSAPP_TO": "+91 98765 43210", "WHATSAPP_APIKEY": "key", "PUBLIC_URL": "https://post.test"}.items():
        monkeypatch.setattr(settings, name, value)
    assert notify.channels() == ["email", "telegram", "webhook", "whatsapp"]

    from technews import flyers
    notify.draft_ready("Cut your AWS bill", "flyer", flyers.render(flyers.get("bill")), wait=True)
    assert ("tls",) in sent["mail"] and ("msg", "me@cloudtrio.in, boss@cloudtrio.in", "CloudTrio: a post is waiting for your approval", 1) in sent["mail"]
    urls = [u for u, _ in sent["http"]]
    assert any("api.telegram.org/bot123:abc/sendPhoto" in u for u in urls) and "https://hooks.test/x" in urls
    wa = next(kw for u, kw in sent["http"] if "callmebot" in u)["params"]
    assert wa["phone"] == "919876543210" and "https://post.test/#approvals" in wa["text"]

    # Twilio, and one channel failing does not stop the rest
    monkeypatch.setattr(settings, "WHATSAPP_PROVIDER", "twilio")
    for name, value in {"TWILIO_SID": "AC1", "TWILIO_WHATSAPP_FROM": "+1 415 523 8886"}.items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(notify.requests, "post", lambda url, **kw: (_ for _ in ()).throw(RuntimeError("down")) if "hooks.test" in url else sent["http"].append((url, kw)) or Resp())
    out = notify.send("t", "b")
    assert out["email"] == "sent" and out["webhook"].startswith("RuntimeError") and out["whatsapp"] == "sent"
    assert "webhook" in notify.last_error()
    tw = next(kw for u, kw in sent["http"] if "twilio" in u)
    assert tw["data"]["From"] == "whatsapp:+14155238886" and tw["data"]["To"] == "whatsapp:+919876543210" and tw["auth"] == ("AC1", "key")


def test_creating_a_draft_triggers_the_alert_and_settings_are_validated(monkeypatch, tmp_path):
    from technews import config, notify

    told = []
    monkeypatch.setattr(notify, "draft_ready", lambda headline, kind, image, **kw: told.append((kind, headline)))
    monkeypatch.setattr(settings, "APPROVAL_MODE", "all")
    pipeline.draft_flyer()
    assert told and told[0][0] == "flyer"

    monkeypatch.setattr(config, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(settings, "SCHEDULER_ENABLED", False)
    for name in ("NOTIFY_EMAIL_TO", "SMTP_PORT", "WHATSAPP_PROVIDER", "WHATSAPP_TO", "NOTIFY_WEBHOOK_URL", "PUBLIC_URL"):
        monkeypatch.setattr(settings, name, getattr(settings, name))
    client = _client()
    ok = client.post("/api/settings", json={"NOTIFY_EMAIL_TO": "a@b.co, c@d.in", "SMTP_PORT": 465, "WHATSAPP_PROVIDER": "twilio",
                                            "WHATSAPP_TO": "+91 98765 43210", "PUBLIC_URL": "https://post.cloudtrio.in/"})
    assert ok.status_code == 200 and settings.PUBLIC_URL == "https://post.cloudtrio.in"
    shown = client.get("/api/settings").get_json()["settings"]
    assert "NOTIFY_WEBHOOK_URL_set" in shown and client.get("/api/status").get_json()["notify"] == []
    for bad in ({"NOTIFY_EMAIL_TO": "nope"}, {"NOTIFY_WEBHOOK_URL": "http://x"}, {"WHATSAPP_PROVIDER": "sms"},
                {"WHATSAPP_TO": "12"}, {"SMTP_PORT": 70000}, {"PUBLIC_URL": "not a url"}):
        assert client.post("/api/settings", json=bad).status_code == 400, bad
    assert client.post("/api/settings/test-notify").get_json()["ok"] is False


def test_the_portal_is_an_installable_app(monkeypatch):
    from technews import create_app

    monkeypatch.setattr(settings, "SCHEDULER_ENABLED", False)
    anon = create_app().test_client()  # the files a phone fetches before anyone signs in
    m = anon.get("/manifest.webmanifest")
    assert m.status_code == 200 and m.mimetype == "application/manifest+json"
    data = m.get_json()
    assert data["display"] == "standalone" and data["start_url"] == "/#approvals"
    assert {i["purpose"] for i in data["icons"]} == {"any", "maskable"}
    for icon in data["icons"]:
        assert anon.get(icon["src"]).status_code == 200
    sw = anon.get("/sw.js")
    assert sw.status_code == 200 and sw.headers["Service-Worker-Allowed"] == "/" and b"addEventListener(\"push\"" in sw.data
    assert anon.get("/api/push/key").status_code == 401  # but only a signed-in device may turn alerts on
    for page in (_client().get("/").data, anon.get("/login").data):
        assert b'rel="manifest"' in page


def test_push_alerts_reach_every_device_and_forget_the_gone_ones(monkeypatch):
    import types

    import pywebpush

    from technews import notify, push

    client = _client()
    key = client.get("/api/push/key").get_json()["key"]
    assert len(key) == 87 and client.get("/api/push/key").get_json()["key"] == key  # a stable 65-byte public key
    assert client.post("/api/push/subscribe", json={"endpoint": "http://x", "keys": {}}).status_code == 400
    one = {"endpoint": "https://push.example/a", "keys": {"p256dh": "k", "auth": "a"}}
    two = {"endpoint": "https://push.example/b", "keys": {"p256dh": "k2", "auth": "a2"}}
    assert client.post("/api/push/subscribe", json=one).get_json()["devices"] == 1
    assert client.post("/api/push/subscribe", json=one).get_json()["devices"] == 1  # the same device is not added twice
    assert client.post("/api/push/subscribe", json=two).get_json()["devices"] == 2

    _no_channels(monkeypatch)
    assert notify.channels() == ["app"]
    seen = []

    def fake(subscription_info, data, vapid_private_key, vapid_claims, timeout, ttl):
        seen.append((subscription_info["endpoint"], vapid_claims["sub"]))
        assert ttl > 0  # Windows (Edge) answers 400 to a push that may not wait at all
        if subscription_info["endpoint"].endswith("/b"):
            raise pywebpush.WebPushException("gone", response=types.SimpleNamespace(status_code=410))

    monkeypatch.setattr(pywebpush, "webpush", fake)
    out = notify.send("Title", "Body line\nsecond")
    assert out == {"app": "sent"} and [e for e, _ in seen] == ["https://push.example/a", "https://push.example/b"]
    assert seen[0][1].startswith("mailto:")
    assert [s["endpoint"] for s in push.subscriptions()] == ["https://push.example/a"]  # the gone device was dropped
    assert client.post("/api/push/unsubscribe", json={"endpoint": one["endpoint"]}).get_json()["devices"] == 0


IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
IPAD = "Mozilla/5.0 (iPad; CPU OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
PIXEL = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36"
ANDROID_TABLET = "Mozilla/5.0 (Linux; Android 13; SM-X710) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
WINDOWS_EDGE = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0"
MAC_SAFARI = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15"


def test_the_device_is_told_apart_from_the_browsers_name():
    from technews import devices

    seen = {ua: devices.parse(ua) for ua in (IPHONE, IPAD, PIXEL, ANDROID_TABLET, WINDOWS_EDGE, MAC_SAFARI)}
    assert [(d["kind"], d["os"], d["browser"]) for d in seen.values()] == [
        ("phone", "iOS", "Safari"), ("tablet", "iPadOS", "Safari"), ("phone", "Android", "Chrome"),
        ("tablet", "Android", "Chrome"), ("desktop", "Windows", "Edge"), ("desktop", "macOS", "Safari")]  # fmt: skip
    assert devices.parse("curl/8", "?1")["kind"] == "phone" and devices.parse("")["kind"] == "desktop"
    assert seen[WINDOWS_EDGE]["label"] == "Edge on Windows · desktop"


def test_the_page_lays_itself_out_for_the_device_and_sign_ins_are_listed(monkeypatch):
    from technews import auth, create_app

    monkeypatch.setattr(settings, "SCHEDULER_ENABLED", False)
    app = create_app()
    auth.set_credentials("brijesh", "Admin@123")
    for ua, kind in ((IPHONE, "phone"), (IPAD, "tablet"), (WINDOWS_EDGE, "desktop")):
        client = app.test_client()
        assert client.post("/login", data={"username": "brijesh", "password": "Admin@123"}, headers={"User-Agent": ua}).status_code == 302
        page = client.get("/", headers={"User-Agent": ua}).data.decode()
        assert f'data-device="{kind}"' in page and 'id="more-btn"' in page and "This device: <b>" in page
    again = app.test_client()
    again.post("/login", data={"username": "brijesh", "password": "Admin@123"}, headers={"User-Agent": WINDOWS_EDGE})  # same device again
    from technews import devices

    rows = devices.recent()
    assert len(rows) == 3 and {r["kind"] for r in rows} == {"phone", "tablet", "desktop"}  # no duplicate row
    page = again.get("/", headers={"User-Agent": WINDOWS_EDGE}).data.decode()
    assert "Devices that signed in (3)" in page and "this device" in page and "Safari on iOS" in page


def test_every_flyer_layout_renders_at_full_size_with_the_longest_copy(tmp_path):
    import dataclasses

    from PIL import Image

    from technews import flyers

    longest = flyers.Flyer("x", "MAXIMUM LENGTH TAG LINE AB", "Microsoft 365, Google Workspace, Zoho and Adobe on one invoice for every team you run",
                           ("Migrate from your old mail server or cPanel hosting without any downtime at all for users",
                            "Each user on the plan they actually need, reviewed every quarter by our engineers",
                            "Businesses have seen 30-50% a year off email and productivity licences",
                            "A fourth point to prove four fit on the flyer"),
                           "Get a free licence review", "Savings vary with your current setup.", "c")  # fmt: skip
    assert len(flyers.LAYOUTS) >= 9
    for layout, _label in flyers.LAYOUTS:
        for flyer in (flyers.FLYERS[0], longest):
            out = flyers.render(dataclasses.replace(flyer, layout=layout), tmp_path / f"{layout}.jpg")
            assert Image.open(out).size == (1080, 1350)


def test_flyer_layout_is_saved_and_checked(monkeypatch):
    from technews import flyers

    monkeypatch.setattr(settings, "SCHEDULER_ENABLED", False)
    client = _client()
    new = {"kicker": "webinar", "headline": "Free AWS cost webinar", "points": "Live Q&A", "cta": "Reserve", "note": "",
           "caption": "Join us.", "enabled": True, "layout": "aurora"}
    made = client.post("/api/flyers", json=new).get_json()["id"]
    assert flyers.get(made).layout == "aurora"
    assert next(f for f in client.get("/api/flyers").get_json() if f["id"] == made)["layout"] == "aurora"
    assert client.post("/api/flyers", json={**new, "layout": "nope"}).status_code == 400
    assert client.post("/api/flyers", json={k: v for k, v in new.items() if k != "layout"}).status_code == 200  # default: classic
    assert "Aurora glass" in client.get("/").get_data(as_text=True)


def test_flyer_colours_override_the_layout_and_are_checked(monkeypatch):
    import dataclasses
    import io

    from PIL import Image

    from technews import flyers

    monkeypatch.setattr(settings, "SCHEDULER_ENABLED", False)
    client = _client()
    base = {"kicker": "webinar", "headline": "Free AWS cost webinar", "points": "Live Q&A", "cta": "Reserve a seat", "note": "",
            "caption": "Join us.", "enabled": True, "layout": "classic"}
    style = {"bg": "#102030", "bg2": "#102030", "text": "#ffffff", "accent": "#ff0066"}
    made = client.post("/api/flyers", json={**base, "style": style}).get_json()["id"]
    assert flyers.get(made).style == style
    assert next(f for f in client.get("/api/flyers").get_json() if f["id"] == made)["style"] == style
    assert client.post("/api/flyers", json={**base, "style": {"bg": "red"}}).status_code == 400  # not #rrggbb
    assert flyers.clean_style({"bg": "#ABCDEF", "nope": "#000000"}) == {"bg": "#abcdef"}  # unknown keys dropped

    plain = dataclasses.replace(flyers.FLYERS[0], layout="classic")
    painted = dataclasses.replace(plain, style=style)
    pixel = lambda f: Image.open(io.BytesIO((flyers.render(f, buf := io.BytesIO()), buf.getvalue())[1])).getpixel((20, 700))  # noqa: E731
    assert pixel(plain) != pixel(painted)  # the classic gradient became the flat colour chosen
    assert pixel(painted) == (16, 32, 48)


def test_studio_suggestions_and_layout_samples(monkeypatch):
    from technews import flyers

    monkeypatch.setattr(settings, "SCHEDULER_ENABLED", False)
    client = _client()
    risky = {"headline": "Save up to 40% on your cloud bill with our partner programme every single month of the year", "points": "a\nb\nc\nd",
             "cta": "Bill review", "note": "", "layout": "classic", "style": {"bg": "#ffffff", "text": "#eeeeee"}}
    out = client.post("/api/flyers/suggest", json=risky).get_json()
    texts = " ".join(c["text"] for c in out["checks"])
    assert "headline" in texts and "condition note" in texts and "contrast" in texts and "action word" in texts
    assert out["palettes"][0]["match"] and len(out["palettes"]) == len(flyers.PALETTES) and "ai" not in out
    assert flyers.contrast((0, 0, 0), (255, 255, 255)) > 20 and flyers.contrast((9, 9, 9), (10, 10, 10)) < 1.1

    monkeypatch.setattr(settings, "NVIDIA_API_KEY", "")
    assert "error" in client.post("/api/flyers/suggest", json={**risky, "ai": True}).get_json()["ai"]  # no key: a message, not a crash

    sample = client.get("/api/flyer-layout/bento.jpg")
    assert sample.mimetype == "image/jpeg" and sample.status_code == 200
    assert client.get("/api/flyer-layout/nope.jpg").status_code == 404


def test_ai_ideas_are_cleaned_before_they_reach_the_studio(monkeypatch):
    from technews import flyers

    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "chat_json", lambda **_: llm.ChatResult(
        {"headlines": ["Pay partner rates on AWS", "", "  Keep your setup, cut the bill  ", "x", "ignored fourth"], "layout": "not-a-layout",
         "palette": "gold", "why": "Savings suit gold.", "tips": ["Add a condition note", "", 5]}, "m", 1))
    ideas = flyers.ai_ideas({"headline": "Cut your AWS bill", "points": "a", "cta": "Get a review"})
    assert ideas["headlines"] == ["Pay partner rates on AWS", "Keep your setup, cut the bill", "x"]
    assert ideas["layout"] == "" and ideas["palette"] == "gold" and ideas["tips"] == ["Add a condition note", "5"]

    def down(**_):
        raise llm.LLMError("503")

    monkeypatch.setattr(llm, "chat_json", down)
    assert "did not answer" in flyers.ai_ideas({"headline": "x"})["error"]


def test_the_big_number_design_picks_the_figure_worth_showing():
    from technews import flyers

    def stat(headline, *points):
        return flyers._hero_stat(flyers.Flyer("x", "K", headline, points, "Go", "", "c"))

    assert stat("Up to USD 200,000 in cloud credits") == "USD 200,000"
    assert stat("Cut your bill", "Teams save an average of 40% on cloud spend") == "40%"
    assert stat("Right-sized licences", "Businesses have seen 30-50% a year off") == "30-50%"
    assert stat("Find the waste", "25+ optimisation rules across your accounts") == "25+"
    assert stat("Microsoft 365, Google Workspace and Zoho on one invoice") == ""  # 365 is a product name, not a result


def test_a_post_waiting_for_approval_can_change_design(monkeypatch):
    from pathlib import Path

    from technews import flyers

    monkeypatch.setattr(settings, "SCHEDULER_ENABLED", False)
    monkeypatch.setattr(settings, "APPROVAL_MODE", "all")
    client = _client()

    # a flyer draft: a new layout and colours for this post only
    post_id = pipeline.draft_flyer(flyers.get("bill"))["post_id"]
    before = db.rows("SELECT image_path FROM posts WHERE id = ?", post_id)[0]["image_path"]
    body = {"kicker": "billing", "headline": "Cut your AWS bill", "points": "One\nTwo", "cta": "Get a review", "note": "Terms apply.",
            "layout": "aurora", "style": {"accent": "#ff0066"}}  # fmt: skip
    assert client.post(f"/api/posts/{post_id}/design?preview=1", json=body).mimetype == "image/jpeg"
    assert db.rows("SELECT image_path FROM posts WHERE id = ?", post_id)[0]["image_path"] == before  # a preview saves nothing
    out = client.post(f"/api/posts/{post_id}/design", json=body).get_json()
    row = db.rows("SELECT * FROM posts WHERE id = ?", post_id)[0]
    assert Path(row["image_path"]).name == out["image"] != Path(before).name and Path(row["image_path"]).exists()
    assert row["headline"] == "Cut your AWS bill" and pipeline.draft_meta(row)["flyer"]["layout"] == "aurora"
    assert flyers.get("bill").layout == "classic"  # the saved flyer is untouched
    assert client.post(f"/api/posts/{post_id}/design", json={**body, "layout": "nope"}).status_code == 400
    assert client.get("/").status_code == 200 and b"Edit design" in client.get("/").data

    # a news draft: new words and a new tag
    story_id = db.execute("INSERT INTO stories (key, title, url, source, published_at, found_at, category, status, review_score) "
                          "VALUES ('k2', 'AWS launches a region', 'https://x.test/b', 'AWS News Blog', ?, ?, 'cloud', 'approved', 9)",
                          db.now(), db.now())  # fmt: skip
    news_id = pipeline.draft_story(story_id)["post_id"]
    assert pipeline.draft_meta(db.rows("SELECT * FROM posts WHERE id = ?", news_id)[0])["category"] == "cloud"
    edit = {"headline": "AWS opens a new region", "points": "Spain, first of its kind\nLower latency for users", "category": "devops"}
    assert client.post(f"/api/posts/{news_id}/design", json=edit).status_code == 200
    saved = db.rows("SELECT * FROM posts WHERE id = ?", news_id)[0]
    assert saved["headline"] == "AWS opens a new region" and pipeline.draft_meta(saved)["points"][0] == "Spain, first of its kind"
    assert client.post(f"/api/posts/{news_id}/design", json={**edit, "points": ""}).status_code == 400
    for layout in sorted(flyers.LAYOUT_IDS):  # a news card can use any flyer layout
        assert client.post(f"/api/posts/{news_id}/design?preview=1", json={**edit, "layout": layout}).mimetype == "image/jpeg"
    assert client.post(f"/api/posts/{news_id}/design", json={**edit, "layout": "bento"}).status_code == 200
    assert pipeline.draft_meta(db.rows("SELECT * FROM posts WHERE id = ?", news_id)[0])["layout"] == "bento"
    assert client.post(f"/api/posts/{news_id}/design", json={**edit, "layout": "nope"}).status_code == 400
    assert client.post(f"/api/posts/{news_id}/design", json={**edit, "category": "nope"}).status_code == 400

    # the writer can offer a fresh version without saving it
    monkeypatch.setattr(pipeline.writer, "write", lambda story, angle="": pipeline.writer.Draft("New headline", ["a", "b"], "New caption", ["tech"], "w"))
    fresh = client.post(f"/api/posts/{news_id}/rewrite").get_json()
    assert fresh["headline"] == "New headline" and "New caption" in fresh["caption"] and db.rows("SELECT headline FROM posts WHERE id = ?", news_id)[0]["headline"] == "AWS opens a new region"
    assert client.post(f"/api/posts/{post_id}/rewrite").status_code == 409  # a flyer has no story to rewrite

    # once decided, a post can no longer be edited
    assert pipeline.reject(news_id)["rejected"]
    assert client.post(f"/api/posts/{news_id}/design", json=edit).status_code == 409


def test_people_have_roles_and_the_log_shows_who_did_what(monkeypatch):
    from technews import users

    monkeypatch.setattr(settings, "SCHEDULER_ENABLED", False)
    admin = _client()
    assert admin.post("/api/users", json={"username": "asha", "password": "Approve-123", "role": "approver"}).status_code == 200
    assert admin.post("/api/users", json={"username": "vik", "password": "Viewer-1234", "role": "viewer"}).status_code == 200
    assert admin.post("/api/users", json={"username": "asha", "password": "Approve-123", "role": "viewer"}).status_code == 400  # taken

    def sign_in(name, pw):
        c = admin.application.test_client()
        assert c.post("/login", data={"username": name, "password": pw}).status_code == 302
        return c

    approver, viewer = sign_in("asha", "Approve-123"), sign_in("vik", "Viewer-1234")
    assert viewer.get("/").status_code == 200 and approver.get("/").status_code == 200
    assert approver.post("/api/settings", json={}).status_code == 403  # only admins change settings
    assert approver.get("/api/users").status_code == 403
    assert viewer.post("/api/cycle", json={}).status_code == 403
    assert viewer.post("/api/posts/999/reject", json={}).status_code == 403
    assert approver.post("/api/posts/999/reject", json={}).status_code != 403  # approvers may decide posts
    assert b"Team and roles" not in approver.get("/").data and b"Team and roles" in admin.get("/").data
    assert any(a["user"] == "asha" and a["action"] == "sign in" for a in users.recent())

    # the last admin cannot be removed or demoted
    users.delete(users.by_name("admin")["id"])  # the account made on first run
    me = users.by_name("brijesh")["id"]
    assert admin.post(f"/api/users/{me}", json={"role": "viewer"}).status_code == 400
    assert admin.delete(f"/api/users/{me}").status_code == 400
    # blocking someone signs them out
    vid = users.by_name("vik")["id"]
    assert admin.post(f"/api/users/{vid}", json={"active": False}).status_code == 200
    assert viewer.get("/").status_code == 302


def test_two_step_sign_in_asks_for_the_code(monkeypatch):
    from technews import auth, users

    monkeypatch.setattr(settings, "SCHEDULER_ENABLED", False)
    monkeypatch.setattr(auth, "_failures", {})
    assert users.code_ok("JBSWY3DPEHPK3PXP", users._code("JBSWY3DPEHPK3PXP", int(1_700_000_000 // 30)), now=1_700_000_000)
    client = _client()
    started = client.post("/api/two-step/start", json={}).get_json()
    assert client.post("/api/two-step/on", json={"code": "000000"}).status_code == 400
    good = users._code(started["secret"], int(__import__("time").time() // 30))
    assert client.post("/api/two-step/on", json={"code": good}).status_code == 200

    other = client.application.test_client()
    first = other.post("/login", data={"username": "brijesh", "password": "Admin@123"})
    assert first.status_code == 200 and b"authenticator" in first.data  # password right, code still needed
    assert other.get("/").status_code == 302
    assert other.post("/login", data={"code": "000000"}).status_code == 401
    assert other.post("/login", data={"code": good}).status_code == 302 and other.get("/").status_code == 200
    # an admin can turn it off for everyone
    client.post("/api/security", json={"two_step": "off"})
    third = client.application.test_client()
    assert third.post("/login", data={"username": "brijesh", "password": "Admin@123"}).status_code == 302


def test_backups_copy_the_database_and_keep_only_the_newest(monkeypatch):
    from technews import backup

    import shutil

    if db.postgres() and not shutil.which("pg_dump"):
        pytest.skip("pg_dump is not installed here")
    monkeypatch.setattr(settings, "SCHEDULER_ENABLED", False)
    client = _client()
    assert client.post("/api/backups/settings", json={"enabled": True, "hours": 24, "keep": 2}).status_code == 200
    assert client.post("/api/backups/settings", json={"enabled": True, "hours": 0, "keep": 2}).status_code == 400
    import time

    for _ in range(3):
        client.post("/api/backups/run", json={})
        time.sleep(1.1)  # file names carry the second
    files = client.get("/api/backups").get_json()["files"]
    assert len(files) == 2 and files[0]["mb"] > 0
    assert backup.verify(backup.folder() / files[0]["name"]) == "ok"
    assert backup.options()["check"].endswith(" ok")
    if not db.postgres():
        import sqlite3

        copy = sqlite3.connect(backup.folder() / files[0]["name"])
        assert copy.execute("SELECT count(*) FROM users").fetchone()[0] >= 1


def test_secrets_in_env_are_sealed_with_a_key_and_open_again(monkeypatch, tmp_path):
    from cryptography.fernet import Fernet

    from technews import config, secretbox

    env = tmp_path / ".env"
    env.write_text('NVIDIA_API_KEY=nvapi-plain-123\nIG_USERNAME=cloudtrio\nLINKEDIN_ACCESS_TOKEN="tok en/with=odd"\n', encoding="utf-8")
    monkeypatch.setattr(config, "ENV_PATH", env)
    assert config.seal_env() == 0 and "nvapi-plain-123" in env.read_text()  # no key: nothing changes
    monkeypatch.setenv("SECRETS_KEY", Fernet.generate_key().decode())
    assert config.seal_env() == 2
    text = env.read_text()
    assert "nvapi-plain-123" not in text and "tok en" not in text and "IG_USERNAME=cloudtrio" in text
    status = config.secrets_status()
    assert status == {"key_set": True, "sealed": 2, "plain": 0}
    from dotenv import dotenv_values

    assert secretbox.reveal(dotenv_values(env)["NVIDIA_API_KEY"]) == "nvapi-plain-123"
    assert secretbox.reveal(dotenv_values(env)["LINKEDIN_ACCESS_TOKEN"]) == "tok en/with=odd"
    monkeypatch.setenv("SECRETS_KEY", Fernet.generate_key().decode())  # a different key cannot open them
    assert secretbox.reveal(dotenv_values(env)["NVIDIA_API_KEY"]) == ""
    # saving a secret from the dashboard seals it too
    monkeypatch.setenv("SECRETS_KEY", Fernet.generate_key().decode())
    config._write_env({"NVIDIA_API_KEY": "nvapi-new-456"})
    assert "nvapi-new-456" not in env.read_text() and secretbox.reveal(dotenv_values(env)["NVIDIA_API_KEY"]) == "nvapi-new-456"


def test_a_second_ai_service_answers_when_nvidia_cannot(monkeypatch):
    from technews.agents import llm

    monkeypatch.setattr(settings, "NVIDIA_API_KEY", "nv")
    monkeypatch.setattr(settings, "LLM2_BASE_URL", "https://other.test/v1")
    monkeypatch.setattr(settings, "LLM2_API_KEY", "k2")
    monkeypatch.setattr(settings, "LLM2_MODEL", "m2")
    calls = []

    def fake(system, user, model, max_tokens, temperature, *, base="", key="", extras=True):
        calls.append((model, base))
        if not base:
            raise llm.LLMError("HTTP 503")
        return llm.ChatResult({"ok": True}, model, 1)

    monkeypatch.setattr(llm, "_chat_once", fake)
    assert llm.chat_json(system="s", user="u").model == "m2"
    assert calls[-1] == ("m2", "https://other.test/v1") and len(calls) == len(llm.model_chain()) + 1
    monkeypatch.setattr(settings, "NVIDIA_API_KEY", "")  # no NVIDIA key at all: the second service alone is enough
    assert llm.available() and llm.chat_json(system="s", user="u").model == "m2"
    monkeypatch.setattr(settings, "LLM2_API_KEY", "")
    assert not llm.available()


def test_the_watchdog_alerts_once_when_cycles_stop_and_again_when_they_return(monkeypatch):
    import json

    from technews import notify, watchdog

    sent = []
    monkeypatch.setattr(notify, "send", lambda title, text, image=None: sent.append(title) or {})
    monkeypatch.setattr(settings, "WATCHDOG_MINUTES", 60)
    old = (datetime.now(UTC) - timedelta(minutes=90)).isoformat()
    db.kv_set(pipeline.LAST_CYCLE_KEY, json.dumps({"finished_at": old}))
    assert watchdog.check()["state"] == "alerted" and watchdog.check()["state"] == "stale"
    assert len(sent) == 1  # one alert, not one every ten minutes
    db.kv_set(pipeline.LAST_CYCLE_KEY, json.dumps({"finished_at": db.now()}))
    assert watchdog.check()["state"] == "recovered" and len(sent) == 2
    monkeypatch.setattr(settings, "WATCHDOG_MINUTES", 0)
    assert watchdog.check()["state"] == "off"


def test_brand_rules_block_words_and_flag_numbers_the_source_does_not_have(monkeypatch):
    from technews import rules

    monkeypatch.setattr(settings, "BLOCKED_WORDS", "lawsuit, Rival Corp")
    assert rules.blocked("Rival Corp launches a thing") == "rival corp" and rules.blocked("A sue-able event") == ""
    story = {"title": "AWS opens a region in Spain", "summary": "The 3 new zones cost 1.2 billion"}
    assert rules.warnings("AWS opens 3 zones", ["Costs 1.2 billion"], story) == []
    assert "99" in rules.warnings("AWS cuts latency by 99%", [], story)[0]

    monkeypatch.setattr(settings, "SCHEDULER_ENABLED", False)
    monkeypatch.setattr(settings, "APPROVAL_MODE", "all")
    sid = db.execute("INSERT INTO stories (key, title, url, source, published_at, found_at, category, status, review_score, summary) "
                     "VALUES ('kb', 'Lawsuit hits cloud vendor', 'https://x.test/c', 'AWS News Blog', ?, ?, 'cloud', 'approved', 9, 's')", db.now(), db.now())  # fmt: skip
    assert pipeline.post_next()["posted"] is False
    assert "blocked word" in db.story(sid)["review_reason"]


def test_the_team_can_leave_notes_on_a_waiting_post(monkeypatch):
    from technews import flyers

    monkeypatch.setattr(settings, "SCHEDULER_ENABLED", False)
    monkeypatch.setattr(settings, "APPROVAL_MODE", "all")
    client = _client()
    post_id = pipeline.draft_flyer(flyers.get("bill"))["post_id"]
    out = client.post(f"/api/posts/{post_id}/notes", json={"text": "Check the claim with legal"}).get_json()
    assert out["notes"][0]["text"] == "Check the claim with legal" and out["notes"][0]["user"] == "brijesh"
    assert client.post(f"/api/posts/{post_id}/notes", json={"text": " "}).status_code == 400
    assert b"Check the claim with legal" in client.get("/").data


def test_the_team_board_shows_who_is_working_and_what_each_agent_did(monkeypatch):
    from technews.agents import team

    monkeypatch.setattr(settings, "SCHEDULER_ENABLED", False)
    assert set(team.board()) == set(team.ROSTER)
    assert team.board()["writer"]["state"] == "new" and team.board()["writer"]["last"] is None
    with team.run("writer") as info:
        info["summary"] = "Wrote a headline"
        assert team.board()["writer"]["state"] == "working"  # seen from another process through the database
    with pytest.raises(RuntimeError), team.run("editor"):
        raise RuntimeError("model unreachable")
    board = team.board()
    assert board["writer"]["state"] == "idle" and board["writer"]["jobs"] == 1 and board["writer"]["hours"][-1] == 1
    assert board["writer"]["last"]["summary"] == "Wrote a headline"
    assert board["editor"]["state"] == "attention" and board["editor"]["failed"] == 1
    team.set_enabled("designer", False)
    assert team.board()["designer"]["state"] == "off"
    data = _client().get("/api/agents").get_json()
    assert data["board"]["writer"]["jobs"] == 1 and any(r["agent"] == "writer" for r in data["activity"])


def test_festival_flyers_are_fetched_and_made_by_themselves(monkeypatch):
    from technews import festivals

    monkeypatch.setattr(settings, "SCHEDULER_ENABLED", False)
    monkeypatch.setattr(settings, "APPROVAL_MODE", "all")
    client = _client()  # starts the app, which creates the first brand
    today = datetime.now().date()
    diwali = today + timedelta(days=2)  # so Dhanteras, two days before, is today
    ics = ("BEGIN:VCALENDAR\r\n"
           f"BEGIN:VEVENT\r\nDTSTART;VALUE=DATE:{diwali:%Y%m%d}\r\nSUMMARY:Diwali/Deepavali\r\nEND:VEVENT\r\n"
           f"BEGIN:VEVENT\r\nDTSTART;VALUE=DATE:{today + timedelta(days=30):%Y%m%d}\r\nSUMMARY:Hazarat Ali's Birthday\r\nEND:VEVENT\r\n"
           "END:VCALENDAR\r\n")

    class Reply:
        text, content = ics, ics.encode()

        def raise_for_status(self):
            pass

    monkeypatch.setattr(festivals.requests, "get", lambda *a, **k: Reply())
    assert festivals.refresh(force=True)["events"] == 1  # only the festivals we know are kept
    names = {k: d for d, k in festivals.dates(today, 5)}
    assert names["diwali"] == diwali and names["dhanteras"] == today
    made = festivals.run()["made"]
    assert any(m.startswith("Dhanteras") for m in made) and any(m.startswith("Diwali") for m in made)
    post = db.rows("SELECT * FROM posts WHERE flyer LIKE 'fest-dhanteras-%'")[0]
    assert post["status"] == "pending" and "#HappyDhanteras" in post["caption"] and "Happy Dhanteras" in post["headline"]
    assert festivals.run()["made"] == []  # not twice
    festivals.set_enabled("diwali", False)
    assert not festivals.enabled("diwali")
    items = {i["key"]: i for i in client.get("/api/festivals").get_json()["items"]}
    assert items["dhanteras"]["post_id"] == post["id"] and items["diwali"]["on"] is False
    assert client.post("/api/festivals/nonsense", json={"enabled": False}).status_code == 400


def test_a_failed_festival_fetch_keeps_working_with_fixed_dates(monkeypatch):
    from technews import festivals

    monkeypatch.setattr(settings, "SCHEDULER_ENABLED", False)

    def boom(*a, **k):
        raise festivals.requests.ConnectionError("offline")

    monkeypatch.setattr(festivals.requests, "get", boom)
    assert festivals.refresh(force=True)["ok"] is False
    assert {"republic", "independence", "christmas"} <= {k for _, k in festivals.dates(datetime.now().date(), 400)}


def test_settings_live_in_the_database_so_a_second_process_sees_changes(monkeypatch):
    from technews import config

    monkeypatch.setattr(settings, "SCHEDULER_ENABLED", False)
    monkeypatch.setattr(settings, "POSTS_PER_DAY", 6)
    client = _client()
    assert client.post("/api/settings", json={"POSTS_PER_DAY": 9, "IG_DELIVERY": "dry_run"}).status_code == 200
    assert db.kv_get("setting:POSTS_PER_DAY") == "9"
    # the scheduler process has its own copy in memory: it starts from the database and then follows it
    monkeypatch.setattr(settings, "POSTS_PER_DAY", 1)
    monkeypatch.setattr(config, "_applied", {})
    monkeypatch.setattr(config, "_seen_version", "")
    assert "POSTS_PER_DAY" in config.refresh(force=True) and settings.POSTS_PER_DAY == 9
    db.kv_set("setting:POSTS_PER_DAY", "4")
    db.kv_set(config.VERSION_KEY, "changed-elsewhere")
    assert config.refresh() == ["POSTS_PER_DAY"] and settings.POSTS_PER_DAY == 4
    assert config.refresh() == []  # nothing new
    # a secret is sealed in the database when a key is set
    from cryptography.fernet import Fernet

    monkeypatch.setenv("SECRETS_KEY", Fernet.generate_key().decode())
    client.post("/api/settings", json={"NVIDIA_API_KEY": "nvapi-secret-1"})
    assert db.kv_get("setting:NVIDIA_API_KEY").startswith("enc:v1:") and settings.NVIDIA_API_KEY == "nvapi-secret-1"


def test_only_one_process_runs_a_cycle_at_a_time():
    assert db.kv_claim("lease:x", 60) is True and db.kv_claim("lease:x", 60) is False
    db.kv_release("lease:x")
    assert db.kv_claim("lease:x", 60) is True
    db.kv_set("pipeline:cycle_lease", "1", ttl=600)  # another process is mid-cycle
    assert pipeline.run_cycle() == {"skipped": "a cycle is already running"}
    db.kv_release("pipeline:cycle_lease")


def test_a_failed_cycle_is_tried_again(monkeypatch):
    from apscheduler.schedulers.background import BackgroundScheduler

    from technews import scheduler

    sched = BackgroundScheduler(timezone="Asia/Kolkata")
    monkeypatch.setattr(scheduler, "_scheduler", sched)
    monkeypatch.setattr(scheduler, "_retries", 0)
    results = iter([{"error": "boom"}, {"error": "boom"}, {"ok": 1}])
    monkeypatch.setattr(pipeline, "run_cycle", lambda **_: next(results))
    scheduler.cycle()
    assert sched.get_job("cycle_retry") is not None and scheduler._retries == 1
    scheduler.cycle()
    assert scheduler._retries == 2
    scheduler.cycle()
    assert scheduler._retries == 0


def test_the_sqlite_database_can_be_copied_into_postgresql(tmp_path, monkeypatch):
    url = os.environ.get("TEST_DATABASE_URL", "")
    if not url:
        pytest.skip("needs TEST_DATABASE_URL")
    import psycopg

    from technews import migrate

    monkeypatch.setattr(settings, "DATABASE_URL", "")  # build a SQLite database first
    db.init()
    sid = db.execute("INSERT INTO stories (key, title, url, source, published_at, found_at) VALUES ('m1','Hello','https://x.test','S',?,?)", db.now(), db.now())
    db.execute("INSERT INTO posts (story_id, headline, caption, image_path, writer, delivered, status, created_at) VALUES (?,?,?,?,?,?,?,?)",
               sid, "h", "c", "i.jpg", "w", "dry_run", "posted", db.now())  # fmt: skip
    db.kv_set("a", "b")
    _reset_postgres(url)
    counts = migrate.to_postgres(settings.DB_PATH, url)
    assert counts["stories"] == 1 and counts["posts"] == 1 and counts["kv"] >= 1
    with pytest.raises(RuntimeError):
        migrate.to_postgres(settings.DB_PATH, url)  # it will not overwrite by accident
    monkeypatch.setattr(settings, "DATABASE_URL", url)
    db.close()
    assert db.rows("SELECT title FROM stories")[0]["title"] == "Hello" and db.kv_get("a") == "b"
    assert db.execute("INSERT INTO stories (key, title, url, source, published_at, found_at) VALUES ('m2','Next','https://x.test/2','S',?,?)", db.now(), db.now()) > sid


def test_each_brand_has_its_own_settings_flyers_and_posts(monkeypatch):
    from technews import brands, flyers

    monkeypatch.setattr(settings, "SCHEDULER_ENABLED", False)
    monkeypatch.setattr(settings, "APPROVAL_MODE", "all")
    monkeypatch.setattr(settings, "IG_HANDLE", "cloudtrio.in")
    client = _client()
    out = client.post("/api/brands", json={"name": "Acme", "handle": "@acme", "ig_username": "acme_ig", "ig_password": "pw-1",
                                           "hashtags": "#acme", "categories": "ai", "posts_per_day": 2}).get_json()  # fmt: skip
    acme = out["id"]
    assert acme > 1 and all(b["ig_password"] == "" for b in out["brands"]) and any(b["ig_password_set"] for b in out["brands"])
    assert client.post("/api/brands", json={"name": ""}).status_code == 400

    with brands.use(acme):  # code running for Acme sees Acme's values, and only inside
        assert settings.IG_HANDLE == "acme" and settings.IG_USERNAME == "acme_ig" and settings.POSTS_PER_DAY == 2
        assert settings.IG_PASSWORD == "pw-1" and settings.IG_SESSION_PATH.name == f"ig_session_{acme}.json"
        assert settings.LINKEDIN_ENABLED is False
    assert settings.IG_HANDLE == "cloudtrio.in" and settings.IG_SESSION_PATH.name == "ig_session.json"

    # flyers and posts are kept apart
    assert len(flyers.listing()) == 7
    assert client.post("/api/brand", json={"id": acme}).status_code == 200
    assert client.get("/api/flyers").get_json() in ([], {"flyers": []}) or len(client.get("/api/insights").get_json()["flyers"]) == 0
    saved = client.post("/api/flyers", json={"kicker": "SALE", "headline": "Acme sale", "points": "One", "cta": "Buy", "note": "Terms", "caption": "Acme caption"}).get_json()
    assert client.post("/api/brand", json={"id": 1}).status_code == 200
    assert len(client.get("/api/insights").get_json()["flyers"]) == 7
    assert len(flyers.listing()) == 7 and any(r["brand_id"] == acme for r in db.rows("SELECT brand_id FROM flyers"))

    # a story goes to the brand whose tags include it
    brands.save(1, {"name": "CloudTrio", "categories": "cloud,devops"})
    sid = db.execute("INSERT INTO stories (key, title, url, source, published_at, found_at, category, status, review_score, summary) "
                     "VALUES ('kb2', 'New model beats benchmark', 'https://x.test/ai', 'Wired', ?, ?, 'ai', 'approved', 9, 's')", db.now(), db.now())  # fmt: skip
    monkeypatch.setattr(pipeline, "scan", lambda: {})
    monkeypatch.setattr(pipeline, "review", lambda: {})
    import threading

    monkeypatch.setattr(pipeline, "LOCK", threading.Lock())  # a cycle left running in the background by an earlier test must not block this one
    db.kv_release(pipeline.CYCLE_LEASE_KEY)
    out = pipeline.run_cycle()
    mine = db.rows("SELECT brand_id, status FROM posts WHERE story_id = ?", sid)
    assert mine == [{"brand_id": acme, "status": "pending"}] and set(out["brands"]) == {"CloudTrio", "Acme"}, out
    # the daily limit counts each brand's own posts
    with brands.use(acme):
        assert pipeline.pending_count() == 1
    assert pipeline.pending_count() == 0
    # a brand with posts cannot be removed; the first brand never
    assert client.delete(f"/api/brands/{acme}").status_code == 400 and client.delete("/api/brands/1").status_code == 400


def test_a_waiting_post_can_be_scheduled_and_goes_out_at_its_time(monkeypatch):
    from technews import flyers

    monkeypatch.setattr(settings, "SCHEDULER_ENABLED", False)
    monkeypatch.setattr(settings, "APPROVAL_MODE", "all")
    client = _client()
    post_id = pipeline.draft_flyer(flyers.get("bill"))["post_id"]
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%dT10:30")
    assert client.post(f"/api/posts/{post_id}/schedule", json={"when": "2020-01-01T10:00"}).status_code == 400
    assert client.post(f"/api/posts/{post_id}/schedule", json={"when": "soon"}).status_code == 400
    assert client.post(f"/api/posts/{post_id}/schedule", json={"when": tomorrow}).status_code == 200
    row = db.rows("SELECT status, scheduled_at FROM posts WHERE id = ?", post_id)[0]
    assert row["status"] == "scheduled" and row["scheduled_at"]
    assert pipeline.publish_due() == []  # not yet
    month = datetime.now().strftime("%Y-%m")
    month2 = (datetime.now() + timedelta(days=1)).strftime("%Y-%m")
    items = client.get(f"/api/calendar?month={month2}").get_json()["items"]
    assert any(i["id"] == post_id and i["status"] == "scheduled" for i in items) and client.get("/api/calendar?month=nope").status_code == 400
    assert client.post(f"/api/posts/{post_id}/unschedule").status_code == 200
    assert db.rows("SELECT status FROM posts WHERE id = ?", post_id)[0]["status"] == "pending"
    client.post(f"/api/posts/{post_id}/schedule", json={"when": tomorrow})
    db.execute("UPDATE posts SET scheduled_at = ? WHERE id = ?", (datetime.now(UTC) - timedelta(minutes=1)).isoformat(timespec="seconds"), post_id)
    done = pipeline.publish_due()
    assert done and done[0]["posted"] and db.rows("SELECT status FROM posts WHERE id = ?", post_id)[0]["status"] == "posted"
    assert any(i["status"] == "posted" for i in client.get(f"/api/calendar?month={month}").get_json()["items"])


def test_another_brand_logo_can_be_uploaded(monkeypatch):
    import io

    from PIL import Image

    from technews import brand, brands

    monkeypatch.setattr(settings, "SCHEDULER_ENABLED", False)
    client = _client()
    acme = client.post("/api/brands", json={"name": "Acme Labs"}).get_json()["id"]
    png = io.BytesIO()
    Image.new("RGBA", (200, 100), (255, 0, 0, 255)).save(png, "PNG")
    assert client.post(f"/api/brands/{acme}/logo", data={"logo": (io.BytesIO(png.getvalue()), "logo.png")}).status_code == 200
    assert client.post(f"/api/brands/{acme}/logo", data={"logo": (io.BytesIO(b"not an image"), "x.png")}).status_code == 400
    assert client.post("/api/brands/1/logo", data={"logo": (io.BytesIO(png.getvalue()), "logo.png")}).status_code == 400
    with brands.use(acme):
        wide = brand.lockup(84)
    assert wide.height == 84 and wide.width > 84  # logo plus the brand's name


def test_the_designer_chooses_a_layout_and_learns_from_your_switches(monkeypatch):
    from technews.agents import designer, team

    monkeypatch.setattr(settings, "SCHEDULER_ENABLED", False)
    monkeypatch.setattr(settings, "APPROVAL_MODE", "all")
    story = {"category": "devops", "title": "t"}
    # it rotates among the strongest few, so the feed keeps changing
    assert designer.candidates("devops", "Kubernetes ships a fix")[0] == "console"  # what suits the topic
    assert designer.choose(story, "Kubernetes ships a fix", [])["layout"] in designer.candidates("devops", "Kubernetes ships a fix")[:4]
    assert designer.candidates("ai", "AI model cuts costs by 40%")[0] == "impact"  # a figure in the headline
    for _ in range(3):  # the editor keeps switching devops cards to bento
        team.feedback("layout_changed", 1, **{"from": "console", "to": "bento", "category": "devops"})
    assert designer.candidates("devops", "Kubernetes ships a fix")[0] == "bento"
    team.set_enabled("designer", False)
    assert designer.choose(story, "x", [])["layout"] == ""  # off means the original card
    team.set_enabled("designer", True)
    # a drafted post carries the designer's choice and the card is drawn in that layout
    sid = db.execute("INSERT INTO stories (key, title, url, source, published_at, found_at, category, status, review_score, summary, angle) "
                     "VALUES ('kd', 'Kubernetes ships a fix', 'https://x.test/k', 'The New Stack', ?, ?, 'devops', 'approved', 9, 'A fix', 'a')", db.now(), db.now())  # fmt: skip
    post_id = pipeline.draft_story(sid)["post_id"]
    meta = pipeline.draft_meta(db.rows("SELECT * FROM posts WHERE id = ?", post_id)[0])
    assert meta["layout"] in designer.candidates("devops", "Kubernetes ships a fix")[:4] and meta["designer"]


def test_the_quality_editor_flags_hype_and_numbers_the_source_lacks(monkeypatch):
    from technews.agents import editor, team

    story = {"title": "Cloud vendor opens a region", "summary": "It opens one region."}
    notes = editor.review("SHOCKING: Vendor DESTROYS rivals with 99 regions", ["Insane growth"], "You won't believe it!!!", story)
    assert any("Hype" in n for n in notes) and any("99" in n for n in notes)
    assert editor.review("Vendor opens a region", ["One region"], "It opens one region.", story) == []
    team.set_enabled("editor", False)
    assert editor.review("SHOCKING", [], "", story) == []


def test_decisions_are_remembered_and_the_trainer_turns_them_into_rules(monkeypatch):
    from technews import flyers
    from technews.agents import analyst, team, trainer

    monkeypatch.setattr(settings, "SCHEDULER_ENABLED", False)
    monkeypatch.setattr(settings, "APPROVAL_MODE", "all")
    client = _client()
    assert trainer.learn()["skipped"]  # too early
    for i in range(6):  # approve and reject a run of posts, shortening headlines on the way
        sid = db.execute("INSERT INTO stories (key, title, url, source, published_at, found_at, category, status, review_score, summary, angle) "
                         "VALUES (?, ?, 'https://x.test/a', 'S', ?, ?, 'ai', 'approved', 9, 's', 'a')", f"kt{i}", f"Story {i}", db.now(), db.now())  # fmt: skip
        pid = pipeline.draft_story(sid)["post_id"]
        if i % 3 == 2:
            assert client.post(f"/api/posts/{pid}/reject", json={}).status_code == 200
        else:
            client.post(f"/api/posts/{pid}/design", json={"headline": "Short one", "points": "A fact", "category": "ai", "layout": "stage"})
            assert client.post(f"/api/posts/{pid}/approve", json={"caption": "My own caption"}).status_code == 200
    counts = team.feedback_counts()
    assert counts["approved"] == 4 and counts["rejected"] == 2 and counts["edited"] == 4
    report = analyst.report()
    assert report["decisions"] == 6 and report["approval_rate"] == 0.67
    out = trainer.learn()
    assert out["proposed"]  # rules were suggested, not applied
    pending = team.proposals()
    assert pending and team.playbook(pending[0]["agent"]) == ""
    assert team.decide(pending[0]["id"], True) and team.playbook(pending[0]["agent"])
    # the writer now reads what it learned and what you approved
    ctx = team.writer_context()
    assert "HOUSE STYLE" in ctx or "APPROVED" in ctx
    # the API shows the whole team
    data = client.get("/api/agents").get_json()
    assert {a["id"] for a in data["agents"]} >= {"writer", "designer", "editor", "analyst", "trainer"} and data["activity"]
    assert client.post("/api/agents/designer", json={"enabled": False}).status_code == 200 and client.post("/api/agents/scanner", json={"enabled": False}).status_code == 400
    assert client.post("/api/agents/writer", json={"playbook": "- Keep it short"}).status_code == 200 and team.playbook("writer") == "- Keep it short"
    team.set_auto_learn(True)
    assert trainer.learn() is not None and client.post("/api/agents/analyst/run").status_code == 200


def test_every_agent_suggests_on_a_waiting_post_and_on_each_page(monkeypatch):
    from technews import flyers
    from technews.agents import advice

    monkeypatch.setattr(settings, "SCHEDULER_ENABLED", False)
    monkeypatch.setattr(settings, "APPROVAL_MODE", "all")
    client = _client()
    sid = db.execute("INSERT INTO stories (key, title, url, source, published_at, found_at, category, status, review_score, summary, angle) "
                     "VALUES ('ka', 'Vendor opens 3 regions: a very long explanation of why this matters to everyone', 'https://x.test/v', 'The New Stack', ?, ?, 'cloud', 'approved', 9, 'Opens 3 regions.', 'a')", db.now(), db.now())  # fmt: skip
    post_id = pipeline.draft_story(sid)["post_id"]
    flyer_post = pipeline.draft_flyer(flyers.get("bill"))["post_id"]

    out = client.get(f"/api/posts/{post_id}/suggestions").get_json()["suggestions"]
    by_agent = {s["agent"] for s in out}
    assert {"writer", "designer", "editor", "analyst"} <= by_agent
    headline = next(s for s in out if s["agent"] == "writer" and s["action"]["kind"] == "headline")
    layout = next(s for s in out if s["agent"] == "designer" and s["action"]["kind"] == "layout")
    when = next(s for s in out if s["action"] and s["action"]["kind"] == "schedule")
    assert len(headline["action"]["value"].split()) < 14 and layout["action"]["value"] in flyers.LAYOUT_IDS and "T" in when["action"]["value"]
    # applying a suggestion goes through the normal edit, and the suggestions refresh
    meta = pipeline.draft_meta(db.rows("SELECT * FROM posts WHERE id = ?", post_id)[0])
    body = {"headline": headline["action"]["value"], "points": "\n".join(meta["points"]), "category": meta["category"], "layout": layout["action"]["value"]}
    assert client.post(f"/api/posts/{post_id}/design", json=body).status_code == 200
    assert client.post(f"/api/posts/{post_id}/schedule", json={"when": when["action"]["value"]}).status_code == 200
    assert client.post("/api/suggestions/used", json={"agent": "writer", "kind": "headline", "post": post_id}).status_code == 200
    from technews.agents import team

    assert team.feedback_counts()["suggestion_used"] == 1
    flyer_advice = client.get(f"/api/posts/{flyer_post}/suggestions").get_json()["suggestions"]
    assert any(s["agent"] == "designer" and "Classic" in s["title"] for s in flyer_advice)

    for area in ("overview", "stories", "flyers", "sources", "calendar", "posts", "settings"):
        resp = client.get(f"/api/suggestions?area={area}")
        assert resp.status_code == 200 and isinstance(resp.get_json()["suggestions"], list), area
    assert any(s["agent"] == "designer" for s in advice.for_area("flyers"))  # all flyers still on Classic
    assert any("AI key" in s["title"] for s in advice.for_area("settings"))
    assert advice.for_area("nope") == []
    page = client.get("/").data
    assert b'data-area="flyers"' in page and b"post-advice" in page


def test_the_source_scout_tracks_feeds_and_suggests_changes(monkeypatch):
    import requests

    from technews.agents import scout

    monkeypatch.setattr(settings, "SCHEDULER_ENABLED", False)
    client = _client()
    sources.seed()
    rows = {r["name"]: r for r in sources.listing() if r["kind"] == "feed"}
    broken, fine = rows["The New Stack"], rows["AWS News Blog"]

    def fake_feed(source, url):
        if source == "The New Stack":
            raise requests.HTTPError("403 Client Error: Forbidden")
        return [item("Something", source=source)]

    monkeypatch.setattr(sources, "_fetch_feed", fake_feed)
    monkeypatch.setattr(sources, "_fetch_search", lambda q: [])
    for _ in range(3):
        sources.fetch_all(timedelta(hours=12))
    listing = {r["name"]: r for r in client.get("/api/sources").get_json() if r["kind"] == "feed"}
    assert listing["The New Stack"]["health"]["state"] == "slow" and listing["The New Stack"]["health"]["fails"] == 3 and "403" in listing["The New Stack"]["health"]["error"]
    assert listing["AWS News Blog"]["health"]["state"] == "ok" and listing["AWS News Blog"]["health"]["items"] == 1
    assert scout.retirements() == []  # a few failures are not enough
    old = (datetime.now(UTC) - timedelta(days=4)).isoformat(timespec="seconds")
    db.execute("UPDATE source_health SET first_fail_at = ? WHERE source_id = ?", old, broken["id"])

    # new feeds are tested before they are suggested
    good = "https://good.test/feed"
    monkeypatch.setattr(scout, "CANDIDATES", (("Good Feed", good), ("Dead Feed", "https://dead.test/feed")))
    xml = ('<rss><channel><item><title>Fresh news</title><link>https://good.test/a</link><pubDate>'
           + datetime.now(UTC).strftime("%a, %d %b %Y %H:%M:%S +0000") + "</pubDate></item></channel></rss>")

    def fake_get(url, **params):
        if url == good:
            return xml
        raise requests.ConnectionError("down")

    monkeypatch.setattr(sources, "_get", fake_get)
    out = scout.run()
    assert out["suggested"] == 2
    ideas = {i["action"]: i for i in client.get("/api/scout").get_json()["ideas"]}
    assert ideas["add"]["name"] == "Good Feed" and ideas["disable"]["name"] == "The New Stack"
    assert scout.run()["suggested"] == 0  # the same ideas are not repeated
    assert client.post(f"/api/scout/ideas/{ideas['add']['id']}", json={"accept": True}).status_code == 200
    assert client.post(f"/api/scout/ideas/{ideas['disable']['id']}", json={"accept": True}).status_code == 200
    now = {r["name"]: r for r in sources.listing()}
    assert now["Good Feed"]["enabled"] and not now["The New Stack"]["enabled"] and fine["id"]
    assert client.post(f"/api/scout/ideas/{ideas['add']['id']}", json={"accept": True}).status_code == 409

    # automatic mode does it without asking, and never deletes
    scout.set_auto(True)
    monkeypatch.setattr(scout, "CANDIDATES", (("Second Good", "https://good.test/feed"),))
    db.execute("UPDATE sources SET enabled = 1 WHERE name = 'The New Stack'")
    assert scout.run()["switched_off"] == ["The New Stack"]
    assert any(r["name"] == "The New Stack" and not r["enabled"] for r in sources.listing())
    assert "scout" in {a["id"] for a in client.get("/api/agents").get_json()["agents"]}


def test_an_idea_or_a_link_becomes_flyers_for_the_business(monkeypatch):
    import io as _io

    from technews import flyers
    from technews.agents import creator, llm

    monkeypatch.setattr(settings, "SCHEDULER_ENABLED", False)
    monkeypatch.setattr(settings, "APPROVAL_MODE", "all")
    client = _client()
    assert client.post("/api/create/generate", json={}).status_code == 400  # nothing to work from

    # no AI key: one plain option built from the words given
    out = client.post("/api/create/generate", json={"idea": "We now offer a free cloud cost review for startups. We find idle servers. We show what to fix first.", "goal": "promote"}).get_json()
    assert out["ai"] is False and len(out["variants"]) == 1 and out["variants"][0]["preview"].startswith("data:image/jpeg;base64,")
    sent = client.post("/api/create/send", json={"idea_id": out["idea_id"], "variant": out["variants"][0]}).get_json()
    assert flyers.get(sent["flyer_id"]).enabled is False  # a one-off: not in the rotation unless asked
    row = db.rows("SELECT status, flyer FROM posts WHERE id = ?", sent["post_id"])[0]
    assert row["status"] == "pending" and row["flyer"] == sent["flyer_id"]
    assert db.rows("SELECT status, post_id FROM ideas WHERE id = ?", out["idea_id"])[0] == {"status": "sent", "post_id": sent["post_id"]}

    # with an AI model: three different angles, each drawn, and claims the idea does not support are flagged
    def fake_chat(**_kw):
        mk = lambda a, h: {"angle": a, "kicker": "COST", "headline": h, "points": ["Idle servers found", "Oversized databases found", "A fix-first list"], "cta": "Book a review", "note": "Free review.", "caption": "Hook\nTwo lines\nWrite to info@cloudtrio.in"}  # noqa: E731
        return llm.ChatResult({"variants": [mk("problem", "Your cloud bill hides waste"), mk("benefit", "Cut your cloud bill by 99%"), mk("how", "How our cost review works")]}, "m", 1)

    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "chat_json", fake_chat)
    out = client.post("/api/create/generate", json={"idea": "Free cloud cost review for startups", "tone": "bold"}).get_json()
    assert out["ai"] and len(out["variants"]) == 3 and len({v["layout"] for v in out["variants"]}) == 3
    assert any("99" in w for w in out["variants"][1]["warnings"]) and not out["variants"][0]["warnings"]

    # a link: only public pages are read
    for bad in ("http://127.0.0.1/admin", "http://localhost:5000/", "ftp://example.com/x", "not a link"):
        assert client.post("/api/create/generate", json={"url": bad}).status_code == 400, bad

    class FakeRaw:
        def read(self, n, decode_content=True):
            return b"<html><head><title>Cloud cost review</title><meta name='description' content='A free review of your cloud bill.'></head><body><script>x()</script><p>We look for idle servers and oversized databases in your AWS account.</p></body></html>"

    class FakeResp:
        is_redirect, status_code, headers, encoding, raw = False, 200, {}, "utf-8", FakeRaw()

    monkeypatch.setattr(creator, "_public", lambda host: True)
    monkeypatch.setattr(creator.requests, "get", lambda *a, **k: FakeResp())
    page = creator.fetch_page("https://example.com/review")
    assert page["title"] == "Cloud cost review" and "idle servers" in page["text"] and "x()" not in page["text"]
    assert client.post("/api/create/generate", json={"url": "https://example.com/review"}).get_json()["page"]["title"] == "Cloud cost review"
    assert client.get("/api/create/history").get_json()["ideas"]


def test_two_processes_can_start_on_postgresql_at_once(tmp_path, monkeypatch):
    url = os.environ.get("TEST_DATABASE_URL", "")
    if not url:
        pytest.skip("needs TEST_DATABASE_URL")
    import threading

    import psycopg

    monkeypatch.setattr(settings, "DATABASE_URL", url)
    db.close()
    _reset_postgres(url)
    errors: list[Exception] = []

    def start() -> None:
        try:
            db.init()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=start) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == []


# --------------------------------------------------------------------------- the improvement round


def test_a_feed_that_blocks_the_server_is_read_through_google_news(monkeypatch):
    import requests

    calls = []

    def fake_get(url, **params):
        calls.append((url, params))
        if "thenewstack.io" in url:
            resp = requests.Response()
            resp.status_code = 403
            raise requests.HTTPError("403 Client Error", response=resp)
        return ("<rss><channel><item><title>Kubernetes adds a thing - The New Stack</title><link>https://news.google.com/a</link>"
                "<pubDate>Fri, 06 Nov 2026 10:00:00 GMT</pubDate><source url='https://thenewstack.io'>The New Stack</source></item></channel></rss>")

    monkeypatch.setattr(sources, "_get", fake_get)
    items = sources._fetch_feed("The New Stack", "https://thenewstack.io/feed/")
    assert [i.title for i in items] == ["Kubernetes adds a thing"] and items[0].source == "The New Stack"
    assert calls[1][1]["q"].startswith("site:thenewstack.io")

    def broken(url, **params):
        resp = requests.Response()
        resp.status_code = 500
        raise requests.HTTPError("500", response=resp)

    monkeypatch.setattr(sources, "_get", broken)
    with pytest.raises(requests.HTTPError):  # a real failure still counts as one
        sources._fetch_feed("The New Stack", "https://thenewstack.io/feed/")


def test_festival_wording_language_preview_and_reminder(monkeypatch):
    from technews import card, festivals, notify

    monkeypatch.setattr(settings, "SCHEDULER_ENABLED", False)
    monkeypatch.setattr(settings, "APPROVAL_MODE", "all")
    client = _client()
    # your own wording replaces the built-in one, and blank puts it back
    assert client.post("/api/festivals/diwali/text", json={"headline": "Happy Diwali, friends", "meaning": "Light up the year"}).get_json()["headline"] == "Happy Diwali, friends"
    assert client.post("/api/festivals/diwali/text", json={"headline": "x" * 61, "meaning": ""}).status_code == 400
    assert festivals.flyer_for("diwali", 2026).headline == "Happy Diwali, friends"
    assert client.post("/api/festivals/diwali/text", json={"headline": "", "meaning": ""}).get_json()["headline"] == "Happy Diwali"
    # the preview draws a picture and saves nothing
    before = len(db.rows("SELECT id FROM flyers"))
    shot = client.get("/api/festivals/diwali/preview.jpg")
    assert shot.status_code == 200 and shot.data[:2] == b"\xff\xd8" and len(db.rows("SELECT id FROM flyers")) == before
    assert client.get("/api/festivals/nope/preview.jpg").status_code == 404
    # Hindi or Gujarati goes in as one more line, and picks the font of its script
    assert client.post("/api/festivals/settings", json={"lang": "klingon"}).status_code == 400
    assert client.post("/api/festivals/settings", json={"lang": "hi"}).get_json()["lang"] == "hi"
    flyer = festivals.flyer_for("diwali", 2026)
    assert "दीपावली" in flyer.points[1] and "दीपावली" in flyer.caption and card.script_of(" ".join(flyer.points)) == "deva"
    festivals.set_language("gu")
    assert card.script_of(festivals.flyer_for("diwali", 2026).points[1]) == "gujr"
    assert card.script_of("English only") == ""
    assert client.get("/api/festivals/diwali/preview.jpg").status_code == 200  # and it draws
    festivals.set_language("en")
    # an unapproved flyer for today is chased once a day, and not once it is decided
    today = datetime.now().date()
    monkeypatch.setattr(festivals, "dates", lambda start=None, days=400: [(today, "republic")])
    post_id = festivals.make("republic", today.year)["post_id"]
    sent = []
    monkeypatch.setattr(notify, "send", lambda title, text, image=None: sent.append(title) or {})
    assert len(festivals.remind()) == 1 and sent == ["CloudTrio: approve the Republic Day flyer"]
    assert festivals.remind() == []
    db.execute("UPDATE posts SET status = 'posted' WHERE id = ?", post_id)
    db.kv_set("festival:1:republic:%d:reminded:%s" % (today.year, today.isoformat()), "")
    assert festivals.remind() == []  # approved: nothing to chase


def test_a_backup_is_sent_away_sealed_and_a_failure_is_told(monkeypatch, tmp_path):
    import sys
    import types

    from cryptography.fernet import Fernet

    from technews import backup, notify

    monkeypatch.setattr(settings, "SCHEDULER_ENABLED", False)
    _client()
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("SECRETS_KEY", key)
    (tmp_path / "ig_session.json").write_text('{"uuids": {"phone_id": "abc"}}', encoding="utf-8")
    assert backup.offsite_configured() == []
    assert backup.run()["offsite"] == ""  # nothing set up: nothing sent, nothing said

    away = tmp_path / "elsewhere"
    monkeypatch.setattr(settings, "BACKUP_COPY_DIR", str(away))
    out = backup.run()
    assert out["offsite"].endswith("ok: folder")
    names = sorted(f.name for f in away.iterdir())
    assert any(n.startswith("newsroom-") and n.endswith(".enc") for n in names) and "ig_session.json.enc" in names
    assert not any(n.endswith(".db") for n in names)  # only sealed files leave the server
    assert b"phone_id" in Fernet(key.encode()).decrypt((away / "ig_session.json.enc").read_bytes())

    uploaded, deleted = [], []

    class FakeS3:
        def put_object(self, Bucket, Key, Body):  # noqa: N803 - boto3's names
            uploaded.append((Bucket, Key))

        def list_objects_v2(self, Bucket, Prefix):  # noqa: N803
            return {"Contents": [{"Key": f"database/newsroom-{i}.db.enc"} for i in range(5)]}

        def delete_objects(self, Bucket, Delete):  # noqa: N803
            deleted.extend(o["Key"] for o in Delete["Objects"])

    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=lambda *a, **k: FakeS3()))
    for name, value in (("BACKUP_S3_BUCKET", "b"), ("BACKUP_S3_ID", "id"), ("BACKUP_S3_SECRET", "sec")):
        monkeypatch.setattr(settings, name, value)
    db.kv_set(backup.KEEP_KEY, "2")
    assert backup.run()["offsite"].endswith("ok: bucket and folder")
    assert any(k.startswith("database/newsroom-") and k.endswith(".enc") for _, k in uploaded) and ("b", "sessions/ig_session.json.enc") in uploaded
    assert len(deleted) == 3  # only the newest two stay in the bucket

    told = []
    monkeypatch.setattr(notify, "send", lambda title, text, image=None: told.append(title) or {})

    class Broken:
        def put_object(self, **kw):
            raise OSError("bucket gone")

    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=lambda *a, **k: Broken()))
    assert "FAILED" in backup.run()["offsite"] and told == ["CloudTrio newsroom: the off-site backup failed"]
    assert "FAILED" in backup.options()["offsite"]["last"]


def test_the_monitor_alerts_after_two_misses_pings_the_outside_service_and_says_when_back(monkeypatch):
    import requests

    from technews import monitor, notify

    _client()
    monkeypatch.setattr(settings, "IG_DELIVERY", "instagram")
    monkeypatch.setattr(settings, "PUBLIC_URL", "https://post.example.test")
    monkeypatch.setattr(settings, "HEARTBEAT_URL", "https://hc.example.test/ping/abc")
    told, seen = [], []
    monkeypatch.setattr(notify, "send", lambda title, text, image=None: told.append(title) or {})
    state = {"up": False}

    class Reply:
        def __init__(self, code):
            self.status_code = code

        def raise_for_status(self):
            pass

    def fake_get(url, timeout=0, **kw):
        seen.append(url)
        if url.startswith("https://post.example.test") and not state["up"]:
            raise requests.ConnectionError("down")
        return Reply(200)

    monkeypatch.setattr(monitor.requests, "get", fake_get)
    assert monitor.tick()["state"] == "ok" and told == []  # one miss is not enough
    assert monitor.tick()["state"] == "down" and told == ["CloudTrio newsroom: the site cannot be reached"]
    assert monitor.tick()["state"] == "down" and len(told) == 1  # said once
    assert not any("hc.example.test" in u for u in seen)  # no "still alive" while something is wrong
    state["up"] = True
    out = monitor.tick()
    assert out["state"] == "ok" and out["heartbeat"] == "sent" and told[-1] == "CloudTrio newsroom: the site is back"
    assert any("hc.example.test" in u for u in seen)
    monkeypatch.setattr(settings, "IG_DELIVERY", "dry_run")
    assert monitor.tick() == {"state": "skipped"}  # staging stays quiet


def test_the_instagram_session_and_two_step_keys_are_stored_sealed(monkeypatch):
    from cryptography.fernet import Fernet

    from technews import instagram, secretbox, users

    monkeypatch.setattr(settings, "SCHEDULER_ENABLED", False)
    client = _client()
    me = users.by_name("brijesh")
    # before a key exists everything stays plain, and sealing later converts it
    users.set_totp(me["id"], "JBSWY3DPEHPK3PXP")
    session_file = settings.DATA_DIR / "ig_session.json"
    instagram.write_session(session_file, {"cookies": {"sessionid": "123:abc"}})
    assert "sessionid" in session_file.read_text(encoding="utf-8") and db.rows("SELECT totp FROM users WHERE id = ?", me["id"])[0]["totp"] == "JBSWY3DPEHPK3PXP"
    monkeypatch.setenv("SECRETS_KEY", Fernet.generate_key().decode())
    assert users.seal_secrets() == 1 and instagram.seal_saved_sessions() == 1
    assert secretbox.sealed(db.rows("SELECT totp FROM users WHERE id = ?", me["id"])[0]["totp"])
    assert "sessionid" not in session_file.read_text(encoding="utf-8")
    assert instagram.read_session(session_file)["cookies"]["sessionid"] == "123:abc"
    assert users.totp_secret(users.get(me["id"])) == "JBSWY3DPEHPK3PXP"
    assert users.seal_secrets() == 0 and instagram.seal_saved_sessions() == 0  # once is enough
    # a sealed 2-step key still lets the person in with a code
    now = 1_700_000_000
    assert users.code_ok(users.totp_secret(users.get(me["id"])), users._code("JBSWY3DPEHPK3PXP", now // 30), now)
    # a different key cannot open it, and says so
    monkeypatch.setenv("SECRETS_KEY", Fernet.generate_key().decode())
    with pytest.raises(instagram.InstagramPublishError):
        instagram.read_session(session_file)
    assert client.get("/api/users").status_code == 200


def test_sign_in_with_google_needs_an_account_with_that_email(monkeypatch):
    import base64
    import json
    import time
    from urllib.parse import parse_qs, urlparse

    from technews import sso, users

    monkeypatch.setattr(settings, "SCHEDULER_ENABLED", False)
    monkeypatch.setattr(settings, "PUBLIC_URL", "https://post.example.test")
    admin = _client()
    fresh = admin.application.test_client()
    assert fresh.get("/login/google").status_code == 404 and b"Sign in with Google" not in fresh.get("/login").data  # off until set up
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_SECRET", "secret")
    assert b"Sign in with Google" in fresh.get("/login").data
    me = users.by_name("brijesh")
    assert admin.post(f"/api/users/{me['id']}", json={"email": "not an email"}).status_code == 400
    assert admin.post(f"/api/users/{me['id']}", json={"email": "Brijesh@Example.com"}).status_code == 200
    assert {u["username"]: u["email"] for u in admin.get("/api/users").get_json()["users"]}["brijesh"] == "brijesh@example.com"

    def token(email, *, nonce, aud="cid.apps.googleusercontent.com", verified=True, exp=None):
        claims = {"aud": aud, "iss": "https://accounts.google.com", "exp": exp or time.time() + 300, "nonce": nonce, "email": email, "email_verified": verified}
        return "h." + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=") + ".s"

    class Reply:
        ok = True

        def __init__(self, id_token):
            self.id_token = id_token

        def json(self):
            return {"id_token": self.id_token}

    def attempt(client, email, **kw):
        go = client.get("/login/google")
        assert go.status_code == 302 and go.headers["Location"].startswith("https://accounts.google.com/")
        query = parse_qs(urlparse(go.headers["Location"]).query)
        assert query["redirect_uri"] == ["https://post.example.test/login/google/callback"]
        nonce = kw.pop("nonce", query["nonce"][0])
        monkeypatch.setattr(sso.requests, "post", lambda *a, **k: Reply(token(email, nonce=nonce, **kw)))
        return client.get("/login/google/callback", query_string={"code": "c", "state": query["state"][0]})

    ok = attempt(fresh, "brijesh@example.com")
    assert ok.status_code == 302 and ok.headers["Location"] == "/" and fresh.get("/api/users").status_code == 200  # signed in
    assert any(a["action"] == "sign in with Google" for a in users.recent(10))

    for label, kw, email in (("a stranger", {}, "stranger@example.com"), ("wrong audience", {"aud": "other"}, "brijesh@example.com"),
                             ("unverified", {"verified": False}, "brijesh@example.com"), ("expired", {"exp": time.time() - 10}, "brijesh@example.com"),
                             ("wrong nonce", {"nonce": "x"}, "brijesh@example.com")):  # fmt: skip
        other = admin.application.test_client()
        done = attempt(other, email, **kw)
        assert done.status_code == 302 and "sso_error" in done.headers["Location"], label
        assert other.get("/api/users").status_code == 401, label  # never signed in
    # a callback that did not start here is refused
    stray = admin.application.test_client()
    assert "sso_error" in stray.get("/login/google/callback", query_string={"code": "c", "state": "s"}).headers["Location"]


def test_results_show_what_the_audience_did_and_a_banner_warns_when_nobody_is_told(monkeypatch):
    from technews.agents import analyst

    monkeypatch.setattr(settings, "SCHEDULER_ENABLED", False)
    client = _client()
    empty = client.get("/api/results").get_json()
    assert empty["posts"] == 0 and empty["top"] == [] and empty["best_hours"] == []
    base = datetime(2026, 11, 3, 10, 0, tzinfo=UTC)
    for i, (likes, comments, flyer, hour) in enumerate([(40, 4, "", 4), (10, 0, "", 9), (30, 2, "bill", 4), (5, 1, "", 9), (22, 3, "", 4), (8, 0, "", 9)], start=1):
        created = base.replace(hour=hour, minute=0).isoformat(timespec="seconds")
        pid = db.execute("INSERT INTO posts (story_id, headline, caption, image_path, writer, delivered, media_id, status, created_at, flyer, brand_id) "
                         "VALUES (0,?,?,?,?,?,?,'posted',?,?,1)", f"Post {i}", "c", "x.jpg", "w", "instagram", f"m{i}", created, flyer)
        db.execute("INSERT INTO metrics (post_id, likes, comments, at) VALUES (?,?,?,?)", pid, likes, comments, db.now())
    out = client.get("/api/results").get_json()
    assert out["posts"] == 6 and out["top"][0]["headline"] == "Post 1" and out["top"][0]["likes"] == 40
    assert out["by_kind"]["flyer"] == {"posts": 1, "average": 32.0} and out["by_kind"]["news"]["posts"] == 5
    assert out["best_hours"][0]["average"] >= out["best_hours"][-1]["average"] and len(out["best_hours"]) == 2
    page = client.get("/").data
    assert b"results-card" in page and b"No one is told when a post is waiting" in page  # nothing is set up to tell anyone
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", "1")
    assert b"No one is told when a post is waiting" not in client.get("/").data
    assert analyst.results()["collecting"] is False


def test_telegram_alert_has_buttons_and_only_the_set_chat_can_decide(monkeypatch):
    import json

    from technews import notify, telegram

    _client()
    monkeypatch.setattr(settings, "APPROVAL_MODE", "all")
    monkeypatch.setattr(settings, "IG_DELIVERY", "instagram")
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", "555")
    monkeypatch.setattr(settings, "PUBLIC_URL", "https://post.test")
    monkeypatch.setattr(notify, "draft_ready", lambda *a, **kw: None)
    post_id = pipeline.draft_flyer()["post_id"]

    # the alert: what it is, the caption, and the two buttons
    posted = []
    monkeypatch.setattr(notify.push, "subscriptions", lambda: [])
    monkeypatch.setattr(notify.requests, "post", lambda url, **kw: posted.append((url, kw)) or type("R", (), {"ok": True})())
    notify.send("t", "b", None, post_id)
    sent = next(kw["data"] for url, kw in reversed(posted) if url.endswith("/sendMessage") and "reply_markup" in kw.get("data", {}))
    keys = json.loads(sent["reply_markup"])["inline_keyboard"][0]
    assert [k["callback_data"] for k in keys] == [f"ap:{post_id}", f"rj:{post_id}"]


    calls = []
    monkeypatch.setattr(telegram, "_api", lambda method, **data: calls.append((method, data)) or {"ok": True})
    published = []
    monkeypatch.setattr(pipeline.instagram, "publish_photo", lambda image, text: published.append(text) or {"delivered": "instagram", "media_id": "1", "permalink": "https://ig.test/p/1"})
    msg = {"chat": {"id": 555}, "message_id": 9, "photo": [1], "caption": "Flyer"}

    # a tap from some other chat does nothing
    telegram.handle({"id": "c0", "data": f"ok:{post_id}", "message": {**msg, "chat": {"id": 999}}})
    assert not published and db.rows("SELECT status FROM posts WHERE id = ?", post_id)[0]["status"] == "pending"

    # Approve only asks "Yes, post it now"; the yes publishes
    telegram.handle({"id": "c1", "data": f"ap:{post_id}", "message": msg})
    assert not published and any("Yes, post it now" in d.get("reply_markup", "") for _, d in calls)
    telegram.handle({"id": "c2", "data": f"ok:{post_id}", "message": msg})
    assert len(published) == 1 and db.rows("SELECT status FROM posts WHERE id = ?", post_id)[0]["status"] == "posted"
    assert any("Approved and posted" in d.get("caption", "") for m, d in calls if m == "editMessageCaption")
    telegram.handle({"id": "c3", "data": f"ok:{post_id}", "message": msg})  # a second tap posts nothing more
    assert len(published) == 1

    # reject, and the polling remembers where it got to
    other = pipeline.draft_flyer()["post_id"]
    updates = {"ok": True, "result": [{"update_id": 40, "callback_query": {"id": "c4", "data": f"rj:{other}", "message": msg}}, {"update_id": 41, "message": {}}]}
    monkeypatch.setattr(telegram, "_api", lambda method, **data: updates if method == "getUpdates" else {"ok": True})
    assert telegram.poll() == 1 and db.rows("SELECT status FROM posts WHERE id = ?", other)[0]["status"] == "rejected"
    assert db.kv_get(telegram.OFFSET_KEY) == "42"
    monkeypatch.setattr(settings, "IG_DELIVERY", "dry_run")
    assert telegram.poll() == 0  # staging does not listen


def test_a_posted_post_can_have_its_caption_changed_be_reposted_and_be_deleted(monkeypatch):
    from technews import instagram

    client = _client()
    monkeypatch.setattr(settings, "APPROVAL_MODE", "all")
    monkeypatch.setattr(instagram, "dry_run", lambda: False)
    calls, ids = [], iter(["111", "222", "333"])

    class FakeIG:
        def media_edit(self, media_id, caption): calls.append(("edit", media_id, caption)); return {}
        def media_delete(self, media_id): calls.append(("delete", media_id)); return True

    monkeypatch.setattr(instagram, "get_client", lambda: FakeIG())
    monkeypatch.setattr(pipeline.instagram, "publish_photo", lambda image, text: {"delivered": "instagram", "media_id": next(ids), "permalink": "https://ig.test/p/x/"})
    first = pipeline.draft_flyer()["post_id"]
    assert pipeline.approve(first)["posted"]

    # the caption changes on Instagram and here; an empty one is refused
    assert client.post(f"/api/posts/{first}/caption", json={"caption": "  Better words  "}).status_code == 200
    assert calls[-1] == ("edit", "111", "Better words") and db.rows("SELECT caption FROM posts WHERE id = ?", first)[0]["caption"] == "Better words"
    assert client.post(f"/api/posts/{first}/caption", json={"caption": " "}).status_code == 409

    # fix and repost: a copy waits in Approvals; once it is up, the old one comes down
    out = client.post(f"/api/posts/{first}/repost", json={"replace": True}).get_json()
    copy = out["post_id"]
    row = db.rows("SELECT * FROM posts WHERE id = ?", copy)[0]
    assert out["replaces"] and row["status"] == "pending" and row["caption"] == "Better words" and row["image_path"] != db.rows("SELECT image_path FROM posts WHERE id = ?", first)[0]["image_path"]
    assert client.post(f"/api/posts/{copy}/repost").status_code == 409  # already waiting
    done = pipeline.approve(copy)
    assert done["posted"] and done["replaced"] == "deleted" and ("delete", "111") in calls
    assert db.rows("SELECT status FROM posts WHERE id = ?", first)[0]["status"] == "deleted"
    assert client.post(f"/api/posts/{first}/caption", json={"caption": "x"}).status_code == 409  # gone

    # delete straight away; a deleted post can be posted again, without replacing anything
    assert client.post(f"/api/posts/{copy}/delete").status_code == 200 and ("delete", "222") in calls
    assert client.post(f"/api/posts/{copy}/delete").status_code == 409
    again = client.post(f"/api/posts/{copy}/repost", json={"replace": True}).get_json()
    assert again["ok"] and not again["replaces"]

    # Instagram refusing leaves the post as it was
    third = pipeline.draft_flyer()["post_id"]
    pipeline.approve(third)
    monkeypatch.setattr(FakeIG, "media_delete", lambda self, media_id: (_ for _ in ()).throw(RuntimeError("login_required")))
    bad = client.post(f"/api/posts/{third}/delete")
    assert bad.status_code == 409 and "login_required" in bad.get_json()["reason"]
    assert db.rows("SELECT status FROM posts WHERE id = ?", third)[0]["status"] == "posted"

    # the Posts page offers the buttons
    page = client.get("/").get_data(as_text=True)
    assert "managePost(this, 'caption')" in page and 'id="mp-dlg"' in page


def test_analytics_reads_the_numbers_counts_followers_passes_on_comments_and_reports_the_week(monkeypatch):
    import json as _json

    from technews import analytics, instagram, notify

    client = _client()
    monkeypatch.setattr(settings, "APPROVAL_MODE", "all")
    monkeypatch.setattr(settings, "IG_DELIVERY", "instagram")
    monkeypatch.setattr(settings, "COLLECT_METRICS", True)
    told = []
    monkeypatch.setattr(notify, "send", lambda title, text, image=None, post_id=0: told.append((title, text)) or {"telegram": "sent"})
    monkeypatch.setattr(notify, "channels", lambda: ["telegram"])
    monkeypatch.setattr(notify, "draft_ready", lambda *a, **kw: None)
    ids = iter(range(1, 50))
    monkeypatch.setattr(pipeline.instagram, "publish_photo", lambda image, text: {"delivered": "instagram", "media_id": str(next(ids)), "permalink": "https://ig.test/p/x/"})
    posted = []
    for _ in range(3):
        pid = pipeline.draft_flyer()["post_id"]
        assert pipeline.approve(pid)["posted"]
        posted.append(pid)
    db.execute("UPDATE posts SET meta = ? WHERE id = ?", _json.dumps({"reel": {"video": "r.mp4", "cover": "c.jpg"}}), posted[2])
    state = {"comments": 1}

    class Media:
        def __init__(self, media_id):
            self.like_count, self.comment_count, self.play_count = 10 * int(media_id), state["comments"], 500 if media_id == "3" else 0

    class FakeIG:
        user_id = 42
        def media_info(self, media_id): return Media(media_id)
        def insights_media(self, media_id): return {"inline_insights_node": {"metrics": {"reach_count": 100 * int(media_id), "save_count": 2, "share_count": {"value": 3}}}}
        def user_info(self, user_id): return type("U", (), {"follower_count": 1234, "following_count": 10, "media_count": 25})()

    monkeypatch.setattr(instagram, "get_client", lambda: FakeIG())
    assert analytics.collect() == 3 and not told  # the first look sets the baseline: no comment alert
    row = db.rows("SELECT * FROM metrics WHERE post_id = ?", posted[2])[0]
    assert (row["likes"], row["plays"], row["reach"], row["saves"], row["shares"]) == (30, 500, 300, 2, 3)
    state["comments"] = 3
    analytics.collect()
    assert len(told) == 3 and told[0][0] == "CloudTrio: 2 new comments on Instagram"

    assert analytics.snapshot_followers() == 1234 and analytics.followers_today()
    out = client.get("/api/analytics?days=7").get_json()
    assert out["followers"]["now"] == 1234 and out["measured"] == 3 and out["total"]["reach"] == 600
    assert set(out["by_kind"]) == {"flyer", "reel"} and out["by_kind"]["reel"]["average"] == 38  # 30 likes + 3 comments + 2 saves + 3 shares
    assert out["insights"] == "ok" and out["engagement_rate"] is not None

    # a personal account: no insights, the basic numbers still come in
    monkeypatch.setattr(FakeIG, "insights_media", lambda self, media_id: (_ for _ in ()).throw(RuntimeError("not a business account")))
    analytics.collect()
    assert "business" in client.get("/api/analytics").get_json()["insights"]

    told.clear()
    analytics.weekly_report()
    assert told and told[0][0] == "CloudTrio: your Instagram week" and "Followers: 1234" in told[0][1]
    monkeypatch.setattr(settings, "IG_DELIVERY", "dry_run")
    assert analytics.collect() == 0  # staging reads nothing
    assert 'id="an-kpis"' in client.get("/").get_data(as_text=True)


def test_a_draft_becomes_a_reel_with_music_and_is_posted_as_a_reel(monkeypatch):
    from technews import motion, notify

    if not motion.available():
        pytest.skip("no ffmpeg here")
    client = _client()
    monkeypatch.setattr(settings, "APPROVAL_MODE", "all")
    monkeypatch.setattr(settings, "REEL_SECONDS", 6)
    alerts = []
    monkeypatch.setattr(notify, "draft_ready", lambda headline, kind, image, **kw: alerts.append((kind, image.suffix)))

    # REELS_PER_WEEK: the first draft of the day becomes a Reel, the next one does not
    monkeypatch.setattr(settings, "REELS_PER_WEEK", 3)
    monkeypatch.setattr(settings, "REEL_MUSIC", "none")
    first = pipeline.draft_flyer()["post_id"]
    second = pipeline.draft_flyer()["post_id"]
    assert alerts == [("animated flyer", ".mp4"), ("flyer", ".jpg")]
    reel = pipeline.draft_meta(db.rows("SELECT meta FROM posts WHERE id = ?", first)[0])["reel"]
    assert (settings.CARDS_DIR / reel["video"]).stat().st_size > 10_000 and (settings.CARDS_DIR / reel["cover"]).exists()

    # from Approvals: make the second one a Reel with the custom track, then back to a picture
    bad = client.post("/api/reels/music", data={"file": (io.BytesIO(b"x" * 20_000), "song.exe")}, content_type="multipart/form-data")
    assert bad.status_code == 400
    wav = motion.music_dir() / "t.wav"
    import wave as _wave
    with _wave.open(str(wav), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000); w.writeframes(b"\0\1" * 16000)
    assert client.post("/api/reels/music", data={"file": (io.BytesIO(wav.read_bytes()), "song.wav")}, content_type="multipart/form-data").status_code == 200
    out = client.post(f"/api/posts/{second}/reel", json={"on": True, "style": "countup", "track": "custom"}).get_json()
    assert out["ok"] and out["style"] == "countup" and out["track"] == "custom"
    # the animated text: a preset, with the words to highlight and the number chosen
    out = client.post(f"/api/posts/{second}/reel", json={"on": True, "style": "offer", "track": "none", "highlight": "AWS, bill", "number": "40%"}).get_json()
    assert out["ok"] and out["style"] == "offer" and out["highlight"] == ["AWS", "bill"] and out["number"] == "40%"
    assert (settings.CARDS_DIR / out["video"]).stat().st_size > 10_000
    assert client.post(f"/api/posts/{second}/reel", json={"on": False}).get_json()["ok"]
    assert "reel" not in pipeline.draft_meta(db.rows("SELECT meta FROM posts WHERE id = ?", second)[0])

    # editing the design keeps it a Reel and redoes the video
    data = pipeline.draft_meta(db.rows("SELECT meta FROM posts WHERE id = ?", first)[0])["flyer"]
    assert pipeline.edit_draft(first, {**data, "headline": "Cut your cloud bill by 30%"})["ok"]
    redone = pipeline.draft_meta(db.rows("SELECT meta FROM posts WHERE id = ?", first)[0])["reel"]
    assert redone["video"] != reel["video"]

    # approving posts the video as a Reel
    sent = []
    monkeypatch.setattr(pipeline.instagram, "publish_reel", lambda video, cover, text: sent.append((video.suffix, cover.suffix)) or {"delivered": "instagram", "media_id": "9", "permalink": "https://ig.test/reel/x/"})
    assert pipeline.approve(first)["posted"] and sent == [(".mp4", ".jpg")]
    page = client.get("/").get_data(as_text=True)
    assert "Animated posts (Reels)" in page and "Make it a Reel" in page

    assert motion.number_in("Save 40% on AWS")[1:] == (40.0, "%") and motion.number_in("AWS in 2026") is None


def test_a_reel_alert_goes_to_telegram_as_a_video(monkeypatch, tmp_path):
    from technews import notify

    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", "555")
    video = tmp_path / "r.mp4"
    video.write_bytes(b"\0" * 100)
    calls = []
    monkeypatch.setattr(notify.requests, "post", lambda url, **kw: calls.append((url, kw)) or type("R", (), {"ok": True})())
    notify._telegram("t", "b", video, 7)
    url, kw = calls[-1]
    assert url.endswith("/sendVideo") and "video" in kw["files"] and kw["data"]["supports_streaming"] == "true"


def test_news_posts_may_become_reels_too(monkeypatch):
    monkeypatch.setattr(settings, "APPROVAL_MODE", "all")
    news = db.execute("INSERT INTO posts (story_id, headline, caption, image_path, writer, delivered, status, created_at, brand_id) "
                      "VALUES (0,?,?,?,?,?,'pending',?,1)", "AWS cuts S3 prices by 40%", "c", "x.jpg", "w", "dry_run", db.now())
    out = pipeline.set_reel(news, True)
    assert "Only a marketing flyer" not in str(out.get("reason", ""))  # news is no longer turned away


def test_the_animated_text_finds_what_sells():
    from technews import flyers, kinetic

    assert kinetic.figure("Up to USD 200,000 in cloud credits")[:3] == ("USD ", 200000.0, "")
    assert kinetic.figure("Save 40% on AWS")[1:3] == (40.0, "%")
    assert kinetic.figure("AWS in 2026", "3 easy steps") is None
    words = "Up to USD 200,000 in cloud credits".split()
    assert {words[i] for i in kinetic.highlights(" ".join(words))} == {"USD", "200,000", "credits"}
    assert {words[i] for i in kinetic.highlights(" ".join(words), ["cloud"])} == {"cloud"}
    film = kinetic.Film(flyers.FLYERS[1], preset="offer", bpm=112)
    beat = 60 / 112
    assert [n for n, _ in film.scenes] == ["intro", "hook", "points", "offer", "cta"]
    assert all(abs(d / beat - round(d / beat)) < 1e-6 for _, d in film.scenes)  # every cut falls on a beat
    assert film.frame(film.cover_at).size == (1080, 1920)
    assert kinetic.auto_preset(flyers.FLYERS[1]) == "offer"


def test_instagram_logins_survive_the_missing_password_key_and_an_encoded_sessionid(monkeypatch):
    from instagrapi.exceptions import TwoFactorRequired

    from technews import instagram, notify

    # the older login route encrypts with a key Instagram no longer serves: it now sends the "version 0" form
    client = instagram.new_client()
    asked = []
    monkeypatch.setattr(client.public, "get", lambda url, **kw: asked.append(url) or type("R", (), {"headers": {}})())
    assert client.password_encrypt("s3cret").startswith("#PWD_INSTAGRAM:0:") and client.password_encrypt("s3cret").endswith(":s3cret")
    assert client.password_encrypt("#PWD_INSTAGRAM:0:1:x") == "#PWD_INSTAGRAM:0:1:x" and not asked

    sent = []

    def fake_request(endpoint, data=None, login=False, **kw):
        sent.append((endpoint, dict(data or {})))
        if endpoint == "accounts/login/" and not any("verification_code" in d for _, d in sent):
            raise TwoFactorRequired("two_factor_required")
        return {}

    monkeypatch.setattr(client, "private_request", fake_request)
    monkeypatch.setattr(client, "pre_login_flow", lambda: True)
    try:
        client.login_legacy("cloudtrio.business", instagram.plain_password("pw"))
    except TwoFactorRequired:
        pass
    login = next(d for e, d in sent if e == "accounts/login/")
    assert login["enc_password"].startswith("#PWD_INSTAGRAM:0:") and login["enc_password"].endswith(":pw")

    # the browser session: the cookie as Edge shows it (%3A) is decoded before use
    seen = []

    class Fake:
        username = "cloudtrio.business"
        def login_by_sessionid(self, sid): seen.append(sid)
        def get_timeline_feed(self): return {}
        def get_settings(self): return {"cookies": {"sessionid": seen[-1]}}

    monkeypatch.setattr(instagram, "new_client", lambda: Fake())
    instagram.login_with_sessionid("17010900558%3AabcdefGHIJ%3A24%3AAYI700pNEzvvTJUUsOYdsV7Jw")
    assert seen == ["17010900558:abcdefGHIJ:24:AYI700pNEzvvTJUUsOYdsV7Jw"]
    monkeypatch.setattr(Fake, "get_timeline_feed", lambda self: (_ for _ in ()).throw(RuntimeError("401 graphql")))
    with pytest.raises(instagram.InstagramPublishError, match="did not accept that session"):
        instagram.login_with_sessionid("17010900558:abcdefGHIJ:24:AYI700pNEzvvTJUUsOYdsV7Jw")

    # a session Instagram ended is told once, not on every try
    told = []
    monkeypatch.setattr(notify, "send", lambda title, text, image=None, post_id=0: told.append(title) or {})
    monkeypatch.setattr(settings, "IG_DELIVERY", "instagram")
    instagram._tell_logged_out("LoginRequired")
    instagram._tell_logged_out("LoginRequired")
    assert told == ["CloudTrio: Instagram logged the newsroom out"]


# --------------------------------------------------------------------------- never missing a big story


def _story(title, *, outlets=("DD News",), status="approved", score=9.0, reason="", category="outage", hours_ago=0.5, scan=7.0):
    import json

    when = (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat(timespec="seconds")
    return db.execute("INSERT INTO stories (key, title, url, source, outlets, titles, published_at, found_at, category, status, review_score, "
                      "review_reason, scan_score) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      f"k{abs(hash(title))}", title, "https://x.test/" + str(abs(hash(title))), outlets[0], json.dumps(list(outlets)),
                      json.dumps([title]), when, when, category, status, score, reason, scan)  # fmt: skip


def test_one_incident_told_in_many_words_is_one_story():
    stories = sources.group([
        item("Microsoft outage disrupts Outlook, Teams and AI services for over five hours", "DD News"),
        item("Microsoft outage cause revealed: It was a perfect storm of problems", "NDTV"),
        item("Key takeaways from Microsoft's Friday the 13th horror outage", "Mashable"),
        item("Outlook is down for thousands of users", "Sherafy.com"),
        item("Microsoft Ignite updates span Copilot Workspaces, new AI models and Azure", "ETIH"),
        item("IT teams hit by ransomware wave", "Some Blog"),
    ])  # fmt: skip
    outage = next(s for s in stories if "five hours" in s.title)
    assert len(stories) == 3 and len(outage.outlets) == 4 and sources.trusted(outage)
    assert not sources.same_event("Microsoft CEO steps down", "Microsoft outage hits Teams")
    assert not sources.same_event("New iPhone design leak", "Apple confirms iCloud breach")


def test_a_story_turned_down_as_unconfirmed_is_read_again_when_others_report_it(monkeypatch):
    sid = _story("Microsoft outage disrupts Outlook, Teams and AI services for over five hours", status="rejected", reason="single unverified source")
    editor = _story("Cloudflare outage takes down thousands of sites", status="rejected", reason="rejected by the editor")
    monkeypatch.setattr(sources, "fetch_all", lambda max_age: [item("Outlook and Teams down worldwide for hours", "NDTV"),
                                                               item("Cloudflare outage: what went wrong", "CNN"),
                                                               item("Cloudflare outage, explained", "Reuters")])  # fmt: skip
    assert pipeline.scan()["new"] == 0  # no new stories: both are reports of ones it has
    again = db.story(sid)
    assert again["status"] == "new" and again["outlets"] == ["DD News", "NDTV"]
    assert db.story(editor)["status"] == "rejected"  # your own no stays a no
    assert len(db.story(editor)["outlets"]) == 3


def test_breaking_news_skips_the_gap_and_the_queue(monkeypatch):
    import json

    _no_channels(monkeypatch)
    monkeypatch.setattr(settings, "APPROVAL_MODE", "all")
    monkeypatch.setattr(settings, "APPROVAL_QUEUE_MAX", 1)
    monkeypatch.setattr(settings, "MIN_POST_GAP_MINUTES", 90)
    db.kv_set(pipeline.LAST_POST_KEY, (datetime.now(UTC) - timedelta(minutes=30)).isoformat())
    db.execute("INSERT INTO posts (story_id, headline, caption, image_path, writer, delivered, status, created_at) "
               "VALUES (0,'h','c','x.jpg','w','dry_run','pending',?)", db.now())  # the news queue is full
    calm = _story("AWS adds a region in Spain", outlets=("AWS News Blog", "The Register"), category="cloud", score=9.5)
    hot = _story("Microsoft outage hits Outlook and Teams worldwide", outlets=("DD News", "NDTV", "CNN"))
    out = pipeline.post_next()
    assert out["breaking"] and out["pending"] and db.story(hot)["status"] == "pending"
    assert json.loads(db.rows("SELECT meta FROM posts WHERE id = ?", out["post_id"])[0]["meta"])["breaking"]
    assert not pipeline.post_next().get("pending") and db.story(calm)["status"] == "approved"  # the rest waits its turn
    monkeypatch.setattr(settings, "BREAKING_NEWS", False)
    assert not pipeline.is_breaking(db.story(hot) | {"status": "approved"})


def test_old_news_drafts_are_withdrawn_but_flyers_wait(monkeypatch):
    from technews import notify

    sent = []
    monkeypatch.setattr(notify, "send", lambda title, text, *a, **kw: sent.append(text) or {})
    sid = _story("Google Cloud adds guardrails for AI agents", status="pending", category="cloud")
    old = (datetime.now(UTC) - timedelta(hours=13)).isoformat(timespec="seconds")
    news = db.execute("INSERT INTO posts (story_id, headline, caption, image_path, writer, delivered, status, created_at) "
                      "VALUES (?,'Guardrails','c','x.jpg','w','dry_run','pending',?)", sid, old)  # fmt: skip
    flyer = db.execute("INSERT INTO posts (story_id, headline, caption, image_path, writer, delivered, status, created_at, flyer) "
                       "VALUES (0,'Flyer','c','x.jpg','flyer','dry_run','pending',?,'credits')", old)  # fmt: skip
    assert pipeline.expire_stale_drafts() == ["Guardrails"] and len(sent) == 1
    assert db.rows("SELECT status FROM posts WHERE id = ?", news)[0]["status"] == "expired"
    assert db.rows("SELECT status FROM posts WHERE id = ?", flyer)[0]["status"] == "pending"
    assert db.story(sid)["review_reason"] == "draft expired" and not pipeline.approve(news)["posted"]
    assert pipeline.expire_stale_drafts() == []


def test_a_waiting_flyer_does_not_hold_up_the_news(monkeypatch):
    _no_channels(monkeypatch)
    monkeypatch.setattr(settings, "APPROVAL_MODE", "all")
    monkeypatch.setattr(settings, "APPROVAL_QUEUE_MAX", 1)
    db.execute("INSERT INTO posts (story_id, headline, caption, image_path, writer, delivered, status, created_at, flyer) "
               "VALUES (0,'Flyer','c','x.jpg','flyer','dry_run','pending',?,'credits')", db.now())  # fmt: skip
    _story("AWS adds a region in Spain", outlets=("AWS News Blog", "The Register"), category="cloud")
    assert pipeline.pending_count(flyer=True) == 1 and pipeline.pending_count(flyer=False) == 0
    assert pipeline.post_next()["pending"]


def test_the_health_checks_speak_up_once(monkeypatch):
    from technews import notify, watchdog

    sent = []
    monkeypatch.setattr(notify, "send", lambda title, text, *a, markup="", **kw: sent.append((title, markup)) or {})
    monkeypatch.setattr(settings, "IG_DELIVERY", "instagram")
    db.execute("INSERT INTO posts (story_id, headline, caption, image_path, writer, delivered, status, created_at, flyer) "
               "VALUES (0,'Flyer','c','x.jpg','flyer','dry_run','pending',?,'credits')", (datetime.now(UTC) - timedelta(hours=7)).isoformat())  # fmt: skip
    db.kv_set(pipeline.LAST_POST_KEY, (datetime.now(UTC) - timedelta(hours=30)).isoformat())
    missed = _story("Cloudflare outage takes down thousands of websites", status="rejected", reason="Single niche outlet",
                    outlets=("A", "B", "C", "D", "E", "F"))  # fmt: skip
    _story("Small vendor blog post", status="rejected", outlets=("A", "B", "C", "D", "E", "F"), category="other")
    found = watchdog.health()
    assert set(found) == {"queue", "quiet", "missed"}
    assert any(f"md:{missed}" in markup for _t, markup in sent) and len(sent) == 3
    assert watchdog.health() == {} and len(sent) == 3  # said once, not every half hour
    monkeypatch.setattr(settings, "IG_DELIVERY", "dry_run")
    assert watchdog.health() == {"state": "skipped"}


def test_make_a_post_from_telegram_drafts_a_missed_story_and_the_reviewer_learns(monkeypatch):
    from technews import telegram
    from technews.agents import team

    _no_channels(monkeypatch)
    monkeypatch.setattr(settings, "APPROVAL_MODE", "all")
    monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", "555")
    calls = []
    monkeypatch.setattr(telegram, "_api", lambda method, **data: calls.append((method, data)) or {"ok": True})
    sid = _story("Cloudflare outage takes down thousands of websites", status="rejected", reason="Single niche outlet")
    telegram.handle({"id": "c1", "data": f"md:{sid}", "message": {"chat": {"id": 555}, "message_id": 3, "text": "missed"}})
    assert db.story(sid)["status"] == "pending" and pipeline.pending_count() == 1
    assert any("waiting for your approval" in d.get("text", "") for m, d in calls if m == "editMessageText")
    assert "Cloudflare outage takes down thousands of websites" in team.reviewer_context()
    telegram.handle({"id": "c2", "data": f"md:{sid}", "message": {"chat": {"id": 555}, "message_id": 3, "text": "missed"}})
    assert pipeline.pending_count() == 1  # a second tap makes nothing more


def test_new_sources_arrive_once_and_google_news_pages_name_their_outlets(monkeypatch):
    sources.seed()
    assert any(r["name"] == "Google News Technology" for r in sources.listing())
    sources.remove(next(r for r in sources.listing() if r["name"] == "Google News Technology")["id"])
    db.kv_set("sources:seeded:v2", "")  # a database from before the new sources
    sources.seed()
    assert any(r["name"] == "Google News Technology" for r in sources.listing())  # added once
    sources.remove(next(r for r in sources.listing() if r["name"] == "Google News Technology")["id"])
    sources.seed()
    assert not any(r["name"] == "Google News Technology" for r in sources.listing())  # and your removal stays
    rss = ('<rss><channel><item><title>Teams is down - The Verge</title><link>https://news.google.com/a</link>'
           '<source url="https://theverge.com">The Verge</source><pubDate>Mon, 23 Nov 2026 08:00:00 GMT</pubDate></item></channel></rss>')
    monkeypatch.setattr(sources, "_get", lambda url, **params: rss)
    got = sources._fetch_feed("Google News Technology", sources.FEEDS["Google News Technology"])
    assert got[0].source == "The Verge" and got[0].title == "Teams is down"


def test_a_company_incident_never_joins_another_companys_story():
    assert not sources.same_story("Slack is down for users worldwide", "WhatsApp down worldwide for users")
    assert not sources.same_story("Slack is down for users worldwide", "Experts: prepare for the next big internet outage worldwide")
    assert sources.same_story("Slack is down for users worldwide", "Slack down worldwide for many users")
    stories = sources.group([item("WhatsApp down worldwide for users", "CNN", 90), item("Slack is down for users worldwide", "NDTV", 30)])
    assert len(stories) == 2


def test_a_retired_ai_model_is_skipped_and_reported(monkeypatch):
    from technews import notify, watchdog
    from technews.agents import llm

    monkeypatch.setattr(settings, "NVIDIA_API_KEY", "k")
    monkeypatch.setattr(settings, "NVIDIA_MODEL", "old/model")
    monkeypatch.setattr(settings, "NVIDIA_FALLBACK_MODELS", ["new/model"])
    monkeypatch.setattr(llm, "_retired", {})
    asked = []

    def once(system, user, model, *a, **kw):
        asked.append(model)
        if model == "old/model":
            raise llm.LLMError("HTTP 410: The model has reached its end of life")
        return llm.ChatResult({"ok": True}, model, 5)

    monkeypatch.setattr(llm, "_chat_once", once)
    assert llm.chat_json(system="s", user="u").model == "new/model"
    assert llm.chat_json(system="s", user="u").model == "new/model"
    assert asked == ["old/model", "new/model", "new/model"]  # the retired one is not asked again
    assert "old/model" in llm.retired()
    sent = []
    monkeypatch.setattr(notify, "send", lambda title, text, *a, **kw: sent.append(title) or {})
    monkeypatch.setattr(settings, "IG_DELIVERY", "instagram")
    assert "models" in watchdog.health() and sent == ["CloudTrio: an AI model was retired"]
    assert "models" not in watchdog.health()  # said once
