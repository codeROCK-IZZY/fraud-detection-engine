"""Case management: one open case per card, a validated workflow, and dispositions
that become training labels.

Workflow::

    open ──claim/assign──▶ in_review ──escalate──▶ escalated
      ▲                     │   ▲                     │
      └──────release────────┘   └──────return─────────┘
                            │                         │
                            └──disposition──▶ closed ◀┘ (escalated: lead only)

Permissions: the assignee works their case; leads and admins may act on any case.
Every change writes an append-only ``case_events`` row.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.api.deps import LEADS
from app.core.errors import Conflict, Forbidden, UnprocessableData
from app.db.models import Case, CaseEvent, CaseNote, CaseTransaction, FraudLabel, User
from app.ml.rules import BAND_ORDER
from app.modules.ingestion.validation import contains_pan
from app.modules.settings.service import get_risk_settings

PRIORITY = {"critical": 1, "high": 2, "sample": 3}
LABEL_FOR = {"confirmed_fraud": "fraud", "legitimate": "legitimate"}


def _event(
    db: Session,
    case: Case,
    event: str,
    actor: uuid.UUID | None,
    frm: str | None = None,
    to: str | None = None,
    **details: Any,
) -> None:
    db.add(CaseEvent(case_id=case.id, actor_id=actor, event=event, from_status=frm, to_status=to, details=details))


def attach(db: Session, card_id: int, transaction_id: int, band: str, sampled: bool) -> Case:
    """Open a case for the card, or add the transaction to its open case.

    Concurrency: the partial unique index ``uq_cases_open_per_card`` guarantees one
    non-closed case per card; ``ON CONFLICT DO NOTHING`` + re-select makes two
    scorers flagging the same card at once converge on the same case.
    """
    reason = band if band in ("high", "critical") else "sample"
    priority = PRIORITY[reason]
    now = datetime.now(UTC)
    sla = now + timedelta(hours=float(get_risk_settings(db)["sla_hours"][str(priority)]))
    new_id = db.execute(
        pg_insert(Case)
        .values(card_id=card_id, status="open", priority=priority, max_band=band, reason=reason, sla_due_at=sla)
        .on_conflict_do_nothing(index_elements=[Case.card_id], index_where=text("status <> 'closed'"))
        .returning(Case.id)
    ).scalar()
    if new_id is not None:
        case = db.get(Case, new_id)
        assert case is not None
        _event(db, case, "opened", None, None, "open", reason=reason, transaction_id=transaction_id)
    else:
        case = db.scalar(select(Case).where(Case.card_id == card_id, Case.status != "closed").with_for_update())
        if case is None:  # closed between our insert and select; retry once
            return attach(db, card_id, transaction_id, band, sampled)
        if priority < case.priority:
            case.priority = priority
            case.sla_due_at = min(case.sla_due_at, sla)
        if BAND_ORDER[band] > BAND_ORDER[case.max_band]:
            case.max_band = band
    added = db.execute(
        pg_insert(CaseTransaction)
        .values(case_id=case.id, transaction_id=transaction_id)
        .on_conflict_do_nothing()
        .returning(CaseTransaction.transaction_id)
    ).scalar()
    if added is not None and new_id is None:
        _event(db, case, "transaction_added", None, transaction_id=transaction_id, band=band)
    db.flush()
    return case


def _is_lead(user: User) -> bool:
    return user.role in LEADS


def _require_worker(case: Case, user: User) -> None:
    if case.status == "closed":
        raise Conflict("Case is closed")
    if not _is_lead(user) and case.assignee_id != user.id:
        raise Forbidden("Only the assigned analyst or a lead can change this case")


def claim(db: Session, case: Case, user: User) -> None:
    if case.status != "open":
        raise Conflict(f"Only open cases can be claimed (status is {case.status})")
    if case.assignee_id not in (None, user.id) and not _is_lead(user):
        raise Forbidden("Case is assigned to someone else")
    prev = case.status
    case.assignee_id = user.id
    case.status = "in_review"
    _event(db, case, "claimed", user.id, prev, case.status)


def assign(db: Session, case: Case, assignee: User, lead: User) -> None:
    if case.status == "closed":
        raise Conflict("Case is closed")
    if assignee.role not in ("analyst", "lead_analyst", "admin") or not assignee.is_active:
        raise UnprocessableData("Cases can only be assigned to active analysts or leads")
    prev = case.status
    case.assignee_id = assignee.id
    if case.status == "open":
        case.status = "in_review"
    _event(db, case, "assigned", lead.id, prev, case.status, assignee_id=str(assignee.id))


def transition(db: Session, case: Case, to: str, user: User) -> None:
    """Non-closing transitions: escalate, return (from escalation) and release (back to queue)."""
    _require_worker(case, user)
    allowed = {("in_review", "escalated"), ("escalated", "in_review"), ("in_review", "open")}
    if (case.status, to) not in allowed:
        raise UnprocessableData(f"Cannot move a case from {case.status} to {to}")
    if case.status == "escalated" and not _is_lead(user):
        raise Forbidden("Only a lead can return an escalated case")
    prev = case.status
    case.status = to
    if to == "open":
        case.assignee_id = None
    _event(db, case, {"escalated": "escalated", "in_review": "returned", "open": "released"}[to], user.id, prev, to)


def add_note(db: Session, case: Case, user: User, body: str) -> CaseNote:
    _require_worker(case, user)
    if contains_pan(body):
        raise UnprocessableData(
            "The note appears to contain a full card number. Refer to cards by their last 4 digits only."
        )
    note = CaseNote(case_id=case.id, author_id=user.id, body=body)
    db.add(note)
    _event(db, case, "note_added", user.id)
    db.flush()
    return note


def dispose(
    db: Session,
    case: Case,
    user: User,
    disposition: str,
    note: str,
    transaction_labels: Mapping[int, str] | None = None,
) -> int:
    """Close the case with a disposition; returns the number of labels written.

    ``confirmed_fraud`` / ``legitimate`` label every transaction in the case unless
    ``transaction_labels`` overrides individual ones; ``inconclusive`` writes none.
    """
    if case.status == "open":
        raise Conflict("Claim the case before closing it")
    _require_worker(case, user)
    if case.status == "escalated" and not _is_lead(user):
        raise Forbidden("Only a lead can close an escalated case")
    if contains_pan(note):
        raise UnprocessableData("The note appears to contain a full card number.")
    txn_ids = list(db.scalars(select(CaseTransaction.transaction_id).where(CaseTransaction.case_id == case.id)))
    overrides = transaction_labels or {}
    unknown = set(overrides) - set(txn_ids)
    if unknown:
        raise UnprocessableData(f"Transactions {sorted(unknown)} are not part of this case")
    now = datetime.now(UTC)
    written = 0
    for tid in txn_ids:
        label = overrides.get(tid, LABEL_FOR.get(disposition))
        if label:
            db.add(
                FraudLabel(
                    transaction_id=tid,
                    label=label,
                    source="analyst",
                    reported_at=now,
                    case_id=case.id,
                    created_by=user.id,
                )
            )
            written += 1
    db.add(CaseNote(case_id=case.id, author_id=user.id, body=note))
    prev = case.status
    case.status = "closed"
    case.disposition = disposition
    case.closed_at = now
    case.closed_by = user.id
    _event(db, case, "closed", user.id, prev, "closed", disposition=disposition, labels_written=written)
    db.flush()
    return written
