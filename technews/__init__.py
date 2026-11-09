"""Tech-news Instagram newsroom: a Flask app whose scheduler runs a team of NVIDIA agents
(scanner, reviewer, writer) and posts only the top IT / tech stories."""

from __future__ import annotations

import json
import io
import logging
import os
import re
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from functools import wraps
from typing import Any
from zoneinfo import ZoneInfo

import click
from flask import Flask, abort, g, jsonify, redirect, render_template, request, send_file, send_from_directory, session, url_for
from flask.sessions import SecureCookieSessionInterface
from werkzeug.middleware.proxy_fix import ProxyFix

from technews import analytics, auth, backup, brands, card, config, db, devices, festivals, flyers, instagram, linkedin, monitor, motion, notify, pipeline, push, scheduler, sources, sso, users
from technews.agents import advice, analyst, creator, llm, scout, team, trainer
from technews.config import settings

PUBLIC_ENDPOINTS: frozenset[str] = frozenset({"login", "login_google", "login_google_callback", "health", "static", "manifest", "service_worker"})


class _Sessions(SecureCookieSessionInterface):
    def get_cookie_secure(self, app: Flask) -> bool:  # the cookie is HTTPS-only whenever the visit is
        return request.is_secure


def create_app() -> Flask:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    app = Flask(__name__)
    db.init()
    config.seed()
    config.refresh(force=True)
    brands.seed()
    app.secret_key = auth.secret_key()
    app.session_interface = _Sessions()
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", PERMANENT_SESSION_LIFETIME=timedelta(hours=12))
    if os.environ.get("BEHIND_PROXY") == "1":  # a reverse proxy (Caddy, nginx) terminates HTTPS in front of the app
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)  # type: ignore[method-assign]
    config.seal_env()
    if (first := auth.bootstrap()) is not None:
        logging.getLogger(__name__).warning("FIRST RUN: sign in as 'admin' with password %s and change it under Settings", first)
    users.seal_secrets()  # 2-step keys and the Instagram session were plain text before the secrets key existed
    instagram.seal_saved_sessions()

    # Under `flask run --debug` the reloader runs the app twice; only the child schedules.
    reloader_parent = app.debug and os.environ.get("WERKZEUG_RUN_MAIN") != "true"
    if settings.SCHEDULER_ENABLED and not reloader_parent and os.environ.get("TECHNEWS_CLI") != "1":
        scheduler.start()

    # approvers may decide and edit waiting posts; viewers only look; everything else is for admins
    approver_ok = frozenset({"api_approve", "api_reject", "api_post_design", "api_post_rewrite", "api_flyer_preview", "api_flyer_suggest", "api_note_add", "api_post_reel", "api_schedule", "api_unschedule", "api_brand_switch", "api_agents", "api_suggestion_used", "api_create_generate", "api_create_send",
                             "api_push_subscribe", "api_push_unsubscribe", "api_account", "api_two_step_start", "api_two_step_on",
                             "api_two_step_off"})  # fmt: skip

    def current_user() -> dict[str, Any] | None:
        who = users.get(session["uid"]) if session.get("uid") else None
        if who and who["active"] and session.get("ver") == who["ver"]:
            return who
        return None

    @app.before_request
    def guard() -> Any:
        config.refresh()  # a setting changed by the scheduler process, or by another browser
        """Everything but the sign-in page needs a signed-in person (or the API token); what they may change depends on their role."""
        g.user = current_user()
        g.role = g.user["role"] if g.user else ("admin" if auth.token_ok(request.headers.get("X-Admin-Token", "")) else "")
        g.authed = bool(g.role)
        if request.endpoint in PUBLIC_ENDPOINTS:
            return None
        if not g.authed:
            if request.path.startswith("/api/"):
                return jsonify({"message": "Sign in required."}), 401
            return redirect(url_for("login", next=request.full_path.rstrip("?") if request.method == "GET" else "/"))
        if (g.user and not g.user["totp"] and not session.get("via_google") and users.two_step_mode() == "required" and request.method not in ("GET", "HEAD")
                and not (request.endpoint or "").startswith("api_two_step") and request.endpoint != "logout"):  # fmt: skip
            return jsonify({"message": "Set up 2-step sign-in first (Settings, Security).", "error": "Set up 2-step sign-in first (Settings, Security)."}), 403
        if g.role != "admin" and request.endpoint in {"api_users", "api_backups", "api_secrets_status"}:
            return jsonify({"message": "Admins only.", "error": "Admins only."}), 403
        if request.method not in ("GET", "HEAD") and g.role != "admin" and request.endpoint not in approver_ok | {"logout"}:
            return jsonify({"message": f"Your role ({g.role}) cannot do this. Ask an admin.", "error": "Your role cannot do this. Ask an admin."}), 403
        if g.role == "viewer" and request.method not in ("GET", "HEAD") and request.endpoint not in {"logout", "api_account", "api_brand_switch", "api_push_subscribe", "api_push_unsubscribe", "api_flyer_preview", "api_flyer_suggest"}:
            return jsonify({"message": "Viewers can look but not change.", "error": "Viewers can look but not change."}), 403
        return None

    audited = {"api_approve": "approve", "api_reject": "reject", "api_post_reel": "make reel", "api_reel_music": "upload reel music", "api_posted_caption": "change posted caption", "api_posted_delete": "delete from Instagram", "api_repost": "repost", "api_post_design": "edit post", "api_post_rewrite": "rewrite post", "api_note_add": "add note",
               "api_cycle": "run cycle", "api_post_story": "post story", "api_settings_save": "change settings", "api_flyer_save": "save flyer",
               "api_flyer_delete": "delete flyer", "api_flyer_queue": "queue flyer", "api_source_add": "add source", "api_source_toggle": "toggle source",
               "api_source_delete": "delete source", "api_account": "change own account", "api_users_add": "add person", "api_users_change": "change person",
               "api_users_delete": "remove person", "api_security_save": "change security", "api_backup_save": "change backups", "api_backup_now": "back up now",
               "api_rotate_token": "rotate token", "api_schedule": "schedule post", "api_unschedule": "unschedule post", "api_brands_save": "save brand", "api_agent_save": "change agent", "api_agent_run": "run agent", "api_create_generate": "create from idea", "api_create_send": "send idea to approvals", "api_scout_run": "find sources", "api_scout_idea": "decide source idea", "api_scout_auto": "change source scout", "api_proposal": "decide new rules", "api_agents_learning": "change auto-learn",
               "api_brands_delete": "remove brand", "api_brand_logo": "brand logo", "api_brand_connect": "connect brand Instagram", "api_two_step_on": "turn on 2-step", "api_two_step_off": "turn off 2-step"}  # fmt: skip

    @app.after_request
    def audit_trail(resp: Any) -> Any:
        action = audited.get(request.endpoint or "")
        if action and resp.status_code < 400 and request.method not in ("GET", "HEAD"):
            who = g.user["username"] if g.get("user") else ("api token" if g.get("authed") else "?")
            if request.endpoint == "api_post_design" and request.args.get("preview"):
                return resp
            try:
                users.record(who, action, request.path, request.remote_addr or "")
            except Exception:  # noqa: BLE001 - the log must never break a request
                logging.getLogger(__name__).exception("audit failed")
        return resp

    @app.before_request
    def pick_brand() -> Any:
        """Everything the dashboard shows or does is for the brand chosen in the sidebar."""
        if not g.get("authed"):
            return None
        brand = brands.get(int(session.get("brand") or brands.DEFAULT_ID))
        if brand is None or not brand["enabled"]:
            brand = brands.get(brands.DEFAULT_ID)
        g.brand = brand
        g.brand_token = config.set_override(brands.overrides(brand)) if brand else None
        return None

    @app.teardown_request
    def drop_brand(_exc: BaseException | None) -> None:
        token = g.get("brand_token")
        if token is not None:
            config.reset_override(token)

    def admin(view: Any) -> Any:
        """Changes need a signed-in person (the guard has already checked their role)."""

        @wraps(view)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            if not g.get("authed"):
                abort(401)
            return view(*args, **kwargs)

        return wrapped

    def this_device() -> dict[str, str]:
        return devices.parse(request.headers.get("User-Agent", ""), request.headers.get("Sec-CH-UA-Mobile", ""))

    def safe_next(target: str) -> str:
        return target if target.startswith("/") and not target.startswith("//") and "\\" not in target else "/"

    def sign_in(user: dict[str, Any], ip: str) -> None:
        session.clear()
        session.update(uid=user["id"], user=user["username"], ver=user["ver"], dev=devices.record(this_device(), ip))
        session.permanent = True
        users.record(user["username"], "sign in", "", ip)

    @app.route("/login", methods=["GET", "POST"])
    def login() -> Any:
        target = safe_next(request.values.get("next", "/"))
        if request.method == "GET" and g.authed:
            return redirect(target)
        error, need_code = "", False
        if request.method == "POST":
            ip = request.remote_addr or "?"
            if auth.blocked(ip):
                return render_template("login.html", error="Too many wrong attempts. Try again in 10 minutes.", next=target, need_code=False, google=sso.enabled()), 429
            code = request.form.get("code", "")
            pre = session.get("pre")
            if code and pre and time.time() - pre["at"] < 300:  # step 2: the six digits
                user = users.get(pre["uid"])
                if user and user["active"] and users.code_ok(users.totp_secret(user), code):
                    auth.cleared(ip)
                    sign_in(user, ip)
                    return redirect(target)
                auth.failed(ip)
                error, need_code = "That code is not right. Check the time on your phone.", True
            else:
                user = users.check_password(request.form.get("username", ""), request.form.get("password", ""))
                if user and user["totp"] and users.two_step_mode() != "off":
                    session.clear()
                    session["pre"] = {"uid": user["id"], "at": time.time()}
                    return render_template("login.html", error="", next=target, need_code=True, google=False), 200
                if user:
                    auth.cleared(ip)
                    sign_in(user, ip)
                    return redirect(target)
                auth.failed(ip)
                users.record(request.form.get("username", "")[:32] or "?", "failed sign in", "", ip)
                error = "That username or password is not right."
        return render_template("login.html", error=error or request.args.get("sso_error", ""), next=target, need_code=need_code, google=sso.enabled() and not need_code), (401 if error else 200)

    @app.get("/login/google")
    def login_google() -> Any:
        if not sso.enabled():
            abort(404)
        url, remember = sso.start(safe_next(request.args.get("next", "/")))
        session.clear()
        session["sso"] = remember
        return redirect(url)

    @app.get("/login/google/callback")
    def login_google_callback() -> Any:
        ip = request.remote_addr or "?"
        if not sso.enabled():
            abort(404)
        remembered = session.pop("sso", None)
        if auth.blocked(ip):
            return redirect(url_for("login", sso_error="Too many wrong attempts. Try again in 10 minutes."))
        try:
            if request.args.get("error") or not request.args.get("code"):
                raise sso.SSOError("Google sign-in was cancelled.")
            email = sso.finish(request.args["code"], request.args.get("state", ""), remembered)
        except sso.SSOError as exc:
            return redirect(url_for("login", sso_error=str(exc)))
        user = users.by_email(email)
        if user is None:
            auth.failed(ip)
            users.record(email[:32], "failed sign in with Google", "no account has this email", ip)
            return redirect(url_for("login", sso_error=f"{email} has no account here. Ask an admin to add this email to your account."))
        auth.cleared(ip)
        sign_in(user, ip)
        session["via_google"] = True
        users.record(user["username"], "sign in with Google", email, ip)
        return redirect((remembered or {}).get("next") or "/")

    @app.post("/logout")
    def logout() -> Any:
        session.clear()
        return redirect(url_for("login"))

    @app.post("/api/account")
    @admin
    def api_account() -> Any:
        """Change your own username and/or password. The current password is always asked for."""
        body = request.get_json(silent=True) or {}
        me = g.get("user")
        if me is None:
            return jsonify({"error": "Sign in with your own account to change it."}), 400
        key = f"account:{request.remote_addr}"
        if auth.blocked(key):
            return jsonify({"error": "Too many wrong attempts. Try again in 10 minutes."}), 429
        if not users.check_password(me["username"], str(body.get("current_password", ""))):
            auth.failed(key)
            return jsonify({"error": "The current password is not right."}), 403
        new_pw = str(body.get("new_password") or "")
        try:
            users.change(me["id"], username=str(body.get("new_username") or me["username"]), password=new_pw or None)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        auth.cleared(key)
        fresh = users.get(me["id"]) or me
        session.update(uid=fresh["id"], user=fresh["username"], ver=fresh["ver"])
        return jsonify({"user": fresh["username"], "password_changed": bool(new_pw)})

    # ---- people, roles and the audit log (admins)

    @app.get("/api/users")
    @admin
    def api_users() -> Any:
        return jsonify({"users": users.listing(), "audit": users.recent(100), "two_step": users.two_step_mode()})

    @app.post("/api/users")
    @admin
    def api_users_add() -> Any:
        body = request.get_json(silent=True) or {}
        try:
            uid = users.create(str(body.get("username", "")), str(body.get("password", "")), str(body.get("role", "viewer")))
            if body.get("email"):
                users.set_email(uid, str(body["email"]))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"id": uid, "users": users.listing()})

    @app.post("/api/users/<int:user_id>")
    @admin
    def api_users_change(user_id: int) -> Any:
        body = request.get_json(silent=True) or {}
        try:
            users.change(user_id, role=body.get("role"), active=body.get("active"), password=body.get("password") or None)
            if body.get("reset_two_step"):
                users.set_totp(user_id, "")
            if "email" in body:
                users.set_email(user_id, str(body["email"] or ""))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"users": users.listing()})

    @app.delete("/api/users/<int:user_id>")
    @admin
    def api_users_delete(user_id: int) -> Any:
        if g.get("user") and g.user["id"] == user_id:
            return jsonify({"error": "You cannot remove the account you are signed in with."}), 400
        try:
            users.delete(user_id)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"users": users.listing()})

    @app.get("/api/security/secrets")
    @admin
    def api_secrets_status() -> Any:
        return jsonify(config.secrets_status())

    @app.post("/api/security")
    @admin
    def api_security_save() -> Any:
        try:
            users.set_two_step_mode(str((request.get_json(silent=True) or {}).get("two_step", "")))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"two_step": users.two_step_mode()})

    # ---- 2-step sign-in for your own account (authenticator app)

    @app.post("/api/two-step/start")
    @admin
    def api_two_step_start() -> Any:
        if g.get("user") is None:
            return jsonify({"error": "Sign in with your own account first."}), 400
        secret = users.new_secret()
        session["pending_totp"] = secret
        return jsonify({"secret": secret, "uri": users.otpauth(g.user["username"], secret)})

    @app.post("/api/two-step/on")
    @admin
    def api_two_step_on() -> Any:
        secret = session.get("pending_totp", "")
        if g.get("user") is None or not users.code_ok(secret, str((request.get_json(silent=True) or {}).get("code", ""))):
            return jsonify({"error": "That code did not match. Check the time on your phone and try again."}), 400
        users.set_totp(g.user["id"], secret)
        session.pop("pending_totp", None)
        return jsonify({"ok": True})

    @app.post("/api/two-step/off")
    @admin
    def api_two_step_off() -> Any:
        body = request.get_json(silent=True) or {}
        if g.get("user") is None or not users.check_password(g.user["username"], str(body.get("password", ""))):
            return jsonify({"error": "The password is not right."}), 403
        if users.two_step_mode() == "required":
            return jsonify({"error": "2-step sign-in is required by your admin."}), 400
        users.set_totp(g.user["id"], "")
        return jsonify({"ok": True})

    # ---- database copies

    @app.get("/api/backups")
    @admin
    def api_backups() -> Any:
        return jsonify({"options": backup.options(), "files": backup.listing(), "monitor": monitor.status()})

    @app.post("/api/backups/settings")
    @admin
    def api_backup_save() -> Any:
        body = request.get_json(silent=True) or {}
        try:
            backup.save_options(bool(body.get("enabled", True)), int(body.get("hours", 24)), int(body.get("keep", 14)))
        except (TypeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"options": backup.options(), "files": backup.listing()})

    @app.post("/api/backups/run")
    @admin
    def api_backup_now() -> Any:
        backup.run()
        return jsonify({"options": backup.options(), "files": backup.listing()})

    def status() -> dict[str, Any]:
        ok, why = pipeline.pacing()
        last = db.kv_get(pipeline.LAST_CYCLE_KEY)
        return {
            "delivery": settings.IG_DELIVERY,
            "llm": settings.NVIDIA_MODEL if llm.available() else "off (keyword fallback)",
            "scheduler": scheduler.jobs(),
            "posts_today": pipeline.posts_today(),
            "posts_per_day": settings.POSTS_PER_DAY,
            "can_post": ok,
            "pacing": why,
            "next_post": "flyer" if pipeline.next_is_flyer() else "news",
            "flyers_enabled": settings.FLYERS_ENABLED,
            "approval": settings.APPROVAL_MODE,
            "notify": notify.channels(),
            "notify_error": notify.last_error(),
            "pending": pipeline.pending_count(),
            "reels_pending": int(db.rows("SELECT COUNT(*) AS n FROM posts WHERE status IN ('pending','scheduled') AND brand_id = ? AND meta LIKE '%\"reel\": {%'", settings.BRAND_ID)[0]["n"]),
            "linkedin": "live" if linkedin.configured() and settings.IG_DELIVERY == "instagram" else "dry run" if settings.LINKEDIN_ENABLED else "off",
            "last_cycle": json.loads(last) if last else None,
            "last_error": db.kv_get(pipeline.LAST_ERROR_KEY),
            "instagram_user": instagram.session_user(),
        }

    def insights() -> dict[str, Any]:
        """Numbers for the dashboard charts: the funnel, categories, daily posts, outlets, flyers."""
        tz = ZoneInfo(settings.TIMEZONE)
        today = datetime.now(tz).date()
        days = [today - timedelta(days=i) for i in range(13, -1, -1)]
        daily = {d.isoformat(): {"news": 0, "flyer": 0} for d in days}
        for p in db.rows("SELECT created_at, flyer FROM posts WHERE status = 'posted' AND brand_id = ? ORDER BY id DESC LIMIT 600", settings.BRAND_ID):
            day = datetime.fromisoformat(p["created_at"]).astimezone(tz).date().isoformat()
            if day in daily:
                daily[day]["flyer" if p["flyer"] else "news"] += 1
        stories = db.rows("SELECT status, category, outlets, found_at FROM stories ORDER BY found_at DESC LIMIT 1500")
        cutoff = (datetime.now(UTC) - timedelta(hours=24)).isoformat(timespec="seconds")
        outlets = Counter(o for s in stories if s["found_at"] >= cutoff for o in s["outlets"])
        posted_flyers = Counter(r["flyer"] for r in db.rows("SELECT flyer FROM posts WHERE status = 'posted' AND flyer != '' AND brand_id = ?", settings.BRAND_ID))
        priority = [s for s in stories if s["category"] in ("cloud", "devops")]
        return {
            "funnel": dict(Counter(s["status"] for s in stories)),
            "categories": Counter(s["category"] for s in stories if s["status"] not in ("not_tech",)).most_common(),
            "daily": [{"day": d, **v} for d, v in daily.items()],
            "outlets": outlets.most_common(8),
            "flyers": [{"id": f.id, "kicker": f.kicker, "headline": f.headline, "points": list(f.points), "cta": f.cta,
                        "note": f.note, "caption": f.caption, "enabled": f.enabled, "layout": f.layout, "style": f.style,
                        "posted": posted_flyers.get(f.id, 0)}
                       for f in flyers.listing()],
            "priority": {"total": len(priority), "posted": sum(s["status"] == "posted" for s in priority)},
            "posted_total": int(db.rows("SELECT COUNT(*) AS n FROM posts WHERE status = 'posted' AND brand_id = ?", settings.BRAND_ID)[0]["n"]),
        }

    @app.get("/api/insights")
    def api_insights() -> Any:
        return jsonify(insights())

    @app.get("/api/analytics")
    def api_analytics() -> Any:
        try:
            days = max(1, min(365, int(request.args.get("days", 30))))
        except ValueError:
            days = 30
        return jsonify(analytics.summary(days))

    @app.post("/api/posts/<int:post_id>/reel")
    @admin
    def api_post_reel(post_id: int) -> Any:
        """Make a waiting draft a Reel (``on``), redo it with another movement or track, or make it a picture again."""
        body = request.get_json(silent=True) or {}
        words = body.get("highlight")
        if isinstance(words, str):
            words = [w for w in re.split(r"[,\n]+", words) if w.strip()]
        out = pipeline.set_reel(post_id, bool(body.get("on", True)), style=str(body.get("style", "")), track=str(body.get("track", "")),
                                highlight=[str(w) for w in words] if isinstance(words, list) else None,
                                number=str(body["number"]) if isinstance(body.get("number"), str) else None,
                                badge=str(body["badge"]) if isinstance(body.get("badge"), str) else None)  # fmt: skip
        if out.get("ok") and body.get("on", True):
            found = db.rows("SELECT headline, image_path FROM posts WHERE id = ?", post_id)
            if found:
                img = settings.CARDS_DIR / Path(str(found[0]["image_path"]).replace("\\", "/")).name
                notify.reel_queued(found[0]["headline"], img if img.exists() else None, post_id)
        return jsonify(out), (200 if out["ok"] else 409)

    @app.get("/api/reels/track/<name>")
    def api_reel_track(name: str) -> Any:
        """Listen to a Reel track (the built-in ones are made the first time)."""
        if name not in motion.TRACK_IDS or name == "none":
            abort(404)
        path = motion.track_file(name)
        if path is None or not path.exists():
            abort(404)
        return send_file(path, conditional=True, max_age=0)

    @app.post("/api/reels/music")
    @admin
    def api_reel_music() -> Any:
        """Upload your own instrumental for the Reels (then pick "Your uploaded track")."""
        upload = request.files.get("file")
        if upload is None:
            return jsonify({"error": "Choose an audio file first."}), 400
        try:
            path = motion.save_custom_track(upload.read(), upload.filename or "")
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True, "file": path.name})

    @app.get("/api/results")
    def api_results() -> Any:
        return jsonify(analyst.results())

    @app.get("/")
    def dashboard() -> str:
        stories = db.rows("SELECT * FROM stories ORDER BY found_at DESC, scan_score DESC LIMIT 150")
        posts = db.rows("SELECT p.*, s.title, s.url FROM posts p LEFT JOIN stories s ON s.id = p.story_id "
                        "WHERE p.status NOT IN ('pending', 'scheduled') AND p.brand_id = ? ORDER BY p.created_at DESC LIMIT 30", settings.BRAND_ID)  # fmt: skip
        pending = db.rows("SELECT p.*, s.url, s.category FROM posts p LEFT JOIN stories s ON s.id = p.story_id "
                          "WHERE p.status IN ('pending', 'scheduled') AND p.brand_id = ? ORDER BY p.id", settings.BRAND_ID)  # fmt: skip
        pending_meta = {p["id"]: pipeline.draft_meta(p) for p in pending}
        pending_still = [p for p in pending if not (pending_meta.get(p["id"]) or {}).get("reel", {}).get("video")]
        pending_reel  = [p for p in pending if     (pending_meta.get(p["id"]) or {}).get("reel", {}).get("video")]
        return render_template("dashboard.html", brand_list=[b for b in brands.listing() if b["enabled"]], brand_now=g.brand["id"], status=status(), stories=stories, posts=posts, pending=pending,
                               pending_still=pending_still, pending_reel=pending_reel,
                               insights=insights(), flyer_layouts=flyers.LAYOUTS, reels_ok=motion.available(), reel_styles=motion.STYLES, reel_tracks=motion.TRACKS,
                               pending_meta=pending_meta,
                               pending_notes={p["id"]: db.rows('SELECT "user" AS user, text, at FROM notes WHERE post_id = ? ORDER BY id', p["id"]) for p in pending},
                               news_tags=[(k, {"AI": "AI", "DEVOPS": "DevOps"}.get(v, v.title())) for k, v in card.LABEL.items()], flyer_themes=flyers.theme_defaults(),
                               flyer_palettes=flyers.PALETTES,
                               flyer_after=settings.FLYER_AFTER_NEWS, user=session.get("user") or "api", role=g.role, my_two_step=bool(g.user and g.user["totp"]), two_step_mode=users.two_step_mode(),
                               device=this_device(), logins=devices.recent(), current_dev=session.get("dev", ""))

    @app.get("/health")
    def health() -> Any:
        return jsonify({"ok": True})  # for the container's health check; no details without signing in

    # ----------------------------------------------------------------- the installable app

    @app.get("/manifest.webmanifest")
    def manifest() -> Any:
        out = jsonify({
            "name": "CloudTrio Newsroom", "short_name": "Newsroom", "id": "/", "start_url": "/#approvals", "scope": "/",
            "display": "standalone", "orientation": "any", "background_color": "#070f20", "theme_color": "#091a36",
            "description": "Review and approve the posts CloudTrio sends to Instagram and LinkedIn.",
            "icons": [
                {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
                {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
                {"src": "/static/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
            ],
            "shortcuts": [{"name": "Approvals", "url": "/#approvals"}, {"name": "Posts", "url": "/#posts"}],
        })  # fmt: skip
        out.mimetype = "application/manifest+json"
        return out

    @app.get("/sw.js")
    def service_worker() -> Any:
        out = send_from_directory(app.static_folder or "static", "sw.js", mimetype="text/javascript")
        out.headers["Cache-Control"] = "no-cache"
        out.headers["Service-Worker-Allowed"] = "/"
        return out

    @app.get("/api/push/key")
    def api_push_key() -> Any:
        return jsonify({"key": push.public_key(), "devices": len(push.subscriptions())})

    @app.post("/api/push/subscribe")
    def api_push_subscribe() -> Any:
        try:
            return jsonify({"devices": push.subscribe(request.get_json(silent=True) or {})})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/push/unsubscribe")
    def api_push_unsubscribe() -> Any:
        return jsonify({"devices": push.unsubscribe(str((request.get_json(silent=True) or {}).get("endpoint", "")))})

    @app.get("/api/status")
    def api_status() -> Any:
        return jsonify(status())

    @app.get("/api/stories")
    def api_stories() -> Any:
        state = request.args.get("status")
        if state:
            return jsonify(db.rows("SELECT * FROM stories WHERE status = ? ORDER BY found_at DESC LIMIT 200", state))
        return jsonify(db.rows("SELECT * FROM stories ORDER BY found_at DESC LIMIT 200"))

    @app.get("/api/posts")
    def api_posts() -> Any:
        return jsonify(db.rows("SELECT * FROM posts WHERE brand_id = ? ORDER BY created_at DESC LIMIT 100", settings.BRAND_ID))

    @app.post("/api/cycle")
    @admin
    def api_cycle() -> Any:
        return jsonify(pipeline.run_cycle(post=request.args.get("post", "1") != "0"))

    @app.post("/api/posts/<int:post_id>/approve")
    @admin
    def api_approve(post_id: int) -> Any:
        caption = (request.get_json(silent=True) or {}).get("caption")
        out = pipeline.approve(post_id, caption if isinstance(caption, str) else None)
        return jsonify(out), (200 if out.get("posted") else 409)

    @app.post("/api/posts/<int:post_id>/reject")
    @admin
    def api_reject(post_id: int) -> Any:
        out = pipeline.reject(post_id)
        return jsonify(out), (200 if out.get("rejected") else 409)

    @app.post("/api/posts/<int:post_id>/caption")
    @admin
    def api_posted_caption(post_id: int) -> Any:
        """Change the caption of a post that is already on Instagram."""
        out = pipeline.edit_posted_caption(post_id, str((request.get_json(silent=True) or {}).get("caption", "")))
        return jsonify(out), (200 if out["ok"] else 409)

    @app.post("/api/posts/<int:post_id>/delete")
    @admin
    def api_posted_delete(post_id: int) -> Any:
        out = pipeline.delete_posted(post_id)
        return jsonify(out), (200 if out["ok"] else 409)

    @app.post("/api/posts/<int:post_id>/repost")
    @admin
    def api_repost(post_id: int) -> Any:
        out = pipeline.repost(post_id, replace=bool((request.get_json(silent=True) or {}).get("replace")))
        return jsonify(out), (200 if out["ok"] else 409)

    @app.post("/api/posts/<int:post_id>/design")
    @admin
    def api_post_design(post_id: int) -> Any:
        """Change the design of a post waiting for approval (``?preview=1`` draws it without saving)."""
        try:
            out = pipeline.edit_draft(post_id, request.get_json(silent=True) or {}, preview=bool(request.args.get("preview")))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        if not out["ok"]:
            return jsonify(out), 409
        if "jpeg" in out:
            return send_file(io.BytesIO(out["jpeg"]), mimetype="image/jpeg", max_age=0)
        return jsonify(out)

    @app.post("/api/posts/<int:post_id>/notes")
    @admin
    def api_note_add(post_id: int) -> Any:
        """A note for the team on a post that is waiting for approval."""
        text = str((request.get_json(silent=True) or {}).get("text", "")).strip()[:500]
        if not text:
            return jsonify({"error": "Write a note first."}), 400
        if not db.rows("SELECT id FROM posts WHERE id = ? AND status = 'pending'", post_id):
            return jsonify({"error": "That post is not waiting for approval."}), 409
        who = g.user["username"] if g.get("user") else "api token"
        db.execute('INSERT INTO notes (post_id, "user", text, at) VALUES (?,?,?,?)', post_id, who, text, db.now())
        return jsonify({"notes": db.rows('SELECT "user" AS user, text, at FROM notes WHERE post_id = ? ORDER BY id', post_id)})

    @app.post("/api/posts/<int:post_id>/schedule")
    @admin
    def api_schedule(post_id: int) -> Any:
        body = request.get_json(silent=True) or {}
        try:
            out = pipeline.schedule(post_id, str(body.get("when", "")), body.get("caption") if isinstance(body.get("caption"), str) else None)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(out), (200 if out.get("scheduled") else 409)

    @app.post("/api/posts/<int:post_id>/unschedule")
    @admin
    def api_unschedule(post_id: int) -> Any:
        out = pipeline.unschedule(post_id)
        return jsonify(out), (200 if out.get("post_id") else 409)

    @app.get("/api/calendar")
    @admin
    def api_calendar() -> Any:
        month = request.args.get("month") or datetime.now(ZoneInfo(settings.TIMEZONE)).strftime("%Y-%m")
        try:
            return jsonify({"month": month, "items": pipeline.calendar(month)})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    # ---- the agent team

    @app.get("/api/agents")
    @admin
    def api_agents() -> Any:
        return jsonify({
            "agents": [{"id": k, **{f: v[f] for f in ("name", "role", "switch", "learns")}, "on": team.enabled(k), "stats": team.stats(k),
                        "playbook": team.playbook(k) if v["learns"] else "", "history": team.playbook_history(k, 3) if v["learns"] else []}
                       for k, v in team.ROSTER.items()],
            "board": team.board(), "proposals": team.proposals(), "activity": team.activity(60), "report": analyst.latest(), "feedback": team.feedback_counts(),
            "auto_learn": team.auto_learn(), "collect_metrics": settings.COLLECT_METRICS, "ai": llm.available(),
        })

    # ---- Indian festival flyers

    @app.get("/api/festivals")
    @admin
    def api_festivals() -> Any:
        return jsonify({"auto": festivals.auto(), "lead": festivals.lead_days(), "lang": festivals.language(), "languages": festivals.LANGUAGES,
                        "items": festivals.upcoming(180), "fetched": festivals._cache()["at"]})  # fmt: skip

    @app.post("/api/festivals/settings")
    @admin
    def api_festivals_settings() -> Any:
        body = request.get_json(silent=True) or {}
        if "auto" in body:
            db.kv_set("festivals:auto", "1" if body["auto"] else "0")
        if "lead" in body:
            db.kv_set("festivals:lead", str(max(0, min(14, int(body["lead"] or 0)))))
        if "lang" in body:
            try:
                festivals.set_language(str(body["lang"]))
            except ValueError as exc:
                return jsonify({"error": str(exc)}), 400
        return jsonify({"auto": festivals.auto(), "lead": festivals.lead_days(), "lang": festivals.language()})

    @app.get("/api/festivals/<key>/preview.jpg")
    @admin
    def api_festival_preview(key: str) -> Any:
        try:
            path = festivals.preview(key, int(request.args.get("year") or 0) or None)
        except ValueError:
            abort(404)
        out = send_from_directory(settings.CARDS_DIR, path.name)
        out.headers["Cache-Control"] = "no-store"
        return out

    @app.get("/api/festivals/<key>/text")
    @admin
    def api_festival_text(key: str) -> Any:
        if key not in festivals.BY_KEY:
            abort(404)
        return jsonify({**festivals.wording(key), "built_in": {k: festivals.BY_KEY[key][k] for k in ("headline", "meaning")}})

    @app.post("/api/festivals/<key>/text")
    @admin
    def api_festival_text_save(key: str) -> Any:
        body = request.get_json(silent=True) or {}
        try:
            return jsonify(festivals.set_wording(key, str(body.get("headline", "")), str(body.get("meaning", ""))))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/festivals/refresh")
    @admin
    def api_festivals_refresh() -> Any:
        return jsonify(festivals.refresh(force=True))

    @app.post("/api/festivals/<key>")
    @admin
    def api_festival_save(key: str) -> Any:
        try:
            festivals.set_enabled(key, bool((request.get_json(silent=True) or {}).get("enabled", True)))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True})

    @app.post("/api/festivals/<key>/make")
    @admin
    def api_festival_make(key: str) -> Any:
        body = request.get_json(silent=True) or {}
        try:
            return jsonify(festivals.make(key, int(body.get("year") or 0) or None, force=bool(body.get("force"))))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.get("/api/suggestions")
    @admin
    def api_suggestions() -> Any:
        """What the agents suggest for one page of the dashboard (overview, stories, flyers, sources, calendar, posts, settings)."""
        return jsonify({"suggestions": advice.for_area(request.args.get("area", ""))})

    @app.get("/api/posts/<int:post_id>/suggestions")
    @admin
    def api_post_suggestions(post_id: int) -> Any:
        return jsonify({"suggestions": advice.for_post(post_id)})

    @app.post("/api/suggestions/used")
    @admin
    def api_suggestion_used() -> Any:
        """The page tells the team a suggestion was used, so the trainer learns which kind you value."""
        body = request.get_json(silent=True) or {}
        team.feedback("suggestion_used", int(body.get("post") or 0), agent=str(body.get("agent", ""))[:20], what=str(body.get("kind", ""))[:20])
        return jsonify({"ok": True})

    @app.post("/api/agents/auto-learn")
    @admin
    def api_agents_learning() -> Any:
        team.set_auto_learn(bool((request.get_json(silent=True) or {}).get("on")))
        return jsonify({"auto_learn": team.auto_learn()})

    @app.post("/api/agents/<agent>")
    @admin
    def api_agent_save(agent: str) -> Any:
        body = request.get_json(silent=True) or {}
        try:
            if "enabled" in body:
                team.set_enabled(agent, bool(body["enabled"]))
            if "playbook" in body:
                team.set_playbook(agent, str(body["playbook"]))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True})

    @app.post("/api/agents/<agent>/run")
    @admin
    def api_agent_run(agent: str) -> Any:
        """Run the analyst or the trainer now."""
        if agent == "analyst":
            return jsonify({"report": analyst.report()})
        if agent == "trainer":
            analyst.report()
            return jsonify(trainer.learn())
        return jsonify({"error": "Only the analyst and the trainer run on their own; the others work as posts are made."}), 400

    @app.post("/api/proposals/<int:proposal_id>")
    @admin
    def api_proposal(proposal_id: int) -> Any:
        ok = team.decide(proposal_id, bool((request.get_json(silent=True) or {}).get("accept")))
        return jsonify({"ok": ok}), (200 if ok else 409)

    @app.post("/api/brand")
    @admin
    def api_brand_switch() -> Any:
        """Choose which brand the dashboard shows."""
        brand = brands.get(int((request.get_json(silent=True) or {}).get("id") or 0))
        if brand is None or not brand["enabled"]:
            return jsonify({"error": "No such brand"}), 400
        session["brand"] = brand["id"]
        return jsonify({"brand": brand["id"]})

    @app.get("/api/brands")
    @admin
    def api_brands() -> Any:
        return jsonify({"brands": brands.listing(), "tags": [(k, v) for k, v in card.LABEL.items()]})

    @app.post("/api/brands")
    @admin
    def api_brands_save() -> Any:
        body = request.get_json(silent=True) or {}
        try:
            brand_id = brands.save(int(body["id"]) if body.get("id") else None, body)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"id": brand_id, "brands": brands.listing()})

    @app.delete("/api/brands/<int:brand_id>")
    @admin
    def api_brands_delete(brand_id: int) -> Any:
        try:
            brands.delete(brand_id)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"brands": brands.listing()})

    @app.post("/api/brands/<int:brand_id>/logo")
    @admin
    def api_brand_logo(brand_id: int) -> Any:
        """A PNG logo for another brand (the first brand keeps the CloudTrio logo)."""
        from PIL import Image, UnidentifiedImageError

        upload = request.files.get("logo")
        if brand_id == brands.DEFAULT_ID or brands.get(brand_id) is None or upload is None:
            return jsonify({"error": "Choose a brand other than the first, and a PNG file."}), 400
        data = upload.read(2_000_001)
        if len(data) > 2_000_000:
            return jsonify({"error": "The logo is over 2 MB."}), 400
        try:
            image = Image.open(io.BytesIO(data))
            image.verify()
            logo = Image.open(io.BytesIO(data)).convert("RGBA")
        except (UnidentifiedImageError, OSError):
            return jsonify({"error": "That file is not an image."}), 400
        logo.thumbnail((800, 800))
        brands.logo_path(brand_id).parent.mkdir(parents=True, exist_ok=True)
        logo.save(brands.logo_path(brand_id), "PNG")
        return jsonify({"brands": brands.listing()})

    @app.post("/api/brands/<int:brand_id>/connect")
    @admin
    def api_brand_connect(brand_id: int) -> Any:
        """Log another brand in to its Instagram account (the first brand uses Settings, Instagram)."""
        brand = brands.get(brand_id)
        if brand is None or brand_id == brands.DEFAULT_ID:
            return jsonify({"state": "error", "message": "Use Settings, Instagram for the first brand."}), 400
        if not (brand["ig_username"] and brand["ig_password"]):
            return jsonify({"state": "error", "message": "Save the Instagram username and password for this brand first."}), 400
        with brands.use(brand):
            try:
                job = instagram.start_login(settings.IG_USERNAME, settings.IG_PASSWORD)
            except instagram.InstagramPublishError as exc:
                return jsonify({"state": "error", "message": str(exc)}), 409
        return jsonify(job.as_dict())

    @app.post("/api/posts/<int:post_id>/rewrite")
    @admin
    def api_post_rewrite(post_id: int) -> Any:
        out = pipeline.rewrite_draft(post_id)
        return jsonify(out), (200 if out["ok"] else 409)

    @app.post("/api/stories/<int:story_id>/post")
    @admin
    def api_post_story(story_id: int) -> Any:
        """Post one story now (a human editor's override: skips the reviewer and the gap,
        never Instagram's pause)."""
        if (left := instagram.paused()) > 0:
            return jsonify({"posted": False, "reason": f"Instagram pause, {left / 60:.0f} min left"}), 409
        return jsonify(pipeline.publish_story(story_id))

    # ----------------------------------------------------------------- settings

    @app.get("/api/settings")
    @admin
    def api_settings() -> Any:
        job = instagram.current_login()
        return jsonify({"settings": config.public(), "instagram_user": instagram.session_user(),
                        "login": job.as_dict() if job else None})  # fmt: skip

    @app.post("/api/settings")
    @admin
    def api_settings_save() -> Any:
        try:
            changed = config.update(request.get_json(silent=True) or {})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        if "SCAN_INTERVAL_MINUTES" in changed:
            scheduler.reschedule()
        return jsonify({"changed": changed, "settings": config.public()})

    @app.post("/api/settings/test-nvidia")
    @admin
    def api_test_nvidia() -> Any:
        try:
            reply = llm.chat_json(system='Reply with {"ok": true}.', user="ping", max_tokens=20)
        except llm.LLMError as exc:
            return jsonify({"ok": False, "message": str(exc)[:300]})
        return jsonify({"ok": True, "message": f"{reply.model} answered in {reply.latency_ms} ms"})

    @app.post("/api/settings/rotate-token")
    @admin
    def api_rotate_token() -> Any:
        return jsonify({"token": config.rotate_admin_token()})

    @app.post("/api/settings/test-notify")
    @admin
    def api_test_notify() -> Any:
        return jsonify(notify.test())

    @app.post("/api/settings/test-linkedin")
    @admin
    def api_test_linkedin() -> Any:
        try:
            return jsonify({"ok": True, "message": linkedin.check()})
        except linkedin.LinkedInError as exc:
            return jsonify({"ok": False, "message": str(exc)[:300]})

    @app.post("/api/instagram/login")
    @admin
    def api_ig_login() -> Any:
        """Connect with the saved username / password (save them first)."""
        if not (settings.IG_USERNAME and settings.IG_PASSWORD):
            return jsonify({"state": "error", "message": "Save the Instagram username and password first."}), 400
        try:
            job = instagram.start_login(settings.IG_USERNAME, settings.IG_PASSWORD)
        except instagram.InstagramPublishError as exc:
            return jsonify({"state": "error", "message": str(exc)}), 409
        return jsonify(job.as_dict())

    @app.get("/api/instagram/login")
    @admin
    def api_ig_login_status() -> Any:
        job = instagram.current_login()
        return jsonify(job.as_dict() if job else {"state": "idle", "message": ""})

    @app.post("/api/instagram/code")
    @admin
    def api_ig_code() -> Any:
        job = instagram.current_login()
        code = str((request.get_json(silent=True) or {}).get("code", "")).strip()
        if job is None or job.state != "need_code":
            return jsonify({"state": "error", "message": "No login is waiting for a code."}), 409
        if not (code.isdigit() and len(code) in (6, 8)):
            return jsonify({"state": "need_code", "message": "The code is 6 digits."}), 400
        job.submit_code(code)
        return jsonify({"state": "running", "message": "Checking the code…"})

    @app.post("/api/instagram/session")
    @admin
    def api_ig_session() -> Any:
        """Connect with the sessionid cookie from instagram.com in the browser."""
        sessionid = str((request.get_json(silent=True) or {}).get("sessionid", ""))
        try:
            instagram.login_with_sessionid(sessionid)
        except Exception as exc:  # noqa: BLE001 - shown on the dashboard
            return jsonify({"state": "error", "message": f"That session did not work: {exc}"}), 400
        user = instagram.session_user()
        if user and user != settings.IG_USERNAME.lower():
            config.update({"IG_USERNAME": user})
        return jsonify({"state": "ok", "message": f"Connected as @{user}."})

    @app.get("/api/flyer/<flyer_id>.jpg")
    def api_flyer(flyer_id: str) -> Any:
        """A flyer's image, rendered fresh, for the dashboard preview."""
        flyer = flyers.get(flyer_id)
        if flyer is None:
            abort(404)
        out = send_from_directory(settings.CARDS_DIR, flyers.render(flyer).name)
        out.headers["Cache-Control"] = "no-store"
        return out

    # ----------------------------------------------------------------- flyers (the editor)

    @app.get("/api/flyers")
    def api_flyers() -> Any:
        return jsonify(insights()["flyers"])

    @app.post("/api/flyers")
    @admin
    def api_flyer_save() -> Any:
        try:
            flyer = flyers.save(request.get_json(silent=True) or {})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"id": flyer.id})

    @app.delete("/api/flyers/<flyer_id>")
    @admin
    def api_flyer_delete(flyer_id: str) -> Any:
        return jsonify({"deleted": flyers.delete(flyer_id)})

    @app.get("/api/flyer-layout/<layout_id>.jpg")
    def api_flyer_layout(layout_id: str) -> Any:
        """A small sample of one layout, for the studio's gallery."""
        if layout_id not in flyers.LAYOUT_IDS:
            return jsonify({"error": "unknown layout"}), 404
        return send_file(io.BytesIO(flyers.layout_sample(layout_id)), mimetype="image/jpeg", max_age=3600)

    @app.post("/api/flyers/suggest")
    @admin
    def api_flyer_suggest() -> Any:
        """Checks and palette suggestions for what is in the studio; ``ai: true`` adds ideas from the model."""
        data = request.get_json(silent=True) or {}
        text = " ".join([str(data.get("kicker") or ""), str(data.get("headline") or ""), str(data.get("points") or "")])
        out: dict[str, Any] = {"checks": flyers.advise(data), "palettes": flyers.palettes_for(text)}
        if data.get("ai"):
            out["ai"] = flyers.ai_ideas(data)
        return jsonify(out)

    @app.post("/api/flyers/preview")
    @admin
    def api_flyer_preview() -> Any:
        """Render what is in the editor, before it is saved."""
        try:
            flyer = flyers.parse(request.get_json(silent=True) or {})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        buf = io.BytesIO()
        flyers.render(flyer, buf)
        buf.seek(0)
        return send_file(buf, mimetype="image/jpeg", max_age=0)

    @app.post("/api/flyers/<flyer_id>/queue")
    @admin
    def api_flyer_queue(flyer_id: str) -> Any:
        """Send this flyer next: to the approval queue, or straight out when approval is off."""
        flyer = flyers.get(flyer_id)
        if flyer is None:
            abort(404)
        if pipeline.needs_approval(flyer=True):
            return jsonify(pipeline.draft_flyer(flyer))
        if (left := instagram.paused()) > 0:
            return jsonify({"posted": False, "reason": f"Instagram pause, {left / 60:.0f} min left"}), 409
        return jsonify(pipeline.publish_flyer(flyer))

    # ----------------------------------------------------------------- news sources

    @app.get("/api/sources")
    def api_sources() -> Any:
        rows = sources.listing()
        for r in rows:
            h = scout.health(r["id"])
            r["health"] = {"state": scout.state(r, h), "ok_at": h["ok_at"], "fails": h["fails"], "items": h["items"], "error": h["error"]} if r["kind"] == "feed" else None
        return jsonify(rows)

    @app.post("/api/create/generate")
    @admin
    def api_create_generate() -> Any:
        """Feature N: an idea and/or a link in, three flyer options out."""
        body = request.get_json(silent=True) or {}
        try:
            return jsonify(creator.generate(str(body.get("idea", "")), str(body.get("url", "")), str(body.get("goal", "promote")), str(body.get("tone", "professional"))))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except llm.LLMError as exc:
            return jsonify({"error": f"The writer could not answer: {str(exc)[:120]}"}), 502

    @app.post("/api/create/send")
    @admin
    def api_create_send() -> Any:
        body = request.get_json(silent=True) or {}
        try:
            return jsonify(creator.send(int(body.get("idea_id") or 0), body.get("variant") or {}, rotate=bool(body.get("rotate"))))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.get("/api/create/history")
    @admin
    def api_create_history() -> Any:
        return jsonify({"ideas": creator.history()})

    @app.get("/api/scout")
    @admin
    def api_scout() -> Any:
        return jsonify({"ideas": scout.ideas(), "auto": scout.auto(), "on": team.enabled("scout"), "recent": scout.ideas("applied")[:5]})

    @app.post("/api/scout/run")
    @admin
    def api_scout_run() -> Any:
        return jsonify(scout.run())

    @app.post("/api/scout/auto")
    @admin
    def api_scout_auto() -> Any:
        scout.set_auto(bool((request.get_json(silent=True) or {}).get("on")))
        return jsonify({"auto": scout.auto()})

    @app.post("/api/scout/ideas/<int:idea_id>")
    @admin
    def api_scout_idea(idea_id: int) -> Any:
        try:
            ok = scout.decide(idea_id, bool((request.get_json(silent=True) or {}).get("accept")))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": ok}), (200 if ok else 409)

    @app.post("/api/sources")
    @admin
    def api_source_add() -> Any:
        body = request.get_json(silent=True) or {}
        try:
            return jsonify(sources.add(str(body.get("kind", "")), str(body.get("name", "")), str(body.get("value", ""))))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/sources/test")
    @admin
    def api_source_test() -> Any:
        try:
            return jsonify({"ok": True, **sources.probe(str((request.get_json(silent=True) or {}).get("value", "")))})
        except Exception as exc:  # noqa: BLE001 - shown on the dashboard
            return jsonify({"ok": False, "message": f"Could not read that feed: {exc}"[:300]})

    @app.post("/api/sources/<int:source_id>")
    @admin
    def api_source_toggle(source_id: int) -> Any:
        ok = sources.set_enabled(source_id, bool((request.get_json(silent=True) or {}).get("enabled")))
        return jsonify({"ok": ok}), (200 if ok else 404)

    @app.delete("/api/sources/<int:source_id>")
    @admin
    def api_source_delete(source_id: int) -> Any:
        return jsonify({"deleted": sources.remove(source_id)})

    @app.get("/cards/<path:name>")
    def cards(name: str) -> Any:
        return send_from_directory(settings.CARDS_DIR, name)

    # ----------------------------------------------------------------- CLI

    @app.cli.command("migrate-db")
    @click.option("--to", "url", required=True, help="postgresql://user:password@host/database")
    @click.option("--replace", is_flag=True, help="Overwrite data already in PostgreSQL.")
    def cli_migrate_db(url: str, replace: bool) -> None:
        """Copy the SQLite database into PostgreSQL (the SQLite file is left as it is)."""
        from technews import migrate

        for table, count in migrate.to_postgres(settings.DB_PATH, url, replace=replace).items():
            click.echo(f"{table}: {count} rows")
        click.echo("Done. Set DATABASE_URL to this address and restart to use PostgreSQL.")

    @app.cli.command("cycle")
    @click.option("--no-post", is_flag=True, help="Scan and review only.")
    def cli_cycle(no_post: bool) -> None:
        """Run one newsroom cycle now."""
        click.echo(json.dumps(pipeline.run_cycle(post=not no_post), indent=2))

    @app.cli.command("post")
    @click.argument("story_id", type=int)
    def cli_post(story_id: int) -> None:
        """Write, render and post one story by id."""
        click.echo(json.dumps(pipeline.publish_story(story_id), indent=2))

    @app.cli.command("ig-login")
    @click.option("--browser", is_flag=True, help="Use the sessionid cookie from instagram.com in your browser.")
    def cli_ig_login(browser: bool) -> None:
        """Log in to Instagram once (handles the 6-digit code) and save the session."""
        try:
            if browser:
                path = instagram.login_with_sessionid(click.prompt("Paste the sessionid cookie", hide_input=True))
            else:
                path = instagram.login_interactive()
        except Exception as exc:  # noqa: BLE001
            raise click.ClickException(f"Login failed: {exc}") from exc
        click.echo(f"Session saved to {path}")

    return app
