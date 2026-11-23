# socialmediapost — design

Date: 2026-11-23 · Status: waiting for approval

## Intent

One console to run social media for several businesses. Today the `technews` app runs one page
(CloudTrio, tech news + promo flyers, Instagram). The owner has other, different businesses and wants
them all in one place, each kept fully separate, plus two new ways to make content: **a Reel straight
from an idea**, and **a post or Reel from a video the business recorded**.

The new app is `C:\Automation Code\socialmediapost`, started as a copy of `technews/agentic-ai`.
CloudTrio moves into it as business #1. `technews` is not edited and keeps running until the switch.

## Goals

1. Many businesses in one console, onboarded from the console.
2. Each business separate: its own Instagram account, NVIDIA key, files, posts, limits, voice.
3. Admins see and manage every business; every other login sees only its own business.
4. One Telegram bot for all; every message says which business it is for.
5. Any business can make a Reel from an idea (Create page) in one step.
6. Any business can upload a recorded video and get a Reel (or a photo post) waiting for approval.
7. CloudTrio works exactly as it does today, proven by tests and a dry run, before the switch.

## Non-goals

- Self-service sign-up or billing. Admins create businesses and their logins.
- Platforms other than Instagram (LinkedIn stays as it is today: CloudTrio only).
- AI that watches a video and picks the best parts, spoken-word subtitles, cutting one long video
  into many Reels. (Possible later; each costs per video.)

## Anti-goals (a failure even if it "works")

- One business ever seeing, posting to, or spending from another business's Instagram, key, posts or files.
- CloudTrio posting less well, less often, or differently than today after the switch.
- A problem in one business (bad key, Instagram block, broken video) stopping the others.
- Two apps fighting over the Telegram bot (only one app polls it at any time).

## Constraints

- Builds on the existing stack: Flask + Jinja dashboard, APScheduler, SQLite (Postgres optional),
  Pillow + ffmpeg (`imageio-ffmpeg` on a dev machine), instagrapi, NVIDIA NIM via `agents/llm.py`.
- Telegram bot answers by polling `getUpdates` (`telegram.py`): one app per bot token.
- Instagram Reels: 1080×1920, up to 3 minutes; 5 hashtags max (existing `HASHTAG_LIMIT`).
- All 91 existing tests pass in the new app before and after every change.
- Deployed the same way (Docker compose + Caddy on post.cloudtrio.in).

## Approaches considered

| | Approach | Trade-off | Decision |
|---|---|---|---|
| 1 | **One new app `socialmediapost`, all businesses incl. CloudTrio** | One console, one bot, one codebase; needs a careful migration of CloudTrio | **Chosen** (by the owner) |
| 2 | Keep `technews` for CloudTrio, new app for the others | CloudTrio untouched, but two consoles, a second Telegram bot, two codebases drifting apart | Rejected |
| 3 | Edit `technews` in place | No migration, but risks the live CloudTrio page while building | Rejected: CloudTrio must stay safe |

Within approach 1, the existing brand system (`brands.py`: a `brands` table, `brands.use()` running
code as one brand through `config.override`) is extended rather than replaced. It already separates
Instagram logins and sessions, hashtags, flyers, posts and daily limits, and the scheduler already
loops over brands with per-brand error isolation.

## Design

### A. Businesses (extends `brands`)

**New fields on a business:** `about` (what it does, who it serves, how it talks — fed to every
writing agent instead of CloudTrio text), `kinds` (which content it makes: `news`, `flyers`, `ideas`,
`videos`), `nvidia_key` (sealed with `secretbox`, like the Instagram password), `telegram_chat`
(optional extra chat for this business), `colors` (brand colours for flyers/titles), plus existing
logo, handle, hashtags, Instagram login, posts per day, categories (news tags).

- **News only for businesses with `news` on.** A business with news off never gets stories, whatever
  its categories. CloudTrio: news + flyers + ideas + videos on, categories blank (as today).
