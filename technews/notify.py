"""Tell the editor a post is waiting for approval: email (with the picture attached), Telegram,
and a webhook (Slack, Discord and Teams all accept the same small JSON). Only the channels set
up in Settings are used, they run in the background, and a failure never holds up the newsroom."""

from __future__ import annotations

import json
import logging
import mimetypes
import re
import smtplib
import threading
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Final

import requests

from technews import db, push
from technews.config import settings

logger = logging.getLogger(__name__)

ERROR_KEY: Final = "notify:last_error"
TIMEOUT: Final = 20


def link() -> str:
    return f"{settings.PUBLIC_URL.rstrip('/')}/#approvals" if settings.PUBLIC_URL else ""


def channels() -> list[str]:
    found = []
    if settings.NOTIFY_EMAIL_TO and settings.SMTP_HOST:
        found.append("email")
    if settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHAT_ID:
        found.append("telegram")
    if push.subscriptions():
        found.append("app")
    if settings.NOTIFY_WEBHOOK_URL:
        found.append("webhook")
    if settings.WHATSAPP_PROVIDER == "callmebot" and settings.WHATSAPP_TO and settings.WHATSAPP_APIKEY:
        found.append("whatsapp")
    if settings.WHATSAPP_PROVIDER == "twilio" and settings.WHATSAPP_TO and settings.WHATSAPP_APIKEY             and settings.TWILIO_SID and settings.TWILIO_WHATSAPP_FROM:
        found.append("whatsapp")
    return found


def _email(title: str, text: str, image: Path | None) -> None:
    msg = EmailMessage()
    msg["Subject"], msg["From"] = title, settings.SMTP_FROM or settings.SMTP_USER or settings.NOTIFY_EMAIL_TO.split(",")[0]
    msg["To"] = ", ".join(p for p in re.split(r"[,;\s]+", settings.NOTIFY_EMAIL_TO) if p)
    msg.set_content(text)
    if image and image.exists():
        kind = (mimetypes.guess_type(image.name)[0] or "image/jpeg").split("/")
        msg.add_attachment(image.read_bytes(), maintype=kind[0], subtype=kind[1], filename=image.name)
    port = settings.SMTP_PORT
    server = smtplib.SMTP_SSL(settings.SMTP_HOST, port, timeout=TIMEOUT) if port == 465 else smtplib.SMTP(settings.SMTP_HOST, port, timeout=TIMEOUT)
    with server:
        if port != 465:
            server.ehlo()
            if server.has_extn("starttls"):
                server.starttls()
                server.ehlo()
        if settings.SMTP_USER:
            server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
        server.send_message(msg)


def buttons(post_id: int) -> str:
    """The Approve and Reject buttons under a Telegram alert (plus a link to the dashboard when the site has a public address)."""
    rows: list[list[dict[str, str]]] = [[{"text": "\u2705 Approve", "callback_data": f"ap:{post_id}"},
                                         {"text": "\u274c Reject", "callback_data": f"rj:{post_id}"}]]  # fmt: skip
    if link().startswith("https://"):
        rows.append([{"text": "Open in the dashboard", "url": link()}])
    return json.dumps({"inline_keyboard": rows})


def _telegram(title: str, text: str, image: Path | None, post_id: int = 0) -> None:
    base = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}"
    body = f"{title}\n\n{text}"
    extra = {"reply_markup": buttons(post_id)} if post_id else {}
    if image and image.exists():
        video = image.suffix.lower() == ".mp4"
        with open(image, "rb") as fh:
            resp = requests.post(f"{base}/{'sendVideo' if video else 'sendPhoto'}", timeout=60 if video else TIMEOUT,
                                 data={"chat_id": settings.TELEGRAM_CHAT_ID, "caption": body[:1000], **({"supports_streaming": "true"} if video else {}), **extra},
                                 files={"video" if video else "photo": fh})  # fmt: skip
    else:
        resp = requests.post(f"{base}/sendMessage", data={"chat_id": settings.TELEGRAM_CHAT_ID, "text": body, **extra}, timeout=TIMEOUT)
    if not resp.ok:
        raise RuntimeError(f"Telegram said {resp.status_code}: {resp.text[:120]}")


def _webhook(title: str, text: str, image: Path | None) -> None:
    message = f"{title}\n{text}"
    resp = requests.post(settings.NOTIFY_WEBHOOK_URL, json={"text": message, "content": message}, timeout=TIMEOUT)
    if not resp.ok:
        raise RuntimeError(f"The webhook said {resp.status_code}: {resp.text[:120]}")


