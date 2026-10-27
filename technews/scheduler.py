"""The background schedule: one newsroom cycle every ``SCAN_INTERVAL_MINUTES``."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler

from technews import pipeline
from technews.config import settings

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None


def start() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    _scheduler = BackgroundScheduler(timezone=settings.TIMEZONE)
    _scheduler.add_job(
        pipeline.run_cycle,
        "interval",
        minutes=settings.SCAN_INTERVAL_MINUTES,
        id="newsroom_cycle",
        next_run_time=datetime.now() + timedelta(seconds=10),  # first cycle right after boot
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )
    _scheduler.start()
    logger.info("scheduler.started every=%dmin", settings.SCAN_INTERVAL_MINUTES)
    return _scheduler


def jobs() -> list[dict[str, str]]:
    if _scheduler is None:
        return []
    return [{"id": j.id, "next_run": j.next_run_time.isoformat() if j.next_run_time else ""} for j in _scheduler.get_jobs()]
