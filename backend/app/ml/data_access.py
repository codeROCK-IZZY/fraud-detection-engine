"""Load feature-ready frames from PostgreSQL.

The SQL here produces exactly the columns ``features.compute_features`` expects,
so online scoring (a few cards, last 90 days) and training (everything) feed the
same feature code. ``tests/test_features.py`` checks the online path matches the
batch path number for number.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.ml.features import HISTORY_DAYS, MERCHANT_PRIOR_RATE, MERCHANT_PRIOR_WEIGHT

_FRAME_SQL = """
SELECT t.id, t.external_id, t.card_id, t.merchant_id, m.external_ref AS merchant_ref, t.occurred_at,
       t.amount::float8 AS amount, t.channel, t.entry_mode, m.category, m.country AS merchant_country,
       t.device_id, t.payee_hash AS payee, t.ip_country, t.lat, t.lon,
       c.home_country, c.home_lat, c.home_lon, k.issued_at AS card_issued_at, d.first_seen_at AS device_first_seen
FROM transactions t
JOIN merchants m ON m.id = t.merchant_id
JOIN cards k ON k.id = t.card_id
JOIN customers c ON c.id = k.customer_id
LEFT JOIN devices d ON d.id = t.device_id
"""


def _frame(db: Session, where: str, params: dict[str, object]) -> pd.DataFrame:
    rows = db.execute(text(_FRAME_SQL + where), params)
    df = pd.DataFrame(rows.fetchall(), columns=list(rows.keys()))
    if df.empty:
        return df
    for col in ("occurred_at", "card_issued_at", "device_first_seen"):
        df[col] = pd.to_datetime(df[col], utc=True)
    for col in ("amount", "lat", "lon", "home_lat", "home_lon"):
        df[col] = df[col].astype(float)
    df["device_id"] = df["device_id"].astype("Int64").astype(object).where(df["device_id"].notna(), None)
    return df


def card_history(db: Session, card_ids: list[int], t_from: datetime, t_to: datetime) -> pd.DataFrame:
    """All transactions of these cards with ``t_from <= occurred_at <= t_to``."""
    return _frame(
        db,
        "WHERE t.card_id = ANY(:cards) AND t.occurred_at >= :t_from AND t.occurred_at <= :t_to",
        {"cards": list(card_ids), "t_from": t_from, "t_to": t_to},
    )


def frame_for_scoring(db: Session, transaction_ids: list[int]) -> tuple[pd.DataFrame, pd.Index]:
    """History frame covering the targets plus 90 days before the earliest one.

    Returns ``(frame, target_index)``; ``merchant_fraud_rate`` is filled for the targets.
    """
    head = db.execute(
        text(
            "SELECT array_agg(DISTINCT card_id), min(occurred_at), max(occurred_at) "
            "FROM transactions WHERE id = ANY(:ids)"
        ),
        {"ids": transaction_ids},
    ).one()
    if head[0] is None:
        return pd.DataFrame(), pd.Index([])
    frame = card_history(db, list(head[0]), head[1] - timedelta(days=HISTORY_DAYS), head[2])
    rates = merchant_rates(db, transaction_ids)
    frame["merchant_fraud_rate"] = frame["id"].map(rates)
    targets = frame.index[frame["id"].isin(transaction_ids)]
    return frame, targets


def merchant_rates(db: Session, transaction_ids: list[int]) -> pd.Series:
    """Point-in-time smoothed merchant fraud rate for each given transaction (same
    definition as ``features.merchant_fraud_rates``)."""
    rows = db.execute(
        text(
            """
            SELECT x.id,
              (SELECT count(*) FROM transactions t2
                 WHERE t2.merchant_id = x.merchant_id AND t2.occurred_at < x.occurred_at) AS n_before,
              (SELECT count(DISTINCT l.transaction_id) FROM fraud_labels l
                 JOIN transactions t3 ON t3.id = l.transaction_id
                 WHERE t3.merchant_id = x.merchant_id AND l.label = 'fraud'
                   AND l.reported_at < x.occurred_at) AS frauds
            FROM transactions x WHERE x.id = ANY(:ids)
            """
        ),
        {"ids": transaction_ids},
    ).all()
    return pd.Series(
        {
            r.id: (r.frauds + MERCHANT_PRIOR_RATE * MERCHANT_PRIOR_WEIGHT) / (r.n_before + MERCHANT_PRIOR_WEIGHT)
            for r in rows
        },
        dtype=float,
    )


def full_frame(db: Session, until: datetime | None = None) -> pd.DataFrame:
    """Every transaction (optionally up to ``until``) — the training path."""
    if until is None:
        return _frame(db, "ORDER BY t.occurred_at, t.id", {})
    return _frame(db, "WHERE t.occurred_at <= :until ORDER BY t.occurred_at, t.id", {"until": until})


def labels_frame(db: Session) -> pd.DataFrame:
    rows = db.execute(
        text("SELECT transaction_id, label, source, reported_at FROM fraud_labels ORDER BY reported_at, id")
    )
    df = pd.DataFrame(rows.fetchall(), columns=["transaction_id", "label", "source", "reported_at"])
    df["reported_at"] = pd.to_datetime(df["reported_at"], utc=True)
    df["transaction_id"] = df["transaction_id"].astype(np.int64)
    return df
