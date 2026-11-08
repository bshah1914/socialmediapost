"""Copies of the database. The free option is a copy on the server's own disk, taken on a schedule
and trimmed to the newest few. How often and how many are set on the dashboard."""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from technews import db, secretbox
from technews.config import settings

logger = logging.getLogger(__name__)

ENABLED_KEY: Final = "backup:enabled"
HOURS_KEY: Final = "backup:hours"
KEEP_KEY: Final = "backup:keep"
LAST_KEY: Final = "backup:last"
OFFSITE_KEY: Final = "backup:offsite"  # the result of the last copy sent away from this server
CHECK_KEY: Final = "backup:check"  # the result of opening the newest copy
DEFAULTS: Final = {"enabled": True, "hours": 24, "keep": 14}


def folder() -> Path:
    return settings.DATA_DIR / "backups"


def options() -> dict[str, Any]:
    return {
        "enabled": (db.kv_get(ENABLED_KEY) or "1") == "1",
        "hours": int(db.kv_get(HOURS_KEY) or DEFAULTS["hours"]),
        "keep": int(db.kv_get(KEEP_KEY) or DEFAULTS["keep"]),
        "last": db.kv_get(LAST_KEY) or "",
        "check": db.kv_get(CHECK_KEY) or "",
        "offsite": offsite_status(),
        "engine": db.engine(),
    }


def save_options(enabled: bool, hours: int, keep: int) -> None:
    if not 1 <= hours <= 168:
        raise ValueError("Back up every 1 to 168 hours")
    if not 1 <= keep <= 90:
        raise ValueError("Keep between 1 and 90 copies")
    db.kv_set(ENABLED_KEY, "1" if enabled else "0")
    db.kv_set(HOURS_KEY, str(hours))
    db.kv_set(KEEP_KEY, str(keep))


def listing() -> list[dict[str, Any]]:
    files = sorted([*folder().glob("newsroom-*.db"), *folder().glob("newsroom-*.dump")], reverse=True) if folder().exists() else []
    return [{"name": f.name, "mb": round(f.stat().st_size / 1_048_576, 2)} for f in files]


def _copy(target: Path) -> None:
    if db.postgres():
        if not shutil.which("pg_dump"):
            raise RuntimeError("pg_dump is not installed on this machine")
        subprocess.run(["pg_dump", "--format=custom", "--file", str(target), settings.DATABASE_URL], check=True, capture_output=True, timeout=600)
        return
    src = sqlite3.connect(settings.DB_PATH, timeout=30)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def verify(path: Path) -> str:
    """Open a copy the way a restore would and check it holds the data. Returns "ok" or what is wrong."""
    try:
        if path.suffix == ".dump":
            if not shutil.which("pg_restore"):
                return "pg_restore is not installed, so the copy could not be checked"
            contents = subprocess.run(["pg_restore", "--list", str(path)], check=True, capture_output=True, text=True, timeout=120).stdout
            missing = [t for t in ("stories", "posts", "users", "kv") if f" {t} " not in contents]
            return "ok" if not missing else "missing tables: " + ", ".join(missing)
        conn = sqlite3.connect(path)
        try:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                return "the copy is damaged"
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            missing = [t for t in ("stories", "posts", "users", "kv") if t not in tables]
            return "ok" if not missing else "missing tables: " + ", ".join(missing)
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - the result is shown to the admin
        return f"could not be opened: {str(exc)[:120]}"


# --------------------------------------------------------------------------- a copy away from this server
# A copy on the same disk does not survive the disk. Each copy (and the Instagram session) can also be sent to a
# bucket at any S3-compatible service (Cloudflare R2, Backblaze B2, AWS S3) and/or a folder (a NAS or a synced drive).
# What leaves the server is sealed with the secrets key first, so a stolen bucket shows nothing.

SESSIONS: Final = "ig_session*.json"


def offsite_configured() -> list[str]:
    where = []
    if settings.BACKUP_S3_BUCKET and settings.BACKUP_S3_ID and settings.BACKUP_S3_SECRET:
        where.append("bucket")
    if settings.BACKUP_COPY_DIR:
        where.append("folder")
    return where


def offsite_status() -> dict[str, Any]:
    return {"where": offsite_configured(), "last": db.kv_get(OFFSITE_KEY) or "", "sealed": secretbox.available()}


