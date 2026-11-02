"""Admin-configurable risk settings and the active rule set."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db.models import AppSetting, RuleSet
from app.ml import rules

RISK_KEY = "risk"
DEFAULT_RISK: dict[str, Any] = {
    # Share of low/medium transactions sent for review at random. These reviews give
    # unbiased labels for transactions the model would never flag (see DESIGN.md §6.8).
    "review_sample_rate": 0.001,
    # Optional manual band thresholds; null = use the active model's capacity-based thresholds.
    "threshold_overrides": None,
    "capacity_per_day": 25,
    # SLA hours by case priority (1 = critical, 2 = high, 3 = random sample).
    "sla_hours": {"1": 4, "2": 24, "3": 72},
}


def get_risk_settings(db: Session) -> dict[str, Any]:
    row = db.get(AppSetting, RISK_KEY)
    return {**DEFAULT_RISK, **(row.value if row else {})}


def put_risk_settings(db: Session, value: dict[str, Any], user_id: uuid.UUID | None) -> dict[str, Any]:
    row = db.get(AppSetting, RISK_KEY)
    merged = {**DEFAULT_RISK, **value}
    if row is None:
        db.add(AppSetting(key=RISK_KEY, value=merged, updated_by=user_id))
    else:
        row.value = merged
        row.updated_by = user_id
    db.flush()
    return merged


def active_rule_set(db: Session) -> RuleSet:
    rs = db.scalar(select(RuleSet).where(RuleSet.is_active))
    if rs is None:  # first use: install the defaults as version 1
        rs = RuleSet(version=1, rules=rules.default_rule_config(), is_active=True, comment="Default rules")
        db.add(rs)
        db.flush()
    return rs


def new_rule_set(db: Session, config: list[dict[str, Any]], comment: str, user_id: uuid.UUID | None) -> RuleSet:
    """Validate and activate a new rule-set version (older versions are kept for audit)."""
    normalised = rules.validate_rule_config(config)
    latest = db.scalar(select(RuleSet.version).order_by(RuleSet.version.desc()).limit(1)) or 0
    db.execute(update(RuleSet).where(RuleSet.is_active).values(is_active=False))
    db.flush()
    rs = RuleSet(version=latest + 1, rules=normalised, is_active=True, comment=comment, created_by=user_id)
    db.add(rs)
    db.flush()
    return rs
