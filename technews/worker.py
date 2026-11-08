"""The scheduler as its own process: ``python -m technews.worker``. The web app then runs with
SCHEDULER_ENABLED=false, so a restart or a busy page never interrupts a cycle."""

from __future__ import annotations

import logging
import signal
import threading

from technews import config, db, scheduler
from technews.config import settings


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    db.init()
    config.seed()
    config.refresh(force=True)
    settings.SCHEDULER_ENABLED = True
    scheduler.start()
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    stop.wait()
    scheduler.stop()


if __name__ == "__main__":
    main()
