"""Shared JSON views of transactions (used by search, cases and card history)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Card, Case, CaseTransaction, Merchant, ModelVersion, Transaction, TransactionScore


def transaction_rows(db: Session, txns: list[Transaction]) -> list[dict[str, Any]]:
    if not txns:
        return []
    merchants = {m.id: m for m in db.scalars(select(Merchant).where(Merchant.id.in_({t.merchant_id for t in txns})))}
    cards = {c.id: c for c in db.scalars(select(Card).where(Card.id.in_({t.card_id for t in txns})))}
    case_of = dict(
        db.execute(
            select(CaseTransaction.transaction_id, CaseTransaction.case_id).where(
                CaseTransaction.transaction_id.in_([t.id for t in txns])
            )
        ).all()
    )
    out = []
    for t in txns:
        m = merchants[t.merchant_id]
        out.append(
            {
                "id": t.id,
                "external_id": t.external_id,
                "occurred_at": t.occurred_at,
                "amount": float(t.amount),
                "currency": t.currency,
                "channel": t.channel,
                "entry_mode": t.entry_mode,
                "ip_country": t.ip_country,
                "lat": t.lat,
                "lon": t.lon,
                "has_device": t.device_id is not None,
                "is_transfer": t.payee_hash is not None,
                "merchant": {
                    "id": m.id,
                    "external_ref": m.external_ref,
                    "name": m.name,
                    "category": m.category,
                    "country": m.country,
                    "city": m.city,
                    "is_online": m.is_online,
                },
                "card": {"id": t.card_id, "last4": cards[t.card_id].last4},
                "risk_score": t.risk_score,
                "risk_band": t.risk_band,
                "case_id": case_of.get(t.id),
                "source": t.source,
            }
        )
    return out


def score_view(db: Session, sc: TransactionScore | None) -> dict[str, Any] | None:
    if sc is None:
        return None
    mv = db.get(ModelVersion, sc.model_version_id) if sc.model_version_id else None
    return {
        "id": sc.id,
        "score": sc.score,
        "risk_band": sc.risk_band,
        "model_band": sc.model_band,
        "anomaly_score": sc.anomaly_score,
        "recommended_action": sc.recommended_action,
        "sampled_for_review": sc.sampled_for_review,
        "reasons": sc.explanation,
        "rules_fired": sc.rules_fired,
        "features": sc.features,
        "rule_set_version": sc.rule_set_version,
        "model": {"id": mv.id, "name": mv.name, "version": mv.version, "algorithm": mv.algorithm} if mv else None,
        "scored_at": sc.scored_at,
        "latency_ms": sc.latency_ms,
    }


def case_summary(db: Session, case_ids: list[int]) -> dict[int, dict[str, Any]]:
    return {
        c.id: {"id": c.id, "status": c.status, "priority": c.priority, "disposition": c.disposition}
        for c in db.scalars(select(Case).where(Case.id.in_(case_ids)))
    }
