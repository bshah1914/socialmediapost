"""Publishing to Instagram (ported from CopyTrading's ``trading/services/instagram.py``).

Uses instagrapi, an UNOFFICIAL client that logs in with the account's username / password
and reuses a saved session. Heavy or bot-like use can trigger Instagram challenges or
blocks, so:

- ``IG_DELIVERY=dry_run`` (the default) only writes the card and caption to ``data/outbox``;
- the scheduler only ever reuses the saved session; the operator primes it once,
  interactively, with ``flask --app run ig-login``;
- posts are spaced ``MIN_POST_GAP_MINUTES`` apart and capped at ``POSTS_PER_DAY`` (pipeline);
- when Instagram flags a post as automated ("We restrict certain activity") the post is not
  retried (it is often live already): if it went up, posting rests ``IG_WARNED_PAUSE_MINUTES``,
  if not, ``IG_RESTRICT_PAUSE_HOURS``.
"""

from __future__ import annotations

import getpass
import logging
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from technews import db
from technews.config import settings

logger = logging.getLogger(__name__)

RESTRICT_PAUSE_KEY: Final = "instagram:restricted"
RESTRICTED_AT_KEY: Final = "instagram:restricted_at"

# CopyTrading's phone profile. The app version is left to instagrapi's default: a pinned
# old one is refused with "Your version of Instagram is out of date".
DEVICE: Final[dict[str, Any]] = {
    "android_version": 34,
    "android_release": "14",
    "dpi": "480dpi",
    "resolution": "1080x2340",
    "manufacturer": "samsung",
    "device": "a35x",
    "model": "SM-S721B",
    "cpu": "exynos2400e",
}


class InstagramPublishError(RuntimeError):
    """Publishing failed; the message says what the operator should do."""


class InstagramRestrictedError(InstagramPublishError):
    """Instagram flagged the upload as automated and it did not go up."""


def dry_run() -> bool:
    return settings.IG_DELIVERY != "instagram"


def paused() -> float:
    """Seconds left of a pause after Instagram flagged a post (0: not paused)."""
    return 0.0 if dry_run() else db.kv_ttl(RESTRICT_PAUSE_KEY)


def _is_restriction(exc: Exception) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return any(flag in text for flag in ("feedbackrequired", "feedback_required", "restrict certain activity"))


def new_client() -> Any:
    try:
        from instagrapi import Client
    except ImportError as exc:
        raise InstagramPublishError("instagrapi is not installed: pip install -r requirements.txt") from exc
    client = Client()
    client.delay_range = [1, 3]
    try:
        client.set_device({**client.device_settings, **DEVICE})
        client.set_user_agent()
    except Exception:  # noqa: BLE001 - a default device still works
        logger.debug("instagram.set_device_failed")
    return client


def get_client() -> Any:
    """A client on the saved session. Never logs in fresh (that needs a human for the code)."""
    if not (settings.IG_USERNAME and settings.IG_PASSWORD):
        raise InstagramPublishError("Set IG_USERNAME / IG_PASSWORD in .env.")
    session = settings.IG_SESSION_PATH
    if not session.exists():
        raise InstagramPublishError("No Instagram session yet: run `flask --app run ig-login` once.")
    client = new_client()
    try:
        client.load_settings(str(session))
        client.get_timeline_feed()  # probe the session without logging in
    except Exception as exc:  # noqa: BLE001
        raise InstagramPublishError(
            f"Saved Instagram session is not valid ({type(exc).__name__}): run `flask --app run ig-login` again."
        ) from exc
    return client


