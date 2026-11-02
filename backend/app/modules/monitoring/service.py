"""Model monitoring: drift (no labels needed) and performance (needs matured labels).

* **Drift** — Population Stability Index (PSI) of the model's most important
  features and of the score, last N days vs. the reference distribution saved when
  the model was trained. PSI < 0.1 stable, 0.1–0.25 warning, > 0.25 alert.
* **Performance** — weekly alert precision, recall and PR-AUC, computed only for
  transactions old enough (``label_maturity_days``) that most fraud has been
  reported. Newer weeks are shown as "immature" rather than as good news.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import DriftReport
from app.ml import data_access, registry
from app.ml.dataset import add_features
from app.ml.training import psi, ranking_metrics

WARN, ALERT = 0.1, 0.25


def _status(value: float) -> str:
    return "alert" if value > ALERT else "warning" if value > WARN else "ok"


def run_drift_report(db: Session, days: int = 7) -> DriftReport:
    mv = registry.active_version(db)
    if mv is None:
        raise ValueError("No active model")
    frame = data_access.full_frame(db)
    if frame.empty:
        raise ValueError("No transactions")
    labels = data_access.labels_frame(db)
    end = frame["occurred_at"].max()
    start = end - timedelta(days=days)
    window = (frame["occurred_at"] > start).to_numpy()
    ref = mv.reference
    X = add_features(frame, labels)[window]
    scores = (
        db.execute(
            text(
                "SELECT s.score FROM transaction_scores s JOIN transactions t ON t.id = s.transaction_id "
                "WHERE s.model_version_id = :mv AND t.occurred_at > :start AND s.score IS NOT NULL"
            ),
            {"mv": mv.id, "start": start.to_pydatetime()},
        )
        .scalars()
        .all()
    )
    feature_psi = {
        f: round(psi(np.array(ref["sample"][f], dtype=float), X[f].to_numpy(), np.array(ref["features"][f])), 4)
        for f in ref["features"]
    }
    score_psi = (
        round(psi(np.array(ref["score_sample"]), np.array(scores, dtype=float), np.array(ref["score_edges"])), 4)
        if scores
        else None
    )
    worst = max([*feature_psi.values(), score_psi or 0.0])
    report = DriftReport(
        model_version_id=mv.id,
        window_start=start.to_pydatetime(),
        window_end=end.to_pydatetime(),
        n_transactions=int(window.sum()),
        feature_psi=feature_psi,
        score_psi=score_psi,
        status=_status(worst),
    )
    db.add(report)
    db.flush()
    return report


def performance(db: Session, weeks: int = 16) -> dict[str, Any]:
    """Weekly metrics of the *current* scores against labels known now."""
    maturity = get_settings().label_maturity_days
    rows = db.execute(
        text(
            """
            SELECT t.occurred_at, t.amount::float8 AS amount, t.risk_score, t.risk_band,
                   EXISTS (SELECT 1 FROM fraud_labels l WHERE l.transaction_id = t.id AND l.label = 'fraud'
                           AND NOT EXISTS (SELECT 1 FROM fraud_labels l2 WHERE l2.transaction_id = t.id
                                           AND l2.label = 'legitimate' AND l2.reported_at > l.reported_at)) AS fraud
            FROM transactions t
            WHERE t.risk_band IS NOT NULL
              AND t.occurred_at > (SELECT max(occurred_at) FROM transactions) - make_interval(weeks => :weeks)
            """
        ),
        {"weeks": weeks},
    ).all()
    if not rows:
        return {"maturity_days": maturity, "weeks": []}
    df = pd.DataFrame(rows, columns=["occurred_at", "amount", "score", "band", "fraud"])
    df["occurred_at"] = pd.to_datetime(df["occurred_at"], utc=True)
    latest = df["occurred_at"].max()
    df["week"] = df["occurred_at"].dt.tz_convert(None).dt.to_period("W").dt.start_time
    out = []
    for week, g in df.groupby("week"):
        y = g["fraud"].to_numpy(dtype=int)
        flagged = g["band"].isin(["high", "critical"]).to_numpy()
        mature = bool(g["occurred_at"].max() <= latest - timedelta(days=maturity))
        s = g["score"].to_numpy(dtype=float)
        has_score = ~np.isnan(s)
        out.append(
            {
                "week_start": week.date().isoformat(),
                "mature": mature,
                "transactions": len(g),
                "flagged": int(flagged.sum()),
                "reported_fraud": int(y.sum()),
                "alert_precision": float(y[flagged].mean()) if flagged.any() else None,
                "recall": float(y[flagged].sum() / y.sum()) if y.sum() else None,
                "dollar_recall": float(
                    g["amount"].to_numpy()[flagged & (y == 1)].sum() / g["amount"].to_numpy()[y == 1].sum()
                )
                if y.sum()
                else None,
                "pr_auc": ranking_metrics(y[has_score], s[has_score])["pr_auc"]
                if has_score.any() and 0 < y.sum() < len(y)
                else None,
            }
        )
    for w in out:
        if w["pr_auc"] is not None and np.isnan(w["pr_auc"]):
            w["pr_auc"] = None
    return {"maturity_days": maturity, "weeks": out}


def latency(db: Session) -> dict[str, Any]:
    row = db.execute(
        text(
            "SELECT count(*), avg(latency_ms), percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms), "
            "percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) FROM transaction_scores "
            "WHERE latency_ms IS NOT NULL AND scored_at > now() - interval '24 hours'"
        )
    ).one()
    return {
        "scores_24h": row[0],
        "mean_ms": row[1],
        "p50_ms": row[2],
        "p95_ms": row[3],
        "note": "Per-transaction share of batch scoring time (feature computation + model + rules + DB writes).",
    }


def recent_reports(db: Session, limit: int = 20) -> list[DriftReport]:
    return list(db.scalars(select(DriftReport).order_by(DriftReport.created_at.desc()).limit(limit)))
