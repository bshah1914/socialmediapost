from datetime import UTC, datetime, timedelta

import pytest

from technews import db, pipeline, sources
from technews.agents import llm, reviewer, scanner, writer
from technews.config import settings


@pytest.fixture(autouse=True)
def tmp_data(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(settings, "CARDS_DIR", tmp_path / "cards")
    monkeypatch.setattr(settings, "OUTBOX_DIR", tmp_path / "outbox")
    monkeypatch.setattr(settings, "IG_DELIVERY", "dry_run")
    monkeypatch.setattr(settings, "NVIDIA_API_KEY", "")
    db.init()


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
        {"items": [{"i": 0, "tech": True, "category": "ai", "score": 9, "why": "major release"}]}, "m", 1))
    story = sources.Story("OpenAI releases GPT-6", "u", "s", "", datetime.now(UTC), ["a"])
    (s,) = scanner.scan([story])
    assert (s.tech, s.category, s.score) == (True, "ai", 9.0)


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
    client = create_app().test_client()

    r = client.post("/api/settings", json={"NVIDIA_API_KEY": "nvapi-secret-1234", "IG_USERNAME": "@cloudtrio",
                                           "IG_PASSWORD": "", "POSTS_PER_DAY": "3"})  # fmt: skip
    assert r.status_code == 200 and set(r.json["changed"]) == {"NVIDIA_API_KEY", "IG_USERNAME", "POSTS_PER_DAY"}
    assert settings.NVIDIA_API_KEY == "nvapi-secret-1234" and settings.POSTS_PER_DAY == 3
    saved = dotenv_values(env)
    assert saved["IG_USERNAME"] == "cloudtrio" and saved["IG_PASSWORD"] == "old"  # blank secret kept

    shown = client.get("/api/settings").json["settings"]
    assert "secret" not in shown["NVIDIA_API_KEY"] and shown["NVIDIA_API_KEY"].endswith("1234")
    assert client.post("/api/settings", json={"IG_DELIVERY": "tiktok"}).status_code == 400
    # Without an admin token, only this machine may change settings.
    remote = client.post("/api/settings", json={"POSTS_PER_DAY": "9"}, environ_base={"REMOTE_ADDR": "192.168.1.20"})
    assert remote.status_code == 403 and settings.POSTS_PER_DAY == 3


def test_password_mask_shows_nothing_of_the_password(monkeypatch):
    from technews import config

    monkeypatch.setattr(settings, "IG_PASSWORD", "hunter2-secret-1998")
    assert config.public()["IG_PASSWORD"] == "•" * 8
