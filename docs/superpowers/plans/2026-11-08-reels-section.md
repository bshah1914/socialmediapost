# Reels Section Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a dedicated Reels workflow — 10 new animation layouts, a separate Reels dashboard section where any pending post (news or flyer) can be queued as a Reel, and a Telegram notification when a Reel is ready for approval.

**Architecture:** Extend `motion.py` with 4 new simple movements and `kinetic.py` with 6 new presets (extending the `Preset` dataclass with 3 optional fields). The pipeline auto-queues news posts as Reels in addition to flyers. The dashboard splits pending posts into two queues — still-image Approvals and Reels — and a new `notify.reel_queued()` function fires a Telegram message with Approve/Reject buttons when a Reel is ready.

**Tech Stack:** Python / Pillow / ffmpeg (existing); Jinja2 dashboard template; APScheduler pipeline; Telegram Bot API via existing `notify.py`.

**Spec:** Conversation context — user requested: (1) 10 new layouts (6 kinetic + 4 simple), (2) separate Reels section in dashboard, (3) any post can be made into a Reel, (4) Telegram notification on Reel queued.

## Global Constraints

- 1080×1920 px, 30 fps, H.264+AAC output — do not change encode parameters
- Existing 8 styles (bold, corporate, tech, offer, slide, zoom, reveal, countup) must keep identical output
- `Preset` dataclass new fields must have defaults so existing `STYLE` dict entries compile unchanged
- Reels section is only shown when `reels_ok` (ffmpeg available) — gate every new UI block on this flag
- Telegram notification reuses `notify.send()` / `notify.draft_ready()` patterns exactly
- Dashboard template changes must not break phone/tablet responsive layout

## Review Focus

- **News post with no number in headline passed to `countup`-derived styles** — `number_in()` returns None; style must fall back to `slide` (this fallback already exists for countup but must be re-verified for new styles that call it)
- **`make_reel()` called on a post whose image file was deleted** — `pipeline.make_reel()` returns `{"ok": False, "reason": ...}`; Reels section JS must display the reason, not silently fail
- **Reel notification fired twice** — `_insert_draft()` already calls `notify.draft_ready()`; `reel_queued()` must only fire when `make_reel()` is triggered from the API (not during `_insert_draft`)
- **`slow` preset on a flyer with 1 point** — `word_at` calculation must still terminate; `_layout()` runs before `slow` multiplier is applied, so the multiplier belongs in `frames()` not `_layout()`
- **Dashboard sent `pending_reel` list when ffmpeg unavailable** — template must check `reels_ok` before showing Reels nav item and section; pending_reel list may be non-empty from a prior ffmpeg-enabled run

---

## Task 1: 4 New Simple Motion Styles

**Files:**
- Modify: `technews/motion.py`

**Interfaces:**
- Produces: `STYLES`, `SIMPLE`, `SIMPLE_IDS` updated to include `drift`, `split`, `focus`, `float`; `frames()` handles all 4 new style strings

- [ ] **Step 1: Add `drift` to `frames()` in `motion.py`**

  In the `if style in ("slide", "countup"):` block, add `elif style == "drift":` branch.
  - `x`: starts at `W + 150`, eases to `(W - card_img.width) // 2` over 1.0 s
  - rotation: starts at 6°, eases to 0° over 1.0 s using `Image.rotate(angle, expand=True, resample=Image.BICUBIC)`; adjust `x, y` to re-centre after expand
  - after 1.0 s: subtle rightward drift (`x += int(0.012 * max(0, ct - 1.0) * card_img.width)`)
  - opacity: fade in over first 0.5 s (`_faded(card_img, _between(ct, 0, 0.5))`)

- [ ] **Step 2: Add `split` to `frames()` in `motion.py`**

  `elif style == "split":` branch:
  - For `ct < 1.2`: split `bg` into top half (`bg.crop((0,0,W,H//2))`) and bottom half; slide top up by `int((1-_ease(ct/1.2))*400)` px and bottom down the same amount; composite onto a black frame; card fades in with `_faded(card_img, _between(ct, 0.4, 1.0))`
  - For `ct >= 1.2`: normal centered card with gentle zoom (`grow = 1 + 0.02 * (ct - 1.2) / max(1, seconds - 1.2)`)

- [ ] **Step 3: Add `focus` to `frames()` in `motion.py`**

  `elif style == "focus":` branch:
  - blur radius decreases: `r = max(0, int(18 * (1 - _between(ct, 0.0, 1.8))))`, apply `card_img.filter(ImageFilter.GaussianBlur(r))`
  - scale decreases from 1.08 to 1.0 over first 1.8 s
  - opacity: fade in over first 0.5 s

