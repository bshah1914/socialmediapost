# Moving CloudTrio from `technews` to `socialmediapost`

`technews` keeps running until the very last step, and its data is never changed, so going back is always one
command away. Do the steps in order.

## 1. Trial (2–3 days, nothing reaches Instagram or Telegram)

1. In the new app's `.env` (on the server: the `socialmediapost-data` volume, `/data/.env`):
   - `IG_DELIVERY=dry_run`
   - `TELEGRAM_BOT_TOKEN=` (blank). **Important:** the live app owns the bot. If both apps send alerts with buttons,
     a tap can reach the wrong app and act on the wrong post.
2. Copy CloudTrio's data from the live app into the new volume (read-only on the old side):
   `technews.db`, `ig_session.json`, `cards/`, `flyers/`, `music/`, `brand-logo-1.png` if present.
3. In the copied database the saved setting may still say live posting. Open the new dashboard, **Settings, Instagram**,
   and check it shows **Dry run**. Change it there if not.
4. Start it next to the old one: `deploy/deploy.sh staging` builds it and runs it on the server as
   `socialmediapost-staging` on port **7088** (folder `~/socialmediapost-staging`). The old app keeps 7086 (live) and
   7087 (its staging).
5. Each day, compare: the new app's **Approvals** and its `outbox/` folder against what `technews` posted. CloudTrio's
   news cards, flyers and Reels should look the same.
6. Onboard your other businesses in the new app (Settings, **Add a business**), with **Trial run** ticked, and check
   their drafts.

## 2. Switch (about 10 minutes)

1. Stop `technews`: `docker compose down` in its folder. (Its volume stays as it is.)
2. Copy its latest `technews.db` and `ig_session.json` into the new volume again (they changed during the trial).
3. In the new `.env`: `IG_DELIVERY=instagram`, and the real `TELEGRAM_BOT_TOKEN`.
4. `deploy/deploy.sh promote`: the staged image becomes `socialmediapost` in `~/socialmediapost` on port **7086**, the
   port `post.cloudtrio.in` already points to. (Set that folder's compose file to publish `7086:5000` and to use the
   `socialmediapost-data` volume holding the copied data.)

## 3. Check

- `https://post.cloudtrio.in/health` answers `{"ok": true}`.
- Settings, **Send a test notification**: Telegram shows "CloudTrio: test notification".
- Approve one waiting CloudTrio post (or make one with **Run full cycle**) and see it on Instagram.
- Businesses table: every business shows the right Instagram account and "Live" or "Trial run" as you intended.

## 4. Going back (if anything looks wrong)

1. `docker compose down` in `~/socialmediapost` on the server.
2. `docker compose up -d` in `~/cloudtrio-newsroom` (the old app, still on port 7086).

The old app picks up where it stopped. Posts made by the new app in between stay on Instagram; nothing else changes.
