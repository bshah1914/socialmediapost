# SDD ledger — plan: C:/Automation Code/socialmediapost/docs/superpowers/plans/2026-11-23-socialmediapost.md
Spec: docs/superpowers/specs/2026-11-23-socialmediapost-design.md
Baseline in technews (read-only run): 89 passed, 2 skipped.

## Pre-flight
- T2→T3: brands.has / settings.BRAND_ABOUT — produced and consumed with the same names. OK.
- T2→T4: per-business CARDS_DIR (overrides) is what isolates /cards for owners. OK.
- T2→T5: brands.telegram_chat column — used by notify.chats_for / telegram.handle. OK.
- T2→T6: readiness reads about, kinds, logo_path, session_user, delivery, nvidia_key, hashtags. OK.
- T4→T9: owner_ok names api_videos_upload, api_video_make, api_video_cover, api_video_delete — T9 routes must use exactly these endpoint names (plus read-only GETs). Ruling below.
- T8→T9: pipeline.draft_video(video_id, mp4, cover) -> int; videos.Options.as_post read by draft_video. OK.
- T8 tests use `_post_of(v)` which no task defines. Ruling below.

## Rulings
Task 1: Ruling: Python 3.11 venv instead of 3.12 — only 3.11 is installed; the old venv and the Dockerfile both use 3.11 — none.
Task 1: Ruling: the source folder was a git repo, so its history came along; removed `origin` from the copy and committed the live working tree (incl. uncommitted live changes) as the start commit on `main`, work on branch `build/socialmediapost` — keeps history, makes no push to the old GitHub repo possible, keeps main clean — none.
Task 8: Ruling: `_post_of(v)` is a test helper in tests/test_videos.py returning `videos.post_id` for the row — tests only.
Task 9: Ruling: the video endpoints are named api_videos_upload (POST /api/videos), api_video_make, api_video_cover, api_video_delete, plus GET api_videos, api_video, api_video_source, api_video_frame — matches Task 4 owner_ok.
Task 1: complete (commits 38112cb..1a4f8dd, tests: .venv/Scripts/python.exe -m pytest -q -p no:cacheprovider → 89 passed, 2 skipped in 196.12s (0:03:16))
Task 2: Ruling: tests/test_agents.py:1141 asserted the old session file name ig_session_<id>.json; changed it to the per-business folder path the spec requires (data/brands/<id>/ig_session.json); old files are moved there on first use — a server that already had business #2+ sessions keeps them — none.
Task 2: Ruling: make_brand() seeds business #1 first (a fresh test DB otherwise gives the first new business id 1) — test helper only — none.
Task 2: Ruling: new business fields (about, kinds, telegram_chat, colors, delivery, key) are written by save() only when present in the request, so the existing brand form keeps working unchanged — none.
Task 2: complete (commits 1a4f8dd..6ef869e, tests: .venv/Scripts/python.exe -m pytest -q -p no:cacheprovider → 98 passed, 2 skipped in 203.24s (0:03:23))
Task 3: Ruling: test_each_brand_has_its_own_settings_flyers_and_posts created a news business via the old form without kinds; new businesses default to no news (spec: kinds picked at onboarding), so the test now sends kinds "news,flyers" explicitly — a brand made through the old form posts no news until news is ticked — low (the onboarding step sets kinds).
Task 3: complete (commits 6ef869e..e175b30, tests: .venv/Scripts/python.exe -m pytest -q -p no:cacheprovider → 105 passed, 2 skipped in 203.11s (0:03:23))
Task 4: Ruling: the old role name "approver" is accepted as an alias of "owner" (API and migration), and a person added without brand_id joins the business the admin has open — keeps the existing team test and API callers working — low.
Task 4: Ruling: the business check only refuses a row that exists and belongs to another business; a missing id keeps the route own answer (e.g. "not waiting for approval") — no behaviour change for missing rows — none.
Task 4: Ruling: the leak test uses a headline that appears nowhere else, plus a positive control (CloudTrio owner does see it); the built-in flyer placeholder text "Cut your AWS bill..." in the editor is static, not data — none.
Task 4: Ruling: owners/viewers also lose Stories when their business has no news, and the "Scan & review"/"Run full cycle" buttons (admin-only calls) — matches what they may do — none.
Task 4: Ruling: template test written after the change; proved by running it against the stashed old template (FAIL) then the new one (PASS) — none.
Task 4: complete (commits e175b30..7176943, tests: .venv/Scripts/python.exe -m pytest -q -p no:cacheprovider → 119 passed, 2 skipped in 248.12s (0:04:08))
Task 5: Ruling: business alerts (drafts, Reels, comments, weekly report, festival flyer, Instagram logout, withdrawn/failed posts) use notify.title() and reach the business chat; server-wide alerts (backup, site down, watchdog, test message) keep their titles and go to the admin chat only — they are about the server, not one business — low.
Task 5: Ruling: notify.reel_queued now returns its thread (like draft_ready) so callers/tests can wait — no caller relied on None — none.
Task 5: Ruling: tests create their posts with alerts off (_quiet_post) because the draft alert fires in the background and raced the assertions — test only — none.
Task 5: complete (commits 7176943..a5f098a, tests: .venv/Scripts/python.exe -m pytest -q -p no:cacheprovider → 130 passed, 2 skipped in 223.86s (0:03:43))
Task 6: Ruling: brands.save() and the existing edit card can still switch a business on directly (admin override); the readiness gate is on the onboarding path (/enable) — the existing Acme test and admins keep a manual switch — low.
Task 6: Ruling: the onboarding screen keeps every answer and sends the whole business on each save, because brands.save() blanks fields it is not given — none.
Task 6: Ruling: onboarding UI has no unit test; it is covered by the Playwright flow test_admin_onboards_business (Task 10); scripts syntax-checked with node --check on the rendered page — medium until Task 10 runs.
Task 6: Ruling: removed the old one-field "Add a brand" form (replaced by the 7-step setup); edit card gained about, kinds, own key, trial run and Telegram chat — none.
Task 6: complete (commits a5f098a..4ff4b94, tests: .venv/Scripts/python.exe -m pytest -q -p no:cacheprovider → 137 passed, 2 skipped in 345.38s (0:05:45))
Task 7: Ruling: added test_real_reel_for_a_new_business_lands_in_its_folder (real ffmpeg render, 3 s) beyond the plan, because the other tests stand in for make_reel and would not catch a per-business folder problem — none.
Task 7: Ruling: Create page Reel controls appear only when ffmpeg is available (reels_ok), like the rest of Reels; when the Reel fails, the flyer waits and the page says why — none.
Task 7: complete (commits 4ff4b94..c26e1a6, tests: .venv/Scripts/python.exe -m pytest -q -p no:cacheprovider → 143 passed, 2 skipped in 572.68s (0:09:32))
Task 8: Ruling: frame_jpg takes a file path (not a video id) so the Reel cover is taken from the rendered Reel (blurred fill, logo, title as people will see it) — none.
Task 8: Ruling: new setting VIDEO_WORKER (default on) starts the background worker as soon as a video is queued; tests set it off and run jobs themselves; the scheduler also drains the queue every minute — none.
Task 8: Ruling: a job claims kv "video-job:<id>" while it runs, so two processes (web + scheduler on Postgres) never make the same video; released at the end — none.
Task 8: Ruling: pipeline.draft_video is complete for Reels here (caption = the line about the video + hashtags); Task 9 adds the AI caption and the photo-post path — none.
Task 8: Ruling: phone videos with rotation metadata are probed with width/height swapped, since ffmpeg turns them upright when decoding — none.
Task 8: complete (commits c26e1a6..c6d639d, tests: .venv/Scripts/python.exe -m pytest -q -p no:cacheprovider → 162 passed, 2 skipped in 641.28s (0:10:41))
Task 8: Ruling: its completion line spans c26e1a6..c6d639d because the Task 9 server commit landed while the Task 8 suite ran in the background (the suite itself ran on Task 8 code loaded at start) — none.
Task 9: Ruling: Task 9 tests live in tests/test_video_flow.py (not test_videos.py) and the clip fixtures moved to tests/conftest.py so both files share them — none.
Task 9: Ruling: tests were written while the Task 8 suite ran and the code followed before they were run; proved by stashing the code (12/12 FAIL) then restoring it (12/12 PASS) — none.
Task 9: Ruling: the editor review of a video caption is stored as warnings on the post (like Idea creator drafts), not used to replace the caption — the person sees the note in Approvals — low.
Task 9: Ruling: without AI the video caption is the line given + "Talk to us: <contact>" + up to 5 business hashtags (REEL_CTA is CloudTrio-specific, so not used) — none.
Task 9: Ruling: Upload video is hidden for businesses without "videos" or without ffmpeg — none.
Task 9: complete (commits 4c6c058..6ae166f, tests: .venv/Scripts/python.exe -m pytest -q -p no:cacheprovider → 175 passed, 2 skipped, 9 deselected in 685.92s (0:11:25))
Task 10: Ruling: e2e helpers live in tests/e2e/helpers.py with tests/e2e/__init__.py (two conftest.py files of the same name would clash); pytest.ini leaves e2e out of the default run (addopts -m "not e2e") — none.
Task 10: Ruling: the e2e app runs with REELS_PER_WEEK=0 so queueing a flyer does not render an automatic Reel (not what these flows check, and slow) — none.
Task 10: Ruling: "post this story now" publishes at once (dry run: outbox), so the CloudTrio news check looks in the outbox and Posts, and the flyer in Approvals — none.
Task 10: Ruling: the owner-isolation flow uses its own unique flyer headline, because the flyer editor placeholder repeats a built-in CloudTrio flyer headline — none.
Task 10: Ruling: every e2e page fails its test on any JavaScript error (pageerror) — none.
Task 10: complete (commits 6ae166f..586be1c, tests: .venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/e2e -m e2e -q → 9 passed in 97.94s (0:01:37))
Task 11: Ruling: deploy.sh/Jenkinsfile point at server folders ~/socialmediapost and ~/socialmediapost-staging, image "socialmediapost", live port stays 7086 (post.cloudtrio.in), new staging port 7088 so it can run beside the old 7087 staging during the trial; nothing was deployed — the person runs the switch — none.
Final review: self-review (no subagent: the session rule says not to start agents unless the person asks) — weaker than a fresh reviewer.
Final: fixed a video stuck "queued" forever when the server stopped during its last attempt — test_job_stuck_on_its_last_attempt_is_marked_failed RED→GREEN
Final: fixed cover previews overwriting each other (the page asks for 4 at once, all were written to one file) — test_cover_previews_asked_together_are_each_their_own_frame RED→GREEN
Final: fixed changing an admin to owner/viewer from the Team list (no business sent → refused); they now join the open business — test_admin_changes_a_person_to_owner_of_the_open_business RED→GREEN
Final: minor (deferred): the Team list does not show or pick which business a person belongs to (it follows the business open in the sidebar).
Final: minor (deferred): an owner cannot finish Instagram's 6-digit code step for their business (that call is admin-only); the admin does it.
Final: minor (deferred): people of a switched-off business get a plain JSON message instead of a friendly page.
Final: minor (deferred): cover thumbnails of a sideways video show the plain frame; the real cover (from the finished Reel) has the blurred fill.
Final: minor (deferred): the flyer editor example text (placeholders) is CloudTrio's for every business.
Final: minor (deferred): the edit card's Active switch can still turn a business on without the readiness check (admin override, see Task 6 ruling).
Task 11: complete (commits 586be1c..8f75270, tests: .venv/Scripts/python.exe -m pytest -q -p no:cacheprovider → 175 passed, 2 skipped, 9 deselected in 718.44s (0:11:58))
