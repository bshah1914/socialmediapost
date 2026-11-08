"""Watches the newsroom from the outside in, so an outage is heard about and not found.

Two checks run every few minutes in the scheduler:

- the public address (``PUBLIC_URL``/health) is fetched the way a visitor reaches it, through the tunnel; two misses in
  a row raise an alert, and a recovery message follows;
- when everything is fine, a "still alive" call goes to ``HEARTBEAT_URL`` (Healthchecks.io, UptimeRobot heartbeat or
  similar). That service is the only thing that can notice the whole server being off, so it is the one that alerts
  when the calls stop.

Both are skipped while the newsroom is a dry run (a staging site must not alarm anyone)."""

from __future__ import annotations

import logging
from typing import Any, Final

import requests

from technews import db, notify, watchdog
from technews.config import settings

logger = logging.getLogger(__name__)

MISSES_KEY: Final = "monitor:misses"
DOWN_KEY: Final = "monitor:down"
LAST_KEY: Final = "monitor:last"
MISSES_BEFORE_ALERT: Final = 2
TIMEOUT: Final = 15


def public_ok() -> tuple[bool, str]:
    if not settings.PUBLIC_URL:
        return True, "no public address set"
    try:
        r = requests.get(settings.PUBLIC_URL.rstrip("/") + "/health", timeout=TIMEOUT)
        return r.status_code == 200, f"answered {r.status_code}"
    except requests.RequestException as exc:
        return False, type(exc).__name__


def tick() -> dict[str, Any]:
    if settings.IG_DELIVERY != "instagram":
        return {"state": "skipped"}
    ok, why = public_ok()
    misses = 0 if ok else int(db.kv_get(MISSES_KEY) or 0) + 1
    db.kv_set(MISSES_KEY, str(misses))
    down = db.kv_get(DOWN_KEY) == "1"
    if misses >= MISSES_BEFORE_ALERT and not down:
        db.kv_set(DOWN_KEY, "1")
        notify.send("CloudTrio newsroom: the site cannot be reached", f"{settings.PUBLIC_URL} did not answer {misses} checks in a row ({why}).")
        logger.error("monitor.down why=%s", why)
    elif ok and down:
        db.kv_set(DOWN_KEY, "")
        notify.send("CloudTrio newsroom: the site is back", f"{settings.PUBLIC_URL} answers again.")
    healthy = ok and db.kv_get(watchdog.ALERTED_KEY) != "1"  # the watchdog has not found the cycles stopped
    beat = "off"
    if settings.HEARTBEAT_URL and healthy:
        try:
            requests.get(settings.HEARTBEAT_URL, timeout=TIMEOUT).raise_for_status()
            beat = "sent"
        except requests.RequestException as exc:
            beat = f"failed: {type(exc).__name__}"
            logger.warning("monitor.heartbeat_failed error=%s", exc)
    elif settings.HEARTBEAT_URL:
        beat = "held back: something is wrong"
    db.kv_set(LAST_KEY, f"{db.now()} public={'ok' if ok else why} heartbeat={beat}")
    return {"state": "down" if misses >= MISSES_BEFORE_ALERT else "ok", "public": why, "heartbeat": beat}


def status() -> dict[str, Any]:
    return {"last": db.kv_get(LAST_KEY) or "", "down": db.kv_get(DOWN_KEY) == "1", "heartbeat": bool(settings.HEARTBEAT_URL)}
