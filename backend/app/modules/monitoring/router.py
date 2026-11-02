from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from app.api.deps import DB, Admin, Lead
from app.jobs import queue
from app.modules.monitoring import service

router = APIRouter(prefix="/monitoring", tags=["monitoring"])


@router.get("/drift")
def drift_reports(db: DB, _: Lead) -> list[dict[str, Any]]:
    return [
        {
            "id": r.id,
            "model_version_id": r.model_version_id,
            "window_start": r.window_start,
            "window_end": r.window_end,
            "n_transactions": r.n_transactions,
            "feature_psi": r.feature_psi,
            "score_psi": r.score_psi,
            "status": r.status,
            "created_at": r.created_at,
        }
        for r in service.recent_reports(db)
    ]


@router.post("/drift", status_code=202)
def request_drift_report(db: DB, _: Admin, days: int = Query(default=7, ge=1, le=60)) -> dict[str, Any]:
    job = queue.enqueue(db, "drift_report", {"days": days}, dedupe=True)
    assert job is not None
    db.commit()
    return {"job_id": job.id}


@router.get("/performance")
def performance(db: DB, _: Lead, weeks: int = Query(default=16, ge=1, le=52)) -> dict[str, Any]:
    return service.performance(db, weeks)


@router.get("/latency")
def latency(db: DB, _: Lead) -> dict[str, Any]:
    return service.latency(db)
