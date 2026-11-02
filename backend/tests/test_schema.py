"""The ORM models and the migrated database must agree, and the job queue must work."""

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from app.db.models import metadata
from app.db.session import get_engine
from app.jobs import queue
from app.jobs.worker import run_one


def test_models_match_migrations(clean_db):
    with get_engine().connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), metadata)
    assert diff == []


def test_job_queue_claims_once_and_retries_with_backoff(db):
    job = queue.enqueue(db, "does_not_exist", {"x": 1}, max_attempts=2)
    db.commit()
    assert job is not None
    assert queue.enqueue(db, "does_not_exist", {"x": 1}, dedupe=True).id == job.id  # type: ignore[union-attr]
    assert run_one(db) is True
    db.refresh(job)
    assert job.status == "queued" and job.attempts == 1 and "no handler" in (job.last_error or "")
    assert run_one(db) is False  # backoff: not runnable yet
