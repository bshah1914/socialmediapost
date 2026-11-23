# socialmediapost Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A copy of the CloudTrio newsroom that runs many separate businesses from one console, with per-business logins, keys and Telegram chats, a one-step Reel from an idea, and a recorded video → Reel/post flow.

**Architecture:** Copy `C:\Automation Code\technews\agentic-ai` into `C:\Automation Code\socialmediapost` and extend the existing brand system (`brands.py` + `config.override` context) instead of replacing it. Business #1 (CloudTrio) keeps today's settings and file paths exactly; every new per-business value applies only to businesses ≥ 2 or only when set. Videos are processed by a single background job runner with jobs stored in the database.

**Tech Stack:** Python 3.11/3.12, Flask 3 + Jinja, APScheduler, SQLite (Postgres optional), Pillow, ffmpeg (`imageio-ffmpeg` on dev machines), instagrapi, NVIDIA NIM (`agents/llm.py`), pytest, Playwright (Python, sync API).

**Spec:** `docs/superpowers/specs/2026-11-23-socialmediapost-design.md`

## Global Constraints

- Never edit anything under `C:\Automation Code\technews`. Copy only.
- The Python package keeps the name `technews` (renaming touches every import and risks CloudTrio); the project, folder, compose service and page title are `socialmediapost`.
- Business #1 (CloudTrio): `brands.overrides(brand_1)` returns exactly what it returns today when the new fields are blank; its paths stay `data/cards`, `data/outbox`, `data/ig_session.json`, `data/brand-logo-1.png`; its AI key stays `NVIDIA_API_KEY` from `.env`.
- Businesses ≥ 2 keep files in `data/brands/<id>/` (`cards/`, `outbox/`, `videos/`, `ig_session.json`, `logo.png`).
- A business without its own NVIDIA key gets template copy: `NVIDIA_API_KEY = ""` and `LLM2_API_KEY = ""` in its overrides. Never another business's key.
- Roles: `admin` (all businesses, `users.brand_id = 0`), `owner` and `viewer` (exactly one business).
- Reel/video encode: 1080×1920, 30 fps, H.264 (`yuv420p`) + AAC, `+faststart` — same as `motion.py`.
- Video upload limits: `.mp4`/`.mov`, ≤ 300 MB (`314_572_800` bytes), ≤ 180 s after trim; originals deleted 30 days after the post goes up.
- One video job runs at a time per server.
- All existing tests (91 in `tests/test_agents.py`) pass after every task: `.venv\Scripts\python.exe -m pytest -q`.
- Trial run: the new `.env` has `IG_DELIVERY=dry_run` and **`TELEGRAM_BOT_TOKEN=` blank** (otherwise its alerts carry buttons the live app would act on with the wrong post ids).
- Commands run from `C:\Automation Code\socialmediapost` in PowerShell.

## Review Focus

1. **Background threads lose the business context** (`threading.Thread` does not carry `contextvars`): `notify.draft_ready`/`reel_queued` and video jobs must pass `brand_id` explicitly → Task 5 test `test_draft_ready_from_thread_uses_post_brand`, Task 8 test `test_job_runs_as_video_brand`.
2. **A business with no flyers, or with news off and flyers off**: `post_next()` must return a reason, not raise (`pick_flyer()` calls `min()` on an empty list) → Task 3 tests.
3. **A bad upload** (renamed non-video, > 180 s untrimmed with no trim, > 300 MB, no audio track): a clear message, nothing left on disk; no-audio + "original sound" still gets a silent AAC track → Task 8 tests.
4. **Server restart while a video is "working"**: the job goes back to the queue, at most 2 attempts, then `failed` with the reason → Task 8 test `test_stuck_job_requeued_then_failed`.
5. **An owner reaching another business** by post/flyer/idea/video id, card file name, brand switch, or an admin-only GET → Task 4 test `test_owner_sees_no_other_business_anywhere`.

---

### Task 1: New project, copied data, green baseline

**Files:**
- Create: `C:\Automation Code\socialmediapost\` (copy of `technews\agentic-ai` without `.venv`, `.pytest_cache`, `__pycache__`)
- Create: `tests/conftest.py` (move `_reset_postgres` and the autouse `tmp_data` fixture out of `tests/test_agents.py`; add `client_as`)
- Modify: `tests/test_agents.py` (remove the moved fixture; keep `_client`)
- Modify: `.env` (`IG_DELIVERY=dry_run`, `TELEGRAM_BOT_TOKEN=`), `docker-compose.yml`, `README.md` title

**Interfaces:**
- Produces: `tests/conftest.py::client_as(username: str, password: str, role: str = "admin", brand_id: int = 0) -> FlaskClient` — creates the user if missing (admin via `auth.set_credentials`, others via `users.create`) and signs in; `tests/conftest.py::make_brand(name: str, **fields) -> int` — `brands.save(None, {"name": name, **fields})`.

- [ ] **Step 1:** Copy: `robocopy "C:\Automation Code\technews\agentic-ai" "C:\Automation Code\socialmediapost" /E /XD .venv .pytest_cache __pycache__` (keeps the existing `docs\superpowers\specs` file). Expected: exit code < 8.
- [ ] **Step 2:** `py -3.12 -m venv .venv; .venv\Scripts\python.exe -m pip install -r requirements.txt imageio-ffmpeg==0.6.0 playwright==1.64.1; .venv\Scripts\python.exe -m playwright install chromium`. Expected: no errors.
- [ ] **Step 3:** Edit `.env`: `IG_DELIVERY=dry_run`, add `TELEGRAM_BOT_TOKEN=`. Rename compose service/container/image/volume to `socialmediapost` / `socialmediapost-data`; README heading `# socialmediapost`.
- [ ] **Step 4:** Move fixtures to `tests/conftest.py`, add `client_as` and `make_brand`.
- [ ] **Step 5:** Run `.venv\Scripts\python.exe -m pytest -q`. Expected: all pass (same count as in `technews`: run the same command there read-only first and note the number).
- [ ] **Step 6:** `git init; git add -A; git commit -m "chore: start socialmediapost from technews"` (`.gitignore` already excludes `.env` and `data/`; check `git status` shows neither).

