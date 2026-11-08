"""LinkedIn publisher: posts a flyer image with its caption to a company page (or a profile)
through LinkedIn's Images and Posts APIs.

Needs an access token with ``w_organization_social`` (company page) or ``w_member_social``
(profile) and the author URN, for example ``urn:li:organization:12345678``. Like Instagram,
``IG_DELIVERY=dry_run`` writes to the outbox instead of posting.
"""

from __future__ import annotations

import logging
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import requests

from technews.config import settings

logger = logging.getLogger(__name__)

API: Final = "https://api.linkedin.com/rest"
VERSION: Final = "202506"  # LinkedIn's API version header (YYYYMM)
TIMEOUT: Final = 60
_RESERVED = re.compile(r"([\\|{}@\[\]()<>*_~])")  # "little text" characters; # stays so hashtags work


class LinkedInError(RuntimeError):
    pass


def configured() -> bool:
    return bool(settings.LINKEDIN_ENABLED and settings.LINKEDIN_ACCESS_TOKEN and settings.LINKEDIN_AUTHOR)


def _headers(*, json: bool = True) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {settings.LINKEDIN_ACCESS_TOKEN}",
        "LinkedIn-Version": VERSION,
        "X-Restli-Protocol-Version": "2.0.0",
    }
    if json:
        headers["Content-Type"] = "application/json"
    return headers


def _check(resp: requests.Response, what: str) -> None:
    if resp.ok:
        return
    hint = " The access token is invalid or expired (they last 60 days)." if resp.status_code == 401 else ""
    raise LinkedInError(f"{what} failed ({resp.status_code}): {resp.text[:200]}{hint}")


def escape(text: str) -> str:
    return _RESERVED.sub(r"\\\1", text)


def publish_photo(path: Path, caption: str, *, alt: str = "") -> dict[str, str]:
    """Post the image and caption; returns ``{"delivered", "post_id", "permalink"}``."""
    if settings.IG_DELIVERY != "instagram":
        settings.OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        image = settings.OUTBOX_DIR / f"linkedin-{stamp}-{Path(path).name}"
        shutil.copyfile(path, image)
        image.with_suffix(".txt").write_text(caption, encoding="utf-8")
        logger.info("linkedin.dry_run file=%s", image.name)
        return {"delivered": "dry_run", "post_id": f"dry:{stamp}", "permalink": ""}
    if not configured():
        raise LinkedInError("LinkedIn is not set up: add the access token and author in Settings.")
    author = settings.LINKEDIN_AUTHOR
    try:
        init = requests.post(f"{API}/images?action=initializeUpload", headers=_headers(), timeout=TIMEOUT,
                             json={"initializeUploadRequest": {"owner": author}})  # fmt: skip
        _check(init, "image upload start")
        value = init.json()["value"]
        with open(path, "rb") as fh:
            put = requests.put(value["uploadUrl"], data=fh, headers=_headers(json=False), timeout=TIMEOUT)
        _check(put, "image upload")
        post = requests.post(f"{API}/posts", headers=_headers(), timeout=TIMEOUT, json={
            "author": author,
            "commentary": escape(caption),
            "visibility": "PUBLIC",
            "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
            "content": {"media": {"altText": alt[:300], "id": value["image"]}},
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        })  # fmt: skip
        _check(post, "post")
    except requests.RequestException as exc:
        raise LinkedInError(f"LinkedIn could not be reached: {exc}") from exc
    post_id = post.headers.get("x-restli-id", "")
    logger.info("linkedin.posted id=%s", post_id)
    return {"delivered": "linkedin", "post_id": post_id,
            "permalink": f"https://www.linkedin.com/feed/update/{post_id}/" if post_id else ""}  # fmt: skip


def check() -> str:
    """Confirm the token works (for the dashboard's Test button); returns who it is."""
    if not (settings.LINKEDIN_ACCESS_TOKEN and settings.LINKEDIN_AUTHOR):
        raise LinkedInError("Save the access token and the author URN first.")
    try:
        resp = requests.get("https://api.linkedin.com/v2/userinfo", headers=_headers(json=False), timeout=20)
    except requests.RequestException as exc:
        raise LinkedInError(f"LinkedIn could not be reached: {exc}") from exc
    if resp.status_code == 403:  # a token with only w_organization_social cannot read the profile
        return f"Token accepted for {settings.LINKEDIN_AUTHOR} (profile read not granted, which is fine for posting)."
    _check(resp, "token check")
    return f"Connected as {resp.json().get('name', 'unknown')}, posting as {settings.LINKEDIN_AUTHOR}."
