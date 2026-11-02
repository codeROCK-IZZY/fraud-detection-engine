"""Assemble feature-ready frames and point-in-time labels.

A *frame* is one row per transaction with the columns ``compute_features`` needs,
plus ``id``, ``merchant_id``, ``merchant_ref``, ``external_id`` and (for simulated
data only) the ground truth ``is_fraud`` / ``scenario``.

Labels are a separate table ``transaction_id, label, reported_at``. The function
``labels_as_of`` answers "what did we know about each transaction on date D?" —
the basis of leakage-safe training.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import numpy as np
import pandas as pd

from app.ml.features import compute_features, merchant_fraud_rates
from app.ml.synthetic import SimData

TRAIN_GAP_DAYS = 60  # training rows: occurred more than 60 days before deployment (labels mature)
CALIB_GAP_DAYS = 30  # calibration rows: 60–30 days before deployment


def sim_frame(sim: SimData) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Frame + labels from simulated data (the evaluation path; the DB path is ``data_access``)."""
    tx = sim.transactions
    m = sim.merchants
    cust = sim.customers
    cards = sim.cards
    card_idx = tx["card_idx"].to_numpy()
    merchant_idx = tx["merchant_idx"].to_numpy()
    frame = pd.DataFrame(
        {
            "id": np.arange(len(tx), dtype=np.int64),
            "external_id": tx["external_id"].to_numpy(),
            "card_id": card_idx,
            "merchant_id": merchant_idx,
            "merchant_ref": m["external_ref"].to_numpy()[merchant_idx],
            "occurred_at": tx["occurred_at"].to_numpy(),
            "amount": tx["amount"].to_numpy(dtype=float),
            "channel": tx["channel"].to_numpy(),
            "entry_mode": tx["entry_mode"].to_numpy(),
            "category": m["category"].to_numpy()[merchant_idx],
            "merchant_country": m["country"].to_numpy()[merchant_idx],
            "device_id": tx["device"].to_numpy(),
            "payee": tx["payee"].to_numpy(),
            "ip_country": tx["ip_country"].to_numpy(),
            "lat": tx["lat"].to_numpy(dtype=float),
            "lon": tx["lon"].to_numpy(dtype=float),
            "home_country": cust["home_country"].to_numpy()[card_idx],
            "home_lat": cust["home_lat"].to_numpy(dtype=float)[card_idx],
            "home_lon": cust["home_lon"].to_numpy(dtype=float)[card_idx],
            "card_issued_at": pd.to_datetime(cards["issued_at"], utc=True).to_numpy()[card_idx],
            "is_fraud": tx["is_fraud"].to_numpy(),
            "scenario": tx["scenario"].to_numpy(),
        }
    )
    frame["occurred_at"] = pd.to_datetime(frame["occurred_at"], utc=True)
    frame["card_issued_at"] = pd.to_datetime(frame["card_issued_at"], utc=True)
    # Device first seen = earliest use by any card (the DB keeps devices.first_seen_at the same way).
    first = frame.dropna(subset=["device_id"]).groupby("device_id")["occurred_at"].min()
    frame["device_first_seen"] = frame["device_id"].map(first)
    ext_to_id = pd.Series(frame["id"].to_numpy(), index=frame["external_id"])
    labels = pd.DataFrame(
        {
            "transaction_id": sim.labels["external_id"].map(ext_to_id).to_numpy(),
            "label": sim.labels["label"].to_numpy(),
            "source": sim.labels["source"].to_numpy(),
            "reported_at": pd.to_datetime(sim.labels["reported_at"], utc=True),
        }
    )
    return frame, labels


def add_features(frame: pd.DataFrame, labels: pd.DataFrame, *, leaky: bool = False) -> pd.DataFrame:
    """Point-in-time merchant rates + all features. Returns a feature frame aligned with ``frame``."""
    fraud = labels[labels["label"] == "fraud"]
    f = frame.copy()
    f["merchant_fraud_rate"] = merchant_fraud_rates(f, fraud, leaky=leaky)
    return compute_features(f)


def labels_as_of(frame: pd.DataFrame, labels: pd.DataFrame, as_of: pd.Timestamp) -> np.ndarray:
    """1 if, by ``as_of``, the latest label reported for the transaction is "fraud"."""
    known = labels[labels["reported_at"] <= as_of].sort_values("reported_at", kind="mergesort")
    latest = known.drop_duplicates("transaction_id", keep="last").set_index("transaction_id")["label"]
    return (frame["id"].map(latest) == "fraud").to_numpy(dtype=np.int8)


@dataclass
class Splits:
    deploy_at: pd.Timestamp
    train: np.ndarray  # boolean masks over the frame
    calib: np.ndarray
    test: np.ndarray

    def describe(self, frame: pd.DataFrame) -> dict[str, object]:
        def rng(mask: np.ndarray) -> dict[str, object]:
            t = frame.loc[mask, "occurred_at"]
            return {
                "rows": int(mask.sum()),
                "from": t.min().isoformat() if len(t) else None,
                "to": t.max().isoformat() if len(t) else None,
            }

        return {
            "deploy_at": self.deploy_at.isoformat(),
            "train": rng(self.train),
            "calibration": rng(self.calib),
            "test": rng(self.test),
        }


def time_splits(frame: pd.DataFrame, deploy_at: pd.Timestamp) -> Splits:
    """Out-of-time splits around a deployment date D.

    * train:       occurred before D − 60 days (labels as known at D are mostly mature)
    * calibration: D − 60 … D − 30 days (used to calibrate probabilities and set thresholds)
    * (gap)        the last 30 days before D are not used: their labels are still immature
    * test:        on/after D — the "future" the model is deployed into
    """
    t = frame["occurred_at"]
    train = (t < deploy_at - timedelta(days=TRAIN_GAP_DAYS)).to_numpy()
    calib = (t >= deploy_at - timedelta(days=TRAIN_GAP_DAYS)) & (t < deploy_at - timedelta(days=CALIB_GAP_DAYS))
    test = (t >= deploy_at).to_numpy()
    return Splits(deploy_at, train, calib.to_numpy(), test)