- [ ] **Step 4: Add `float` to `frames()` in `motion.py`**

  `elif style == "float":` branch:
  - card appears instantly at `ct=0`; `y = card_top + int(14 * math.sin(ct * math.pi * 0.55))`
  - background drift: `bg` offset by `int(-ct * 8) % (H + 600)` (use same crop logic as `_background`)
  - `grow = 1.0` (no scaling)

- [ ] **Step 5: Update constants at top of `motion.py`**

  ```python
  SIMPLE: Final = (
      ("slide", "Simple: slide in"), ("zoom", "Simple: zoom"),
      ("reveal", "Simple: reveal"), ("countup", "Simple: count up"),
      ("drift", "Simple: drift in"), ("split", "Simple: split reveal"),
      ("focus", "Simple: focus in"), ("float", "Simple: float"),
  )
  STYLES: Final = (
      ("bold", "Bold (animated text)"), ("corporate", "Corporate (animated text)"),
      ("tech", "Tech (animated text)"), ("offer", "Offer (animated text)"),
      ("minimal", "Minimal (animated text)"), ("neon", "Neon (animated text)"),
      ("magazine", "Magazine (animated text)"), ("impact", "Impact (animated text)"),
      ("story", "Story (animated text)"), ("data", "Data (animated text)"),
      *SIMPLE,
  )
  ```

- [ ] **Step 6: Smoke-test locally**

  ```bash
  cd "C:\Automation Code\technews\agentic-ai"
  .venv\Scripts\python.exe -c "
  from pathlib import Path
  from technews import motion, db
  db.init()
  import os; os.environ['TECHNEWS_CLI']='1'
  # use the first card in data/cards
  imgs = list(Path('data/cards').glob('story-*.jpg'))
  assert imgs, 'no story cards found'
  for style in ('drift','split','focus','float'):
      out = Path('data/cards') / f'test-{style}.mp4'
      v, c = motion.render(imgs[0], out, style=style, track='none', seconds=6)
      assert v.exists() and c.exists(), f'{style} failed'
      print(f'{style}: OK {v.stat().st_size//1024}KB')
  "
  ```
  Expected: 4 lines each ending in `OK <n>KB`

- [ ] **Step 7: Commit**
  ```bash
  git add technews/motion.py
  git commit -m "feat(reels): add drift, split, focus, float motion styles"
  ```

---

## Task 2: 6 New Kinetic Presets

**Files:**
- Modify: `technews/kinetic.py`

**Interfaces:**
- Produces: `PRESETS`, `PRESET_IDS` updated; `Preset` dataclass has 3 new optional fields (`glow`, `big_hook`, `slow`); `STYLE` dict has 6 new entries; `auto_preset()` can return any of the 10 preset IDs; `Film._word()` handles `"slam"` word type

- [ ] **Step 1: Extend `Preset` dataclass**

  Add three optional fields with defaults so existing entries compile unchanged:
  ```python
  @dataclass(frozen=True, slots=True)
  class Preset:
      words: str   # punch | rise | type | slam
      wipe: bool
      pulse: bool
      grid: bool
      stamp: bool
      sweep: bool
      glow: bool = False      # neon glow halo behind each word
      big_hook: bool = False  # hook scene uses 1.6× font size (magazine)
      slow: bool = False      # word timing multiplied by 1.6 (story)
  ```

- [ ] **Step 2: Add 6 new entries to `STYLE` dict**

  ```python
  "minimal":   Preset("rise",  False, False, False, False, False),
  "neon":      Preset("punch", True,  True,  True,  False, True,  glow=True),
  "magazine":  Preset("punch", False, False, False, False, True,  big_hook=True),
  "impact":    Preset("slam",  True,  True,  False, False, False),
  "story":     Preset("rise",  False, False, False, False, False, slow=True),
  "data":      Preset("type",  False, False, True,  True,  False),
  ```

- [ ] **Step 3: Update `PRESETS` and `PRESET_IDS` constants**

  ```python
  PRESETS: Final = (
      ("bold","Bold"), ("corporate","Corporate"), ("tech","Tech"), ("offer","Offer"),
      ("minimal","Minimal"), ("neon","Neon"), ("magazine","Magazine"),
      ("impact","Impact"), ("story","Story"), ("data","Data"),
  )
  ```