---

### Task 2: Business fields, own key, own folders

**Files:**
- Modify: `technews/db.py` (SCHEMA `brands` + `added` columns), `technews/brands.py`, `technews/config.py` (`BRAND_ABOUT = ""`)
- Test: `tests/test_businesses.py`

**Interfaces:**
- Produces in `brands.py`:
  - `KINDS: Final = ("news", "flyers", "ideas", "videos")`
  - `kinds(brand: dict) -> set[str]`; `has(kind: str, brand: dict | None = None) -> bool` (None → `current()`)
  - `folder(brand_id: int) -> Path` → `DATA_DIR/"brands"/str(id)`, created on first call
  - `session_path(id)`, `logo_path(id)`: id 1 unchanged; others `folder(id)/"ig_session.json"`, `folder(id)/"logo.png"` (move an old `DATA_DIR/ig_session_<id>.json` / `brand-logo-<id>.png` there if found)
  - `save(brand_id, data)` also takes `about` (≤ 1500), `kinds` (list or comma text, filtered to `KINDS`; new brand default `"flyers,ideas,videos"`), `nvidia_key` (sealed; blank keeps; `clear_nvidia_key: true` empties), `telegram_chat` (`^-?\d{3,20}$` or blank, else `ValueError("Telegram chat id is a number")`), `colors` (`{"bg": "#rrggbb", "accent": "#rrggbb"}` stored as JSON), `delivery` (`""` or `"dry_run"`)
  - `save_profile(brand_id: int, data: dict) -> int` — owner edits: only `about, hashtags, email, site, colors, telegram_chat`
  - `listing()` adds `nvidia_key_set: bool` and blanks `nvidia_key`
  - `overrides(brand)` adds, for id ≥ 2: `CARDS_DIR`, `OUTBOX_DIR` (in `folder(id)`), `NVIDIA_API_KEY` (revealed or `""`), `LLM2_API_KEY = ""`; for any id: `BRAND_ABOUT` when `about` set, `IG_DELIVERY = "dry_run"` when `delivery == "dry_run"`
- New columns (`TEXT NOT NULL DEFAULT ''` unless noted): `about`, `kinds` (`DEFAULT 'news,flyers,ideas,videos'`), `nvidia_key`, `telegram_chat`, `colors`, `delivery`.

- [ ] **Step 1: Write the failing tests**

```python
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
    assert o["BRAND_ABOUT"] == "Fresh bread in Pune"
    assert brands.session_path(bid) == settings.DATA_DIR / "brands" / str(bid) / "ig_session.json"

def test_business_without_key_never_borrows():
    bid = make_brand("Salon")
    with brands.use(bid):
        assert settings.NVIDIA_API_KEY == "" and not llm.available()

def test_key_hidden_and_kept_when_blank():
    bid = make_brand("Gym", nvidia_key="nvapi-gym")
    brands.save(bid, {"name": "Gym", "nvidia_key": ""})
    assert brands.overrides(brands.get(bid))["NVIDIA_API_KEY"] == "nvapi-gym"
    row = next(b for b in brands.listing() if b["id"] == bid)
    assert row["nvidia_key"] == "" and row["nvidia_key_set"]

def test_new_business_default_kinds_and_bad_chat():
    bid = make_brand("Cafe")
    assert brands.kinds(brands.get(bid)) == {"flyers", "ideas", "videos"}
    with pytest.raises(ValueError, match="Telegram chat id"):
        brands.save(bid, {"name": "Cafe", "telegram_chat": "my-chat"})

def test_profile_cannot_set_key():
    bid = make_brand("Shop")
    brands.save_profile(bid, {"about": "Phones", "nvidia_key": "nvapi-x", "name": "Hacked"})
    b = brands.get(bid)
    assert b["about"] == "Phones" and b["name"] == "Shop" and b["nvidia_key"] == ""
```

- [ ] **Step 2:** Run `.venv\Scripts\python.exe -m pytest tests/test_businesses.py -q`. Expected: FAIL (no `kinds`, no columns).
- [ ] **Step 3:** Implement the interfaces above.
- [ ] **Step 4:** Run `.venv\Scripts\python.exe -m pytest -q`. Expected: all pass.
- [ ] **Step 5:** `git add -A; git commit -m "feat: per-business key, kinds, profile and folders"`

---

### Task 3: What each business makes (kinds) and its voice

**Files:**
- Modify: `technews/pipeline.py` (`post_next`), `technews/agents/creator.py` (`_business`, `generate`)
- Test: `tests/test_businesses.py`

