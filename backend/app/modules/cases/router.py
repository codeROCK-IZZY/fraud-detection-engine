"""Case queue and investigation workflow."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.deps import DB, Analyst, Lead, Staff
from app.core.errors import NotFound
from app.db.models import (
    Card,
    Case,
    CaseEvent,
    CaseNote,
    CaseTransaction,
    Customer,
    Transaction,
    TransactionScore,
    User,
)
from app.modules import audit
from app.modules.cases import service
from app.modules.transactions.views import score_view, transaction_rows

router = APIRouter(prefix="/cases", tags=["cases"])


def _case(db: DB, case_id: int, lock: bool = False) -> Case:
    q = select(Case).where(Case.id == case_id)
    case = db.scalar(q.with_for_update() if lock else q)
    if case is None:
        raise NotFound("Case not found")
    return case


def _summary(
    case: Case, users: dict[uuid.UUID, User], stats: dict[int, tuple[int, float]], cards: dict[int, Card]
) -> dict[str, Any]:
    n, total = stats.get(case.id, (0, 0.0))
    assignee = users.get(case.assignee_id) if case.assignee_id else None
    return {
        "id": case.id,
        "status": case.status,
        "priority": case.priority,
        "max_band": case.max_band,
        "reason": case.reason,
        "card": {"id": case.card_id, "last4": cards[case.card_id].last4 if case.card_id in cards else None},
        "assignee": {"id": assignee.id, "email": assignee.email, "full_name": assignee.full_name} if assignee else None,
        "disposition": case.disposition,
        "opened_at": case.opened_at,
        "sla_due_at": case.sla_due_at,
        "overdue": case.status != "closed" and case.sla_due_at < datetime.now(UTC),
        "closed_at": case.closed_at,
        "transactions": n,
        "total_amount": total,
    }


@router.get("")
def list_cases(
    db: DB,
    user: Staff,
    status: list[Literal["open", "in_review", "escalated", "closed"]] | None = Query(default=None),
    assignee: str | None = Query(default=None, description="'me', 'unassigned' or a user id"),
    priority: int | None = Query(default=None, ge=1, le=3),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    q = select(Case)
    if status:
        q = q.where(Case.status.in_(status))
    if assignee == "me":
        q = q.where(Case.assignee_id == user.id)
    elif assignee == "unassigned":
        q = q.where(Case.assignee_id.is_(None))
    elif assignee:
        q = q.where(Case.assignee_id == uuid.UUID(assignee))
    if priority:
        q = q.where(Case.priority == priority)
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    cases = list(db.scalars(q.order_by(Case.priority, Case.sla_due_at, Case.id).limit(limit).offset(offset)))
    ids = [c.id for c in cases]
    stats = {
        r[0]: (int(r[1]), float(r[2] or 0))
        for r in db.execute(
            select(CaseTransaction.case_id, func.count(), func.sum(Transaction.amount))
            .join(Transaction, Transaction.id == CaseTransaction.transaction_id)
            .where(CaseTransaction.case_id.in_(ids))
            .group_by(CaseTransaction.case_id)
        )
    }
    users = {
        u.id: u for u in db.scalars(select(User).where(User.id.in_({c.assignee_id for c in cases if c.assignee_id})))
    }
    cards = {c.id: c for c in db.scalars(select(Card).where(Card.id.in_({c.card_id for c in cases})))}
    return {"total": total, "items": [_summary(c, users, stats, cards) for c in cases]}


@router.get("/{case_id}")
def get_case(case_id: int, db: DB, user: Analyst) -> dict[str, Any]:
    """Everything an analyst needs: flagged transactions with explanations, the card's
    recent history, notes and the workflow timeline. The read is audit-logged."""
    case = _case(db, case_id)
    card = db.get(Card, case.card_id)
    assert card is not None
    cust = db.get(Customer, card.customer_id)
    assert cust is not None
    txns = list(
        db.scalars(
            select(Transaction)
            .join(CaseTransaction, CaseTransaction.transaction_id == Transaction.id)
            .where(CaseTransaction.case_id == case.id)
            .order_by(Transaction.occurred_at)
        )
    )
    rows = transaction_rows(db, txns)
    scores = {
        s.id: s
        for s in db.scalars(
            select(TransactionScore).where(
                TransactionScore.id.in_([t.current_score_id for t in txns if t.current_score_id])
            )
        )
    }
    for row, t in zip(rows, txns, strict=True):
        row["score"] = score_view(db, scores.get(t.current_score_id)) if t.current_score_id else None
    first = min((t.occurred_at for t in txns), default=case.opened_at)
    last = max((t.occurred_at for t in txns), default=case.opened_at)
    history = list(
        db.scalars(
            select(Transaction)
            .where(
                Transaction.card_id == card.id,
                Transaction.occurred_at >= first - timedelta(days=7),
                Transaction.occurred_at <= last + timedelta(days=1),
            )
            .order_by(Transaction.occurred_at.desc())
            .limit(300)
        )
    )
    users = {u.id: u for u in db.scalars(select(User))}
    notes = db.scalars(select(CaseNote).where(CaseNote.case_id == case.id).order_by(CaseNote.created_at))
    events = db.scalars(select(CaseEvent).where(CaseEvent.case_id == case.id).order_by(CaseEvent.ts))

    def who(uid: uuid.UUID | None) -> str | None:
        return users[uid].email if uid and uid in users else None

    stats = {case.id: (len(txns), float(sum(t.amount for t in txns)))}
    audit.record(db, user.id, "case.view", "case", str(case.id))
    db.commit()
    return {
        **_summary(case, users, stats, {card.id: card}),
        "customer": {
            "external_ref": cust.external_ref,
            "home_city": cust.home_city,
            "home_country": cust.home_country,
            "home_lat": cust.home_lat,
            "home_lon": cust.home_lon,
        },
        "card_detail": {"id": card.id, "last4": card.last4, "network": card.network, "issued_at": card.issued_at},
        "flagged": rows,
        "history": transaction_rows(db, history),
        "notes": [{"id": n.id, "author": who(n.author_id), "body": n.body, "created_at": n.created_at} for n in notes],
        "events": [
            {
                "event": e.event,
                "actor": who(e.actor_id),
                "from_status": e.from_status,
                "to_status": e.to_status,
                "details": e.details,
                "ts": e.ts,
            }
            for e in events
        ],
        "closed_by": who(case.closed_by),
    }


@router.post("/{case_id}/claim")
def claim(case_id: int, db: DB, user: Analyst) -> dict[str, Any]:
    case = _case(db, case_id, lock=True)
    service.claim(db, case, user)
    db.commit()
    return {"id": case.id, "status": case.status}


class AssignBody(BaseModel):
    assignee_id: uuid.UUID


@router.post("/{case_id}/assign")
def assign(case_id: int, body: AssignBody, db: DB, lead: Lead) -> dict[str, Any]:
    case = _case(db, case_id, lock=True)
    assignee = db.get(User, body.assignee_id)
    if assignee is None:
        raise NotFound("User not found")
    service.assign(db, case, assignee, lead)
    db.commit()
    return {"id": case.id, "status": case.status}


class StatusBody(BaseModel):
    to: Literal["escalated", "in_review", "open"]


@router.post("/{case_id}/status")
def change_status(case_id: int, body: StatusBody, db: DB, user: Analyst) -> dict[str, Any]:
    case = _case(db, case_id, lock=True)
    service.transition(db, case, body.to, user)
    db.commit()
    return {"id": case.id, "status": case.status}


class NoteBody(BaseModel):
    body: str = Field(min_length=1, max_length=5000)


@router.post("/{case_id}/notes", status_code=201)
def add_note(case_id: int, body: NoteBody, db: DB, user: Analyst) -> dict[str, Any]:
    case = _case(db, case_id, lock=True)
    note = service.add_note(db, case, user, body.body)
    db.commit()
    return {"id": note.id, "created_at": note.created_at}


class DispositionBody(BaseModel):
    disposition: Literal["confirmed_fraud", "legitimate", "inconclusive"]
    note: str = Field(min_length=10, max_length=5000, description="Reasoning for the decision (kept in the case)")
    transaction_labels: dict[int, Literal["fraud", "legitimate"]] | None = None


@router.post("/{case_id}/disposition")
def dispose(case_id: int, body: DispositionBody, db: DB, user: Analyst) -> dict[str, Any]:
    """Close the case. Labels are written for the case's transactions and used by the next training run."""
    case = _case(db, case_id, lock=True)
    written = service.dispose(
        db, case, user, body.disposition, body.note, dict(body.transaction_labels) if body.transaction_labels else None
    )
    audit.record(db, user.id, "case.close", "case", str(case.id), disposition=body.disposition, labels=written)
    db.commit()
    return {"id": case.id, "status": case.status, "disposition": case.disposition, "labels_written": written}
