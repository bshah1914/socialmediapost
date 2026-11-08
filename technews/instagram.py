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
import json
import re
import logging
import queue
import shutil
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from urllib.parse import unquote

from technews import config, db, secretbox
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
    return 0.0 if dry_run() else db.kv_ttl(RESTRICT_PAUSE_KEY + _brand_suffix())


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
    client.password_encrypt = plain_password  # type: ignore[method-assign]
    try:
        client.set_device({**client.device_settings, **DEVICE})
        client.set_user_agent()
    except Exception:  # noqa: BLE001 - a default device still works
        logger.debug("instagram.set_device_failed")
    return client


def plain_password(password: str) -> str:
    """The password in Instagram's "version 0" form. instagrapi encrypts it with a key read from /qe/sync/, which
    Instagram stopped serving (2026-10-27): its older login route then failed with ``int(None)``. The login accepts
    this form without the key, as the newer route already does."""
    return password if password.startswith("#PWD_") else f"#PWD_INSTAGRAM:0:{int(time.time())}:{password}"


SESSION_USER_KEY: Final = "instagram:session_user"
LOGOUT_ALERT_KEY: Final = "instagram:logout_alert"


def _brand_suffix() -> str:
    return "" if settings.BRAND_ID == 1 else f":{settings.BRAND_ID}"


def session_user() -> str:
    """The account the saved session belongs to ("" when there is none)."""
    return (db.kv_get(SESSION_USER_KEY + _brand_suffix()) or "") if settings.IG_SESSION_PATH.exists() else ""


def get_client() -> Any:
    """A client on the saved session. Never logs in fresh (that needs a human for the code)."""
    session = settings.IG_SESSION_PATH
    if not session.exists():
        raise InstagramPublishError("Instagram is not connected: Settings > Instagram > Connect on the dashboard.")
    owner = session_user()
    if owner and settings.IG_USERNAME and owner.lower() != settings.IG_USERNAME.lower():
        raise InstagramPublishError(
            f"The saved session is @{owner}, but the username is @{settings.IG_USERNAME}: connect that account."
        )
    client = new_client()
    try:
        client.set_settings(read_session(session))
        client.get_timeline_feed()  # probe the session without logging in
    except Exception as exc:  # noqa: BLE001
        _tell_logged_out(type(exc).__name__)
        raise InstagramPublishError(
            f"Saved Instagram session is not valid ({type(exc).__name__}): connect Instagram again."
        ) from exc
    return client


def _tell_logged_out(why: str) -> None:
    if settings.IG_DELIVERY != "instagram" or not db.kv_claim(LOGOUT_ALERT_KEY + _brand_suffix(), 6 * 3600):
        return
    try:
        from technews import notify

        notify.send("CloudTrio: Instagram logged the newsroom out",
                    f"Instagram ended the saved session ({why}), so nothing can be posted. Reconnect under Settings, Instagram "
                    f"(password, or the browser session). {settings.PUBLIC_URL.rstrip('/')}/#settings")  # fmt: skip
    except Exception:  # noqa: BLE001 - the alert is best effort
        logger.exception("instagram.logout_alert_failed")


def read_session(path: Path) -> dict[str, Any]:
    """The saved session as a dict, whether the file is plain JSON or sealed with the secrets key."""
    text = path.read_text(encoding="utf-8").strip()
    if secretbox.sealed(text):
        text = secretbox.reveal(text)
        if not text:
            raise InstagramPublishError("The saved Instagram session is sealed and cannot be opened with this SECRETS_KEY: connect Instagram again.")
    return json.loads(text)


