"""Audit log (admins and auditors) and system information."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Query
from sqlalchemy import select

from app.api.deps import DB, Auditing, Staff
from app.core.config import get_settings
from app.db.models import AuditLog, User
from app.ml import registry
from app.modules.settings.service import active_rule_set

router = APIRouter(tags=["system"])


@router.get("/audit-log")
def audit_log(
    db: DB,
    _: Auditing,
    action: str | None = Query(default=None, max_length=100),
    user_id: uuid.UUID | None = None,
    entity: str | None = Query(default=None, max_length=50),
    before_id: int | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    q = select(AuditLog, User.email).outerjoin(User, User.id == AuditLog.user_id)
    if action:
        q = q.where(AuditLog.action.startswith(action))
    if user_id:
        q = q.where(AuditLog.user_id == user_id)
    if entity:
        q = q.where(AuditLog.entity == entity)
    if before_id:
        q = q.where(AuditLog.id < before_id)
    rows = db.execute(q.order_by(AuditLog.id.desc()).limit(limit)).all()
    items = [
        {
            "id": a.id,
            "ts": a.ts,
            "user": email,
            "action": a.action,
            "entity": a.entity,
            "entity_id": a.entity_id,
            "details": a.details,
        }
        for a, email in rows
    ]
    return {"items": items, "next_cursor": items[-1]["id"] if len(items) == limit else None}


@router.get("/system/info")
def info(db: DB, _: Staff) -> dict[str, Any]:
    mv = registry.active_version(db)
    rs = active_rule_set(db)
    db.commit()
    return {
        "streaming_enabled": get_settings().streaming_enabled,
        "active_model": {
            "id": mv.id,
            "name": mv.name,
            "version": mv.version,
            "algorithm": mv.algorithm,
            "trained_at": mv.trained_at,
        }
        if mv
        else None,
        "rule_set_version": rs.version,
        "decision_policy": "Advisory only: the engine returns allow/review and opens cases. "
        "It never declines payments or blocks accounts.",
    }


@router.get("/users/directory")
def directory(db: DB, _: Staff) -> list[dict[str, Any]]:
    """Active analysts and leads (for the case assignment picker)."""
    users = db.scalars(
        select(User).where(User.is_active, User.role.in_(["analyst", "lead_analyst", "admin"])).order_by(User.email)
    )
    return [{"id": u.id, "email": u.email, "full_name": u.full_name, "role": u.role} for u in users]
