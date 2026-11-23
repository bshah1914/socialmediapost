"""Approve or reject a post from the Telegram alert: the alert carries buttons, and this picks up the taps.

The server asks Telegram for new taps every few seconds (no inbound port is needed). Only taps from the
chat set in Settings count. Approving asks "Yes, post it now" first, because it publishes at once."""

from __future__ import annotations

import json
import logging
from typing import Any, Final

import requests

from technews import db, notify, pipeline
from technews.config import settings

logger = logging.getLogger(__name__)

OFFSET_KEY: Final = "telegram:offset"
TIMEOUT: Final = 15


def active() -> bool:
    """Only the live site listens (a staging copy sharing the bot would otherwise steal the taps)."""
    return bool(settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHAT_ID) and settings.IG_DELIVERY == "instagram"


def _api(method: str, **data: Any) -> dict[str, Any]:
    resp = requests.post(f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/{method}", data=data, timeout=TIMEOUT)
    try:
        return resp.json()
    except ValueError:
        return {"ok": False}


def _markup(rows: list[list[dict[str, str]]]) -> str:
    return json.dumps({"inline_keyboard": rows})


def _edit(msg: dict[str, Any], note: str = "", markup: str | None = None) -> None:
    """Change the alert under the tap: a result line at the bottom and the buttons swapped or removed."""
    ids = {"chat_id": msg["chat"]["id"], "message_id": msg["message_id"]}
    markup = _markup([]) if markup is None else markup
    try:
        if note:
            if msg.get("photo") or msg.get("video"):
                _api("editMessageCaption", **ids, caption=f"{msg.get('caption', '')}\n\n{note}"[:1024], reply_markup=markup)
            else:
                _api("editMessageText", **ids, text=f"{msg.get('text', '')}\n\n{note}"[:4000], reply_markup=markup)
        else:
            _api("editMessageReplyMarkup", **ids, reply_markup=markup)
    except requests.RequestException as exc:
        logger.warning("telegram.edit_failed error=%s", exc)


def _answer(cb: dict[str, Any], text: str, *, alert: bool = False) -> None:
    try:
        _api("answerCallbackQuery", callback_query_id=cb["id"], text=text[:190], show_alert="true" if alert else "false")
    except requests.RequestException as exc:
        logger.warning("telegram.answer_failed error=%s", exc)


def _allowed(chat: str, action: str, post_id: int) -> bool:
    """The admin chat decides every business's posts; a business's own chat only that business's posts
    (and never the "possibly missed" news taps, which are the admin's)."""
    if chat == str(settings.TELEGRAM_CHAT_ID).strip():
        return True
    if action == "md":
        return False
    found = db.rows("SELECT b.telegram_chat FROM posts p JOIN brands b ON b.id = p.brand_id WHERE p.id = ?", post_id)
    return bool(found) and bool(str(found[0]["telegram_chat"] or "").strip()) and str(found[0]["telegram_chat"]).strip() == chat


def handle(cb: dict[str, Any]) -> None:
    msg = cb.get("message") or {}
    chat = str((msg.get("chat") or {}).get("id", ""))
    action, _, raw = str(cb.get("data") or "").partition(":")
    known = raw.isdigit() and action in ("ap", "rj", "ok", "no", "md")
    if not _allowed(chat, action, int(raw) if known else 0):
        logger.warning("telegram.tap_from_other_chat")
        _answer(cb, "This chat is not allowed to approve posts.", alert=True)
        return
    if not known:
        _answer(cb, "That button is not recognised.")
        return
    post_id = int(raw)
    if action == "md":  # a "possibly missed" story: make it into a draft (it arrives as its own message)
        _answer(cb, "Making the post...")
        out = pipeline.draft_missed(post_id)
        _edit(msg, "\U0001f4dd Made into a post: it is waiting for your approval" if out.get("pending") else
              "✅ Posted" if out.get("posted") else f"Nothing done: {out.get('reason', '')}")  # fmt: skip
        return
    if action == "ap":  # publishing is immediate, so ask once more
        _answer(cb, "Tap Yes to post it now.")
        _edit(msg, markup=_markup([[{"text": "✅ Yes, post it now", "callback_data": f"ok:{post_id}"},
                                    {"text": "Cancel", "callback_data": f"no:{post_id}"}]]))  # fmt: skip
    elif action == "no":
        _answer(cb, "Cancelled. It is still waiting.")
        _edit(msg, markup=notify.buttons(post_id))
    elif action == "rj":
        out = pipeline.reject(post_id)
        _answer(cb, "Rejected." if out.get("rejected") else str(out.get("reason", "Could not reject it.")))
        _edit(msg, "❌ Rejected" if out.get("rejected") else f"Nothing done: {out.get('reason', '')}")
    else:  # ok
        _answer(cb, "Posting...")
        out = pipeline.approve(post_id)
        if out.get("posted"):
            _edit(msg, "✅ Approved and posted" + (f"\n{out['permalink']}" if out.get("permalink") else ""))
        elif "not waiting" in str(out.get("reason", "")):
            _edit(msg, f"Nothing done: {out['reason']}")
        else:  # a pause or a login problem: it stays waiting, so the buttons stay
            _edit(msg, f"⚠ Not posted: {out.get('reason', 'unknown problem')}", markup=notify.buttons(post_id))


def poll() -> int:
    """Take the taps made since the last look; returns how many were handled."""
    if not active():
        return 0
    got = _api("getUpdates", offset=int(db.kv_get(OFFSET_KEY) or 0), timeout=0, allowed_updates=json.dumps(["callback_query"]))
    handled = 0
    for update in got.get("result", []) if got.get("ok") else []:
        db.kv_set(OFFSET_KEY, str(update["update_id"] + 1))  # first, so a crash can never approve twice
        if cb := update.get("callback_query"):
            try:
                handle(cb)
                handled += 1
            except Exception:  # noqa: BLE001 - one bad tap must not block the next
                logger.exception("telegram.tap_failed")
    return handled