def write_session(path: Path, data: dict[str, Any]) -> None:
    """Write the session sealed (plain only when no secrets key is set), readable by this account alone."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(secretbox.seal(json.dumps(data, indent=4)), encoding="utf-8")
    path.chmod(0o600)


def seal_saved_sessions() -> int:
    """Seal any session file still held as plain text (every brand's). Returns how many were changed."""
    if not secretbox.available():
        return 0
    changed = 0
    for path in settings.DATA_DIR.glob("ig_session*.json"):
        try:
            if not secretbox.sealed(path.read_text(encoding="utf-8").strip()):
                write_session(path, json.loads(path.read_text(encoding="utf-8")))
                changed += 1
        except (OSError, ValueError):
            logger.exception("instagram.seal_failed file=%s", path.name)
    return changed


def _save_session(client: Any, username: str) -> str:
    session = settings.IG_SESSION_PATH
    write_session(session, client.get_settings())
    db.kv_set(SESSION_USER_KEY + _brand_suffix(), username.lower())
    return str(session)


def _wants_code(exc: Exception) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return any(k in text for k in ("two_factor", "twofactor", "two-factor", "please wait"))


def login(username: str, password: str, ask_code: Callable[[str], str]) -> str:
    """Log in with the password and save the session. ``ask_code(why)`` returns the 6-digit
    code when Instagram asks for one (a challenge or two-step verification)."""
    username = username.strip().lstrip("@")
    client = new_client()

    def challenge_code(_username: Any = None, _choice: Any = None) -> str:
        return ask_code("Instagram wants to verify this login")

    def change_password(_username: Any) -> str:
        raise InstagramPublishError("Instagram asks for a password reset: do it in the app, then connect again.")

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
        if code or not _wants_code(exc):
            raise
        client.login(username, sent, verification_code=ask_code("Instagram wants a two-step verification code"))
    client.get_timeline_feed()
    return _save_session(client, username)


def login_interactive() -> str:
    """The CLI login: asks for the code on the terminal."""
    username = settings.IG_USERNAME or input("IG username: ").strip()
    password = settings.IG_PASSWORD or getpass.getpass("IG password: ")

    def ask_code(why: str) -> str:
        print(f"\n{why}: enter the 6-digit code (SMS / email / WhatsApp / authenticator app).")
        while True:
            code = input("Code: ").strip()
            if code.isdigit() and len(code) == 6:
                return code
            print("Exactly 6 digits, please.")

    return login(username, password, ask_code)


def login_with_sessionid(sessionid: str) -> str:
    """Adopt a browser session instead of logging in with the password: Instagram throttles
    password logins from scripts ("Please wait a few minutes"), never a session you already
    have. The ``sessionid`` cookie comes from instagram.com in the browser."""
    sessionid = unquote(sessionid.strip().strip('"').strip())
    if not re.match(r"^\d+:", sessionid) or len(sessionid) < 30:
        raise InstagramPublishError("That is not a sessionid cookie (it starts with your numeric user id).")
    client = new_client()
    try:
        client.login_by_sessionid(sessionid)
        client.get_timeline_feed()
    except Exception as exc:  # noqa: BLE001
        logger.warning("instagram.sessionid_failed error=%s", str(exc)[:200], exc_info=True)
        raise InstagramPublishError(
            "Instagram did not accept that session. Copy sessionid again from a browser that is still logged in to "
            "Instagram (and do not log out there afterwards)."
        ) from exc
    username = str(getattr(client, "username", "") or client.account_info().username)
    return _save_session(client, username)


# --------------------------------------------------------------------------- dashboard login


class LoginJob:
    """A password login run in the background for the dashboard: when Instagram asks for a
    code the job waits (``state == "need_code"``) until ``submit_code`` hands it one."""

    CODE_TIMEOUT: Final = 600

    def __init__(self, username: str, password: str) -> None:
        self.username = username.strip().lstrip("@")
        self.state = "running"  # running | need_code | ok | error
        self.message = "Logging in…"
        self._codes: queue.Queue[str] = queue.Queue()
        self._ov = config.current_override()  # the job runs for the brand that started it
        threading.Thread(target=self._run, args=(password,), daemon=True).start()

    def _ask_code(self, why: str) -> str:
        self.state, self.message = "need_code", f"{why}: enter the 6-digit code Instagram sent (SMS / email / app)."
        try:
            code = self._codes.get(timeout=self.CODE_TIMEOUT)
        except queue.Empty:
            raise InstagramPublishError("No code entered within 10 minutes: connect again.") from None
        self.state, self.message = "running", "Checking the code…"
        return code

    def _run(self, password: str) -> None:
        try:
            with config.override(self._ov):
                login(self.username, password, self._ask_code)
            self.state, self.message = "ok", f"Connected as @{self.username}."
        except Exception as exc:  # noqa: BLE001 - shown on the dashboard
            logger.warning("instagram.login_failed error=%s", str(exc)[:200], exc_info=True)
            hint = " Instagram is throttling logins from this machine: wait, or use the browser sessionid." if "please wait" in str(exc).lower() else ""
            self.state, self.message = "error", f"Login failed: {exc}.{hint}"

    def submit_code(self, code: str) -> None:
        self._codes.put(code.strip())

    def as_dict(self) -> dict[str, str]:
        return {"state": self.state, "message": self.message, "username": self.username}


_job: LoginJob | None = None


def start_login(username: str, password: str) -> LoginJob:
    global _job
    if _job is not None and _job.state in ("running", "need_code"):
        raise InstagramPublishError("A login is already in progress.")
    _job = LoginJob(username, password)
    return _job


def current_login() -> LoginJob | None:
    return _job


def _restricted(exc: Exception, client: Any, caption: str) -> dict[str, str]:
    """Instagram answered "we restrict certain activity". If the post went up anyway (the
    usual case) it is recorded and posting rests briefly; if not, it rests for hours."""
    db.kv_set(RESTRICTED_AT_KEY + _brand_suffix(), datetime.now(UTC).isoformat())
    try:
        for media in client.user_medias(client.user_id, amount=3):
            if (getattr(media, "caption_text", "") or "").strip() == caption.strip():
                db.kv_set(RESTRICT_PAUSE_KEY + _brand_suffix(), "1", ttl=settings.IG_WARNED_PAUSE_MINUTES * 60)
                logger.warning("instagram.warned_but_live error=%s", str(exc)[:200])
                return {**_media(media), "restricted": "1"}
    except Exception:  # noqa: BLE001 - the lookup is best effort
        logger.exception("instagram.restricted_lookup_failed")
    db.kv_set(RESTRICT_PAUSE_KEY + _brand_suffix(), "1", ttl=settings.IG_RESTRICT_PAUSE_HOURS * 3600)
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


def publish_reel(video: Path, cover: Path, caption: str, *, client: Any = None) -> dict[str, str]:
    """An animated post: the video as a Reel, with ``cover`` as its picture in the grid."""
    if dry_run():
        settings.OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        out = settings.OUTBOX_DIR / f"{stamp}-{Path(video).name}"
        shutil.copyfile(video, out)
        out.with_suffix(".txt").write_text(caption, encoding="utf-8")
        logger.info("instagram.dry_run_reel file=%s", out.name)
        return {"media_id": f"dry:{stamp}", "permalink": "", "delivered": "dry_run"}
    client = client or get_client()
    try:
        media = client.clip_upload(Path(video), caption, thumbnail=Path(cover))
    except Exception as exc:  # noqa: BLE001
        if _is_restriction(exc):
            return _restricted(exc, client, caption)
        if "challenge" in str(exc).lower():
            raise InstagramPublishError("Instagram is challenging this login: run `flask --app run ig-login`.") from exc
        raise InstagramPublishError(f"clip_upload failed: {exc}") from exc
    code = getattr(media, "code", "") or ""
    return {**_media(media), "permalink": f"https://www.instagram.com/reel/{code}/" if code else ""}


def _live_media(media_id: str) -> bool:
    """A post that is really on Instagram (not a dry-run copy on staging)."""
    return bool(media_id) and not media_id.startswith("dry:") and not dry_run()


def edit_caption(media_id: str, caption: str, *, client: Any = None) -> None:
    """Change the caption of a post that is already on Instagram (the picture cannot be changed)."""
    if not _live_media(media_id):
        return
    client = client or get_client()
    try:
        client.media_edit(media_id, caption)
    except Exception as exc:  # noqa: BLE001
        raise InstagramPublishError(f"Instagram did not take the new caption: {exc}") from exc


def delete_media(media_id: str, *, client: Any = None) -> None:
    """Take a post down from Instagram."""
    if not _live_media(media_id):
        return
    client = client or get_client()
    try:
        done = client.media_delete(media_id)
    except Exception as exc:  # noqa: BLE001
        raise InstagramPublishError(f"Instagram did not delete the post: {exc}") from exc
    if done is False:
        raise InstagramPublishError("Instagram did not delete the post")
