"""Transaction search and detail for analysts."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Query
from sqlalchemy import select

from app.api.deps import DB, Analyst, Staff
from app.core.errors import NotFound
from app.db.models import Card, CaseTransaction, Customer, FraudLabel, Merchant, Transaction, TransactionScore
from app.modules import audit
from app.modules.transactions.views import case_summary, score_view, transaction_rows

router = APIRouter(prefix="/transactions", tags=["transactions"])

Band = Literal["low", "medium", "high", "critical"]


@router.get("")
def search(
    db: DB,
    _: Staff,
    card_id: int | None = None,
    customer_ref: str | None = Query(default=None, max_length=64),
    merchant_ref: str | None = Query(default=None, max_length=64),
    external_id: str | None = Query(default=None, max_length=64),
    band: list[Band] | None = Query(default=None),
    channel: Literal["pos", "ecommerce", "atm", "transfer"] | None = None,
    min_amount: float | None = Query(default=None, ge=0),
    max_amount: float | None = Query(default=None, ge=0),
    date_from: datetime | None = Query(default=None, alias="from"),
    date_to: datetime | None = Query(default=None, alias="to"),
    before_id: int | None = Query(default=None, description="Cursor: return transactions with a smaller id"),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    """Newest first; paginate with ``before_id`` = the last ``id`` of the previous page."""
    q = select(Transaction)
    if card_id is not None:
        q = q.where(Transaction.card_id == card_id)
    if customer_ref:
        q = (
            q.join(Card, Card.id == Transaction.card_id)
            .join(Customer, Customer.id == Card.customer_id)
            .where(Customer.external_ref == customer_ref)
        )
    if merchant_ref:
        q = q.join(Merchant, Merchant.id == Transaction.merchant_id).where(Merchant.external_ref == merchant_ref)
    if external_id:
        q = q.where(Transaction.external_id == external_id)
    if band:
        q = q.where(Transaction.risk_band.in_(band))
    if channel:
        q = q.where(Transaction.channel == channel)
    if min_amount is not None:
        q = q.where(Transaction.amount >= min_amount)
    if max_amount is not None:
        q = q.where(Transaction.amount <= max_amount)
    if date_from:
        q = q.where(Transaction.occurred_at >= date_from)
    if date_to:
        q = q.where(Transaction.occurred_at < date_to)
    if before_id:
        q = q.where(Transaction.id < before_id)
    txns = list(db.scalars(q.order_by(Transaction.id.desc()).limit(limit)))
    items = transaction_rows(db, txns)
    return {"items": items, "next_cursor": items[-1]["id"] if len(items) == limit else None}


@router.get("/{transaction_id}")
def detail(transaction_id: int, db: DB, user: Analyst) -> dict[str, Any]:
    """Full detail including every score version, labels and cases. The read is audit-logged."""
    txn = db.get(Transaction, transaction_id)
    if txn is None:
        raise NotFound("Transaction not found")
    row = transaction_rows(db, [txn])[0]
    scores = list(
        db.scalars(
            select(TransactionScore)
            .where(TransactionScore.transaction_id == txn.id)
            .order_by(TransactionScore.scored_at.desc())
        )
    )
    labels = db.scalars(select(FraudLabel).where(FraudLabel.transaction_id == txn.id).order_by(FraudLabel.reported_at))
    case_ids = list(db.scalars(select(CaseTransaction.case_id).where(CaseTransaction.transaction_id == txn.id)))
    current = next((s for s in scores if s.id == txn.current_score_id), None)
    audit.record(db, user.id, "transaction.view", "transaction", str(txn.id))
    db.commit()
    return {
        **row,
        "score": score_view(db, current),
        "score_history": [score_view(db, s) for s in scores],
        "labels": [
            {"label": lab.label, "source": lab.source, "reported_at": lab.reported_at, "case_id": lab.case_id}
            for lab in labels
        ],
        "cases": list(case_summary(db, case_ids).values()),
    }
