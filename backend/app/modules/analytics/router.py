"""Dashboards, trends and CSV reports.

Time windows are measured back from the latest transaction in the database, so
the figures stay meaningful for a demo dataset as well as live traffic.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import text

from app.api.deps import DB, Auditing, Lead, Staff
from app.modules import audit

router = APIRouter(tags=["analytics"])


def _as_of(db: DB) -> datetime | None:
    return db.execute(text("SELECT max(occurred_at) FROM transactions")).scalar()


@router.get("/analytics/summary")
def summary(db: DB, _: Staff) -> dict[str, Any]:
    as_of = _as_of(db)
    if as_of is None:
        return {"as_of": None}
    p = {"as_of": as_of}
    last24 = db.execute(
        text(
            """
            SELECT count(*), coalesce(sum(amount), 0)::float8,
                   count(*) FILTER (WHERE risk_band IN ('high', 'critical')),
                   count(*) FILTER (WHERE risk_band = 'critical'),
                   count(*) FILTER (WHERE risk_band = 'medium')
            FROM transactions WHERE occurred_at > CAST(:as_of AS timestamptz) - interval '24 hours'
            """
        ),
        p,
    ).one()
    cases = db.execute(
        text(
            """
            SELECT count(*) FILTER (WHERE status <> 'closed'),
                   count(*) FILTER (WHERE status = 'open' AND assignee_id IS NULL),
                   count(*) FILTER (WHERE status <> 'closed' AND sla_due_at < now()),
                   count(*) FILTER (WHERE status = 'escalated'),
                   count(*) FILTER (WHERE status <> 'closed' AND priority = 1)
            FROM cases
            """
        )
    ).one()
    outcomes: dict[str, int] = dict(
        db.execute(
            text(
                "SELECT disposition, count(*) FROM cases WHERE status = 'closed' "
                "AND closed_at > now() - interval '30 days' GROUP BY disposition"
            )
        ).all()
    )
    reported = db.execute(
        text(
            """
            SELECT count(*), coalesce(sum(t.amount), 0)::float8
            FROM transactions t
            WHERE EXISTS (SELECT 1 FROM fraud_labels l WHERE l.transaction_id = t.id AND l.label = 'fraud'
                          AND l.reported_at > CAST(:as_of AS timestamptz) - interval '30 days')
            """
        ),
        p,
    ).one()
    decided = outcomes.get("confirmed_fraud", 0) + outcomes.get("legitimate", 0)
    return {
        "as_of": as_of,
        "last_24h": {
            "transactions": last24[0],
            "amount": last24[1],
            "flagged": last24[2],
            "critical": last24[3],
            "medium": last24[4],
        },
        "cases": {
            "open": cases[0],
            "unassigned": cases[1],
            "overdue": cases[2],
            "escalated": cases[3],
            "critical_open": cases[4],
        },
        "outcomes_30d": outcomes,
        "analyst_confirmed_rate_30d": outcomes.get("confirmed_fraud", 0) / decided if decided else None,
        "fraud_reported_30d": {"transactions": reported[0], "amount": reported[1]},
    }


@router.get("/analytics/trends")
def trends(db: DB, _: Staff, days: int = Query(default=30, ge=1, le=180)) -> dict[str, Any]:
    as_of = _as_of(db)
    if as_of is None:
        return {"daily": [], "by_category": [], "by_channel": []}
    p = {"as_of": as_of, "days": days}
    window = "t.occurred_at > CAST(:as_of AS timestamptz) - make_interval(days => :days)"
    fraud = "EXISTS (SELECT 1 FROM fraud_labels l WHERE l.transaction_id = t.id AND l.label = 'fraud')"
    daily = (
        db.execute(
            text(
                f"""
            SELECT date_trunc('day', t.occurred_at) AS day, count(*) AS transactions,
                   count(*) FILTER (WHERE t.risk_band IN ('high','critical')) AS flagged,
                   count(*) FILTER (WHERE {fraud}) AS reported_fraud,
                   coalesce(sum(t.amount) FILTER (WHERE {fraud}), 0)::float8 AS fraud_amount
            FROM transactions t WHERE {window} GROUP BY 1 ORDER BY 1
            """
            ),
            p,
        )
        .mappings()
        .all()
    )
    by_category = (
        db.execute(
            text(
                f"""
            SELECT m.category, count(*) AS transactions,
                   count(*) FILTER (WHERE t.risk_band IN ('high','critical')) AS flagged,
                   count(*) FILTER (WHERE {fraud}) AS reported_fraud
            FROM transactions t JOIN merchants m ON m.id = t.merchant_id WHERE {window}
            GROUP BY 1 ORDER BY reported_fraud DESC, flagged DESC
            """
            ),
            p,
        )
        .mappings()
        .all()
    )
    by_channel = (
        db.execute(
            text(
                f"""
            SELECT t.channel, count(*) AS transactions,
                   count(*) FILTER (WHERE t.risk_band IN ('high','critical')) AS flagged,
                   count(*) FILTER (WHERE {fraud}) AS reported_fraud
            FROM transactions t WHERE {window} GROUP BY 1 ORDER BY 1
            """
            ),
            p,
        )
        .mappings()
        .all()
    )
    bands: dict[str, int] = dict(
        db.execute(
            text(f"SELECT coalesce(t.risk_band, 'unscored'), count(*) FROM transactions t WHERE {window} GROUP BY 1"), p
        ).all()
    )
    return {
        "as_of": as_of,
        "days": days,
        "daily": [dict(r) for r in daily],
        "by_category": [dict(r) for r in by_category],
        "by_channel": [dict(r) for r in by_channel],
        "bands": bands,
    }


def _csv(name: str, header: list[str], rows: Any) -> StreamingResponse:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for r in rows:
        w.writerow(["" if v is None else v for v in r])
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}"'}
    )


@router.get("/reports/cases.csv")
def cases_report(db: DB, user: Lead, days: int = Query(default=30, ge=1, le=365)) -> StreamingResponse:
    rows = db.execute(
        text(
            """
            SELECT c.id, c.status, c.priority, c.max_band, c.reason, c.disposition, k.last4, c.opened_at, c.sla_due_at,
                   c.closed_at, u.email AS assignee,
                   (SELECT count(*) FROM case_transactions ct WHERE ct.case_id = c.id) AS transactions
            FROM cases c JOIN cards k ON k.id = c.card_id LEFT JOIN users u ON u.id = c.assignee_id
            WHERE c.opened_at > now() - make_interval(days => :days) ORDER BY c.id
            """
        ),
        {"days": days},
    ).all()
    audit.record(db, user.id, "report.export", "report", "cases", days=days)
    db.commit()
    return _csv(
        "cases.csv",
        [
            "case_id",
            "status",
            "priority",
            "max_band",
            "reason",
            "disposition",
            "card_last4",
            "opened_at",
            "sla_due_at",
            "closed_at",
            "assignee",
            "transactions",
        ],
        rows,
    )


@router.get("/reports/flagged.csv")
def flagged_report(db: DB, user: Auditing, days: int = Query(default=7, ge=1, le=90)) -> StreamingResponse:
    rows = db.execute(
        text(
            """
            SELECT t.external_id, t.occurred_at, t.amount, t.channel, m.category, m.country, k.last4,
                   t.risk_score, t.risk_band, s.recommended_action, s.sampled_for_review, s.rule_set_version
            FROM transactions t JOIN merchants m ON m.id = t.merchant_id JOIN cards k ON k.id = t.card_id
            LEFT JOIN transaction_scores s ON s.id = t.current_score_id
            WHERE t.risk_band IN ('high','critical')
              AND t.occurred_at > (SELECT max(occurred_at) FROM transactions) - make_interval(days => :days)
            ORDER BY t.occurred_at
            """
        ),
        {"days": days},
    ).all()
    audit.record(db, user.id, "report.export", "report", "flagged", days=days)
    db.commit()
    return _csv(
        "flagged.csv",
        [
            "external_id",
            "occurred_at",
            "amount",
            "channel",
            "category",
            "merchant_country",
            "card_last4",
            "risk_score",
            "risk_band",
            "recommended_action",
            "sampled",
            "rule_set_version",
        ],
        rows,
    )
