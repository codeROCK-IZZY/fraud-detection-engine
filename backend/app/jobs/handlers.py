"""Background job handlers. Each receives a DB session and the job payload and
returns a small JSON-serialisable result stored on the job row."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import IngestionBatch
from app.ml.backfill import backfill
from app.ml.pipeline import train_from_db
from app.modules.ingestion import uploads
from app.modules.ingestion.service import import_csv
from app.modules.monitoring.service import run_drift_report


def handle_csv_import(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    batch = db.get(IngestionBatch, uuid.UUID(payload["batch_id"]))
    if batch is None:
        raise ValueError("batch not found")
    batch.status = "processing"
    db.commit()
    batch_id = batch.id
    try:
        with uploads.fetch(payload) as path:
            report = import_csv(db, path)
    except Exception as exc:
        db.rollback()
        failed = db.get(IngestionBatch, batch_id)
        if failed is not None:
            failed.status = "failed"
            failed.errors = [{"row": None, "error": str(exc)[:500]}]
            failed.finished_at = datetime.now(UTC)
            db.commit()
        raise
    batch = db.get(IngestionBatch, batch_id)
    assert batch is not None
    batch.status = "completed"
    batch.rows_total, batch.rows_accepted = report.total, report.accepted
    batch.rows_rejected, batch.rows_duplicate = report.rejected, report.duplicates
    batch.errors = report.errors or []
    batch.finished_at = datetime.now(UTC)
    return {"accepted": report.accepted, "rejected": report.rejected, "duplicates": report.duplicates}


def handle_train_model(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    mv = train_from_db(db, capacity=float(payload.get("capacity_per_day", 25)), activate=payload.get("activate", True))
    db.commit()
    result: dict[str, Any] = {"model_version_id": mv.id, "version": mv.version, "algorithm": mv.algorithm}
    days = int(payload.get("backfill_days", 14))
    if mv.is_active and days:
        result["backfill"] = backfill(db, days=days)
    return result


def handle_drift_report(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    r = run_drift_report(db, days=int(payload.get("days", 7)))
    return {"report_id": r.id, "status": r.status}


def handle_backfill(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    return backfill(db, days=int(payload.get("days", 14)))


HANDLERS = {
    "csv_import": handle_csv_import,
    "train_model": handle_train_model,
    "drift_report": handle_drift_report,
    "backfill": handle_backfill,
}
