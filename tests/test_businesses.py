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
