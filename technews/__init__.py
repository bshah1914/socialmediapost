"""Tech-news Instagram newsroom: a Flask app whose scheduler runs a team of NVIDIA agents
(scanner, reviewer, writer) and posts only the top IT / tech stories."""

from __future__ import annotations

import json
import logging
import os
from functools import wraps
from typing import Any

import click
from flask import Flask, abort, jsonify, render_template, request, send_from_directory

from technews import config, db, instagram, pipeline, scheduler
from technews.agents import llm
from technews.config import settings

LOCAL: frozenset[str] = frozenset({"127.0.0.1", "::1"})


def create_app() -> Flask:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    app = Flask(__name__)
    db.init()

    # Under `flask run --debug` the reloader runs the app twice; only the child schedules.
    reloader_parent = app.debug and os.environ.get("WERKZEUG_RUN_MAIN") != "true"
    if settings.SCHEDULER_ENABLED and not reloader_parent and os.environ.get("TECHNEWS_CLI") != "1":
        scheduler.start()

    def admin(view: Any) -> Any:
        """Changes need the admin token; without one set, only this machine may make them
        (the settings hold the NVIDIA key and the Instagram password)."""

        @wraps(view)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            if settings.ADMIN_TOKEN:
                if request.headers.get("X-Admin-Token") != settings.ADMIN_TOKEN:
                    abort(401)
            elif request.remote_addr not in LOCAL:
                abort(403, "Set ADMIN_TOKEN in .env to make changes from another machine.")
            return view(*args, **kwargs)

        return wrapped

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
            "last_cycle": json.loads(last) if last else None,
            "last_error": db.kv_get(pipeline.LAST_ERROR_KEY),
            "instagram_user": instagram.session_user(),
        }

    @app.get("/")
    def dashboard() -> str:
        stories = db.rows("SELECT * FROM stories ORDER BY found_at DESC, scan_score DESC LIMIT 150")
        posts = db.rows("SELECT p.*, s.title, s.url FROM posts p JOIN stories s ON s.id = p.story_id "
                        "ORDER BY p.created_at DESC LIMIT 30")  # fmt: skip
        return render_template("dashboard.html", status=status(), stories=stories, posts=posts)

    @app.get("/health")
    def health() -> Any:
        return jsonify(status())

    @app.get("/api/stories")
    def api_stories() -> Any:
        state = request.args.get("status")
        if state:
            return jsonify(db.rows("SELECT * FROM stories WHERE status = ? ORDER BY found_at DESC LIMIT 200", state))
        return jsonify(db.rows("SELECT * FROM stories ORDER BY found_at DESC LIMIT 200"))

    @app.get("/api/posts")
    def api_posts() -> Any:
        return jsonify(db.rows("SELECT * FROM posts ORDER BY created_at DESC LIMIT 100"))

    @app.post("/api/cycle")
    @admin
    def api_cycle() -> Any:
        return jsonify(pipeline.run_cycle(post=request.args.get("post", "1") != "0"))

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
        return jsonify({"changed": changed, "settings": config.public()})

    @app.post("/api/settings/test-nvidia")
    @admin
    def api_test_nvidia() -> Any:
        try:
            reply = llm.chat_json(system='Reply with {"ok": true}.', user="ping", max_tokens=20)
        except llm.LLMError as exc:
            return jsonify({"ok": False, "message": str(exc)[:300]})
        return jsonify({"ok": True, "message": f"{reply.model} answered in {reply.latency_ms} ms"})

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

    @app.get("/cards/<path:name>")
    def cards(name: str) -> Any:
        return send_from_directory(settings.CARDS_DIR, name)

    # ----------------------------------------------------------------- CLI

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
