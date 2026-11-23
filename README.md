# socialmediapost: every business, one console (Instagram posts, Reels, flyers, tech news)

One console for the Instagram pages of several businesses. Each business is kept apart: its own
Instagram account, NVIDIA key, files, posts, hashtags, voice and daily limit. CloudTrio (business #1)
also runs the tech-news newsroom: it watches IT / tech news in near real time and posts the top
stories as image cards, with marketing flyers between them.

## Businesses

- **Add a business** (Settings, admins): seven steps (profile, what it posts, logo and colours, Instagram,
  its own AI key, hashtags and Telegram chat) and a readiness check before it is switched on. Tick
  **Trial run** to make its posts without putting them on Instagram.
- **What it posts**: any of tech news, flyers, posts and Reels from ideas, its own videos.
- **People**: admins see every business; an **owner** (approves, creates, uploads) or **viewer** (looks)
  belongs to one business and never sees the others.
- **AI key**: a business uses only its own key; without one its captions come from templates.
- **Telegram**: one bot. The admin chat gets every business's alerts, each starting with the business
  name; a business can add its own chat, which gets only its alerts and may approve only its posts.
- **Reel from an idea**: Create, *Make it as: Reel*, pick the animation and music.
- **Upload video**: MP4/MOV up to 300 MB, trimmed to 3 minutes or less, fitted to 1080x1920 (a sideways video
  gets a blurred fill), logo, title, sound (original, music, or both), cover, then a Reel or a photo post
  waiting for approval. Originals are deleted 30 days after posting.

Moving the live CloudTrio page over from `technews`: see `docs/switch-over.md`.

## The agent team (NVIDIA NIM)

Every agent runs on NVIDIA's hosted models (`integrate.api.nvidia.com`, OpenAI-compatible),
with one job each, a JSON contract and a fallback model chain.

```
 feeds + Google News ─▶ group duplicates ─▶ 🛰  News Scanner ─▶ 🧑‍⚖️ Reviewer ─▶ ✍️ Writer ─▶ 🧑‍⚖️ Reviewer ─▶ card ─▶ Instagram
 (10 tech/security     (one story, many     tech only?          approves only      card text     checks the     1080x1350
  feeds + 9 searches)   outlets = buzz)     category, 0-10      the top stories    + caption     draft          JPEG
```

| Agent | File | Job |
|---|---|---|
| News Scanner | `technews/agents/scanner.py` | Keeps only IT / tech news, tags a category, scores 0-10 for buzz |
| Reviewer | `technews/agents/reviewer.py` | Picks the top stories side by side (impact, buzz, freshness, credibility); never approves a single unknown source; then approves each draft: **every number must come from the story**, no hype |
| Writer | `technews/agents/writer.py` | Card headline + 2-3 points + caption + 5 hashtags; a rejected draft goes back once with the reviewer's notes, then a template is used |

Without `NVIDIA_API_KEY` everything still runs on keyword scoring and template captions.

## Instagram

The publisher (`technews/instagram.py`) is ported from CopyTrading: instagrapi (unofficial
client), with a saved session that is reused and never re-logged in by the scheduler. It
also handles the "We restrict certain activity" flag: if the post went up anyway it pauses
`IG_WARNED_PAUSE_MINUTES`, if not it pauses `IG_RESTRICT_PAUSE_HOURS`. Posting is also
limited to `POSTS_PER_DAY` and `MIN_POST_GAP_MINUTES` apart.

`IG_DELIVERY=dry_run` (default) only writes each card + caption to `data/outbox/`.

## Run

```bash
uv venv -p 3.12 .venv && uv pip install -p .venv -r requirements.txt
cp .env.example .env          # add NVIDIA_API_KEY (build.nvidia.com)
.venv/bin/python run.py       # dashboard http://localhost:5000 + scheduler
```

Going live on Instagram:

```bash
# .env: IG_DELIVERY=instagram, IG_USERNAME, IG_PASSWORD, IG_HANDLE
./ig-login             # once; enter the 6-digit code
./ig-login --browser   # if Instagram says "Please wait a few minutes": paste the
                       # sessionid cookie from instagram.com (DevTools > Application > Cookies)
```

Use `.venv/bin/flask`, not a system `flask`. CLI: `flask --app run cycle [--no-post]`, `flask --app run post <story_id>`.

## API

| | |
|---|---|
| `GET /` | dashboard: posts, stories with scores and reasons, run buttons |
| `GET /health` | delivery, model, next run, pacing, last cycle, last error |
| `GET /api/stories?status=approved` | stories (`new`, `approved`, `rejected`, `posted`, `low`, `not_tech`, `failed`) |
| `GET /api/posts` | posts made |
| `POST /api/cycle[?post=0]` | run a cycle now |
| `POST /api/stories/<id>/post` | editor override: post this story now |

POST endpoints need `X-Admin-Token` when `ADMIN_TOKEN` is set.

## Tests

Browser tests (Playwright; start the app themselves, in dry run, with an empty data folder):

```bash
.venv/bin/python -m playwright install chromium   # once
.venv/bin/python -m pytest tests/e2e -m e2e -q
```

Unit and API tests:

```bash
.venv/bin/python -m pytest -q
```
