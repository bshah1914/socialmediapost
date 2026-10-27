"""One chat call to an NVIDIA NIM endpoint, returning the reply as a JSON object (ported
from CopyTrading's ``nvidia_client``).

NVIDIA's free catalogue retires and overloads models without notice (a 410 or a 503), so
every call tries ``NVIDIA_MODEL`` and then each of ``NVIDIA_FALLBACK_MODELS``. Every failure
surfaces as :class:`LLMError`; each agent decides how to fail (a rule-based fallback).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import requests

from technews.config import settings

logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """Transport, protocol or parse failure talking to the model."""


@dataclass(frozen=True, slots=True)
class ChatResult:
    data: dict[str, Any]
    model: str
    latency_ms: int


def available() -> bool:
    return bool(settings.NVIDIA_API_KEY)


def extract_json_object(text: str) -> dict[str, Any]:
    """The reply as one JSON object, tolerating code fences and prose around it."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("```")[1] if "```" in text[3:] else text[3:]
        if text.lstrip().startswith("json"):
            text = text.lstrip()[4:]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, stop = text.find("{"), text.rfind("}")
        if start == -1 or stop <= start:
            raise LLMError("no JSON object in reply") from None
        try:
            data = json.loads(text[start : stop + 1])
        except json.JSONDecodeError as exc:
            raise LLMError(f"malformed JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise LLMError(f"JSON was {type(data).__name__}, not an object")
    return data


def model_chain() -> list[str]:
    chain: list[str] = []
    for name in (settings.NVIDIA_MODEL, *settings.NVIDIA_FALLBACK_MODELS):
        if name and name not in chain:
            chain.append(name)
    return chain


def _extras(model: str) -> dict[str, Any]:
    """Nemotron 3 thinks out loud by default, which spends the tokens and breaks the JSON."""
    if model.startswith("nvidia/nemotron-3"):
        return {"chat_template_kwargs": {"enable_thinking": False}}
    return {}


def chat_json(*, system: str, user: str, max_tokens: int = 2000, temperature: float = 0.2) -> ChatResult:
    """The first model in the chain that answers with a JSON object."""
    if not available():
        raise LLMError("no NVIDIA_API_KEY configured")
    errors = []
    for model in model_chain():
        try:
            return _chat_once(system, user, model, max_tokens, temperature)
        except LLMError as exc:
            logger.warning("llm.model_failed model=%s error=%s", model, str(exc)[:200])
            errors.append(f"{model}: {exc}")
    raise LLMError("; ".join(errors)[:500] or "no model configured")


def _chat_once(system: str, user: str, model: str, max_tokens: int, temperature: float) -> ChatResult:
    url = f"{settings.NVIDIA_API_BASE_URL.rstrip('/')}/chat/completions"
    if not url.startswith("https://"):
        raise LLMError(f"refusing non-HTTPS endpoint {url!r}")
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
        **_extras(model),
    }
    started = time.monotonic()
    try:
        r = requests.post(
            url,
            json=body,
            headers={"Authorization": f"Bearer {settings.NVIDIA_API_KEY}", "Accept": "application/json"},
            timeout=settings.NVIDIA_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise LLMError(f"transport: {exc}") from exc
    if r.status_code != 200:
        raise LLMError(f"HTTP {r.status_code}: {r.text[:200]}")
    try:
        content = r.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"unexpected response shape: {exc}") from exc
    return ChatResult(extract_json_object(content), model, int((time.monotonic() - started) * 1000))
