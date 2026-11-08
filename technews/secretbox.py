"""Keeps passwords and tokens in ``.env`` unreadable to anyone who only gets the data folder or a backup.
The key lives outside it (the ``SECRETS_KEY`` setting of the container). Without a key nothing changes."""

from __future__ import annotations

import logging
import os
from typing import Final

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

PREFIX: Final = "enc:v1:"


def _box() -> Fernet | None:
    key = os.environ.get("SECRETS_KEY", "").strip()
    if not key:
        return None
    try:
        return Fernet(key.encode())
    except ValueError:
        logger.error("SECRETS_KEY is not a valid key (make one with: python -m technews.secretbox)")
        return None


def available() -> bool:
    return _box() is not None


def sealed(value: str) -> bool:
    return value.startswith(PREFIX)


def seal(value: str) -> str:
    box = _box()
    if not value or box is None or sealed(value):
        return value
    return PREFIX + box.encrypt(value.encode()).decode()


def reveal(value: str) -> str:
    """The plain text of a sealed value; anything else comes back as it is. An unreadable one is empty."""
    if not sealed(value):
        return value
    box = _box()
    if box is None:
        logger.error("a secret is sealed but SECRETS_KEY is not set")
        return ""
    try:
        return box.decrypt(value[len(PREFIX) :].encode()).decode()
    except InvalidToken:
        logger.error("a secret could not be opened: SECRETS_KEY is not the key it was sealed with")
        return ""


if __name__ == "__main__":
    print(Fernet.generate_key().decode())