- [ ] **Step 4: Handle `slow` in `Film._layout()`**

  After computing `self.word_at` list, multiply each entry by 1.6 when `self.p.slow`:
  ```python
  if self.p.slow:
      self.word_at = [at * 1.6 for at in self.word_at]
      self.words_end *= 1.6
  ```

- [ ] **Step 5: Handle `slam` in `Film._word()`**

  Add `elif self.p.words == "slam":` branch before the final `else: # rise`:
  ```python
  elif self.p.words == "slam":
      # word slams in full-screen (scale 6→1) then settles
      k = span(t, start, 0.35)
      if k <= 0:
          return
      scale = 6.0 - 5.0 * ease_back(k)
      put(fr, w["img"], w["x"], w["y"] - lift, alpha=a * ease(k * 3), scale=scale)
  ```

- [ ] **Step 6: Handle `glow` in `Film.hook()`**

  After `for w in lit:` marker loop and before `for w in self.words:` drawing loop, add:
  ```python
  if self.p.glow:
      for w in self.words:
          start = self.word_at[w["n"]]
          if span(t, start, 0.45) <= 0:
              continue
          glow_img = w["img"].filter(ImageFilter.GaussianBlur(22))
          put(fr, faded(glow_img, 0.55 * (1 - out)), w["x"] - 11, w["y"] - lift - 11, alpha=1.0)
  ```

- [ ] **Step 7: Handle `big_hook` in `Film._layout()`**

  When `self.p.big_hook`, increase headline font size cap by 1 step (max 128→160) and re-run wrap:
  ```python
  sizes = (160, 148, 136, 128, 116, 104, 94, 84, 76) if self.p.big_hook else (128, 116, 104, 94, 84, 76)
  for size in sizes:
      hf = font(size, mono=False)
      lines = wrap(f.headline, hf, width)
      if len(lines) <= 3:
          break
  ```

- [ ] **Step 8: Update `auto_preset()`**

  ```python
  def auto_preset(flyer: Any) -> str:
      text = " ".join([flyer.kicker, flyer.headline, *flyer.points]).lower()
      if figure(flyer.headline, *flyer.points) or re.search(r"\b(credits?|discount|free|offer)\b", text):
          return "offer"
      if re.search(r"\b(devops|kubernetes|terraform|ci/cd|pipeline|automation|monitoring|security)\b", text):
          return "tech"
      if re.search(r"\b(ai|llm|gpt|claude|gemini|model|neural|openai|anthropic)\b", text):
          return "neon"
      return "bold"
  ```

- [ ] **Step 9: Smoke-test all 6 new presets**

  ```bash
  .venv\Scripts\python.exe -c "
  import os; os.environ['TECHNEWS_CLI']='1'
  from pathlib import Path
  from technews import db, kinetic, motion, flyers
  db.init()
  all_flyers = flyers.listing()
  assert all_flyers
  f = all_flyers[0]
  for preset in ('minimal','neon','magazine','impact','story','data'):
      out = Path('data/cards') / f'test-kinetic-{preset}.mp4'
      film = kinetic.Film(f, preset=preset, bpm=112, handle='cloudtrio.business')
      v, c = motion.encode(film.frames(), out, seconds=film.length, track='none')
      assert v.exists(), f'{preset} failed'
      print(f'{preset}: OK {v.stat().st_size//1024}KB {film.length:.1f}s')
  "
  ```

- [ ] **Step 10: Commit**
  ```bash
  git add technews/kinetic.py
  git commit -m "feat(reels): add minimal, neon, magazine, impact, story, data kinetic presets"
  ```

---

## Task 3: Telegram Notification for Reel Queue + News Auto-Reel

**Files:**
- Modify: `technews/notify.py`
- Modify: `technews/pipeline.py`

**Interfaces:**
- Produces: `notify.reel_queued(headline, image, post_id)` — fires Telegram with Approve/Reject buttons; `pipeline._insert_draft()` auto-reels news posts when `reel_wanted()`; `pipeline.make_reel()` calls `notify.reel_queued()` after a successful reel render triggered from the API

- [ ] **Step 1: Add `reel_queued()` to `notify.py`**

  ```python
  def reel_queued(headline: str, image: Path | None, post_id: int = 0) -> None:
      """Tell the editor a Reel is ready to approve (fires in background)."""
      if not channels():
          return
      where = link()
      text = (f"Reel ready for approval: {headline}\n\n"
              f"Review at: {where}" if where else "Open the dashboard, Reels, to review it.")
      threading.Thread(
          target=send,
          args=("CloudTrio: a Reel is waiting for approval", text, image, post_id),
          daemon=True,
      ).start()
  ```

