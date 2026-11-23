"""The background schedule: one newsroom cycle every ``SCAN_INTERVAL_MINUTES``."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler

from technews import analytics, backup, brands, config, db, festivals, monitor, pipeline, telegram, videos, watchdog
from technews.agents import analyst, scout, trainer
from technews.config import settings

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None


def start() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    _scheduler = BackgroundScheduler(timezone=settings.TIMEZONE)
    _scheduler.add_job(
        cycle,
        "interval",
        minutes=settings.SCAN_INTERVAL_MINUTES,
        id="newsroom_cycle",
        next_run_time=datetime.now(_scheduler.timezone) + timedelta(seconds=10),  # first cycle right after boot
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )
    _scheduler.add_job(backup.due, "interval", hours=1, id="backup", next_run_time=datetime.now(_scheduler.timezone) + timedelta(minutes=2), max_instances=1, coalesce=True, misfire_grace_time=3600)
    _scheduler.add_job(study, "cron", hour=3, minute=30, id="agents_study", max_instances=1, coalesce=True, misfire_grace_time=3600)
    _scheduler.add_job(scout_sources, "cron", hour=4, minute=10, id="agents_scout", max_instances=1, coalesce=True, misfire_grace_time=3600)
    _scheduler.add_job(festivals_daily, "cron", hour=6, minute=0, id="festivals", max_instances=1, coalesce=True, misfire_grace_time=6 * 3600)
    _scheduler.add_job(festivals_daily, "date", run_date=datetime.now(_scheduler.timezone) + timedelta(minutes=3), id="festivals_boot", replace_existing=True)
    _scheduler.add_job(festival_reminders, "cron", hour="8,17", minute=0, id="festival_reminders", max_instances=1, coalesce=True, misfire_grace_time=3600)
    _scheduler.add_job(monitor_tick, "interval", minutes=5, id="monitor", max_instances=1, coalesce=True, misfire_grace_time=300)
    _scheduler.add_job(telegram_buttons, "interval", seconds=8, id="telegram_buttons", max_instances=1, coalesce=True, misfire_grace_time=30)
    _scheduler.add_job(followers, "cron", hour=23, minute=40, id="analytics_followers", max_instances=1, coalesce=True, misfire_grace_time=6 * 3600)
    _scheduler.add_job(followers, "date", run_date=datetime.now(_scheduler.timezone) + timedelta(minutes=4), id="analytics_followers_boot", replace_existing=True)
    _scheduler.add_job(weekly_report, "cron", day_of_week="mon", hour=9, minute=30, id="analytics_weekly", max_instances=1, coalesce=True, misfire_grace_time=6 * 3600)
    _scheduler.add_job(metrics, "interval", hours=6, id="agents_metrics", max_instances=1, coalesce=True, misfire_grace_time=3600)
    _scheduler.add_job(pipeline.publish_due, "interval", minutes=1, id="scheduled_posts", max_instances=1, coalesce=True, misfire_grace_time=120)
    _scheduler.add_job(heartbeat, "interval", seconds=20, id="heartbeat", max_instances=1, coalesce=True)
    _scheduler.add_job(watchdog.check, "interval", minutes=10, id="watchdog", max_instances=1, coalesce=True, misfire_grace_time=600)
    _scheduler.add_job(watchdog.health, "interval", minutes=30, id="health", max_instances=1, coalesce=True, misfire_grace_time=900,
                       next_run_time=datetime.now(_scheduler.timezone) + timedelta(minutes=6))  # fmt: skip
    _scheduler.add_job(videos_tick, "interval", minutes=1, id="videos", max_instances=1, coalesce=True, misfire_grace_time=120)
    _scheduler.add_job(videos.cleanup, "cron", hour=2, minute=40, id="videos_cleanup", max_instances=1, coalesce=True, misfire_grace_time=6 * 3600)
    _scheduler.start()
    logger.info("scheduler.started every=%dmin", settings.SCAN_INTERVAL_MINUTES)
    return _scheduler


RETRY_MINUTES = 2
MAX_RETRIES = 3
_retries = 0


def cycle() -> None:
    """One cycle with the latest settings; if it failed, try again in a couple of minutes (a few times)."""
    global _retries
    config.refresh()
    result = pipeline.run_cycle()
    if result.get("error") and _scheduler is not None and _retries < MAX_RETRIES:
        _retries += 1
        logger.warning("scheduler.retry attempt=%d in=%dmin", _retries, RETRY_MINUTES)
        _scheduler.add_job(cycle, "date", run_date=datetime.now(_scheduler.timezone) + timedelta(minutes=RETRY_MINUTES), id="cycle_retry", replace_existing=True)
    elif not result.get("error"):
        _retries = 0


def study() -> None:
    """Overnight, for every active brand: the analyst reports and the trainer turns your decisions into rules."""
    for brand in brands.active():
        with brands.use(brand):
            for step in (analyst.report, trainer.learn):
                try:
                    step()
                except Exception:  # noqa: BLE001 - one brand's problem must not stop the others
                    logger.exception("scheduler.study_failed step=%s brand=%s", step.__module__, brand["name"])


def scout_sources() -> None:
    try:
        scout.run()
    except Exception:  # noqa: BLE001
        logger.exception("scheduler.scout_failed")


def festivals_daily() -> None:
    try:
        festivals.run()
    except Exception:  # noqa: BLE001
        logger.exception("scheduler.festivals_failed")


def festival_reminders() -> None:
    try:
        festivals.remind()
    except Exception:  # noqa: BLE001
        logger.exception("scheduler.festival_reminders_failed")


def monitor_tick() -> None:
    try:
        monitor.tick()
    except Exception:  # noqa: BLE001
        logger.exception("scheduler.monitor_failed")


def telegram_buttons() -> None:
    try:
        telegram.poll()
    except Exception:  # noqa: BLE001
        logger.exception("scheduler.telegram_failed")


def followers() -> None:
    for brand in brands.active():
        with brands.use(brand):
            try:
                if not analytics.followers_today():
                    analytics.snapshot_followers()
            except Exception:  # noqa: BLE001
                logger.exception("scheduler.followers_failed brand=%s", brand["name"])


def weekly_report() -> None:
    for brand in brands.active():
        with brands.use(brand):
            try:
                analytics.weekly_report()
            except Exception:  # noqa: BLE001
                logger.exception("scheduler.weekly_report_failed brand=%s", brand["name"])


def videos_tick() -> int:
    """Make every video waiting in the queue (each as its own business). Returns how many jobs ran."""
    ran = 0
    try:
        while videos.run_next():
            ran += 1
    except Exception:  # noqa: BLE001 - the next minute tries again
        logger.exception("scheduler.videos_failed")
    return ran


def metrics() -> None:
    for brand in brands.active():
        with brands.use(brand):
            try:
                analyst.collect_metrics()
            except Exception:  # noqa: BLE001
                logger.exception("scheduler.metrics_failed brand=%s", brand["name"])


def heartbeat() -> None:
    """Let the web app see that this scheduler is alive, and take settings changed on the dashboard."""
    if config.refresh() and _scheduler is not None:
        reschedule()
    db.kv_set("scheduler:beat", json.dumps({"at": db.now(), "jobs": jobs()}), ttl=180)


def reschedule() -> None:
    """Apply a changed SCAN_INTERVAL_MINUTES to the running schedule."""
    if _scheduler is not None and _scheduler.get_job("newsroom_cycle"):
        _scheduler.reschedule_job("newsroom_cycle", trigger="interval", minutes=settings.SCAN_INTERVAL_MINUTES)
        logger.info("scheduler.rescheduled every=%dmin", settings.SCAN_INTERVAL_MINUTES)


def jobs() -> list[dict[str, str]]:
    if _scheduler is None:  # the scheduler runs in its own process; its last report is in the database
        try:
            return json.loads(db.kv_get("scheduler:beat") or "{}").get("jobs", [])
        except ValueError:
            return []
    return [{"id": j.id, "next_run": j.next_run_time.isoformat() if j.next_run_time else ""} for j in _scheduler.get_jobs()]


def stop() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=True)
        _scheduler = None
