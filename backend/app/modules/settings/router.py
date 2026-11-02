"""Risk settings and the versioned rule set."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select

from app.api.deps import DB, Admin, Staff
from app.core.errors import UnprocessableData
from app.db.models import RuleSet
from app.ml import rules
from app.modules import audit
from app.modules.settings import service

router = APIRouter(tags=["settings"])


class Thresholds(BaseModel):
    medium: float = Field(ge=0, le=1)
    high: float = Field(ge=0, le=1)
    critical: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _ordered(self) -> Thresholds:
        if not self.medium <= self.high <= self.critical:
            raise ValueError("thresholds must satisfy medium <= high <= critical")
        return self


class RiskSettings(BaseModel):
    review_sample_rate: float = Field(ge=0, le=0.05)
    threshold_overrides: Thresholds | None = None
    capacity_per_day: float = Field(gt=0, le=10_000)
    sla_hours: dict[str, float]

    @model_validator(mode="after")
    def _sla(self) -> RiskSettings:
        if set(self.sla_hours) != {"1", "2", "3"} or any(v <= 0 for v in self.sla_hours.values()):
            raise ValueError("sla_hours needs positive values for priorities '1', '2' and '3'")
        return self


@router.get("/settings/risk", response_model=RiskSettings)
def get_risk(db: DB, _: Staff) -> dict[str, Any]:
    return service.get_risk_settings(db)


@router.put("/settings/risk", response_model=RiskSettings)
def put_risk(body: RiskSettings, db: DB, admin: Admin) -> dict[str, Any]:
    value = service.put_risk_settings(db, body.model_dump(), admin.id)
    audit.record(db, admin.id, "settings.risk_update", "app_setting", "risk", **body.model_dump())
    db.commit()
    return value


@router.get("/rules")
def get_rules(db: DB, _: Staff) -> dict[str, Any]:
    active = service.active_rule_set(db)
    db.commit()
    versions = db.scalars(select(RuleSet).order_by(RuleSet.version.desc()).limit(20))
    return {
        "active_version": active.version,
        "rules": [{**r, "description": rules.RULES[r["code"]].description} for r in active.rules],
        "versions": [
            {"version": v.version, "comment": v.comment, "created_at": v.created_at, "is_active": v.is_active}
            for v in versions
        ],
    }


class RulesBody(BaseModel):
    rules: list[dict[str, Any]] = Field(min_length=1)
    comment: str = Field(min_length=3, max_length=500)


@router.put("/rules")
def put_rules(body: RulesBody, db: DB, admin: Admin) -> dict[str, Any]:
    try:
        rs = service.new_rule_set(db, body.rules, body.comment, admin.id)
    except ValueError as exc:
        raise UnprocessableData(str(exc)) from exc
    audit.record(db, admin.id, "rules.update", "rule_set", str(rs.version), comment=body.comment)
    db.commit()
    return {"active_version": rs.version, "rules": rs.rules}
