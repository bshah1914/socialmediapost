"""Which device is this? A phone, a tablet or a computer, from the browser's own words, so the
dashboard can lay itself out to suit (the page refines the guess in the browser, which also knows
the screen). Sign-ins are logged, so the portal can show which devices have signed in."""

from __future__ import annotations

import json
import re
import secrets
from datetime import UTC, datetime
from typing import Any, Final

from technews import db

KEY: Final = "auth:logins"
KEEP: Final = 20


def parse(ua: str, mobile_hint: str = "") -> dict[str, str]:
    """``{"kind": phone|tablet|desktop, "os", "browser", "label"}`` from a User-Agent string
    (and the ``Sec-CH-UA-Mobile`` hint when the browser sends one)."""
    if re.search(r"iPad", ua) or (re.search(r"Android", ua) and not re.search(r"Mobile", ua)) or re.search(r"Tablet|Silk", ua):
        kind = "tablet"
    elif re.search(r"iPhone|iPod|Android.*Mobile|Mobi|Windows Phone", ua) or mobile_hint.strip() == "?1":
        kind = "phone"
    else:
        kind = "desktop"
    if re.search(r"iPhone|iPad|iPod", ua):
        os_name = "iOS" if "iPad" not in ua else "iPadOS"
    elif "Android" in ua:
        os_name = "Android"
    elif "Windows" in ua:
        os_name = "Windows"
    elif "CrOS" in ua:
        os_name = "ChromeOS"
    elif "Mac OS X" in ua or "Macintosh" in ua:
        os_name = "macOS"
    elif "Linux" in ua:
        os_name = "Linux"
    else:
        os_name = "Unknown"
    for pattern, name in ((r"Edg(e|A|iOS)?/", "Edge"), (r"OPR/|Opera", "Opera"), (r"SamsungBrowser", "Samsung Internet"),
                          (r"Firefox/|FxiOS", "Firefox"), (r"Chrome/|CriOS", "Chrome"), (r"Safari/", "Safari")):  # fmt: skip
        if re.search(pattern, ua):
            browser = name
            break
    else:
        browser = "Browser"
    return {"kind": kind, "os": os_name, "browser": browser, "label": f"{browser} on {os_name} · {kind}"}


def recent() -> list[dict[str, Any]]:
    try:
        return list(json.loads(db.kv_get(KEY) or "[]"))
    except ValueError:
        return []


def record(info: dict[str, str], ip: str) -> str:
    """Note a sign-in; the same browser on the same address updates its row. Returns the device id."""
    found = recent()
    now = datetime.now(UTC).isoformat(timespec="seconds")
    same = next((d for d in found if (d["os"], d["browser"], d["kind"], d["ip"]) == (info["os"], info["browser"], info["kind"], ip)), None)
    if same:
        same["at"] = now
        device_id = same["id"]
    else:
        device_id = secrets.token_hex(4)
        found.append({"id": device_id, "kind": info["kind"], "os": info["os"], "browser": info["browser"], "ip": ip, "at": now})
    found.sort(key=lambda d: d["at"], reverse=True)
    db.kv_set(KEY, json.dumps(found[:KEEP]))
    return device_id