**Interfaces:**
- Consumes: `brands.has`, `settings.BRAND_ABOUT` (Task 2)
- Produces: `post_next()` rules — news (incl. breaking) only when `has("news")`; flyers only when `has("flyers") and settings.FLYERS_ENABLED`; with only flyers on every turn is a flyer; reasons `"this business makes no automatic posts"` and `"no flyers in the rotation"`. `creator.generate` raises `ValueError("Ideas are switched off for this business.")` when `not has("ideas")`. `creator._business()` starts with `"{BRAND_NAME}. What it does: {BRAND_ABOUT}."` when about is set, else the exact current string.

- [ ] **Step 1: Write the failing tests**

```python
def test_flyer_only_business_never_posts_news(approved_story):  # fixture: one approved story in the DB
    bid = make_brand("Bakery", kinds="flyers")
    with brands.use(bid):
        flyers.save({"kicker": "NEW", "headline": "Sourdough every Sunday", "points": ["Baked at 6am"], "cta": "Visit us", "caption": "x", "enabled": True})
        out = pipeline.post_next()
    assert out.get("flyer") or out.get("pending")
    assert db.story(approved_story)["status"] == "approved"

def test_no_automatic_kinds_gives_reason():
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
```

(`approved_story` goes in `tests/conftest.py`: insert one story with `status="approved"` the way existing pipeline tests in `test_agents.py` do.)

- [ ] **Step 2:** Run the new tests. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `.venv\Scripts\python.exe -m pytest -q`. Expected: all pass.
- [ ] **Step 5:** `git commit -am "feat: businesses choose what they post and their voice"`

---

### Task 4: Logins locked to one business

**Files:**
- Modify: `technews/db.py` (`users.brand_id INTEGER NOT NULL DEFAULT 0`), `technews/users.py`, `technews/__init__.py` (`guard`, `pick_brand`, `api_brand_switch`, users API, new `api_brand_profile`), `technews/templates/dashboard.html`
- Test: `tests/test_access.py`

**Interfaces:**
- Produces:
  - `users.ROLES = ("admin", "owner", "viewer")`; `users.migrate()` also runs `UPDATE users SET role = 'owner', brand_id = 1 WHERE role = 'approver'`
  - `users.create(username, password, role, brand_id: int = 0) -> int` — admin forces 0; owner/viewer need an existing brand else `ValueError("Pick the business this person belongs to")`; `users.change(..., brand_id: int | None = None)`
  - `/api/users` POST/change accept `brand_id`; listing shows it
  - `g.brand` for non-admins is always their own business (session ignored); a switched-off business → 403 `"Your business is switched off. Ask an admin."`
  - `POST /api/brand` (switch): non-admin → 403 unless it is their own id
  - Ownership check in `pick_brand`, for every request: if `request.view_args` has `post_id`/`flyer_id`/`idea_id`/`video_id` and that row's `brand_id` ≠ `g.brand["id"]` → 404 `{"error": "Not found"}` (admins too: they act on the business they have open). `brand_id` view arg: non-admin must equal own id → 404.
  - `owner_ok` = today's `approver_ok` + `{"api_brand_profile", "api_brand_connect", "api_brand_logo", "api_videos_upload", "api_video_make", "api_video_cover", "api_video_delete"}`
  - `ADMIN_ONLY` (any method, non-admin → 403): `{"api_users", "api_backups", "api_secrets_status", "api_settings", "api_brands", "api_sources", "api_scout", "api_businesses_health", "api_instagram_login"}` plus every endpoint in the onboarding Task 6
  - `POST /api/brand/profile` → `brands.save_profile(g.brand["id"], body)`
  - Template: brand switcher, Settings, Businesses, People, Sources and Scout hidden when `role != "admin"`; role label "Owner" replaces "Approver"

- [ ] **Step 1: Write the failing tests**

```python
def test_owner_only_sees_own_business():
    b2 = make_brand("Bakery"); b3 = make_brand("Salon")
    c = client_as("baker", "Pass@1234", "owner", b2)
    assert c.post("/api/brand", json={"id": b3}).status_code == 403
    assert c.post("/api/brand", json={"id": 1}).status_code == 403

def test_owner_cannot_touch_other_post():
    b2 = make_brand("Bakery")
    pid = pipeline.draft_flyer()["post_id"]  # business 1's post
    c = client_as("baker", "Pass@1234", "owner", b2)
    assert c.post(f"/api/posts/{pid}/approve").status_code == 404
    assert c.post(f"/api/posts/{pid}/reject").status_code == 404

def test_owner_approves_own_post():
    b2 = make_brand("Bakery")
    with brands.use(b2):
        flyers.save({"kicker": "NEW", "headline": "Sourdough every Sunday", "points": ["Baked at 6am"], "cta": "Visit us", "caption": "x", "enabled": True})
        pid = pipeline.draft_flyer()["post_id"]
    c = client_as("baker", "Pass@1234", "owner", b2)
    assert c.post(f"/api/posts/{pid}/approve").status_code == 200

def test_owner_blocked_from_admin_pages():
    c = client_as("baker", "Pass@1234", "owner", make_brand("Bakery"))
    for url in ("/api/settings", "/api/users", "/api/brands", "/api/backups", "/api/sources"):
        assert c.get(url).status_code == 403, url

def test_owner_sees_no_other_business_anywhere():
    pid = pipeline.draft_flyer()["post_id"]
    secret = db.rows("SELECT headline, image_path FROM posts WHERE id = ?", pid)[0]
    c = client_as("baker", "Pass@1234", "owner", make_brand("Bakery"))
    app = c.application
    for rule in app.url_map.iter_rules():
        if "GET" in rule.methods and not rule.arguments and not rule.rule.startswith("/static"):
            body = c.get(rule.rule).get_data(as_text=True)
            assert secret["headline"] not in body, rule.rule
    assert c.get("/cards/" + Path(secret["image_path"]).name).status_code == 404

def test_approver_becomes_owner_of_cloudtrio():
    uid = db.execute("INSERT INTO users (username, hash, role, created_at) VALUES ('old', 'x', 'approver', ?)", db.now())
    users.migrate()
    assert users.get(uid)["role"] == "owner" and users.get(uid)["brand_id"] == 1

def test_owner_needs_a_business():
    with pytest.raises(ValueError, match="Pick the business"):
        users.create("nobody", "Pass@1234", "owner", 0)
```

