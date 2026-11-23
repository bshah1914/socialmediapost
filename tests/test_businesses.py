"""Many businesses in one console: each keeps its own key, folders, profile and kinds of posts."""

import pytest

from conftest import make_brand
from technews import brands
from technews.agents import llm
from technews.config import settings


def test_cloudtrio_overrides_unchanged():
    b1 = brands.get(1)
    assert brands.overrides(b1) == {"BRAND_ID": 1, "BRAND_NAME": "CloudTrio", "IG_SESSION_PATH": settings.DATA_DIR / "ig_session.json"}


def test_cloudtrio_has_every_kind():
    assert brands.kinds(brands.get(1)) == {"news", "flyers", "ideas", "videos"}


def test_new_business_own_key_and_folders():
    bid = make_brand("Bakery", nvidia_key="nvapi-bakery", about="Fresh bread in Pune")
    o = brands.overrides(brands.get(bid))
    assert o["NVIDIA_API_KEY"] == "nvapi-bakery" and o["LLM2_API_KEY"] == ""
    assert o["CARDS_DIR"] == settings.DATA_DIR / "brands" / str(bid) / "cards"
    assert o["OUTBOX_DIR"] == settings.DATA_DIR / "brands" / str(bid) / "outbox"
    assert o["BRAND_ABOUT"] == "Fresh bread in Pune"
    assert brands.session_path(bid) == settings.DATA_DIR / "brands" / str(bid) / "ig_session.json"
    assert brands.logo_path(bid) == settings.DATA_DIR / "brands" / str(bid) / "logo.png"


def test_business_without_key_never_borrows(monkeypatch):
    monkeypatch.setattr(settings, "NVIDIA_API_KEY", "nvapi-cloudtrio")
    bid = make_brand("Salon")
    with brands.use(bid):
        assert settings.NVIDIA_API_KEY == "" and not llm.available()


def test_key_hidden_and_kept_when_blank():
    bid = make_brand("Gym", nvidia_key="nvapi-gym")
    brands.save(bid, {"name": "Gym", "nvidia_key": ""})
    assert brands.overrides(brands.get(bid))["NVIDIA_API_KEY"] == "nvapi-gym"
    row = next(b for b in brands.listing() if b["id"] == bid)
    assert row["nvidia_key"] == "" and row["nvidia_key_set"]
    brands.save(bid, {"name": "Gym", "clear_nvidia_key": True})
    assert brands.overrides(brands.get(bid))["NVIDIA_API_KEY"] == ""


def test_new_business_default_kinds_and_bad_chat():
    bid = make_brand("Cafe")
    assert brands.kinds(brands.get(bid)) == {"flyers", "ideas", "videos"}
    with pytest.raises(ValueError, match="Telegram chat id"):
        brands.save(bid, {"name": "Cafe", "telegram_chat": "my-chat"})


def test_kinds_filtered_and_saved():
    bid = make_brand("Studio", kinds=["videos", "rockets"])
    assert brands.kinds(brands.get(bid)) == {"videos"}
    with brands.use(bid):
        assert brands.has("videos") and not brands.has("news")


def test_profile_cannot_set_key():
    bid = make_brand("Shop")
    brands.save_profile(bid, {"about": "Phones", "nvidia_key": "nvapi-x", "name": "Hacked"})
    b = brands.get(bid)
    assert b["about"] == "Phones" and b["name"] == "Shop" and b["nvidia_key"] == ""


def test_dry_run_business():
    bid = make_brand("Trial", delivery="dry_run")
    assert brands.overrides(brands.get(bid))["IG_DELIVERY"] == "dry_run"


# --------------------------------------------------------------------------- what each business makes, and its voice

from conftest import SOURDOUGH  # noqa: E402
from technews import db, flyers, pipeline  # noqa: E402
from technews.agents import creator  # noqa: E402


def test_flyer_only_business_never_posts_news(approved_story):
    bid = make_brand("Bakery", kinds="flyers")
    with brands.use(bid):
        flyers.save(SOURDOUGH)
        out = pipeline.post_next()
    assert out.get("flyer") or out.get("pending"), out
    assert db.story(approved_story)["status"] == "approved"


def test_news_business_still_posts_news(approved_story):
    out = pipeline.post_next()  # CloudTrio, as today
    assert out.get("posted") and db.story(approved_story)["status"] == "posted"


def test_no_automatic_kinds_gives_reason(approved_story):
    bid = make_brand("Studio", kinds="videos")
    with brands.use(bid):
        assert pipeline.post_next() == {"posted": False, "reason": "this business makes no automatic posts"}


