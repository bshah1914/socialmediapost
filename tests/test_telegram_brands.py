"""One Telegram bot for every business: the admin chat gets everything, a business's own chat only its own."""

import pytest

from conftest import SOURDOUGH, make_brand
from technews import brands, db, flyers, notify, pipeline, telegram
from technews.config import settings


class _Ok:
    ok = True
    status_code = 200
    text = ""

    def json(self):
        return {"ok": True}


@pytest.fixture
def sent(monkeypatch):
    """Every Telegram message sent: the data of each request."""
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", "-1001")
    got = []
    monkeypatch.setattr(notify.requests, "post", lambda url, data=None, timeout=None, files=None: got.append(dict(data or {})) or _Ok())
    return got


def _quiet_post(brand_id):
    """A waiting flyer post made with alerts off, so its own background alert cannot land in a test's list."""
    token, settings.TELEGRAM_BOT_TOKEN = settings.TELEGRAM_BOT_TOKEN, ""
    try:
        with brands.use(brand_id):
            flyers.save(SOURDOUGH)
            return pipeline.draft_flyer()["post_id"]
    finally:
        settings.TELEGRAM_BOT_TOKEN = token


def _text(m):
    return m.get("text") or m.get("caption") or ""


def test_cloudtrio_title_unchanged():
    assert notify.title("a post is waiting for your approval", 1) == "CloudTrio: a post is waiting for your approval"


def test_title_follows_the_business_running():
    bid = make_brand("Bakery")
    with brands.use(bid):
        assert notify.title("a post is waiting for your approval") == "Bakery: a post is waiting for your approval"


def test_chats_for_business():
    settings.TELEGRAM_CHAT_ID = "-1001"
    b2 = make_brand("Bakery", telegram_chat="-1002")
    b3 = make_brand("Salon", telegram_chat="-1001")  # the same chat as the admin: one message, not two
    assert notify.chats_for(b2) == ["-1001", "-1002"] and notify.chats_for(b3) == ["-1001"] and notify.chats_for(1) == ["-1001"]


def test_business_draft_goes_to_admin_and_business_chat(sent):
    b2 = make_brand("Bakery", telegram_chat="-1002")
    notify.send(notify.title("x", b2), "y", brand_id=b2)
    assert sorted(m["chat_id"] for m in sent) == ["-1001", "-1002"]
    assert all(_text(m).startswith("Bakery: x") for m in sent)


def test_one_chat_failing_does_not_stop_the_other(monkeypatch):
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", "-1001")
    b2 = make_brand("Bakery", telegram_chat="-1002")
    got = []

    def post(url, data=None, timeout=None, files=None):
        if data["chat_id"] == "-1001":
            raise OSError("admin chat down")
        got.append(data["chat_id"])
        return _Ok()

    monkeypatch.setattr(notify.requests, "post", post)
    out = notify.send("t", "x", brand_id=b2)
    assert got == ["-1002"] and "admin chat down" in out["telegram"]


def test_draft_ready_from_thread_uses_post_brand(sent):
    b2 = make_brand("Bakery", telegram_chat="-1002")
    pid = _quiet_post(b2)
    with brands.use(b2):
        job = notify.draft_ready("Sourdough", "flyer", None, post_id=pid)
    job.join()
    assert sorted(m["chat_id"] for m in sent) == ["-1001", "-1002"]
    assert all(_text(m).startswith("Bakery: a post is waiting") for m in sent)


def test_reel_queued_goes_to_post_business(sent):
    b2 = make_brand("Bakery", telegram_chat="-1002")
    pid = _quiet_post(b2)
    notify.reel_queued("Sourdough", None, pid).join()  # called outside the business, like a request for another page
    assert sorted(m["chat_id"] for m in sent) == ["-1001", "-1002"]


def _tap(monkeypatch, chat, data):
    answers = []
    monkeypatch.setattr(telegram, "_answer", lambda cb, text, alert=False: answers.append(text))
    monkeypatch.setattr(telegram, "_edit", lambda *a, **k: None)
    monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", "-1001")
    telegram.handle({"id": "1", "data": data, "message": {"chat": {"id": chat}, "message_id": 5}})
    return answers


def test_business_chat_cannot_approve_other_business(monkeypatch):
    make_brand("Bakery", telegram_chat="-1002")
    pid = pipeline.draft_flyer()["post_id"]  # business 1
    answers = _tap(monkeypatch, -1002, f"rj:{pid}")
    assert "not allowed" in answers[0]
    assert db.rows("SELECT status FROM posts WHERE id = ?", pid)[0]["status"] == "pending"


def test_business_chat_rejects_own_post(monkeypatch):
    b2 = make_brand("Bakery", telegram_chat="-1002")
    with brands.use(b2):
        flyers.save(SOURDOUGH)
        pid = pipeline.draft_flyer()["post_id"]
    _tap(monkeypatch, -1002, f"rj:{pid}")
    assert db.rows("SELECT status FROM posts WHERE id = ?", pid)[0]["status"] == "rejected"


def test_admin_chat_still_decides_every_business(monkeypatch):
    b2 = make_brand("Bakery", telegram_chat="-1002")
    with brands.use(b2):
        flyers.save(SOURDOUGH)
        pid = pipeline.draft_flyer()["post_id"]
    _tap(monkeypatch, -1001, f"rj:{pid}")
    assert db.rows("SELECT status FROM posts WHERE id = ?", pid)[0]["status"] == "rejected"


def test_missed_story_taps_only_from_admin_chat(monkeypatch):
    make_brand("Bakery", telegram_chat="-1002")
    answers = _tap(monkeypatch, -1002, "md:1")
    assert "not allowed" in answers[0]
