"""End-to-end: the real app in its own process (empty data folder, dry run, no AI key, no Telegram), driven by Chromium.

Run with:  .venv\\Scripts\\python.exe -m pytest tests/e2e -m e2e -q
"""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests

from e2e.helpers import ADMIN, ROOT


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def server(tmp_path_factory):
    data = tmp_path_factory.mktemp("e2e-data")
    (data / ".env").write_text("", encoding="utf-8")
    port = _free_port()
    env = {**os.environ, "DATA_DIR": str(data), "ENV_FILE": str(data / ".env"), "PORT": str(port), "IG_DELIVERY": "dry_run",
           "NVIDIA_API_KEY": "", "LLM2_API_KEY": "", "TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": "", "SCHEDULER_ENABLED": "0",
           "ADMIN_USER": ADMIN[0], "ADMIN_PASSWORD": ADMIN[1], "VIDEO_WORKER": "1", "REEL_SECONDS": "4", "SECRETS_KEY": "",
           "PUBLIC_URL": "", "PYTHONUNBUFFERED": "1"}  # fmt: skip
    log = (data / "server.log").open("w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "run.py"], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    for _ in range(120):
        try:
            if requests.get(base + "/health", timeout=1).ok:
                break
        except requests.RequestException:
            pass
        if proc.poll() is not None:
            raise RuntimeError("the app stopped: " + (data / "server.log").read_text(encoding="utf-8")[-2000:])
        time.sleep(0.5)
    else:
        proc.kill()
        raise RuntimeError("the app did not start")
    yield {"base": base, "data": data}
    proc.terminate()
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.kill()
    log.close()


@pytest.fixture(scope="session")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def sign_in(server, browser):
    """sign_in(user, password, **context options) -> a page signed in, on the dashboard. Page errors fail the test."""
    pages = []

    def make(user=ADMIN[0], password=ADMIN[1], **ctx):
        context = browser.new_context(base_url=server["base"], **ctx)
        page = context.new_page()
        page.errors = []
        page.on("pageerror", lambda exc: page.errors.append(str(exc)))
        page.goto("/login")
        page.get_by_label("Username").fill(user)
        page.get_by_label("Password", exact=True).fill(password)
        page.get_by_role("button", name="Sign in").click()
        page.wait_for_url("**/")
        pages.append(page)
        return page

    yield make
    for page in pages:
        errors = list(page.errors)
        page.context.close()
        assert not errors, f"JavaScript errors on the page: {errors}"