- [ ] **Step 2: Enable auto-Reel for news posts in `pipeline._insert_draft()`**

  Change the condition from `if flyer_id and reel_wanted():` to `if reel_wanted():`:
  ```python
  media, kind = Path(str(image)), "flyer" if flyer_id else "news"
  if reel_wanted():
      out = make_reel(post_id)
      if out["ok"]:
          media = settings.CARDS_DIR / out["video"]
          kind = f"animated {kind}"
  notify.draft_ready(headline, kind, media, post_id=post_id)
  ```

- [ ] **Step 3: Fire `reel_queued()` from `pipeline.make_reel()` when called via API**

  `make_reel()` does not know its caller. Instead, fire from the API handler in `__init__.py` (Task 4). No change needed here — `make_reel()` returns `{"ok": True, ...}` which the API handler checks.

- [ ] **Step 4: Verify `notify.reel_queued` signature matches `notify.send` pattern**

  ```bash
  .venv\Scripts\python.exe -c "
  import os; os.environ['TECHNEWS_CLI']='1'
  from technews import db, notify
  db.init()
  # channels() returns [] if Telegram not configured — that is fine
  print('channels:', notify.channels())
  notify.reel_queued('Test Reel Headline', None, 0)
  print('reel_queued: OK (no channels, skipped silently)')
  "
  ```

- [ ] **Step 5: Commit**
  ```bash
  git add technews/notify.py technews/pipeline.py
  git commit -m "feat(reels): auto-reel news posts; add reel_queued Telegram notification"
  ```

---

## Task 4: Backend — Split Pending Posts + Reels API

**Files:**
- Modify: `technews/__init__.py` (dashboard view + api_post_reel handler)

**Interfaces:**
- Consumes: `pipeline.draft_meta(post)` to check `meta["reel"]["video"]`; `notify.reel_queued()`
- Produces: `dashboard()` passes `pending_still` and `pending_reel` to template; `api_post_reel` fires `notify.reel_queued()` on success; `reels_pending` count available as `status["reels_pending"]` for nav badge

- [ ] **Step 1: Split pending in `dashboard()` view**

  After `pending = db.rows(...)` and `pending_meta = {p["id"]: pipeline.draft_meta(p) for p in pending}`:
  ```python
  pending_still = [p for p in pending if not (pending_meta.get(p["id"]) or {}).get("reel", {}).get("video")]
  pending_reel  = [p for p in pending if     (pending_meta.get(p["id"]) or {}).get("reel", {}).get("video")]
  ```
  Pass both to `render_template(...)` alongside existing `pending`.

- [ ] **Step 2: Add `reels_pending` to `status()` dict**

  In the `status()` function:
  ```python
  "reels_pending": int(db.rows(
      "SELECT COUNT(*) AS n FROM posts WHERE status IN ('pending','scheduled') AND brand_id = ? AND meta LIKE '%\"reel\": {%'",
      settings.BRAND_ID)[0]["n"]),
  ```

- [ ] **Step 3: Fire `notify.reel_queued()` from `api_post_reel` on success**

  In the `api_post_reel` handler, after `out = pipeline.set_reel(...)`, when `out["ok"]` is True and `body.get("on", True)` is True:
  ```python
  if out.get("ok") and body.get("on", True):
      found = db.rows("SELECT headline, image_path FROM posts WHERE id = ?", post_id)
      if found:
          img = settings.CARDS_DIR / Path(str(found[0]["image_path"]).replace("\\", "/")).name
          notify.reel_queued(found[0]["headline"], img if img.exists() else None, post_id)
  ```

- [ ] **Step 4: Verify template receives new variables**

  ```bash
  .venv\Scripts\python.exe -c "
  import os; os.environ['TECHNEWS_CLI']='1'
  from technews import create_app
  app = create_app()
  with app.test_client() as c:
      c.post('/login', data={'username':'brijesh','password':'adminadmin'}, follow_redirects=True)
      r = c.get('/')
      assert r.status_code == 200
      assert b'pending_reel' not in r.data  # template var not leaked to HTML
      print('dashboard OK', len(r.data), 'bytes')
  "
  ```

- [ ] **Step 5: Commit**
  ```bash
  git add technews/__init__.py
  git commit -m "feat(reels): split pending into still/reel queues; fire Telegram on reel queued"
  ```

---

## Task 5: Dashboard — Reels Section

**Files:**
- Modify: `technews/templates/dashboard.html`

