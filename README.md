# socialmediapost: every business, one console (Instagram posts, Reels, flyers, tech news)

A Flask app with a scheduler that watches IT / tech news in near real time (hacks, breaches,
zero-days, scams, AI launches, big tech, outages, viral tech moments) and posts only the top
stories to Instagram as image cards.

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

```bash
.venv/bin/python -m pytest -q
```
