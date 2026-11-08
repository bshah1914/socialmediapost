"""Web Push: alerts that reach a phone or computer where the app is installed, even when the
dashboard is closed. The keys are made once and kept in the database; each device that turns
alerts on is remembered by its push address."""

from __future__ import annotations

import base64
import json
import logging
from typing import Any, Final

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from technews import db
from technews.config import settings

logger = logging.getLogger(__name__)

PRIVATE_KEY: Final = "push:private"
PUBLIC_KEY: Final = "push:public"
SUBSCRIPTIONS: Final = "push:subscriptions"
MAX_DEVICES: Final = 20
TTL_SECONDS: Final = 6 * 3600  # how long the push service keeps an alert for a device that is off; Windows (Edge) refuses 0


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def public_key() -> str:
    """The key the browser needs to subscribe; made on first use."""
    if not db.kv_get(PUBLIC_KEY):
        key = ec.generate_private_key(ec.SECP256R1())
        db.kv_set(PRIVATE_KEY, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                 serialization.NoEncryption()).decode())  # fmt: skip
        db.kv_set(PUBLIC_KEY, _b64(key.public_key().public_bytes(serialization.Encoding.X962,
                                                                 serialization.PublicFormat.UncompressedPoint)))  # fmt: skip
    return db.kv_get(PUBLIC_KEY) or ""


def subscriptions() -> list[dict[str, Any]]:
    try:
        return list(json.loads(db.kv_get(SUBSCRIPTIONS) or "[]"))
    except ValueError:
        return []


def _save(subs: list[dict[str, Any]]) -> None:
    db.kv_set(SUBSCRIPTIONS, json.dumps(subs[-MAX_DEVICES:]))


def subscribe(sub: dict[str, Any]) -> int:
    """Remember a device. Raises ValueError when the subscription is not a real one."""
    endpoint = str(sub.get("endpoint", ""))
    keys = sub.get("keys") or {}
    if not endpoint.startswith("https://") or not keys.get("p256dh") or not keys.get("auth"):
        raise ValueError("That is not a valid push subscription")
    kept = [s for s in subscriptions() if s.get("endpoint") != endpoint]
    _save([*kept, {"endpoint": endpoint, "keys": {"p256dh": keys["p256dh"], "auth": keys["auth"]}}])
    return len(subscriptions())


def unsubscribe(endpoint: str) -> int:
    _save([s for s in subscriptions() if s.get("endpoint") != endpoint])
    return len(subscriptions())


def send(title: str, body: str, url: str = "/#approvals") -> dict[str, str]:
    """Push to every device; a device that has gone away is forgotten. Returns per-device results."""
    from pywebpush import WebPushException, webpush  # imported here: only needed when something is sent
    from py_vapid import Vapid

    public_key()
    vapid = Vapid.from_pem((db.kv_get(PRIVATE_KEY) or "").encode())
    payload = json.dumps({"title": title, "body": body, "url": url})
    results: dict[str, str] = {}
    for sub in subscriptions():
        host = sub["endpoint"].split("/")[2]
        try:
            webpush(subscription_info=sub, data=payload, vapid_private_key=vapid,
                    vapid_claims={"sub": f"mailto:{settings.BRAND_EMAIL}"}, timeout=15, ttl=TTL_SECONDS)  # fmt: skip
            results[host] = "sent"
        except WebPushException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (404, 410):  # the app was uninstalled or alerts were turned off
                unsubscribe(sub["endpoint"])
                results[host] = "removed (device gone)"
            else:
                results[host] = f"failed: {exc}"[:150]
    return results