def test_flyers_on_but_none_saved():
    bid = make_brand("Florist", kinds="flyers")
    with brands.use(bid):
        assert pipeline.post_next()["reason"] == "no flyers in the rotation"


def test_ideas_off_refuses():
    bid = make_brand("Clinic", kinds="flyers")
    with brands.use(bid), pytest.raises(ValueError, match="Ideas are switched off"):
        creator.generate("free check-up")


def test_business_voice_in_brief():
    bid = make_brand("Bakery", about="Family bakery in Pune since 1998")
    with brands.use(bid):
        assert creator._business().startswith("Bakery. What it does: Family bakery in Pune since 1998.")


def test_cloudtrio_brief_unchanged():
    assert creator._business().startswith(f"{settings.BRAND_NAME}. Website {settings.BRAND_SITE}")


# --------------------------------------------------------------------------- onboarding a business, and how each one is doing

from conftest import client_as  # noqa: E402
from technews import notify  # noqa: E402


def _logo(bid):
    from PIL import Image

    Image.new("RGBA", (4, 4), "#123456").save(brands.logo_path(bid), "PNG")


def test_new_business_not_ready_until_complete():
    bid = make_brand("Bakery", enabled=False)
    missing = {i["key"] for i in brands.readiness(bid) if i["required"] and not i["ok"]}
    assert missing == {"profile", "logo", "instagram", "hashtags"}
    with pytest.raises(ValueError, match="Not ready: "):
        brands.switch_on(bid)
    assert brands.get(bid)["enabled"] == 0


def test_ready_in_dry_run_without_key():
    bid = make_brand("Bakery", about="Family bakery in Pune since 1998", hashtags="#bread", delivery="dry_run", enabled=False)
    _logo(bid)
    brands.switch_on(bid)
    assert brands.get(bid)["enabled"] == 1
    key = next(i for i in brands.readiness(bid) if i["key"] == "ai_key")
    assert key["ok"] is False and key["required"] is False


def test_switch_on_from_the_console():
    bid = make_brand("Bakery", enabled=False)
    admin = client_as("boss", "Admin@1234")
    out = admin.post(f"/api/brands/{bid}/enable")
    assert out.status_code == 400 and "Not ready" in out.get_json()["error"]
    assert {i["key"] for i in admin.get(f"/api/brands/{bid}/ready").get_json()["checks"]} == {"profile", "kinds", "logo", "instagram", "ai_key", "hashtags"}


def test_onboarding_is_for_admins_only():
    bid = make_brand("Bakery")
    owner = client_as("baker", "Pass@1234", "owner", bid)
    assert owner.get(f"/api/brands/{bid}/ready").status_code == 403
    assert owner.post(f"/api/brands/{bid}/enable").status_code == 403
    assert owner.post(f"/api/brands/{bid}/test-key", json={"key": "x"}).status_code == 403
    assert owner.get("/api/businesses/health").status_code == 403


def test_test_key_uses_only_that_key(monkeypatch):
    seen = []
    monkeypatch.setattr(settings, "NVIDIA_API_KEY", "nvapi-cloudtrio")

    def fake(**kw):
        seen.append((settings.NVIDIA_API_KEY, settings.LLM2_API_KEY))
        raise llm.LLMError("401 unauthorized")

    monkeypatch.setattr(llm, "chat_json", fake)
    ok, message = llm.check_key("nvapi-new")
    assert not ok and "401" in message and seen == [("nvapi-new", "")]
    assert settings.NVIDIA_API_KEY == "nvapi-cloudtrio"


def test_test_telegram_goes_to_the_business_chat_only(monkeypatch):
    bid = make_brand("Bakery", telegram_chat="-1002")
    got = []
    monkeypatch.setattr(notify, "_telegram", lambda title, text, image, post_id=0, markup="", chat="": got.append((chat, title)))
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "t")
    out = client_as("boss", "Admin@1234").post(f"/api/brands/{bid}/test-telegram").get_json()
    assert out["ok"] and got == [("-1002", "Bakery: test message")]


def test_health_lists_every_business():
    make_brand("Bakery", delivery="dry_run")
    rows = client_as("boss", "Admin@1234").get("/api/businesses/health").get_json()["businesses"]
    assert [r["name"] for r in rows][:2] == ["CloudTrio", "Bakery"]
    assert set(rows[1]) >= {"id", "name", "enabled", "delivery", "instagram", "ai_key", "last_post_at", "waiting"}
    assert rows[1]["delivery"] == "dry_run" and rows[1]["ai_key"] is False