**Interfaces:**
- Consumes: `pending_still`, `pending_reel`, `pending_meta`, `pending_notes`, `reels_ok`, `reel_styles`, `reel_tracks`, `status.reels_pending` from backend

- [ ] **Step 1: Add `reels` icon and nav item**

  In the `{% set icons = {...} %}` block, add:
  ```jinja
  'reels': '<path d="M15 10l-4-2.5v5l4-2.5z"/><rect x="2" y="6" width="20" height="12" rx="3"/>',
  ```

  In the nav `{% for key, label in [...] %}` loop, insert `('reels','Reels')` after `('approvals','Approvals')`.

  In the badge span condition on the nav link, also show badge for reels:
  ```jinja
  {% if key == 'approvals' and status.pending %}...{% endif %}
  {% if key == 'reels' and status.reels_pending %}<span class="badge">{{ status.reels_pending }}</span>{% endif %}
  ```

- [ ] **Step 2: Update Approvals section to use `pending_still` and allow news Reels**

  Change the loop from `{% for p in pending %}` to `{% for p in pending_still %}`.

  Change the Reel controls gate from `{% if reels_ok and p.flyer %}` to `{% if reels_ok %}` so news posts also get the Make as Reel button.

- [ ] **Step 3: Add Reels section after Approvals**

  Insert after `</section>` of `approvals`:
  ```html
  <!-- ============================================================ REELS -->
  <section id="reels" data-view="reels" hidden aria-labelledby="view-title">
    {% if not reels_ok %}
    <div class="card">Animated Reels need ffmpeg on the server. It is not available here.</div>
    {% elif pending_reel %}
    <div class="queue">
      {% for p in pending_reel %}
      {% set img = p.image_path.replace('\\', '/').split('/')[-1] %}
      {% set reel = (pending_meta.get(p.id) or {}).get('reel') or {} %}
      <article class="card review" id="reel-{{ p.id }}">
        <div>
          {% if reel.video %}
          <video src="/cards/{{ reel.video }}" poster="/cards/{{ reel.cover }}"
                 controls playsinline preload="metadata"
                 style="width:100%;max-height:70vh;border-radius:12px;background:#000"
                 aria-label="Reel preview: {{ p.headline }}"></video>
          {% else %}
          <img src="/cards/{{ img }}" alt="{{ p.headline }}">
          {% endif %}
        </div>
        <div>
          <h2>{{ p.headline }}</h2>
          <div class="dest">
            <span class="pill {{ 'flyer' if p.flyer else 'news' }}">{{ "flyer" if p.flyer else "news" }}</span>
            <span class="pill low">Instagram Reel</span>
            {% if p.url %}<a href="{{ p.url }}" target="_blank" rel="noopener">Source</a>{% endif %}
          </div>
          <label for="rcap-{{ p.id }}">Caption</label>
          <textarea id="rcap-{{ p.id }}">{{ p.caption }}</textarea>
          <div class="row" style="margin-top:8px">
            <label class="sr" for="rrs-{{ p.id }}">Style</label>
            <select id="rrs-{{ p.id }}" style="max-width:14em">
              {% for k, l in reel_styles %}<option value="{{ k }}" {{ 'selected' if reel.style == k }}>{{ l }}</option>{% endfor %}
            </select>
            <label class="sr" for="rrt-{{ p.id }}">Music</label>
            <select id="rrt-{{ p.id }}" style="max-width:13em">
              {% for k, l in reel_tracks %}<option value="{{ k }}" {{ 'selected' if reel.track == k }}>{{ l }}</option>{% endfor %}
            </select>
            <button class="btn ghost" type="button" onclick="reelSetR({{ p.id }}, this)">Redo Reel</button>
          </div>
          {% if p.flyer %}
          <details style="margin-top:6px"><summary>Animated text options</summary>
            <div class="two" style="margin-top:8px">
              <div><label for="rrh-{{ p.id }}">Words to highlight</label><input id="rrh-{{ p.id }}" type="text" maxlength="120" value="{{ (reel.highlight or [])|join(', ') }}"></div>
              <div><label for="rrn-{{ p.id }}">Big number to count up</label><input id="rrn-{{ p.id }}" type="text" maxlength="30" value="{{ reel.number or '' }}"></div>
              <div><label for="rrb-{{ p.id }}">Stamp text</label><input id="rrb-{{ p.id }}" type="text" maxlength="40" value="{{ reel.badge or '' }}"></div>
            </div>
          </details>
          {% endif %}
          <div class="row" style="margin-top:12px">
            <button class="btn" type="button" onclick="reelApprove({{ p.id }}, this)">Approve &amp; post as Reel</button>
            <button class="btn ghost" type="button" onclick="decide({{ p.id }}, 'reject', this)">Reject</button>
          </div>
          <div class="msg" id="reel-msg-{{ p.id }}" role="status" aria-live="polite"></div>
        </div>
      </article>
      {% endfor %}
    </div>
    {% else %}
    <div class="card empty">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 10l-4-2.5v5l4-2.5z"/><rect x="2" y="6" width="20" height="12" rx="3"/></svg>
      <b>No Reels waiting</b><br>Go to Approvals and click "Make as Reel" on any post.
    </div>
    {% endif %}
  </section>
  ```

