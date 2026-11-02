"""Feature engineering: correctness, no look-ahead, and online/batch parity."""

import numpy as np
import pandas as pd
import pytest

from app.ml import data_access
from app.ml.dataset import add_features, sim_frame
from app.ml.features import FEATURE_NAMES, compute_features, merchant_fraud_rates
from app.ml.synthetic import SimConfig, generate

T0 = pd.Timestamp("2026-03-01 12:00", tz="UTC")


def _frame(rows: list[dict]) -> pd.DataFrame:
    base = {
        "card_id": 1,
        "amount": 10.0,
        "channel": "ecommerce",
        "entry_mode": "online",
        "category": "retail",
        "merchant_country": "US",
        "device_id": None,
        "payee": None,
        "ip_country": "US",
        "lat": np.nan,
        "lon": np.nan,
        "home_country": "US",
        "home_lat": 40.71,
        "home_lon": -74.01,
        "card_issued_at": T0 - pd.Timedelta(days=400),
        "device_first_seen": pd.NaT,
        "merchant_fraud_rate": 0.003,
    }
    df = pd.DataFrame([{**base, **r} for r in rows])
    df["occurred_at"] = pd.to_datetime(df["occurred_at"], utc=True)
    df["device_first_seen"] = pd.to_datetime(df["device_first_seen"], utc=True)
    return df


def test_velocity_windows_exclude_the_transaction_itself():
    df = _frame(
        [
            {"occurred_at": T0, "amount": 1.0},
            {"occurred_at": T0 + pd.Timedelta(minutes=1), "amount": 2.0},
            {"occurred_at": T0 + pd.Timedelta(minutes=2), "amount": 300.0},
            {"occurred_at": T0 + pd.Timedelta(minutes=20), "amount": 5.0},
            {"occurred_at": T0, "amount": 50.0, "card_id": 2},  # another card: never counted
        ]
    )
    f = compute_features(df)
    assert f["cnt_10m"].tolist() == [0, 1, 2, 0, 0]
    assert f["small_cnt_10m"].tolist() == [0, 1, 2, 0, 0]
    assert f["cnt_1h"].tolist() == [0, 1, 2, 3, 0]
    assert f["sum_1h"].tolist() == [0, 1, 3, 303, 0]
    assert np.isnan(f["log_secs_since_prev"].iloc[0])
    assert f["log_secs_since_prev"].iloc[1] == pytest.approx(np.log1p(60))
    assert list(f.columns) == list(FEATURE_NAMES)


def test_device_and_payee_novelty_have_a_90_day_memory():
    df = _frame(
        [
            {"occurred_at": T0, "device_id": "d1"},
            {"occurred_at": T0 + pd.Timedelta(days=1), "device_id": "d1"},
            {"occurred_at": T0 + pd.Timedelta(days=2), "device_id": "d2"},
            {"occurred_at": T0 + pd.Timedelta(days=200), "device_id": "d1"},  # unused for >90 days: new again
            {"occurred_at": T0 + pd.Timedelta(days=201), "channel": "transfer", "payee": "p1"},
            {"occurred_at": T0 + pd.Timedelta(days=202), "channel": "transfer", "payee": "p1"},
            {"occurred_at": T0 + pd.Timedelta(days=203), "channel": "pos", "entry_mode": "chip", "ip_country": None},
        ]
    )
    f = compute_features(df)
    assert f["new_device"].tolist() == [1, 0, 1, 1, 0, 0, 0]
    assert f["has_device"].tolist() == [1, 1, 1, 1, 0, 0, 0]
    assert f["new_payee"].tolist() == [0, 0, 0, 0, 1, 0, 0]
    assert np.isnan(f["ip_mismatch"].iloc[6])  # no IP for in-person: unknown, not "matches"


def test_impossible_travel_speed_and_distance_from_home():
    df = _frame(
        [
            {"occurred_at": T0, "channel": "pos", "lat": 40.71, "lon": -74.01},  # New York (home)
            {"occurred_at": T0 + pd.Timedelta(minutes=30), "channel": "ecommerce"},  # online: ignored for speed
            {"occurred_at": T0 + pd.Timedelta(hours=1), "channel": "pos", "lat": 51.51, "lon": -0.13},  # London
        ]
    )
    f = compute_features(df)
    assert f["km_from_home"].iloc[0] == pytest.approx(0, abs=1)
    assert f["km_from_home"].iloc[2] == pytest.approx(5570, rel=0.02)
    assert np.isnan(f["speed_kmh"].iloc[0]) and np.isnan(f["speed_kmh"].iloc[1])
    assert f["speed_kmh"].iloc[2] == pytest.approx(5570, rel=0.02)  # ~5,570 km in one hour


def test_amount_z_needs_history():
    rows = [{"occurred_at": T0 + pd.Timedelta(days=i), "amount": 20.0 + i} for i in range(6)]
    rows.append({"occurred_at": T0 + pd.Timedelta(days=7), "amount": 2000.0})
    f = compute_features(_frame(rows))
    assert f["amount_z"].iloc[:5].isna().all()  # fewer than 5 prior transactions
    assert f["amount_z"].iloc[-1] > 5
    assert f["amount_ratio_max"].iloc[-1] == pytest.approx(2000 / 25)


@pytest.fixture(scope="module")
def small_sim():
    return generate(SimConfig(n_customers=60, days=120, seed=3, drift_start_day=100))


def test_no_look_ahead(small_sim):
    """Features of a transaction must not change when everything after it is deleted."""
    frame, labels = sim_frame(small_sim)
    cut = frame["occurred_at"].quantile(0.6)
    full = add_features(frame, labels)
    past = frame[frame["occurred_at"] <= cut]
    truncated = add_features(past, labels[labels["reported_at"] <= cut])
    a, b = full.loc[past.index].to_numpy(), truncated.to_numpy()
    assert np.allclose(a, b, equal_nan=True)


def test_merchant_rate_counts_only_reports_before_the_transaction():
    tx = pd.DataFrame(
        {
            "id": [1, 2, 3],
            "merchant_id": [7, 7, 7],
            "occurred_at": pd.to_datetime(["2026-01-01", "2026-01-10", "2026-01-20"], utc=True),
        }
    )
    labels = pd.DataFrame({"transaction_id": [1], "reported_at": pd.to_datetime(["2026-01-15"], utc=True)})
    pit = merchant_fraud_rates(tx, labels)
    leaky = merchant_fraud_rates(tx, labels, leaky=True)
    prior = 0.003 * 200
    assert pit.tolist() == pytest.approx([prior / 200, prior / 201, (1 + prior) / 202])
    assert leaky.iloc[1] > pit.iloc[1]  # the leak "knows" about a report that hasn't happened yet


def test_online_matches_batch(db, small_sim):
    """The online path (PostgreSQL, last 90 days per card) gives exactly the training features."""
    from app.ml.demo import load_sim

    load_sim(db, small_sim)
    db.commit()
    frame = data_access.full_frame(db)
    labels = data_access.labels_frame(db)
    batch = add_features(frame, labels)
    rng = np.random.default_rng(0)
    late = frame.index[frame["occurred_at"] > frame["occurred_at"].quantile(0.5)]
    sample_ids = frame.loc[rng.choice(late, size=150, replace=False), "id"].tolist()
    online_frame, targets = data_access.frame_for_scoring(db, sample_ids)
    online = compute_features(online_frame).loc[targets]
    online.index = online_frame.loc[targets, "id"].to_numpy()
    expected = batch.set_index(frame["id"]).loc[online.index]
    assert np.allclose(online.to_numpy(), expected.to_numpy(), equal_nan=True, rtol=1e-9, atol=1e-9)
