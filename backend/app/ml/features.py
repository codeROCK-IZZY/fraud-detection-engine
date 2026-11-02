"""Point-in-time feature engineering — one implementation for training *and* scoring.

``compute_features`` takes a frame of transactions (any number of cards) and returns
one feature row per input row. Two rules make it safe:

1. **Only the past.** Rolling windows are ``closed="left"`` (they end strictly before
   the transaction's own timestamp) and "previous transaction" features look at the
   previous row of the same card. Nothing from the transaction's future can leak in.
2. **A fixed 90-day memory.** Every history-based feature looks back at most
   ``HISTORY_DAYS``. That is what lets online scoring load only a card's last 90 days
   from PostgreSQL and still get exactly the numbers training saw (verified by
   ``tests/test_features.py::test_online_matches_batch``).

Context the caller must supply (because it comes from outside the card's history):
``home_country/home_lat/home_lon`` (customer), ``card_issued_at``, ``device_first_seen``
(across all cards) and ``merchant_fraud_rate`` (point-in-time, see
``merchant_fraud_rates``).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.ml.synthetic import CATEGORY_CODES

HISTORY_DAYS = 90
SMALL_AMOUNT = 5.0
MERCHANT_PRIOR_RATE = 0.003
MERCHANT_PRIOR_WEIGHT = 200.0

CHANNELS = ("pos", "ecommerce", "atm", "transfer")

FEATURE_FAMILIES: dict[str, tuple[str, ...]] = {
    "velocity": ("cnt_10m", "cnt_1h", "cnt_24h", "sum_1h", "sum_24h", "small_cnt_10m", "log_secs_since_prev"),
    "amount": ("amount", "log_amount", "amount_z", "amount_ratio_max"),
    "device": ("has_device", "new_device", "device_age_days"),
    "location": ("km_from_home", "speed_kmh", "ip_mismatch", "foreign_merchant"),
    "time": ("hour_familiarity", "is_night"),
    "merchant": ("merchant_fraud_rate", *(f"cat_{c}" for c in CATEGORY_CODES)),
    "channel": (*(f"ch_{c}" for c in CHANNELS), "em_swipe", "em_manual"),
    "account": ("card_age_days", "new_payee", "cnt_90d"),
}
FEATURE_NAMES: tuple[str, ...] = tuple(f for fam in FEATURE_FAMILIES.values() for f in fam)
FAMILY_OF: dict[str, str] = {f: fam for fam, fs in FEATURE_FAMILIES.items() for f in fs}

REQUIRED_COLUMNS = (
    "card_id",
    "occurred_at",
    "amount",
    "channel",
    "entry_mode",
    "category",
    "merchant_country",
    "device_id",
    "payee",
    "ip_country",
    "lat",
    "lon",
    "home_country",
    "home_lat",
    "home_lon",
    "card_issued_at",
    "device_first_seen",
    "merchant_fraud_rate",
)


def haversine_km(lat1: np.ndarray, lon1: np.ndarray, lat2: np.ndarray, lon2: np.ndarray) -> np.ndarray:
    lat1, lon1, lat2, lon2 = (np.radians(np.asarray(a, dtype=float)) for a in (lat1, lon1, lat2, lon2))
    a = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    return np.asarray(2 * 6371.0 * np.arcsin(np.sqrt(np.clip(a, 0, 1))))


def _rolling(d: pd.DataFrame, keys: list[str], col: str, window: str, how: str) -> pd.Series:
    """Per-group time-window aggregate over the window *strictly before* each row."""
    gb = d.groupby(keys, sort=False)
    values = getattr(gb.rolling(window, on="occurred_at", closed="left")[col], how)().to_numpy()
    # Results come out group by group (in ``ngroup`` order), rows within a group in frame order.
    order = np.argsort(gb.ngroup().to_numpy(), kind="stable")
    out = np.full(len(d), np.nan)
    out[order] = values
    return pd.Series(out, index=d.index)


def _gap_since_previous(d: pd.DataFrame, keys: list[str]) -> pd.Series:
    """Time since the previous row with the same keys; NaN if none within HISTORY_DAYS."""
    gap = d.groupby(keys, sort=False)["occurred_at"].diff()
    return gap.where(gap <= pd.Timedelta(days=HISTORY_DAYS))


def compute_features(df: pd.DataFrame) -> pd.DataFrame:
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"compute_features: missing columns {missing}")
    # Deterministic order: by card, then time, then the caller's row order for ties.
    d = df.assign(_pos=np.arange(len(df))).sort_values(["card_id", "occurred_at", "_pos"], kind="mergesort")
    d = d.copy()
    d["occurred_at"] = pd.to_datetime(d["occurred_at"], utc=True)
    amt = d["amount"].astype(float)
    d["_one"] = 1.0
    d["_amt"] = amt
    d["_log"] = np.log1p(amt)
    d["_small"] = (amt < SMALL_AMOUNT).astype(float)
    t = d["occurred_at"]
    f = pd.DataFrame(index=d.index)

    # --- velocity
    card = ["card_id"]
    f["cnt_10m"] = _rolling(d, card, "_one", "10min", "sum").fillna(0)
    f["cnt_1h"] = _rolling(d, card, "_one", "1h", "sum").fillna(0)
    f["cnt_24h"] = _rolling(d, card, "_one", "24h", "sum").fillna(0)
    f["sum_1h"] = _rolling(d, card, "_amt", "1h", "sum").fillna(0)
    f["sum_24h"] = _rolling(d, card, "_amt", "24h", "sum").fillna(0)
    f["small_cnt_10m"] = _rolling(d, card, "_small", "10min", "sum").fillna(0)
    gap = _gap_since_previous(d, card)
    f["log_secs_since_prev"] = np.log1p(gap.dt.total_seconds())

    # --- amount relative to this card's own last 90 days
    window = f"{HISTORY_DAYS}D"
    cnt90 = _rolling(d, card, "_one", window, "sum").fillna(0)
    mean90 = _rolling(d, card, "_log", window, "mean")
    std90 = _rolling(d, card, "_log", window, "std")
    max90 = _rolling(d, card, "_amt", window, "max")
    enough = cnt90 >= 5
    f["amount"] = amt
    f["log_amount"] = d["_log"]
    f["amount_z"] = ((d["_log"] - mean90) / np.maximum(std90.fillna(0), 0.25)).where(enough)
    f["amount_ratio_max"] = (amt / max90).where(cnt90 >= 1)

    # --- device
    has_dev = d["device_id"].notna()
    dev_gap = _gap_since_previous(d.assign(_dev=d["device_id"].fillna(-1)), ["card_id", "_dev"])
    f["has_device"] = has_dev.astype(float)
    f["new_device"] = (has_dev & dev_gap.isna()).astype(float)
    first_seen = pd.to_datetime(d["device_first_seen"], utc=True)
    # Capped at 90 days: "new vs. established device" is the signal. Uncapped, the value grows with
    # calendar time (every device in a dataset ages), a spurious trend that drifts by construction.
    f["device_age_days"] = ((t - first_seen).dt.total_seconds() / 86400).clip(upper=HISTORY_DAYS).where(has_dev)

    # --- location
    located = d["lat"].notna() & d["lon"].notna()
    home_ok = d["home_lat"].notna()
    f["km_from_home"] = pd.Series(haversine_km(d["lat"], d["lon"], d["home_lat"], d["home_lon"]), index=d.index).where(
        located & home_ok
    )
    speed = pd.Series(np.nan, index=d.index)
    if located.any():
        loc = d[located]
        prev = loc.groupby("card_id", sort=False)[["lat", "lon", "occurred_at"]].shift(1)
        hours = (loc["occurred_at"] - prev["occurred_at"]).dt.total_seconds() / 3600
        dist = haversine_km(loc["lat"], loc["lon"], prev["lat"], prev["lon"])
        s = pd.Series(dist / np.maximum(hours.to_numpy(), 1 / 60), index=loc.index)
        s = s.where(hours <= HISTORY_DAYS * 24).clip(upper=20_000)
        speed.loc[loc.index] = s
    f["speed_kmh"] = speed
    ip = d["ip_country"]
    f["ip_mismatch"] = (ip != d["home_country"]).astype(float).where(ip.notna())
    f["foreign_merchant"] = (d["merchant_country"] != d["home_country"]).astype(float)

    # --- time of day relative to this card's habits (3-hour buckets)
    d["_bucket"] = t.dt.hour // 3
    same_bucket = _rolling(d, ["card_id", "_bucket"], "_one", window, "sum").fillna(0)
    f["hour_familiarity"] = (same_bucket / cnt90.where(cnt90 > 0)).where(cnt90 >= 5)
    f["is_night"] = (t.dt.hour < 6).astype(float)

    # --- merchant / channel
    f["merchant_fraud_rate"] = d["merchant_fraud_rate"].astype(float)
    for c in CATEGORY_CODES:
        f[f"cat_{c}"] = (d["category"] == c).astype(float)
    for c in CHANNELS:
        f[f"ch_{c}"] = (d["channel"] == c).astype(float)
    f["em_swipe"] = (d["entry_mode"] == "swipe").astype(float)
    f["em_manual"] = (d["entry_mode"] == "manual").astype(float)

    # --- account
    issued = pd.to_datetime(d["card_issued_at"], utc=True)
    f["card_age_days"] = (t - issued).dt.total_seconds() / 86400
    has_payee = d["payee"].notna()
    payee_gap = _gap_since_previous(d.assign(_payee=d["payee"].fillna("")), ["card_id", "_payee"])
    f["new_payee"] = (has_payee & payee_gap.isna()).astype(float)
    f["cnt_90d"] = cnt90

    return f.reindex(df.index)[list(FEATURE_NAMES)].astype(float)


def merchant_fraud_rates(tx: pd.DataFrame, labels: pd.DataFrame, *, leaky: bool = False) -> pd.Series:
    """Smoothed fraud rate of each transaction's merchant, **as known at that moment**.

    For a transaction at time t: ``(frauds reported before t + prior) / (transactions
    at the merchant before t + prior weight)``. "Reported before t" is the crucial part:
    a fraud that happened last week but is only reported next month must not count.

    ``tx`` needs ``id, merchant_id, occurred_at``; ``labels`` needs ``transaction_id,
    reported_at`` for fraud labels. ``leaky=True`` deliberately ignores report time
    (uses every label, including future ones) — used only by the evaluation report to
    show how much a leak inflates results.
    """
    d = tx[["id", "merchant_id", "occurred_at"]].copy()
    d["occurred_at"] = pd.to_datetime(d["occurred_at"], utc=True)
    d = d.assign(_pos=np.arange(len(d))).sort_values(["merchant_id", "occurred_at", "_pos"], kind="mergesort")
    # Transactions at this merchant strictly before t (ties at the same instant excluded).
    n_before = d.groupby("merchant_id").cumcount() - d.groupby(["merchant_id", "occurred_at"]).cumcount()
    frauds = pd.Series(0.0, index=d.index)
    if len(labels):
        lab = labels[["transaction_id", "reported_at"]].copy()
        lab["reported_at"] = pd.to_datetime(lab["reported_at"], utc=True)
        lab = lab.sort_values("reported_at").drop_duplicates("transaction_id")  # earliest fraud report per txn
        lab = lab.merge(d[["id", "merchant_id"]], left_on="transaction_id", right_on="id", how="inner")
        if leaky:
            totals = lab.groupby("merchant_id").size()
            frauds = d["merchant_id"].map(totals).fillna(0).astype(float)
        elif len(lab):
            positions = d.groupby("merchant_id", sort=False).indices
            times = d["occurred_at"].to_numpy(dtype="datetime64[ns]")
            counts = np.zeros(len(d))
            for mid, g in lab.groupby("merchant_id"):
                rep = np.sort(g["reported_at"].to_numpy(dtype="datetime64[ns]"))
                pos = positions.get(mid)
                if pos is not None:
                    counts[pos] = np.searchsorted(rep, times[pos], side="left")
            frauds = pd.Series(counts, index=d.index)
    rate = (frauds + MERCHANT_PRIOR_RATE * MERCHANT_PRIOR_WEIGHT) / (n_before + MERCHANT_PRIOR_WEIGHT)
    return rate.reindex(tx.index)
