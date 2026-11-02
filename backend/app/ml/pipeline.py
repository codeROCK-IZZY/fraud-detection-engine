"""Train a production model from the database (what ``POST /models/train`` runs).

Uses exactly the same functions as the offline evaluation (features, splits,
labels-as-of, calibration, thresholds), with the deployment date D = the latest
transaction in the database: the model learns from transactions older than 60
days using the labels known now, calibrates on the 60–30 day window, and leaves
the last 30 days alone because their labels are still immature.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from app.db.models import ModelVersion
from app.ml import data_access, registry
from app.ml.dataset import CALIB_GAP_DAYS, TRAIN_GAP_DAYS, add_features, labels_as_of, time_splits
from app.ml.training import (
    PRODUCTION_CANDIDATES,
    cross_validate,
    global_importance,
    ranking_metrics,
    train_bundle,
)

logger = logging.getLogger(__name__)
MIN_TRAIN_POSITIVES = 20
MIN_CALIB_POSITIVES = 5


def clean_nans(obj: Any) -> Any:
    """JSONB cannot store NaN; replace with None recursively."""
    if isinstance(obj, float) and np.isnan(obj):
        return None
    if isinstance(obj, dict):
        return {k: clean_nans(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [clean_nans(v) for v in obj]
    return obj


def train_from_db(
    db: Session, *, capacity: float = 25, activate: bool = True, seed: int = 42, deploy_at: pd.Timestamp | None = None
) -> ModelVersion:
    frame = data_access.full_frame(db)
    if frame.empty:
        raise ValueError("No transactions in the database")
    labels = data_access.labels_frame(db)
    deploy = deploy_at or frame["occurred_at"].max() + pd.Timedelta(seconds=1)
    X = add_features(frame, labels)
    y = labels_as_of(frame, labels, deploy)
    sp = time_splits(frame, deploy)
    n_train, n_cal = int(y[sp.train].sum()), int(y[sp.calib].sum())
    if n_train < MIN_TRAIN_POSITIVES or n_cal < MIN_CALIB_POSITIVES:
        raise ValueError(
            f"Not enough labelled fraud to train: {n_train} in the training window (need {MIN_TRAIN_POSITIVES}) "
            f"and {n_cal} in the calibration window (need {MIN_CALIB_POSITIVES}). Training needs more than "
            f"{TRAIN_GAP_DAYS} days of history with reported fraud."
        )
    cv = cross_validate(X[sp.train], y[sp.train], frame["card_id"].to_numpy()[sp.train], seed)
    algorithm = max(PRODUCTION_CANDIDATES, key=lambda n: cv[n]["pr_auc_mean"])
    summary = {
        "deploy_at": deploy.isoformat(),
        "transactions": len(frame),
        "train_rows": int(sp.train.sum()),
        "train_known_fraud": n_train,
        "calibration_rows": int(sp.calib.sum()),
        "calibration_known_fraud": n_cal,
        "labels_used": int((labels["reported_at"] <= deploy).sum()),
        "seed": seed,
    }
    bundle = train_bundle(
        X,
        y,
        sp.train,
        sp.calib,
        algorithm=algorithm,
        calib_days=float(TRAIN_GAP_DAYS - CALIB_GAP_DAYS),
        capacity=capacity,
        seed=seed,
        trained_on=summary,
    )
    Xc, yc = X[sp.calib], y[sp.calib]
    p = bundle.classifier.predict_proba(Xc)
    metrics = clean_nans(
        {
            "cross_validation": cv,
            "algorithm_choice": f"{algorithm}: best mean cross-validated PR-AUC among {list(PRODUCTION_CANDIDATES)}",
            "calibration_window": {
                **ranking_metrics(yc, p),
                "note": f"Labels as known at deployment; transactions {CALIB_GAP_DAYS}–{TRAIN_GAP_DAYS} days old, "
                "so some late-reported fraud is still missing.",
            },
            "global_importance": global_importance(
                bundle.classifier.model, Xc.sample(n=min(len(Xc), 20_000), random_state=seed)
            ),
            "data": summary,
        }
    )
    mv = registry.register(db, bundle, metrics, activate=activate)
    logger.info("model trained", extra={"extra_fields": {"version": mv.version, "algorithm": algorithm}})
    return mv


def latest_data_time(db: Session) -> pd.Timestamp | None:
    from sqlalchemy import func, select

    from app.db.models import Transaction

    t = db.scalar(select(func.max(Transaction.occurred_at)))
    return pd.Timestamp(t) if t else None
