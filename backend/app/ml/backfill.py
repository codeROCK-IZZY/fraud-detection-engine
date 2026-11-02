"""Replay the active model over recent history (bulk), e.g. after loading demo data
or activating a new model.

Same features, model, thresholds and rules as live scoring, but computed for many
transactions at once from the full frame (the training path, which
``tests/test_features.py`` proves identical to the online path). Differences from
live scoring, by design:

* reason sentences are stored only for medium-and-above (low scores get none);
* cases are opened only for transactions in the last ``case_hours`` (older flagged
  activity would flood the queue with cases nobody can act on any more).
"""

from __future__ import annotations

import logging
import random
from datetime import timedelta
from typing import Any

import numpy as np
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db.models import TransactionScore
from app.ml import data_access, explain, registry, rules
from app.ml.dataset import add_features
from app.ml.features import FEATURE_NAMES
from app.ml.training import band_of, contributions
from app.modules.cases import service as cases
from app.modules.settings.service import active_rule_set, get_risk_settings

logger = logging.getLogger(__name__)
CHUNK = 5000


def backfill(db: Session, days: int = 60, case_hours: int = 48, seed: int = 7) -> dict[str, Any]:
    loaded = registry.get_active(db)
    bundle = loaded.bundle
    frame = data_access.full_frame(db)
    if frame.empty:
        return {"scored": 0}
    labels = data_access.labels_frame(db)
    end = frame["occurred_at"].max()
    window = (frame["occurred_at"] > end - timedelta(days=days)).to_numpy()
    X_all = add_features(frame, labels)
    X = X_all[window][list(FEATURE_NAMES)]
    rows = frame[window]
    p = bundle.classifier.predict_proba(X)
    anomaly = bundle.anomaly.score(X)
    settings = get_risk_settings(db)
    thresholds = settings.get("threshold_overrides") or bundle.thresholds
    model_bands = band_of(p, thresholds)
    rule_set = active_rule_set(db)
    rng = random.Random(seed)
    sample_rate = float(settings["review_sample_rate"])
    names = list(FEATURE_NAMES)
    Xv = X.to_numpy()
    need_explain = np.flatnonzero(model_bands != "low")
    contrib = np.zeros_like(Xv)
    if len(need_explain):
        contrib[need_explain] = contributions(bundle.classifier.model, X.iloc[need_explain])
    case_from = end - timedelta(hours=case_hours)

    records: list[dict[str, Any]] = []
    to_case: list[tuple[int, int, str, bool]] = []
    counts: dict[str, int] = {}
    for k in range(len(X)):
        fdict = {n: (None if np.isnan(v) else float(v)) for n, v in zip(names, Xv[k], strict=True)}
        fired = rules.evaluate(fdict, {"merchant_ref": rows["merchant_ref"].iat[k]}, rule_set.rules)
        band = rules.max_band(model_bands[k], rules.floor_band(fired))
        sampled = band not in ("high", "critical") and rng.random() < sample_rate
        action = "review" if band in ("high", "critical") or sampled else "allow"
        reasons = explain.top_reasons(names, Xv[k].tolist(), contrib[k].tolist()) if band != "low" else []
        records.append(
            {
                "transaction_id": int(rows["id"].iat[k]),
                "model_version_id": loaded.version.id,
                "score": float(p[k]),
                "risk_band": band,
                "model_band": str(model_bands[k]),
                "anomaly_score": float(anomaly[k]),
                "recommended_action": action,
                "sampled_for_review": sampled,
                "rules_fired": fired,
                "explanation": reasons + fired,
                "features": fdict if band != "low" else None,
                "rule_set_version": rule_set.version,
                "latency_ms": None,
            }
        )
        counts[band] = counts.get(band, 0) + 1
        if action == "review" and rows["occurred_at"].iat[k] >= case_from:
            to_case.append((int(rows["card_id"].iat[k]), int(rows["id"].iat[k]), band, sampled))
    for i in range(0, len(records), CHUNK):
        stmt = pg_insert(TransactionScore).values(records[i : i + CHUNK])
        db.execute(
            stmt.on_conflict_do_update(
                constraint="uq_scores_txn_model",
                set_={c: stmt.excluded[c] for c in records[0] if c not in ("transaction_id", "model_version_id")},
            )
        )
    db.execute(
        text(
            """
            UPDATE transactions t SET current_score_id = s.id, risk_score = s.score, risk_band = s.risk_band
            FROM transaction_scores s
            WHERE s.transaction_id = t.id AND s.model_version_id = :mv AND t.occurred_at > :since
            """
        ),
        {"mv": loaded.version.id, "since": end - timedelta(days=days)},
    )
    for card_id, txn_id, band, sampled in to_case:
        cases.attach(db, card_id, txn_id, band, sampled)
    logger.info("backfill done", extra={"extra_fields": {"scored": len(records), "cases": len(to_case)}})
    return {"scored": len(records), "bands": counts, "case_transactions": len(to_case), "days": days}
