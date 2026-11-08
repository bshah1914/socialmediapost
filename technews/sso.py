"""Sign in with Google (OpenID Connect, authorization-code flow).

Only people who already have an account here can use it: their Google email must match the email set on their
account (Settings, Team). Nobody is created by signing in with Google, so a Google account alone gets no access.
Roles, blocking and removal work as before. Google's own 2-step check replaces the authenticator code."""

from __future__ import annotations

import base64
import json
import secrets
import time
from typing import Any, Final
from urllib.parse import urlencode

import requests

from technews.config import settings

AUTH_URL: Final = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL: Final = "https://oauth2.googleapis.com/token"
ISSUERS: Final = ("https://accounts.google.com", "accounts.google.com")
TIMEOUT: Final = 15


class SSOError(ValueError):
    """The sign-in could not be completed; the message is safe to show on the login page."""


def enabled() -> bool:
    return bool(settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET)


def redirect_uri() -> str:
    return settings.PUBLIC_URL.rstrip("/") + "/login/google/callback"


def start(next_url: str) -> tuple[str, dict[str, Any]]:
    """The Google address to send the person to, and what to remember in their session until they come back."""
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    query = urlencode({"client_id": settings.GOOGLE_CLIENT_ID, "redirect_uri": redirect_uri(), "response_type": "code", "scope": "openid email",
                       "state": state, "nonce": nonce, "prompt": "select_account"})  # fmt: skip
    return f"{AUTH_URL}?{query}", {"state": state, "nonce": nonce, "next": next_url, "at": time.time()}


def _payload(id_token: str) -> dict[str, Any]:
    try:
        body = id_token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (IndexError, ValueError) as exc:
        raise SSOError("Google sent an answer that could not be read.") from exc


def finish(code: str, state: str, remembered: dict[str, Any] | None) -> str:
    """Trade the code for the verified email address of the person. Raises SSOError when anything does not add up."""
    if not remembered or not secrets.compare_digest(str(remembered.get("state", "")), state or "-") or time.time() - remembered.get("at", 0) > 600:
        raise SSOError("That sign-in took too long or did not start here. Try again.")
    try:
        resp = requests.post(TOKEN_URL, timeout=TIMEOUT, data={"code": code, "client_id": settings.GOOGLE_CLIENT_ID, "client_secret": settings.GOOGLE_CLIENT_SECRET,
                                                               "redirect_uri": redirect_uri(), "grant_type": "authorization_code"})  # fmt: skip
    except requests.RequestException as exc:
        raise SSOError("Google could not be reached. Try again.") from exc
    if not resp.ok:
        raise SSOError("Google did not accept the sign-in. Try again.")
    # The token arrives straight from Google over HTTPS in answer to our own request, so it needs no signature check
    # (OpenID Connect Core 3.1.3.7); its audience, issuer, expiry and nonce must still match.
    claims = _payload(str(resp.json().get("id_token", "")))
    if (claims.get("aud") != settings.GOOGLE_CLIENT_ID or claims.get("iss") not in ISSUERS or float(claims.get("exp", 0)) < time.time()
            or not secrets.compare_digest(str(claims.get("nonce", "")), str(remembered["nonce"]))):  # fmt: skip
        raise SSOError("Google's answer did not check out. Try again.")
    if not claims.get("email_verified") or not claims.get("email"):
        raise SSOError("That Google account has no verified email address.")
    return str(claims["email"]).lower()