- **NVIDIA key per business.** `brands.overrides()` sets `NVIDIA_API_KEY` from the business's key.
  A business with no key gets template copy (the existing no-AI fallback), never another business's key.
  CloudTrio (#1) keeps using the `.env` key, so nothing changes for it.
- **Files per business.** New businesses keep cards, videos, uploads, logo and Instagram session under
  `data/brands/<id>/`. CloudTrio (#1) keeps today's paths, so its existing files and session still work.
- **Per-business failure isolation.** Every per-brand loop already catches errors per brand; the same
  is enforced for the new video jobs and Telegram. An Instagram pause is already per account.

**Onboarding (admin only):** a guided "Add a business" page, in steps, each saved as you go:
1. Name, what it does (`about`), who it is for, tone. 2. Kinds of content. 3. Logo and colours.
4. Instagram login (existing connect flow, incl. 6-digit code / browser session). 5. NVIDIA key
(with a "test key" button). 6. Hashtags, posts per day, Telegram chat (with "send test message").
7. Readiness check: lists what is missing; the business is switched on only when it passes
(or the admin switches it on in dry run).

### B. Logins and access

- **Admin** (`brand_id` empty): every business, onboarding, keys, users, global settings.
- **Owner** (one business): approve/reject/schedule, Create, upload videos, edit that business's
  profile, logo, hashtags and Instagram connection. Cannot see keys or other businesses.
- **Viewer** (one business): read only.
- Enforced on the server for every request: the business comes from the login, never from the page,
  for non-admins; every post/flyer/video/idea lookup is filtered by business; media files are served
  only to logins of that business. Tests cover a user trying another business's post id, file name and
  API calls.
- Migration: the existing admin stays admin. Old role `approver` maps to `owner` (none exist today).

### C. Telegram

- One bot. The admin chat (`TELEGRAM_CHAT_ID`) gets every business's messages; each message title
  starts with the business name (no more fixed "CloudTrio:").
- A business with `telegram_chat` set also gets its own drafts there, and only those.
- An Approve/Reject tap is accepted only from the admin chat or that post's business chat.

### D. Reel from an idea (Create page)

- The Create page gets "Make it as: Flyer · Reel". With Reel chosen it also shows animation style and
  music (the existing `motion.STYLES` / tracks), defaulting to the business's usual.
- Send makes the flyer draft as today, then immediately `make_reel()` with those choices, so it lands
  in the Reels queue in one step, with the existing `reel_queued` Telegram message (once, not twice).
- The daily/weekly Reel limit (`REELS_PER_WEEK`) does not block a Reel the user asked for.

### E. Recorded video → Reel or post

1. **Upload** (owner/admin): MP4/MOV, up to 300 MB and 3 minutes, stored in the business's folder.
   Upload size and type checked on the server (real file check with ffprobe, not just the name).
2. **Edit screen:** play the video, set start and end (trim; result at most 3 minutes), sound
   (original · business music · both, original lowered), logo in a corner (on/off), title at the
   start (on/off, text editable), one line "about this video".
3. **Processing runs in the background** as a job (table `video_jobs`: queued → working → done/failed,
   with the reason), so the dashboard never hangs. One job at a time per server; a failed job keeps
   the original and shows why. ffmpeg: scale/crop to 1080×1920 (sideways video gets a blurred fill),
   trim, overlay logo/title, mix audio, H.264+AAC 30 fps (same encode as today's Reels).
4. **Caption:** the writer agent writes caption + hashtags from the one line, the business `about` and
   hashtags, using only what was given (same rules as the Idea creator; editor checks numbers).
5. **Cover:** a few frames suggested; the user can pick another.
6. **Result:** a pending post in Reels (or, with "Make post", a photo post from the chosen frame),
   Telegram message to admin + business chat, posted by `instagram.publish_reel` on approval.
7. Originals are deleted 30 days after the post goes up (keeps disk use in check); setting per server.

### F. Reliability and switch-over

- Copy CloudTrio data into the new app (database, Instagram session, cards, flyers, music, backups),
  run the full test suite, then run in `dry_run` with Telegram off and compare drafts with `technews`.
- Switch: stop `technews`, copy the latest database again, start `socialmediapost` on the same address.
  Rollback: stop the new one, start the old one.
- Health page shows status per business (Instagram connected, key working, last post, last error).

## Testing

- Existing 91 tests + new unit tests for each part (access rules, per-business key, news off,
  Telegram chat checks, idea → Reel, video job states, ffmpeg command, caption limits).
- **Playwright end-to-end tests** (Python Playwright, already installed in the venv), against the app
  in `dry_run`: admin onboards a business; owner logs in and sees only theirs; owner cannot open
  another business's post by URL; Create → Reel lands in Reels; upload a small test video → trim →
  Reel in Reels; approve → file in outbox; CloudTrio news cycle and flyer still produce drafts.

## Calls I made (check these)

- Business roles are **owner** and **viewer**; only admins see keys.
- No key → template copy, not a shared key.
- New businesses' files under `data/brands/<id>/`; CloudTrio keeps its current paths.
- Video limits: 300 MB, 3 minutes (Instagram's current Reel limit; earlier I said 90 s — 3 min is right).
- Originals deleted 30 days after posting.
- One video job at a time (a small server; easy to raise later).
- A Reel the user asks for ignores the weekly Reel limit.

## Left to the builder

Exact table columns and migrations, page layout details within the existing dashboard style,
ffmpeg filter graph, job runner (thread vs APScheduler job), file naming.