def _whatsapp(title: str, text: str, image: Path | None) -> None:
    """WhatsApp text (the picture sits behind the sign-in, so the message carries the link instead)."""
    body = f"{title}\n\n{text}"
    to = re.sub(r"\D", "", settings.WHATSAPP_TO)
    sender = re.sub(r"\D", "", settings.TWILIO_WHATSAPP_FROM)
    if settings.WHATSAPP_PROVIDER == "twilio":
        resp = requests.post(f"https://api.twilio.com/2010-04-01/Accounts/{settings.TWILIO_SID}/Messages.json",
                             auth=(settings.TWILIO_SID, settings.WHATSAPP_APIKEY), timeout=TIMEOUT,
                             data={"From": f"whatsapp:+{sender}",
                                   "To": f"whatsapp:+{to}", "Body": body[:1500]})  # fmt: skip
    else:  # CallMeBot
        resp = requests.get("https://api.callmebot.com/whatsapp.php", timeout=TIMEOUT,
                            params={"phone": to, "text": body[:1500], "apikey": settings.WHATSAPP_APIKEY})  # fmt: skip
    if not resp.ok:
        raise RuntimeError(f"WhatsApp ({settings.WHATSAPP_PROVIDER}) said {resp.status_code}: {resp.text[:120]}")


def _app(title: str, text: str, image: Path | None) -> None:
    """A push alert to the installed app on each device."""
    bad = {k: v for k, v in push.send(title, text.splitlines()[0], "/#approvals").items() if not v.startswith(("sent", "removed"))}
    if bad:
        raise RuntimeError("; ".join(f"{k}: {v}" for k, v in bad.items()))


SENDERS: Final = {"app": _app, "email": _email, "telegram": _telegram, "webhook": _webhook, "whatsapp": _whatsapp}


def send(title: str, text: str, image: Path | None = None, post_id: int = 0) -> dict[str, str]:
    """Send to every set-up channel; returns ``{channel: "sent" | error}``. With ``post_id`` Telegram gets Approve and Reject buttons."""
    out: dict[str, str] = {}
    for name in channels():
        try:
            if name == "telegram":
                _telegram(title, text, image, post_id)
            else:
                SENDERS[name](title, text, image)
            out[name] = "sent"
        except Exception as exc:  # noqa: BLE001 - one broken channel must not stop the others
            out[name] = f"{type(exc).__name__}: {exc}"[:200]
            logger.warning("notify.%s_failed error=%s", name, exc)
    problems = {k: v for k, v in out.items() if v != "sent"}
    db.kv_set(ERROR_KEY, "; ".join(f"{k}: {v}" for k, v in problems.items()) if problems else "")
    return out


def draft_ready(headline: str, kind: str, image: Path | None, *, wait: bool = False, post_id: int = 0) -> threading.Thread | None:
    """A draft is waiting: tell the editor (in the background unless ``wait``). The message says what it is and
    carries the caption; Telegram also gets Approve and Reject buttons for ``post_id``."""
    if not channels():
        return None
    where = link()
    caption = ""
    if post_id and (found := db.rows("SELECT caption FROM posts WHERE id = ?", post_id)):
        caption = str(found[0]["caption"] or "").strip()
    label = {"flyer": "Flyer", "animated flyer": "Animated flyer (Reel)", "animated news": "Animated news post (Reel)"}.get(kind, "News post")
    text = f"{label}: {headline}\n\n"
    if caption:
        text += f"Caption:\n{caption[:560]}{'...' if len(caption) > 560 else ''}\n\n"
    text += f"Review and approve: {where}" if where else "Open the dashboard, Approvals, to review it."
    job = threading.Thread(target=send, args=("CloudTrio: a post is waiting for your approval", text, image, post_id), daemon=True)
    job.start()
    if wait:
        job.join()
    return job


def last_error() -> str:
    return db.kv_get(ERROR_KEY) or ""


def test() -> dict[str, Any]:
    if not channels():
        return {"ok": False, "message": "Set up email, Telegram or a webhook first, then save."}
    results = send("CloudTrio: test notification", "This is a test. If you can read it, approval alerts will reach you here.\n" + link())
    ok = all(v == "sent" for v in results.values())
    return {"ok": ok, "message": "; ".join(f"{k}: {v}" for k, v in results.items()), "results": results}