def _outgoing(path: Path) -> tuple[str, bytes]:
    """The name and bytes to send away: sealed with the secrets key when there is one."""
    data = path.read_bytes()
    if secretbox.available():
        from cryptography.fernet import Fernet

        return path.name + ".enc", Fernet(os.environ["SECRETS_KEY"].strip().encode()).encrypt(data)
    return path.name, data


def _s3():
    import boto3  # imported here: only a server that sends copies away needs it

    return boto3.client("s3", endpoint_url=settings.BACKUP_S3_ENDPOINT or None, aws_access_key_id=settings.BACKUP_S3_ID,
                        aws_secret_access_key=settings.BACKUP_S3_SECRET, region_name="auto")  # fmt: skip


def send_offsite(path: Path) -> list[str]:
    """Send one copy (and the saved Instagram sessions) away. Returns where it went; raises when a destination fails."""
    files = [path, *sorted(settings.DATA_DIR.glob(SESSIONS))]
    sent: list[str] = []
    if settings.BACKUP_S3_BUCKET and settings.BACKUP_S3_ID and settings.BACKUP_S3_SECRET:
        client = _s3()
        for f in files:
            name, data = _outgoing(f)
            prefix = "database/" if f is path else "sessions/"
            client.put_object(Bucket=settings.BACKUP_S3_BUCKET, Key=prefix + name, Body=data)
        listed = client.list_objects_v2(Bucket=settings.BACKUP_S3_BUCKET, Prefix="database/").get("Contents", [])
        stale = sorted((o["Key"] for o in listed), reverse=True)[options()["keep"] :]
        if stale:
            client.delete_objects(Bucket=settings.BACKUP_S3_BUCKET, Delete={"Objects": [{"Key": k} for k in stale]})
        sent.append("bucket")
    if settings.BACKUP_COPY_DIR:
        target = Path(settings.BACKUP_COPY_DIR)
        target.mkdir(parents=True, exist_ok=True)
        for f in files:
            name, data = _outgoing(f)
            (target / name).write_bytes(data)
        for old in sorted(target.glob("newsroom-*"), reverse=True)[options()["keep"] :]:
            old.unlink(missing_ok=True)
        sent.append("folder")
    return sent


def _offsite(path: Path) -> str:
    if not offsite_configured():
        return ""
    try:
        sent = send_offsite(path)
        result = f"{db.now()} ok: {' and '.join(sent)}"
    except Exception as exc:  # noqa: BLE001 - shown on the dashboard, and the admin is told
        result = f"{db.now()} FAILED: {type(exc).__name__}: {str(exc)[:160]}"
        logger.error("backup.offsite_failed error=%s", exc)
        from technews import notify

        notify.send("CloudTrio newsroom: the off-site backup failed", result)
    db.kv_set(OFFSITE_KEY, result)
    return result


def run() -> dict[str, Any]:
    """Take a copy now (safe while the app is running), open it to check it, and delete the oldest beyond the limit."""
    folder().mkdir(parents=True, exist_ok=True)
    suffix = "dump" if db.postgres() else "db"
    target = folder() / f"newsroom-{datetime.now().strftime('%Y%m%d-%H%M%S')}.{suffix}"
    _copy(target)
    result = verify(target)
    db.kv_set(CHECK_KEY, f"{db.now()} {result}")
    if result != "ok":
        logger.error("backup.verify_failed file=%s result=%s", target.name, result)
    for old in listing()[options()["keep"] :]:
        (folder() / old["name"]).unlink(missing_ok=True)
    db.kv_set(LAST_KEY, db.now())
    away = _offsite(target) if result == "ok" else ""  # a damaged copy is never sent away
    logger.info("backup.done file=%s check=%s offsite=%s", target.name, result, away or "-")
    return {"name": target.name, "mb": round(target.stat().st_size / 1_048_576, 2), "check": result, "offsite": away}


def due() -> None:
    """Called by the schedule every hour; takes a copy when one is due."""
    opts = options()
    if not opts["enabled"]:
        return
    last = datetime.fromisoformat(opts["last"]) if opts["last"] else None
    if last is None or (datetime.now(last.tzinfo) - last).total_seconds() >= opts["hours"] * 3600 - 60:
        run()
