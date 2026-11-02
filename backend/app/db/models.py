"""SQLAlchemy ORM models.

The schema itself is created by the Alembic migrations in ``migrations/versions``
(the source of truth); ``tests/test_schema.py`` checks that models and the
migrated database agree.

ID strategy: high-volume tables (transactions, scores, labels) use BIGINT identity
keys for compact indexes; users use UUIDs so IDs can't be enumerated.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Double,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

ROLES = ("admin", "lead_analyst", "analyst", "auditor")
RISK_BANDS = ("low", "medium", "high", "critical")
CHANNELS = ("pos", "ecommerce", "atm", "transfer")
ENTRY_MODES = ("chip", "contactless", "swipe", "manual", "online")
CASE_STATUSES = ("open", "in_review", "escalated", "closed")
DISPOSITIONS = ("confirmed_fraud", "legitimate", "inconclusive")
LABEL_SOURCES = ("chargeback", "analyst", "customer_report", "simulator_truth")
JOB_STATUSES = ("queued", "running", "succeeded", "failed")
API_SCOPES = ("transactions:write", "reference:write", "labels:write")

metadata = MetaData(
    naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_name)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)


class Base(DeclarativeBase):
    metadata = metadata


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )


def _bigint_pk() -> Mapped[int]:
    return mapped_column(BigInteger, primary_key=True, autoincrement=True)


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


# ----------------------------------------------------------------------------- identity & access
class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False, server_default="")
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    failed_login_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created()


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"
    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created()


class ApiKey(Base):
    """Credentials for machine clients (payment systems, chargeback feeds)."""

    __tablename__ = "api_keys"
    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    key_prefix: Mapped[str] = mapped_column(String(12), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = _created()
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ----------------------------------------------------------------------------- reference data
class Customer(Base):
    """Pseudonymous customer: no names, birth dates or demographic attributes are stored."""

    __tablename__ = "customers"
    id: Mapped[int] = _bigint_pk()
    external_ref: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    home_country: Mapped[str] = mapped_column(String(2), nullable=False)
    home_city: Mapped[str | None] = mapped_column(String(100))
    home_lat: Mapped[float | None] = mapped_column(Double)
    home_lon: Mapped[float | None] = mapped_column(Double)
    created_at: Mapped[datetime] = _created()


class Card(Base):
    __tablename__ = "cards"
    id: Mapped[int] = _bigint_pk()
    customer_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    last4: Mapped[str] = mapped_column(String(4), nullable=False)
    network: Mapped[str] = mapped_column(String(20), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="active")


class Merchant(Base):
    __tablename__ = "merchants"
    id: Mapped[int] = _bigint_pk()
    external_ref: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    mcc: Mapped[str] = mapped_column(String(4), nullable=False)
    category: Mapped[str] = mapped_column(String(30), nullable=False)
    country: Mapped[str] = mapped_column(String(2), nullable=False)
    city: Mapped[str | None] = mapped_column(String(100))
    lat: Mapped[float | None] = mapped_column(Double)
    lon: Mapped[float | None] = mapped_column(Double)
    is_online: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))


class Device(Base):
    __tablename__ = "devices"
    id: Mapped[int] = _bigint_pk()
    fingerprint_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# ----------------------------------------------------------------------------- transactions & scores
class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (
        Index("ix_transactions_card_time", "card_id", "occurred_at"),
        Index("ix_transactions_merchant_time", "merchant_id", "occurred_at"),
        Index("ix_transactions_occurred_at", "occurred_at"),
        Index("ix_transactions_band_time", "risk_band", "occurred_at"),
    )
    id: Mapped[int] = _bigint_pk()
    external_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    card_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("cards.id"), nullable=False)
    merchant_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("merchants.id"), nullable=False)
    device_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("devices.id"))
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default="USD")
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    entry_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    payee_hash: Mapped[str | None] = mapped_column(String(64))
    ip_country: Mapped[str | None] = mapped_column(String(2))
    lat: Mapped[float | None] = mapped_column(Double)
    lon: Mapped[float | None] = mapped_column(Double)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    source: Mapped[str] = mapped_column(String(10), nullable=False)
    # Denormalised from the current score so search/filter by risk is a simple index scan.
    current_score_id: Mapped[int | None] = mapped_column(BigInteger)
    risk_score: Mapped[float | None] = mapped_column(Double)
    risk_band: Mapped[str | None] = mapped_column(String(10))


class TransactionScore(Base):
    """One scoring of one transaction by one model version (and rule set).

    ``features`` (the exact model inputs) are stored for medium-and-above scores,
    which are the ones a human may review or dispute; low scores can be reproduced
    by recomputing features from the stored history.
    """

    __tablename__ = "transaction_scores"
    __table_args__ = (
        UniqueConstraint(
            "transaction_id", "model_version_id", name="uq_scores_txn_model", postgresql_nulls_not_distinct=True
        ),
        Index("ix_transaction_scores_scored_at", "scored_at"),
    )
    id: Mapped[int] = _bigint_pk()
    transaction_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("transactions.id", ondelete="CASCADE"), nullable=False
    )
    model_version_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("model_versions.id"))
    score: Mapped[float | None] = mapped_column(Double)
    risk_band: Mapped[str] = mapped_column(String(10), nullable=False)
    model_band: Mapped[str | None] = mapped_column(String(10))
    anomaly_score: Mapped[float | None] = mapped_column(Double)
    recommended_action: Mapped[str] = mapped_column(String(10), nullable=False)
    sampled_for_review: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    rules_fired: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    explanation: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    features: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    rule_set_version: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[float | None] = mapped_column(Double)
    scored_at: Mapped[datetime] = _created()


class FraudLabel(Base):
    """What became known about a transaction, and *when* (``reported_at``).

    ``reported_at`` is what makes training leakage-safe: a model trained at time T
    may only use labels reported before T.
    """

    __tablename__ = "fraud_labels"
    __table_args__ = (Index("ix_fraud_labels_reported_at", "reported_at"),)
    id: Mapped[int] = _bigint_pk()
    transaction_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("transactions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    label: Mapped[str] = mapped_column(String(12), nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    reported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    case_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("cases.id", ondelete="SET NULL"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )


# ----------------------------------------------------------------------------- cases
class Case(Base):
    __tablename__ = "cases"
    __table_args__ = (
        # At most one non-closed case per card: later flagged activity joins it.
        Index("uq_cases_open_per_card", "card_id", unique=True, postgresql_where=text("status <> 'closed'")),
        Index("ix_cases_status_priority", "status", "priority"),
    )
    id: Mapped[int] = _bigint_pk()
    card_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("cards.id"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="open")
    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    max_band: Mapped[str] = mapped_column(String(10), nullable=False)
    reason: Mapped[str] = mapped_column(String(20), nullable=False)
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    disposition: Mapped[str | None] = mapped_column(String(20))
    opened_at: Mapped[datetime] = _created()
    sla_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))


class CaseTransaction(Base):
    __tablename__ = "case_transactions"
    __table_args__ = (PrimaryKeyConstraint("case_id", "transaction_id"),)
    case_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("cases.id", ondelete="CASCADE"), nullable=False)
    transaction_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("transactions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    added_at: Mapped[datetime] = _created()


class CaseNote(Base):
    """Append-only (a database trigger rejects UPDATE and DELETE)."""

    __tablename__ = "case_notes"
    id: Mapped[int] = _bigint_pk()
    case_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("cases.id"), nullable=False, index=True)
    author_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _created()


class CaseEvent(Base):
    """Append-only workflow history."""

    __tablename__ = "case_events"
    id: Mapped[int] = _bigint_pk()
    case_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("cases.id"), nullable=False, index=True)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    event: Mapped[str] = mapped_column(String(40), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(20))
    to_status: Mapped[str | None] = mapped_column(String(20))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    ts: Mapped[datetime] = _created()


# ----------------------------------------------------------------------------- rules, models, settings
class RuleSet(Base):
    """Deterministic rules are data, versioned: every score records which version fired."""

    __tablename__ = "rule_sets"
    __table_args__ = (Index("uq_rule_sets_active", "is_active", unique=True, postgresql_where=text("is_active")),)
    version: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    rules: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    comment: Mapped[str] = mapped_column(String(500), nullable=False, server_default="")
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = _created()


class ModelVersion(Base):
    __tablename__ = "model_versions"
    __table_args__ = (
        UniqueConstraint("name", "version", name="uq_model_versions_name_version"),
        Index("uq_model_versions_active", "name", unique=True, postgresql_where=text("is_active")),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    algorithm: Mapped[str] = mapped_column(String(40), nullable=False)
    feature_names: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    thresholds: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    reference: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    artifact_uri: Mapped[str] = mapped_column(String(500), nullable=False)
    artifact_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    data_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    trained_at: Mapped[datetime] = _created()
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AppSetting(Base):
    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(50), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = _created()
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )


class DriftReport(Base):
    __tablename__ = "drift_reports"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    model_version_id: Mapped[int] = mapped_column(Integer, ForeignKey("model_versions.id"), nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    n_transactions: Mapped[int] = mapped_column(Integer, nullable=False)
    feature_psi: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    score_psi: Mapped[float | None] = mapped_column(Double)
    status: Mapped[str] = mapped_column(String(10), nullable=False)
    created_at: Mapped[datetime] = _created()


# ----------------------------------------------------------------------------- operations
class IngestionBatch(Base):
    __tablename__ = "ingestion_batches"
    id: Mapped[uuid.UUID] = _uuid_pk()
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="queued")
    rows_total: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    rows_accepted: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    rows_rejected: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    rows_duplicate: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    errors: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _created()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_status_run_after", "status", "run_after"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    job_type: Mapped[str] = mapped_column(String(50), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="queued")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="3")
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created()


class AuditLog(Base):
    """Append-only. Records admin actions, workflow decisions and reads of case/transaction detail."""

    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_log_ts", "ts"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    entity: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(100))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
