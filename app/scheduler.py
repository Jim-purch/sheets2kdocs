"""APScheduler wiring: one job per enabled task, refreshed on task changes."""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from . import db, sync_engine

logger = logging.getLogger("sheets2kdocs.scheduler")

scheduler = BackgroundScheduler(timezone="Asia/Shanghai")  # tz set at start()


def start(timezone: str) -> None:
    scheduler.configure(timezone=timezone)
    scheduler.start()
    for t in db.q_all("SELECT * FROM sync_tasks"):
        apply_task(dict(t))


def shutdown() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)


def _job(task_id: int) -> None:
    try:
        result = sync_engine.run_task(task_id)
        if result.get("status") == "error":
            logger.warning("scheduled task %s failed: %s", task_id, result.get("error"))
    except Exception:
        logger.exception("scheduled task %s crashed", task_id)


def build_trigger(task: dict):
    stype = task.get("schedule_type") or "manual"
    value = (task.get("schedule_value") or "").strip()
    if not task.get("enabled") or stype == "manual" or not value:
        return None
    if stype == "interval":
        try:
            minutes = int(value)
        except ValueError:
            logger.error("invalid interval value %r", value)
            return None
        return IntervalTrigger(minutes=max(1, minutes))
    if stype == "cron":
        try:
            return CronTrigger.from_crontab(value, timezone=scheduler.timezone)
        except ValueError as e:
            logger.error("invalid cron %r: %s", value, e)
            return None
    return None


def apply_task(task: dict) -> None:
    job_id = f"task_{task['id']}"
    existing = scheduler.get_job(job_id)
    trigger = build_trigger(task)
    if trigger is None:
        if existing:
            scheduler.remove_job(job_id)
        return
    if existing:
        existing.reschedule(trigger)
    else:
        scheduler.add_job(
            _job,
            trigger=trigger,
            id=job_id,
            args=[task["id"]],
            max_instances=1,
            coalesce=True,
            misfire_grace_time=300,
        )


def remove_task(task_id: int) -> None:
    job_id = f"task_{task_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)


def next_run_times() -> dict[int, str]:
    out: dict[int, str] = {}
    for job in scheduler.get_jobs():
        if job.id.startswith("task_"):
            try:
                tid = int(job.id.removeprefix("task_"))
                nxt = job.next_run_time
                out[tid] = nxt.strftime("%m-%d %H:%M") if nxt else "-"
            except (ValueError, AttributeError):
                continue
    return out
