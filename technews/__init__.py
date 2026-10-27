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

from technews import db, instagram, pipeline, scheduler
from technews.agents import llm
from technews.config import settings


def create_app() -> Flask:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    app = Flask(__name__)
    db.init()

    # Under `flask run --debug` the reloader runs the app twice; only the child schedules.
    reloader_parent = app.debug and os.environ.get("WERKZEUG_RUN_MAIN") != "true"
    if settings.SCHEDULER_ENABLED and not reloader_parent and os.environ.get("TECHNEWS_CLI") != "1":
        scheduler.start()

    def admin(view: Any) -> Any:
        @wraps(view)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            if settings.ADMIN_TOKEN and request.headers.get("X-Admin-Token") != settings.ADMIN_TOKEN:
                abort(401)
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
