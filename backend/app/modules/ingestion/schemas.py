from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from app.modules.ingestion.validation import looks_like_pan

Channel = Literal["pos", "ecommerce", "atm", "transfer"]
EntryMode = Literal["chip", "contactless", "swipe", "manual", "online"]
MAX_CLOCK_SKEW = timedelta(minutes=5)


class TransactionIn(BaseModel):
    """One transaction as sent by a payment system. Card numbers must be tokens."""

    model_config = ConfigDict(extra="forbid")

    external_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._:\-]+$")
    card_token: str = Field(min_length=8, max_length=64)
    merchant_ref: str = Field(min_length=1, max_length=64)
    amount: Decimal = Field(gt=0, le=1_000_000, max_digits=14, decimal_places=2)
    currency: Literal["USD"] = "USD"
    channel: Channel
    entry_mode: EntryMode
    occurred_at: AwareDatetime
    device_fingerprint: str | None = Field(default=None, min_length=1, max_length=256)
    payee_ref: str | None = Field(default=None, min_length=1, max_length=64)
    ip_country: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)

    @field_validator("card_token")
    @classmethod
    def _not_a_pan(cls, v: str) -> str:
        if looks_like_pan(v):
            raise ValueError("card_token looks like a raw card number; send the tokenised value instead")
        return v

    @field_validator("occurred_at")
    @classmethod
    def _not_future(cls, v: datetime) -> datetime:
        if v > datetime.now(UTC) + MAX_CLOCK_SKEW:
            raise ValueError("occurred_at is in the future")
        return v.astimezone(UTC)

    @model_validator(mode="after")
    def _consistency(self) -> TransactionIn:
        if (self.lat is None) != (self.lon is None):
            raise ValueError("lat and lon must be given together")
        if self.channel == "transfer" and not self.payee_ref:
            raise ValueError("payee_ref is required for transfers")
        if self.channel != "transfer" and self.payee_ref:
            raise ValueError("payee_ref is only allowed for transfers")
        if self.channel in ("pos", "atm") and self.entry_mode == "online":
            raise ValueError("in-person transactions cannot use entry_mode 'online'")
        return self


class TransactionBatch(BaseModel):
    transactions: list[TransactionIn] = Field(min_length=1, max_length=1000)


class ModelRef(BaseModel):
    name: str
    version: int
    algorithm: str


class ScoreOut(BaseModel):
    transaction_id: int
    external_id: str
    status: Literal["created", "duplicate"]
    risk_score: float | None = Field(
        description="Calibrated fraud probability from the model (null if no active model)"
    )
    risk_band: str
    model_band: str | None
    recommended_action: Literal["allow", "review"] = Field(
        description="Advisory only. The engine never declines payments or blocks accounts."
    )
    sampled_for_review: bool
    anomaly_score: float | None
    model: ModelRef | None
    reasons: list[dict[str, Any]]
    case_id: int | None
    latency_ms: float | None


class BatchItemError(BaseModel):
    index: int
    external_id: str | None
    error: str


class BatchResult(BaseModel):
    results: list[ScoreOut]
    errors: list[BatchItemError]
