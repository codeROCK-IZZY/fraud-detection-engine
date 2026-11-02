"""Worker process: polls the job queue and dispatches to handlers.

Run with ``python -m app.cli worker``. Stops gracefully on SIGTERM/SIGINT after
the current job finishes (important for container orchestrators like ECS).
"""

import logging
import signal
import time
import traceback
from collections.abc import Callable, Mapping
from typing import Any

from sqlalchemy.orm import Session

from app.core.logging import log_extra
from app.db.models import Job
from app.db.session import new_session
from app.jobs import queue

logger = logging.getLogger(__name__)

Handler = Callable[[Session, dict[str, Any]], dict[str, Any] | None]


def _handlers() -> Mapping[str, Handler]:
    from app.jobs import handlers

    return handlers.HANDLERS


def run_one(db: Session) -> bool:
    """Claim and run a single job. Returns False if the queue was empty."""
    job = queue.claim_next(db)
    if job is None:
        return False
    job_id = job.id
    handler = _handlers().get(job.job_type)
    started = time.perf_counter()
    try:
        if handler is None:
            raise ValueError(f"no handler for job type '{job.job_type}'")
        result = handler(db, job.payload)
        db.commit()
        queue.mark_succeeded(db, job, result)
        logger.info(
            "job succeeded",
            extra=log_extra(job_id=job.id, job_type=job.job_type, seconds=round(time.perf_counter() - started, 2)),
        )
    except Exception as exc:
        db.rollback()
        fresh = db.get(Job, job_id, populate_existing=True)
        if fresh is None:
            raise
        job = fresh
        queue.mark_failed(db, job, f"{exc}\n{traceback.format_exc()}")
        logger.exception("job failed", extra=log_extra(job_id=job.id, job_type=job.job_type, attempt=job.attempts))
    return True


def drain(max_jobs: int = 10_000) -> int:
    """Run jobs until the queue is empty (used by the CLI and tests)."""
    db = new_session()
    try:
        n = 0
        while n < max_jobs and run_one(db):
            n += 1
        return n
    finally:
        db.close()


def run_forever(poll_seconds: float = 2.0) -> None:
    stopping = False

    def _stop(*_: object) -> None:
        nonlocal stopping
        stopping = True
        logger.info("worker stopping after current job")

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    logger.info("worker started")
    db = new_session()
    last_stale_check = 0.0
    try:
        while not stopping:
            if time.monotonic() - last_stale_check > 300:
                if n := queue.requeue_stale(db):
                    logger.warning("requeued stale jobs", extra=log_extra(count=n))
                last_stale_check = time.monotonic()
            try:
                worked = run_one(db)
            except Exception:  # DB outage etc. — back off, keep the worker alive
                logger.exception("worker loop error")
                db.rollback()
                worked = False
                time.sleep(poll_seconds * 5)
            if not worked:
                time.sleep(poll_seconds)
    finally:
        db.close()
