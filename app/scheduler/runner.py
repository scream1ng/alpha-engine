from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import text

from db.models import DATABASE_URL, PipelineLog, SessionLocal

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None


def get_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = BackgroundScheduler(timezone="UTC")
    return _scheduler


def _ensure_lock_table(db) -> None:
    try:
        db.execute(text("""
            CREATE TABLE IF NOT EXISTS scheduler_locks (
                job_name  TEXT PRIMARY KEY,
                locked_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """))
        db.commit()
    except Exception:
        db.rollback()


def _acquire_lock(db, job_name: str) -> bool:
    try:
        if "postgresql" in DATABASE_URL:
            db.execute(text("DELETE FROM scheduler_locks WHERE locked_at < NOW() - INTERVAL '2 hours'"))
        elif "sqlite" in DATABASE_URL:
            db.execute(text("DELETE FROM scheduler_locks WHERE locked_at < datetime('now', '-2 hours')"))
        result = db.execute(
            text(
                "INSERT INTO scheduler_locks (job_name) VALUES (:name) "
                "ON CONFLICT DO NOTHING RETURNING job_name"
            ),
            {"name": job_name},
        )
        db.commit()
        return result.fetchone() is not None
    except Exception as exc:
        logger.warning("Scheduler lock unavailable (%s); running without lock", exc)
        try:
            db.rollback()
        except Exception:
            pass
        return True


def _release_lock(db, job_name: str) -> None:
    try:
        db.execute(text("DELETE FROM scheduler_locks WHERE job_name = :name"), {"name": job_name})
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass


def _tracked(job_name: str, fn: Callable[[], dict | None]) -> None:
    db = SessionLocal()
    _ensure_lock_table(db)
    if not _acquire_lock(db, job_name):
        logger.warning("Scheduler job %s skipped; lock already held", job_name)
        db.close()
        return

    started = datetime.now(timezone.utc)
    try:
        db.add(PipelineLog(
            market="all",
            stage=f"scheduler:{job_name}",
            outcome="running",
            details={"started_at": started.isoformat()},
        ))
        db.commit()

        result = fn() or {}
        duration_s = (datetime.now(timezone.utc) - started).total_seconds()
        db.add(PipelineLog(
            market="all",
            stage=f"scheduler:{job_name}",
            outcome="completed",
            details={**result, "duration_s": round(duration_s, 2)},
        ))
        db.commit()
        logger.info("Scheduler job %s completed in %.1fs", job_name, duration_s)
    except Exception as exc:
        db.add(PipelineLog(
            market="all",
            stage=f"scheduler:{job_name}",
            outcome="failed",
            details={"error": str(exc)[:800]},
        ))
        db.commit()
        logger.exception("Scheduler job %s failed", job_name)
    finally:
        _release_lock(db, job_name)
        db.close()


def register_jobs() -> None:
    from app.scheduler.jobs import (
        run_commodity_scan_paper,
        run_crypto_scan_paper,
        run_equities_scan_paper,
    )

    scheduler = get_scheduler()
    jobs = [
        ("equities_scan_paper", run_equities_scan_paper, "mon-fri"),
        ("commodity_scan_paper", run_commodity_scan_paper, "sun,mon,tue,wed,thu,fri"),
        ("crypto_scan_paper", run_crypto_scan_paper, "sun,mon,tue,wed,thu,fri,sat"),
    ]
    for job_id, fn, day_of_week in jobs:
        scheduler.add_job(
            _tracked,
            "cron",
            args=[job_id, fn],
            day_of_week=day_of_week,
            hour=10,
            minute=0,
            id=job_id,
            replace_existing=True,
        )
    logger.info("Registered %d scheduler job(s)", len(scheduler.get_jobs()))


def start_scheduler() -> None:
    if os.getenv("ALPHA_SCHEDULER_ENABLED", "1").strip().lower() in {"0", "false", "no"}:
        logger.info("Scheduler disabled by ALPHA_SCHEDULER_ENABLED")
        return

    scheduler = get_scheduler()
    register_jobs()
    if not scheduler.running:
        scheduler.start()
        logger.info("Scheduler started")


def shutdown_scheduler() -> None:
    scheduler = get_scheduler()
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")
