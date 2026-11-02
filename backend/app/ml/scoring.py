"""The one scoring path used by REST, CSV import, the Kafka stream scorer and backfills.

For a batch of stored transactions:

1. load each card's last 90 days (``data_access.frame_for_scoring``)
2. compute point-in-time features (``features.compute_features``)
3. model: calibrated probability, SHAP contributions, anomaly score, model band
4. rules: deterministic checks that can only raise the band
5. decide: ``review`` for high/critical (plus a small random sample), else ``allow``
6. persist the score (idempotent per transaction × model version), denormalise the
   current score onto the transaction, and open or extend the card's case

Batches matter for throughput: one history query and one model call per batch
rather than per transaction.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from typing import Any

import numpy as np
from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.logging import log_extra
from app.db.models import Transaction, TransactionScore
from app.ml import data_access, explain, registry, rules
from app.ml.features import FEATURE_NAMES, compute_features
from app.ml.training import band_of, contributions
from app.modules.cases import service as cases
from app.modules.settings.service import active_rule_set, get_risk_settings

logger = logging.getLogger(__name__)
REVIEW_BANDS = ("high", "critical")


@dataclass
class Scored:
    transaction_id: int
    score: TransactionScore
    case_id: int | None


def score_transactions(
    db: Session,
    transaction_ids: list[int],
    *,
    loaded: registry.LoadedModel | None = None,
    open_cases: bool = True,
    rng: random.Random | None = None,
) -> list[Scored]:
    """Score stored transactions (caller commits). Uses the active model if ``loaded`` is None."""
    if not transaction_ids:
        return []
    started = time.perf_counter()
    frame, targets = data_access.frame_for_scoring(db, transaction_ids)
    if not len(targets):
        return []
    X = compute_features(frame).loc[targets, list(FEATURE_NAMES)]
    rows = frame.loc[targets]
    loaded = loaded or registry.get_active_or_none(db)
    settings = get_risk_settings(db)
    rule_set = active_rule_set(db)
    rng = rng or random.SystemRandom()

    n = len(X)
    p = np.full(n, np.nan)
    anomaly = np.full(n, np.nan)
    model_bands: list[str | None] = [None] * n
    contrib = None
    if loaded is not None:
        bundle = loaded.bundle
        p = bundle.classifier.predict_proba(X)
        contrib = contributions(bundle.classifier.model, X)
        anomaly = bundle.anomaly.score(X)
        thresholds = settings.get("threshold_overrides") or bundle.thresholds
        model_bands = list(band_of(p, thresholds))
    per_txn_ms = (time.perf_counter() - started) * 1000 / n

    out: list[Scored] = []
    sample_rate = float(settings["review_sample_rate"])
    names = list(FEATURE_NAMES)
    for k, (idx, feat) in enumerate(X.iterrows()):
        row = rows.loc[idx]
        fdict = {name: (None if np.isnan(v) else float(v)) for name, v in feat.items()}
        fired = rules.evaluate(fdict, {"merchant_ref": row["merchant_ref"]}, rule_set.rules)
        band = rules.max_band(model_bands[k], rules.floor_band(fired))
        sampled = band not in REVIEW_BANDS and rng.random() < sample_rate
        action = "review" if band in REVIEW_BANDS or sampled else "allow"
        reasons: list[dict[str, Any]] = []
        if contrib is not None:
            reasons = explain.top_reasons(names, feat.to_list(), contrib[k].tolist())
        reasons += fired
        values = {
            "transaction_id": int(row["id"]),
            "model_version_id": loaded.version.id if loaded else None,
            "score": None if np.isnan(p[k]) else float(p[k]),
            "risk_band": band,
            "model_band": model_bands[k],
            "anomaly_score": None if np.isnan(anomaly[k]) else float(anomaly[k]),
            "recommended_action": action,
            "sampled_for_review": sampled,
            "rules_fired": fired,
            "explanation": reasons,
            "features": fdict if band != "low" else None,
            "rule_set_version": rule_set.version,
            "latency_ms": round(per_txn_ms, 3),
        }
        base = pg_insert(TransactionScore).values(**values)
        stmt = base.on_conflict_do_update(
            constraint="uq_scores_txn_model",
            set_={k2: base.excluded[k2] for k2 in values if k2 not in ("transaction_id", "model_version_id")}
            | {"scored_at": base.excluded.scored_at},
        ).returning(TransactionScore)
        score = db.execute(stmt).scalar_one()
        db.execute(
            update(Transaction)
            .where(Transaction.id == score.transaction_id)
            .values(current_score_id=score.id, risk_score=score.score, risk_band=band)
        )
        case_id = None
        if action == "review" and open_cases:
            case_id = cases.attach(db, int(row["card_id"]), score.transaction_id, band, sampled).id
        out.append(Scored(score.transaction_id, score, case_id))
    logger.debug("scored", extra=log_extra(n=n, ms_per_txn=round(per_txn_ms, 2)))
    return out