def login_interactive() -> str:
    """First-time login: resolves Instagram's 6-digit challenge once and saves the session."""
    username = settings.IG_USERNAME or input("IG username: ").strip()
    password = settings.IG_PASSWORD or getpass.getpass("IG password: ")
    session = settings.IG_SESSION_PATH
    session.parent.mkdir(parents=True, exist_ok=True)
    session.unlink(missing_ok=True)
    client = new_client()

    def challenge_code(_username: Any = None, _choice: Any = None) -> str:
        print("\nInstagram wants to verify this login: enter the 6-digit code.")
        while True:
            code = input("Code: ").strip()
            if code.isdigit() and len(code) == 6:
                return code
            print("Exactly 6 digits, please.")

    def change_password(_username: Any) -> str:
        raise RuntimeError("Instagram asks for a password reset: do it in the app, then re-run.")

    client.challenge_code_handler = challenge_code
    client.change_password_handler = change_password
    # instagrapi encrypts the password with a key from /qe/sync/, which Instagram no longer
    # serves (404, 2026-10-27); the login accepts the "version 0" form without that key.
    sent = password if password.startswith("#PWD_") else f"#PWD_INSTAGRAM:0:{int(time.time())}:{password}"
    code = client.totp_generate_code(settings.IG_TOTP_SECRET) if settings.IG_TOTP_SECRET else ""
    try:
        client.login(username, sent, verification_code=code)
    except Exception as exc:  # noqa: BLE001 - instagrapi raises several types for one 2FA step
        # Two-step verification: Instagram texted / emailed a code (or it's in the
        # authenticator app). It sometimes answers "please wait" at this step too.
        if code or not any(k in f"{type(exc).__name__} {exc}".lower() for k in ("two_factor", "twofactor", "two-factor", "please wait")):
            raise
        print(f"\nInstagram wants a two-step verification code ({type(exc).__name__}).")
        print("Use the code Instagram just sent (SMS / email / WhatsApp) or your authenticator app.")
        code = challenge_code()
        client.login(username, sent, verification_code=code)
    client.get_timeline_feed()
    client.dump_settings(str(session))
    return str(session)


def login_with_sessionid(sessionid: str) -> str:
    """Adopt a browser session instead of logging in with the password: Instagram throttles
    password logins from scripts ("Please wait a few minutes"), never a session you already
    have. The ``sessionid`` cookie comes from instagram.com in the browser."""
    sessionid = sessionid.strip().strip('"')
    if not sessionid[:1].isdigit() or len(sessionid) < 30:
        raise InstagramPublishError("That is not a sessionid cookie (it starts with your numeric user id).")
    client = new_client()
    client.login_by_sessionid(sessionid)
    client.get_timeline_feed()
    session = settings.IG_SESSION_PATH
    session.parent.mkdir(parents=True, exist_ok=True)
    client.dump_settings(str(session))
    return str(session)


def _restricted(exc: Exception, client: Any, caption: str) -> dict[str, str]:
    """Instagram answered "we restrict certain activity". If the post went up anyway (the
    usual case) it is recorded and posting rests briefly; if not, it rests for hours."""
    db.kv_set(RESTRICTED_AT_KEY, datetime.now(UTC).isoformat())
    try:
        for media in client.user_medias(client.user_id, amount=3):
            if (getattr(media, "caption_text", "") or "").strip() == caption.strip():
                db.kv_set(RESTRICT_PAUSE_KEY, "1", ttl=settings.IG_WARNED_PAUSE_MINUTES * 60)
                logger.warning("instagram.warned_but_live error=%s", str(exc)[:200])
                return {**_media(media), "restricted": "1"}
    except Exception:  # noqa: BLE001 - the lookup is best effort
        logger.exception("instagram.restricted_lookup_failed")
    db.kv_set(RESTRICT_PAUSE_KEY, "1", ttl=settings.IG_RESTRICT_PAUSE_HOURS * 3600)
    raise InstagramRestrictedError(
        f"Instagram refused the post as automated; posting paused {settings.IG_RESTRICT_PAUSE_HOURS:g} h ({exc})"
    ) from exc


def _media(media: Any) -> dict[str, str]:
    code = getattr(media, "code", "") or ""
    return {
        "media_id": str(getattr(media, "pk", "") or getattr(media, "id", "")),
        "permalink": f"https://www.instagram.com/p/{code}/" if code else "",
        "delivered": "instagram",
    }


def publish_photo(path: Path, caption: str, *, client: Any = None) -> dict[str, str]:
    if dry_run():
        settings.OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        image = settings.OUTBOX_DIR / f"{stamp}-{Path(path).name}"
        shutil.copyfile(path, image)
        image.with_suffix(".txt").write_text(caption, encoding="utf-8")
        logger.info("instagram.dry_run file=%s", image.name)
        return {"media_id": f"dry:{stamp}", "permalink": "", "delivered": "dry_run"}
    client = client or get_client()
    try:
        media = client.photo_upload(str(path), caption)
    except Exception as exc:  # noqa: BLE001
        if _is_restriction(exc):
            return _restricted(exc, client, caption)
        if "challenge" in str(exc).lower():
            raise InstagramPublishError("Instagram is challenging this login: run `flask --app run ig-login`.") from exc
        raise InstagramPublishError(f"photo_upload failed: {exc}") from exc
    return _media(media)
