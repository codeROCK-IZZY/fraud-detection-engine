"""Model registry: list, inspect, train (background job), activate, roll back."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import DB, Admin, Staff
from app.core.errors import NotFound, UnprocessableData
from app.db.models import Job, ModelVersion
from app.jobs import queue
from app.ml import registry
from app.modules import audit

router = APIRouter(tags=["models"])


def _view(mv: ModelVersion, full: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": mv.id,
        "name": mv.name,
        "version": mv.version,
        "algorithm": mv.algorithm,
        "is_active": mv.is_active,
        "trained_at": mv.trained_at,
        "activated_at": mv.activated_at,
        "thresholds": mv.thresholds,
        "calibration_pr_auc": (mv.metrics.get("calibration_window") or {}).get("pr_auc"),
        "data": mv.metrics.get("data"),
    }
    if full:
        out.update(
            metrics=mv.metrics,
            feature_names=mv.feature_names,
            data_snapshot=mv.data_snapshot,
            monitored_features=list(mv.reference.get("features", {})),
        )
    return out


@router.get("/models")
def list_models(db: DB, _: Staff) -> list[dict[str, Any]]:
    return [_view(m) for m in db.scalars(select(ModelVersion).order_by(ModelVersion.version.desc()))]


@router.get("/models/{model_id}")
def get_model(model_id: int, db: DB, _: Staff) -> dict[str, Any]:
    mv = db.get(ModelVersion, model_id)
    if mv is None:
        raise NotFound("Model not found")
    return _view(mv, full=True)


class TrainBody(BaseModel):
    capacity_per_day: float = Field(default=25, gt=0, le=10_000)
    activate: bool = True
    backfill_days: int = Field(default=14, ge=0, le=90)


@router.post("/models/train", status_code=202)
def train(body: TrainBody, db: DB, admin: Admin) -> dict[str, Any]:
    job = queue.enqueue(db, "train_model", body.model_dump())
    assert job is not None
    audit.record(db, admin.id, "model.train_requested", "job", str(job.id), **body.model_dump())
    db.commit()
    return {"job_id": job.id, "status": job.status}


@router.post("/models/{model_id}/activate")
def activate(model_id: int, db: DB, admin: Admin) -> dict[str, Any]:
    mv = db.get(ModelVersion, model_id)
    if mv is None:
        raise NotFound("Model not found")
    previous = registry.active_version(db)
    registry.load_version(mv)  # verify the artifact loads (and passes its integrity check) before switching
    registry.activate_version(db, mv)
    audit.record(
        db,
        admin.id,
        "model.activate",
        "model_version",
        str(mv.id),
        version=mv.version,
        previous=previous.version if previous else None,
    )
    db.commit()
    return _view(mv)


@router.post("/models/rollback")
def rollback(db: DB, admin: Admin) -> dict[str, Any]:
    """Re-activate the most recently trained version older than the active one."""
    current = registry.active_version(db)
    if current is None:
        raise UnprocessableData("No active model to roll back from")
    prev = db.scalar(
        select(ModelVersion)
        .where(ModelVersion.name == current.name, ModelVersion.version < current.version)
        .order_by(ModelVersion.version.desc())
        .limit(1)
    )
    if prev is None:
        raise UnprocessableData("There is no earlier version to roll back to")
    return activate(prev.id, db, admin)


@router.get("/jobs/{job_id}")
def get_job(job_id: int, db: DB, _: Staff) -> dict[str, Any]:
    job = db.get(Job, job_id)
    if job is None:
        raise NotFound("Job not found")
    return {
        "id": job.id,
        "type": job.job_type,
        "status": job.status,
        "attempts": job.attempts,
        "result": job.result,
        "error": (job.last_error or "").split("\n")[0] or None,
        "created_at": job.created_at,
        "finished_at": job.finished_at,
    }
