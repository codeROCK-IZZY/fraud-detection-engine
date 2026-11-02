"""Chargeback / dispute feed: what the card network later reports about transactions.

Each label keeps ``reported_at``, the moment it became known. Training only uses
labels reported before its cut-off, which is what makes it leakage-safe.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter
from pydantic import AwareDatetime, BaseModel, Field, field_validator
from sqlalchemy import select

from app.api.deps import DB, LabelsKey
from app.db.models import FraudLabel, Transaction

router = APIRouter(prefix="/labels", tags=["labels"])


class ChargebackIn(BaseModel):
    external_id: str = Field(min_length=1, max_length=64)
    label: Literal["fraud", "legitimate"] = "fraud"
    reported_at: AwareDatetime | None = None

    @field_validator("reported_at")
    @classmethod
    def _not_future(cls, v: datetime | None) -> datetime | None:
        if v is not None and v > datetime.now(UTC) + timedelta(minutes=5):
            raise ValueError("reported_at is in the future")
        return v


class ChargebacksBody(BaseModel):
    chargebacks: list[ChargebackIn] = Field(min_length=1, max_length=5000)


@router.post("/chargebacks")
def post_chargebacks(body: ChargebacksBody, db: DB, _: LabelsKey) -> dict[str, object]:
    ids = dict(
        db.execute(
            select(Transaction.external_id, Transaction.id).where(
                Transaction.external_id.in_({c.external_id for c in body.chargebacks})
            )
        ).all()
    )
    now = datetime.now(UTC)
    unknown = []
    for c in body.chargebacks:
        if c.external_id not in ids:
            unknown.append(c.external_id)
            continue
        db.add(
            FraudLabel(
                transaction_id=ids[c.external_id], label=c.label, source="chargeback", reported_at=c.reported_at or now
            )
        )
    db.commit()
    return {"accepted": len(body.chargebacks) - len(unknown), "unknown_external_ids": unknown[:100]}