- [ ] **Step 4: Add `reelSetR()` and `reelApprove()` JS functions**

  Near the existing `reelSet()` function, add:
  ```javascript
  async function reelSetR(id, btn) {
    const style = val(`rrs-${id}`), track = val(`rrt-${id}`);
    const highlight = (val(`rrh-${id}`) || "").split(",").map(s => s.trim()).filter(Boolean);
    const number = val(`rrn-${id}`) || "";
    const badge  = val(`rrb-${id}`) || "";
    await runJob(`/api/posts/${id}/reel`, btn, {on:true, style, track, highlight:highlight.join(","), number, badge});
    setTimeout(() => location.reload(), 1200);
  }
  async function reelApprove(id, btn) {
    const caption = val(`rcap-${id}`) || undefined;
    const r = await post(`/api/posts/${id}/approve`, {caption});
    const msg = $(`reel-msg-${id}`);
    if (r.posted) { msg.className = "msg ok"; msg.textContent = "Posted as a Reel ✓"; btn.closest("article").style.opacity = ".4"; }
    else { msg.className = "msg err"; msg.textContent = r.reason || "Could not post"; btn.disabled = false; }
  }
  ```

- [ ] **Step 5: Add Reels to the view titles and subtitles JS map**

  Find the `VIEWS` or `titles` JS object in the dashboard and add:
  ```javascript
  reels: { title: "Reels", sub: "Animated posts waiting for your approval before going to Instagram." },
  ```

- [ ] **Step 6: Smoke-test dashboard renders without errors**

  ```bash
  .venv\Scripts\python.exe -c "
  import os; os.environ['TECHNEWS_CLI']='1'
  from technews import create_app
  app = create_app()
  with app.test_client() as c:
      c.post('/login', data={'username':'brijesh','password':'adminadmin'}, follow_redirects=True)
      r = c.get('/')
      assert r.status_code == 200
      assert b'id=\"reels\"' in r.data
      assert b'reelApprove' in r.data
      print('Reels section present:', 'OK')
  "
  ```

- [ ] **Step 7: Commit**
  ```bash
  git add technews/templates/dashboard.html
  git commit -m "feat(reels): add Reels dashboard section with 10-style picker and Telegram notification"
  ```

---

## Task 6: Deploy to Staging and Smoke-test End-to-End

**Files:** None (deploy only)

- [ ] **Step 1: Build and push to staging**
  ```bash
  cd "C:\Automation Code\technews\agentic-ai"
  docker build -q -t cloudtrio/newsroom:staging .
  docker save cloudtrio/newsroom:staging | ssh brijesh@192.168.50.183 \
    "docker load && cd ~/cloudtrio-newsroom-staging && docker compose up -d --force-recreate 2>&1 | tail -3"
  ```

- [ ] **Step 2: Verify staging health**
  ```bash
  ssh brijesh@192.168.50.183 "curl -fs http://192.168.50.183:7087/health"
  ```
  Expected: `{"ok":true}`

- [ ] **Step 3: Open staging dashboard and verify**

  Open `http://192.168.50.183:7087`, sign in, confirm:
  - "Reels" appears in the sidebar nav
  - Approvals shows non-reel pending posts
  - "Make as Reel" button appears for news posts (not just flyers)
  - After clicking "Make as Reel", post moves to Reels section
  - All 10 new styles appear in the style dropdown
  - Approving from Reels section calls `/api/posts/<id>/approve`

- [ ] **Step 4: Promote to live (after staging confirmed)**
  ```bash
  ssh brijesh@192.168.50.183 "docker tag cloudtrio/newsroom:staging cloudtrio/newsroom:latest && cd ~/cloudtrio-newsroom && docker compose up -d --force-recreate 2>&1 | tail -3"
  ```