(`/cards/...` returns 404 for the business-1 file because business 2's `CARDS_DIR` is its own folder — Task 2.)

- [ ] **Step 2:** Run `.venv\Scripts\python.exe -m pytest tests/test_access.py -q`. Expected: FAIL.
- [ ] **Step 3:** Implement. Where an existing test acts on another business's post through the dashboard, switch the session to that business first rather than weakening the check.
- [ ] **Step 4:** Run `.venv\Scripts\python.exe -m pytest -q`. Expected: all pass.
- [ ] **Step 5:** `git commit -am "feat: logins locked to their business"`

---

### Task 5: One Telegram bot, a chat per business

**Files:**
- Modify: `technews/notify.py`, `technews/telegram.py`, `technews/pipeline.py:249,909` (titles)
- Test: `tests/test_telegram_brands.py`

**Interfaces:**
- Produces:
  - `notify.title(text: str, brand_id: int | None = None) -> str` → `f"{brand name}: {text}"` (business 1 → `"CloudTrio: …"`, identical to today)
  - `notify.chats_for(brand_id: int) -> list[str]` → `[TELEGRAM_CHAT_ID]` plus the business's `telegram_chat` when set and different
  - `notify.send(title, text, image=None, post_id=0, *, markup="", brand_id: int | None = None)`; `brand_id` defaults to the post's business when `post_id`, else `settings.BRAND_ID`, resolved **before** any thread starts; Telegram goes to every chat in `chats_for`; one chat failing does not stop the other; email/push/webhook/WhatsApp unchanged (admin channels, with the prefixed title)
  - `draft_ready` / `reel_queued` capture `settings.BRAND_ID` before `threading.Thread` and pass `brand_id`
  - `telegram.handle`: a tap counts when the chat is the admin chat, or it is the `telegram_chat` of the tapped post's business; `md` (missed story) taps only from the admin chat

- [ ] **Step 1: Write the failing tests** (monkeypatch `requests.post` to record `data["chat_id"]` and `data["caption"]/["text"]`)

```python
def test_cloudtrio_title_unchanged():
    assert notify.title("a post is waiting for your approval", 1) == "CloudTrio: a post is waiting for your approval"

def test_business_draft_goes_to_admin_and_business_chat(sent):
    b2 = make_brand("Bakery", telegram_chat="-1002")
    settings.TELEGRAM_BOT_TOKEN, settings.TELEGRAM_CHAT_ID = "t", "-1001"
    notify.send(notify.title("x", b2), "y", brand_id=b2)
    assert sorted(m["chat_id"] for m in sent) == ["-1001", "-1002"]
    assert all(m["text"].startswith("Bakery: x") for m in sent)

def test_draft_ready_from_thread_uses_post_brand(sent):
    b2 = make_brand("Bakery", telegram_chat="-1002")
    with brands.use(b2):
        t = notify.draft_ready("Sourdough", "flyer", None, post_id=0)
    t.join()
    assert {m["chat_id"] for m in sent} == {"-1001", "-1002"}

def test_business_chat_cannot_approve_other_business(monkeypatch):
    b2 = make_brand("Bakery", telegram_chat="-1002")
    pid = pipeline.draft_flyer()["post_id"]  # business 1
    answers = []
    monkeypatch.setattr(telegram, "_answer", lambda cb, text, alert=False: answers.append(text))
    telegram.handle({"id": "1", "data": f"rj:{pid}", "message": {"chat": {"id": -1002}, "message_id": 5}})
    assert "not allowed" in answers[0]
    assert db.rows("SELECT status FROM posts WHERE id = ?", pid)[0]["status"] == "pending"

def test_business_chat_rejects_own_post(monkeypatch): ...  # same, post made under brands.use(b2) → status 'rejected'
```

(`sent` fixture in the test file: sets token/chat id and records posted data.)

- [ ] **Step 2:** Run the tests. Expected: FAIL.
- [ ] **Step 3:** Implement; replace the five `"CloudTrio: …"` literals with `notify.title(…)`.
- [ ] **Step 4:** Run `.venv\Scripts\python.exe -m pytest -q`. Expected: all pass.
- [ ] **Step 5:** `git commit -am "feat: Telegram alerts name the business and reach its chat"`

---

### Task 6: Onboarding a business and per-business health

**Files:**
- Modify: `technews/brands.py`, `technews/agents/llm.py`, `technews/__init__.py`, `technews/templates/dashboard.html`
- Test: `tests/test_businesses.py`

**Interfaces:**
- Produces:
  - `brands.readiness(brand_id: int) -> list[dict]`, items `{"key", "label", "ok": bool, "required": bool}` for keys: `profile` (name and `about` ≥ 20 chars, required), `kinds` (at least one, required), `logo` (required), `instagram` (connected session, or `delivery == "dry_run"`; required), `ai_key` (own key set; not required — note "captions will use templates"), `hashtags` (required)
  - `brands.switch_on(brand_id: int) -> None` → `ValueError("Not ready: " + labels of failing required items)`; `save()` itself is unchanged
  - `llm.check_key(key: str) -> tuple[bool, str]` — one tiny chat call with that key only (reuse what `/api/settings/test-nvidia` does)
  - Admin routes: `GET /api/brands/<id>/ready`, `POST /api/brands/<id>/enable`, `POST /api/brands/<id>/test-key` (body `{"key"}` optional; blank tests the saved key), `POST /api/brands/<id>/test-telegram` (sends `notify.title("test message", id)` to that business's chat only), `GET /api/businesses/health` → list of `{"id", "name", "enabled", "delivery", "instagram", "ai_key", "last_post_at", "waiting"}`
  - Dashboard: "Add a business" stepper (7 steps from the spec, each step saves via `POST /api/brands`/`/api/brands/<id>/logo`/`/connect`), readiness list with ticks, "Switch on" button; Businesses table from the health endpoint

- [ ] **Step 1: Write the failing tests**

```python
def test_new_business_not_ready_until_complete():
    bid = make_brand("Bakery")
    missing = {i["key"] for i in brands.readiness(bid) if i["required"] and not i["ok"]}
    assert missing == {"profile", "logo", "instagram", "hashtags"}
    with pytest.raises(ValueError, match="Not ready"):
        brands.switch_on(bid)

def test_ready_in_dry_run_without_key():
    bid = make_brand("Bakery", about="Family bakery in Pune since 1998", hashtags="#bread", delivery="dry_run", enabled=False)
    brands.logo_path(bid).write_bytes(PNG_1PX)
    brands.switch_on(bid)
    assert brands.get(bid)["enabled"] == 1
    assert next(i for i in brands.readiness(bid) if i["key"] == "ai_key")["ok"] is False

def test_health_lists_every_business():
    make_brand("Bakery")
    rows = _client().get("/api/businesses/health").get_json()["businesses"]
    assert [r["name"] for r in rows][:2] == ["CloudTrio", "Bakery"]
```

- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `.venv\Scripts\python.exe -m pytest -q`. Expected: all pass.
- [ ] **Step 5:** `git commit -am "feat: onboard a business with a readiness check; business health"`

---

### Task 7: Reel straight from an idea

**Files:**
- Modify: `technews/pipeline.py` (`_insert_draft`, `draft_flyer`), `technews/agents/creator.py` (`send`), `technews/__init__.py` (`api_create_send`), `technews/templates/dashboard.html` (Create page)
- Test: `tests/test_idea_reel.py`

**Interfaces:**
- Produces:
  - `pipeline._insert_draft(..., for_reel: bool = False)` and `pipeline.draft_flyer(flyer=None, *, tags=None, for_reel: bool = False)` — `for_reel` skips the automatic `reel_wanted()` Reel and the `draft_ready` alert
  - `creator.send(idea_id, variant, *, rotate=False, as_reel=False, style="", track="") -> {"flyer_id", "post_id", "reel": bool, "reason": str}` — with `as_reel`: `draft_flyer(for_reel=True)`, then `pipeline.make_reel(post_id, style=style, track=track)`; success → one `notify.draft_ready(headline, "animated flyer", CARDS_DIR/video, post_id=…)`; failure → one `notify.draft_ready(headline, "flyer", image, post_id=…)`, `reel False`, `reason` from `make_reel`
  - `/api/create/send` body adds `as` (`"flyer"` | `"reel"`), `style`, `track`
  - Create page: "Make it as: Flyer · Reel"; with Reel, two selects filled from `reel_styles` and `reel_tracks` (already in the template context), shown only when `reels_ok`; after sending, the message links to the Reels section, or shows the reason

- [ ] **Step 1: Write the failing tests** (monkeypatch `pipeline.make_reel` and `notify.draft_ready` to record calls)

```python
def test_idea_sent_as_reel_alerts_once(calls):
    idea = creator.generate("Free cloud cost review for startups")
    out = creator.send(idea["idea_id"], idea["variants"][0], as_reel=True, style="slide", track="calm")
    assert out["reel"] is True
    assert calls.make_reel == [(out["post_id"], "slide", "calm")]
    assert [c.kind for c in calls.draft_ready] == ["animated flyer"]

def test_reel_failure_keeps_flyer(calls_failing):
    idea = creator.generate("Free cloud cost review for startups")
    out = creator.send(idea["idea_id"], idea["variants"][0], as_reel=True)
    assert out["reel"] is False and out["reason"]
    assert [c.kind for c in calls_failing.draft_ready] == ["flyer"]

def test_reel_ignores_weekly_limit(calls, monkeypatch):
    monkeypatch.setattr(pipeline, "reel_wanted", lambda: False)
    idea = creator.generate("Free cloud cost review")
    assert creator.send(idea["idea_id"], idea["variants"][0], as_reel=True)["reel"] is True
```

- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `.venv\Scripts\python.exe -m pytest -q`. Expected: all pass.
- [ ] **Step 5:** `git commit -am "feat: make a Reel straight from an idea"`

---

### Task 8: Video processing core

**Files:**
- Create: `technews/videos.py`
- Modify: `technews/db.py` (table `videos`, add to `_ID_TABLES` and `migrate.TABLES`), `technews/scheduler.py` (every minute `videos.run_next()`; daily `videos.cleanup()`)
- Test: `tests/test_videos.py` (makes tiny clips with `ffmpeg -f lavfi -i testsrc=size=640x360:rate=30 -f lavfi -i sine -t 3`; one without audio; one portrait 360x640)

**Interfaces:**
- Table `videos`: `id, brand_id, at, stored (file name), original_name, seconds REAL, width, height, has_audio INTEGER, status ('uploaded'|'queued'|'working'|'done'|'failed'), error, options (JSON), attempts INTEGER DEFAULT 0, started_at, post_id INTEGER DEFAULT 0, posted_at`
- Produces in `videos.py`:
  - `MAX_BYTES = 314_572_800`, `MAX_SECONDS = 180`, `SUFFIXES = (".mp4", ".mov")`
  - `class VideoError(ValueError)`
  - `folder() -> Path` → `brands.folder(settings.BRAND_ID) / "videos"`
  - `probe(path: Path) -> dict` `{"seconds", "width", "height", "has_audio"}` — parses `ffmpeg -hide_banner -i <path>` stderr (`Duration: HH:MM:SS.xx`, first `Video:` stream `WxH`, any `Audio:`); no video stream → `VideoError("That file is not a video we can read.")`
  - `save_upload(stream: IO[bytes], filename: str) -> dict` — suffix check (`VideoError("Upload an MP4 or MOV video.")`), copies in 1 MB chunks and stops past `MAX_BYTES` (`VideoError("The video is over 300 MB.")`), probes, deletes the file on any error, inserts row `status='uploaded'`, returns the row
  - `Options` dataclass: `start: float = 0, end: float = 0 (0 = to the end), sound: str = "original" ('original'|'music'|'both'), logo: bool = True, title: bool = True, title_text: str = "", about: str = "", as_post: bool = False, cover_at: float | None = None, track: str = ""` — `Options.check(probe) -> Options` clamps and raises `VideoError("Trim the video to 3 minutes or less.")` when `end - start > 180`
  - `build_command(src: Path, out: Path, opts: Options, info: dict, *, logo: Path | None, title_png: Path | None, music: Path | None) -> list[str]` — landscape (`width > height`): blurred, scaled-to-cover copy behind, video scaled to 1080 wide, centred; portrait: scale to cover 1080×1920 and centre-crop; logo top-right 160 px wide, 48 px margin; title PNG (drawn with Pillow in business colours) for the first 2.5 s; audio: `original` (no audio → `anullsrc` silent track), `music` (track only), `both` (original 1.0 + music 0.25, `amix duration=first`); encode `-c:v libx264 -preset veryfast -crf 23 -r 30 -pix_fmt yuv420p -c:a aac -b:a 128k -movflags +faststart`, `-ss start -to end`
  - `frames(video_id: int, n: int = 4) -> list[float]` — seconds of `n` evenly spaced candidate covers inside the trim; `frame_jpg(video_id: int, at: float, out: Path, size=(1080, 1920)) -> Path`
  - `queue(video_id: int, opts: Options) -> None` (status `queued`, options saved); `run_next() -> bool` — under a module lock; re-queues `working` rows with `started_at` older than 15 min; picks the oldest `queued` with `attempts < 2`, sets `working` and `attempts + 1`, runs **inside `brands.use(row["brand_id"])`**, renders to `CARDS_DIR/video-<id>-<ms>.mp4` + cover jpg, calls `pipeline.draft_video(video_id, mp4, cover)` (Task 9), sets `done`; on error sets `queued` again if attempts < 2 else `failed` with `error`
  - `cleanup(days: int = 30) -> int` — deletes originals of rows with `posted_at` older than `days`
  - `start_worker()` — kicks `run_next()` in a daemon thread right after `queue()`, so it starts without waiting for the scheduler

- [ ] **Step 1: Write the failing tests**

```python
def test_probe_reads_landscape_clip(clip_landscape):
    info = videos.probe(clip_landscape)
    assert info["width"] == 640 and info["height"] == 360 and 2.9 < info["seconds"] < 3.1 and info["has_audio"]

def test_renamed_text_file_rejected_and_removed(tmp_path):
    with pytest.raises(videos.VideoError, match="not a video"):
        videos.save_upload(io.BytesIO(b"hello"), "fake.mp4")
    assert not any(videos.folder().iterdir())

def test_wrong_suffix_and_too_big(monkeypatch):
    with pytest.raises(videos.VideoError, match="MP4 or MOV"):
        videos.save_upload(io.BytesIO(b"x"), "clip.avi")
    monkeypatch.setattr(videos, "MAX_BYTES", 10)
    with pytest.raises(videos.VideoError, match="over 300 MB"):
        videos.save_upload(io.BytesIO(b"x" * 11), "clip.mp4")

def test_over_three_minutes_needs_trim():
    with pytest.raises(videos.VideoError, match="3 minutes"):
        videos.Options(start=0, end=200).check({"seconds": 240})

def test_landscape_gets_blurred_fill(clip_landscape, tmp_path):
    cmd = " ".join(videos.build_command(clip_landscape, tmp_path / "o.mp4", videos.Options(), {"width": 640, "height": 360, "has_audio": True, "seconds": 3}, logo=None, title_png=None, music=None))
    assert "boxblur" in cmd and "1080" in cmd and "-r 30" in cmd and "+faststart" in cmd

def test_no_audio_gets_silent_track(clip_silent, tmp_path):
    v = videos.save_upload(clip_silent.open("rb"), "silent.mp4")
    videos.queue(v["id"], videos.Options(logo=False, title=False))
    assert videos.run_next()
    out = videos.probe(settings.CARDS_DIR / json.loads(db.rows("SELECT meta FROM posts WHERE id = ?", _post_of(v))[0]["meta"])["reel"]["video"])
    assert out["width"] == 1080 and out["height"] == 1920 and out["has_audio"]

def test_job_runs_as_video_brand(clip_portrait):
    bid = make_brand("Bakery", kinds="videos")
    with brands.use(bid):
        v = videos.save_upload(clip_portrait.open("rb"), "p.mp4")
        videos.queue(v["id"], videos.Options(logo=False, title=False))
    assert videos.run_next()  # called outside any business context, like the scheduler
    assert db.rows("SELECT brand_id FROM posts WHERE id = ?", _post_of(v))[0]["brand_id"] == bid
    assert list((brands.folder(bid) / "cards").glob("video-*.mp4"))

def test_stuck_job_requeued_then_failed(clip_portrait, monkeypatch):
    v = videos.save_upload(clip_portrait.open("rb"), "p.mp4")
    videos.queue(v["id"], videos.Options())
    db.execute("UPDATE videos SET status='working', attempts=1, started_at='2000-01-01T00:00:00+00:00' WHERE id = ?", v["id"])
    monkeypatch.setattr(videos, "build_command", lambda *a, **k: ["ffmpeg", "-badflag"])
    videos.run_next()
    row = db.rows("SELECT status, attempts, error FROM videos WHERE id = ?", v["id"])[0]
    assert row["status"] == "failed" and row["attempts"] == 2 and row["error"]
```

- [ ] **Step 2:** Run `.venv\Scripts\python.exe -m pytest tests/test_videos.py -q`. Expected: FAIL (no module).
- [ ] **Step 3:** Implement `videos.py` and the table; `pipeline.draft_video` may start as the minimal version Task 9 defines so the tests run.
- [ ] **Step 4:** Run `.venv\Scripts\python.exe -m pytest -q`. Expected: all pass.
- [ ] **Step 5:** `git add -A; git commit -m "feat: recorded video processing jobs"`

---

### Task 9: Video → draft, caption, cover, upload screen

**Files:**
- Modify: `technews/pipeline.py` (`draft_video`, approve path for photo-from-video), `technews/agents/writer.py` (`video_caption`), `technews/__init__.py` (routes, `MAX_CONTENT_LENGTH`), `technews/templates/dashboard.html` (Upload video section)
- Test: `tests/test_videos.py`

**Interfaces:**
- Consumes: `videos.*` (Task 8), `brands.has("videos")` (Task 2)
- Produces:
  - `writer.video_caption(about: str, title: str) -> str` — with AI: business `about` + the user's line, only those facts, no hashtags; editor check as in `creator`; fallback without AI: `f"{about}\n\n{CTA}: {BRAND_EMAIL}"`; then `\n\n` + `flyers.hashtags()` (≤ 5)
  - `pipeline.draft_video(video_id: int, mp4: Path, cover: Path) -> int` — Reel: post with `image_path=cover`, `meta={"reel": {"video": mp4.name, "cover": cover.name, "style": "upload"}, "video_id": id}`, `writer="video"`; `as_post`: photo post from `frame_jpg(..., size=(1080, 1350))`, `meta={"video_id": id}`; then `notify.draft_ready(headline, "video reel" | "video post", media, post_id=…)`; sets `videos.post_id`. On approve, set `videos.posted_at` (in the existing `approve` success path)
  - `notify.draft_ready` labels: `"video reel": "Your video (Reel)"`, `"video post": "Photo from your video"`
  - `app.config["MAX_CONTENT_LENGTH"] = 330_000_000`; a 413 returns `{"error": "The video is over 300 MB."}`
  - Routes (owner/admin, business must have `videos`, else 403 `"Videos are switched off for this business."`): `POST /api/videos` (multipart `file`) → row; `GET /api/videos` → this business's rows; `GET /api/videos/<int:video_id>/source` → the original (HTTP range via `send_file(conditional=True)`); `GET /api/videos/<int:video_id>` → row + `frames` seconds; `GET /api/videos/<int:video_id>/frame.jpg?at=` → preview; `POST /api/videos/<int:video_id>/make` (Options fields) → `queued`; `POST /api/videos/<int:video_id>/cover` `{"at"}` → new cover on the waiting post; `POST /api/videos/<int:video_id>/delete`
  - Upload screen as in the spec sketch: file picker with progress bar, player with start/end handles, sound radio, logo/title toggles and title text, "About this video" line, cover choices, "Make Reel" / "Make post"; shows `queued → working → done` (polls `GET /api/videos/<id>` every 3 s) and the reason on failure

- [ ] **Step 1: Write the failing tests**

```python
def test_upload_to_reel_end_to_end(clip_landscape):
    c = _client()
    vid = c.post("/api/videos", data={"file": (clip_landscape.open("rb"), "office.mp4")}, content_type="multipart/form-data").get_json()["id"]
    assert c.post(f"/api/videos/{vid}/make", json={"about": "Our new Pune office", "logo": False, "title": False}).status_code == 200
    assert videos.run_next()
    post = db.rows("SELECT * FROM posts WHERE id = (SELECT post_id FROM videos WHERE id = ?)", vid)[0]
    assert post["status"] == "pending" and pipeline.reel_of(post)["video"].startswith("video-")
    assert "Our new Pune office" in post["caption"] and post["caption"].count("#") <= 5

def test_make_post_from_video(clip_portrait): ...  # as_post=True → no reel in meta, image 1080x1350

def test_videos_off_for_business():
    bid = make_brand("Bakery", kinds="flyers")
    c = client_as("baker", "Pass@1234", "owner", bid)
    assert c.post("/api/videos", data={}).status_code == 403

def test_approve_marks_video_posted(clip_portrait): ...  # approve in dry_run → videos.posted_at set, mp4 in outbox
```

- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `.venv\Scripts\python.exe -m pytest -q`. Expected: all pass.
- [ ] **Step 5:** `git commit -am "feat: upload a recorded video and get a Reel or post"`

---

### Task 10: Playwright end-to-end suite

**Files:**
- Create: `tests/e2e/conftest.py`, `tests/e2e/test_flows.py`, `pytest.ini` marker `e2e` (default run excludes it: `addopts = -m "not e2e"`)

**Interfaces:**
- `tests/e2e/conftest.py::server` (session fixture): temp `DATA_DIR`, env `IG_DELIVERY=dry_run`, `NVIDIA_API_KEY=`, `TELEGRAM_BOT_TOKEN=`, `SCHEDULER_ENABLED=0`, `ADMIN_USER=admin`, `ADMIN_PASSWORD=Admin@12345`; starts `.venv\Scripts\python.exe run.py` on a free port, waits for `/health` 200, yields base URL, stops it
- `page_as(username, password)` fixture helper using Playwright `sync_api` Chromium

- [ ] **Step 1: Write the flows** (each a test, using visible labels/roles, not CSS ids where a label exists)
  - `test_admin_onboards_business` — Add a business through all 7 steps (dry run, logo upload, no key) → readiness all required ticks → Switch on → appears in the switcher and the Businesses table
  - `test_owner_sees_only_own_business` — admin adds owner `baker` for Bakery; `baker` signs in: no switcher, no Settings/People/Sources; CloudTrio headline not on the page; `page.goto(base + "/api/posts/1/approve")`-style POST via `page.request` → 404
  - `test_idea_to_reel` — as admin on CloudTrio: Create, type an idea, choose Reel + "Simple: slide in", generate, send option 1 → Reels section shows it with a playable `<video>`
  - `test_video_upload_to_reel` — Upload video (3 s lavfi clip), set end 2 s, sound "original", Make Reel → status reaches "done" (wait ≤ 60 s) → it is in Reels → Approve → shows posted (dry run) 
  - `test_cloudtrio_still_drafts_news_and_flyers` — run a cycle with a seeded approved story and a flyer turn (`POST /api/cycle?post=1` via `page.request`) → Approvals shows a news card and a flyer for CloudTrio, Telegram-free
  - `test_phone_width` — viewport 390×844: Create, Upload video and Approvals have no horizontal scroll (`document.documentElement.scrollWidth <= 390`)
- [ ] **Step 2:** Run `.venv\Scripts\python.exe -m pytest tests/e2e -m e2e -q`. Expected: all pass. Fix the app (not the test) when a flow fails.
- [ ] **Step 3:** `git add -A; git commit -m "test: Playwright flows for every feature"`

---

### Task 11: Deploy files and the switch-over runbook

**Files:**
- Modify: `docker-compose.yml`, `deploy/deploy.sh`, `Jenkinsfile` (names → `socialmediapost`), `README.md`
- Create: `docs/switch-over.md`

**Interfaces:**
- `docs/switch-over.md` steps, in this order: (1) trial — run the new app in `dry_run` with `TELEGRAM_BOT_TOKEN` blank for 2–3 days, compare its CloudTrio drafts with `technews`; (2) switch — stop `technews`, copy its latest `data/technews.db`, `data/ig_session.json`, `data/cards`, `data/flyers`, `data/music` into the new data volume, set `IG_DELIVERY=instagram` and the Telegram token in the new `.env`, start `socialmediapost` on the same port behind the same Caddy site; (3) check — `/health`, one Telegram test message, one approval; (4) rollback — stop `socialmediapost`, start `technews` (its data was never changed)

- [ ] **Step 1:** Update the files; `docker compose config` parses without errors.
- [ ] **Step 2:** Run `.venv\Scripts\python.exe -m pytest -q` and the e2e suite once more. Expected: all pass.
- [ ] **Step 3:** `git commit -am "docs: deploy and switch-over for socialmediapost"`
