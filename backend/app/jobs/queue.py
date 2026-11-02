"""PostgreSQL-backed job queue.

How claiming works: ``SELECT ... FOR UPDATE SKIP LOCKED`` lets many workers poll
the same table safely — each locked row is invisible to other workers until the
claiming transaction commits, so a job is never run twice concurrently.

Retries: a failed job is re-queued with exponential backoff (30s, 60s, 120s...)
until ``max_attempts`` is reached, then marked ``failed`` with the error kept.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db.models import Job

STALE_AFTER = timedelta(minutes=30)


def enqueue(
    db: Session, job_type: str, payload: dict[str, Any] | None = None, *, max_attempts: int = 3, dedupe: bool = False
) -> Job | None:
    """Add a job to the current transaction. With ``dedupe`` an identical queued
    job is reused instead of adding another (e.g. repeated scoring requests)."""
    payload = payload or {}
    if dedupe:
        existing = db.scalar(
            select(Job).where(Job.job_type == job_type, Job.status == "queued", Job.payload == payload).limit(1)
        )
        if existing:
            return existing
    job = Job(job_type=job_type, payload=payload, max_attempts=max_attempts)
    db.add(job)
    db.flush()
    return job


def claim_next(db: Session) -> Job | None:
    """Atomically claim the oldest runnable job. Commits the claim."""
    row = db.execute(
        text(
            """
            UPDATE jobs SET status = 'running', locked_at = now(), attempts = attempts + 1
            WHERE id = (
                SELECT id FROM jobs
                WHERE status = 'queued' AND run_after <= now()
                ORDER BY run_after, id
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            RETURNING id
            """
        )
    ).first()
    db.commit()
    # populate_existing: the row was changed by raw SQL, so refresh any cached instance.
    return db.get(Job, row[0], populate_existing=True) if row else None


def mark_succeeded(db: Session, job: Job, result: dict[str, Any] | None = None) -> None:
    job.status = "succeeded"
    job.finished_at = datetime.now(UTC)
    job.result = result or {}
    job.last_error = None
    db.commit()


def mark_failed(db: Session, job: Job, error: str) -> None:
    job.last_error = error[-4000:]
    if job.attempts >= job.max_attempts:
        job.status = "failed"
        job.finished_at = datetime.now(UTC)
    else:
        job.status = "queued"
        job.run_after = datetime.now(UTC) + timedelta(seconds=30 * 2 ** (job.attempts - 1))
    job.locked_at = None
    db.commit()


def requeue_stale(db: Session) -> int:
    """Return jobs stuck in 'running' (e.g. worker crashed) to the queue."""
    result = db.execute(
        text("UPDATE jobs SET status = 'queued', locked_at = NULL WHERE status = 'running' AND locked_at < :cutoff"),
        {"cutoff": datetime.now(UTC) - STALE_AFTER},
    )
    db.commit()
    return int(result.rowcount)  # type: ignore[attr-defined]
