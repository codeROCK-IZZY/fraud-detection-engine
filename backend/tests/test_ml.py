"""ML building blocks: generator, labels, splits, SHAP, calibration, thresholds, PSI, rules, explanations."""

import math

import numpy as np
import pandas as pd
import pytest

from app.ml import explain, rules
from app.ml.dataset import add_features, labels_as_of, sim_frame, time_splits
from app.ml.features import FEATURE_NAMES
from app.ml.synthetic import SCENARIOS, SimConfig, generate
from app.ml.training import (
    PlattCalibrator,
    _fit,
    band_of,
    choose_thresholds,
    contributions,
    evaluate,
    make_model,
    negative_sample,
    psi,
    psi_edges,
    recall_at_capacity,
)


@pytest.fixture(scope="module")
def sim():
    return generate(SimConfig(n_customers=120, days=200, seed=5, drift_start_day=160))


@pytest.fixture(scope="module")
def data(sim):
    frame, labels = sim_frame(sim)
    return frame, labels, add_features(frame, labels)


# ----------------------------------------------------------------------------- synthetic data
def test_generator_is_deterministic_and_realistic(sim):
    again = generate(SimConfig(n_customers=120, days=200, seed=5, drift_start_day=160))
    pd.testing.assert_frame_equal(sim.transactions, again.transactions)
    tx = sim.transactions
    assert 0.0005 < tx.is_fraud.mean() < 0.02  # rare, as in reality
    assert set(tx[tx.is_fraud].scenario) == set(SCENARIOS)
    drift_start = pd.Timestamp(sim.config.start) + pd.Timedelta(days=160)
    assert (tx[tx.scenario == "device_compromise"].occurred_at >= drift_start).all()
    assert tx.external_id.is_unique and tx.occurred_at.is_monotonic_increasing


def test_labels_come_after_the_transaction_and_some_fraud_is_never_reported(sim):
    tx = sim.transactions.set_index("external_id")
    lab = sim.labels
    assert (lab.reported_at.to_numpy() > tx.loc[lab.external_id, "occurred_at"].to_numpy()).all()
    reported = set(lab.external_id)
    fraud = set(tx.index[tx.is_fraud])
    assert fraud - reported  # hidden positives exist
    assert reported - fraud  # and some legitimate transactions were disputed (label noise)


def test_no_protected_attributes_in_customers(sim):
    assert set(sim.customers.columns) == {"external_ref", "home_city", "home_country", "home_lat", "home_lon"}


# ----------------------------------------------------------------------------- labels & splits
def test_labels_as_of_uses_latest_label_reported_by_then():
    frame = pd.DataFrame({"id": [1, 2, 3]})
    labels = pd.DataFrame(
        {
            "transaction_id": [1, 2, 2, 3],
            "label": ["fraud", "fraud", "legitimate", "fraud"],
            "reported_at": pd.to_datetime(["2026-01-05", "2026-01-05", "2026-01-08", "2026-02-01"], utc=True),
        }
    )
    assert labels_as_of(frame, labels, pd.Timestamp("2026-01-06", tz="UTC")).tolist() == [1, 1, 0]
    assert labels_as_of(frame, labels, pd.Timestamp("2026-01-10", tz="UTC")).tolist() == [1, 0, 0]  # overturned
    assert labels_as_of(frame, labels, pd.Timestamp("2026-03-01", tz="UTC")).tolist() == [1, 0, 1]


def test_time_splits_leave_a_label_maturity_gap(data):
    frame, _, _ = data
    d = frame["occurred_at"].max() - pd.Timedelta(days=40)
    sp = time_splits(frame, d)
    t = frame["occurred_at"]
    assert t[sp.train].max() < d - pd.Timedelta(days=60)
    assert t[sp.calib].min() >= d - pd.Timedelta(days=60) and t[sp.calib].max() < d - pd.Timedelta(days=30)
    assert t[sp.test].min() >= d
    gap = (t >= d - pd.Timedelta(days=30)) & (t < d)
    assert not (sp.train | sp.calib | sp.test)[gap.to_numpy()].any()


# ----------------------------------------------------------------------------- models & explanations
@pytest.mark.parametrize("algorithm", ["xgboost", "logistic_regression"])
def test_contributions_match_shap_library_and_sum_to_log_odds(data, algorithm):
    import shap

    frame, _labels, X = data
    y = frame["is_fraud"].to_numpy(dtype=int)
    idx, w = negative_sample(y, 0)
    model = _fit(make_model(algorithm, 50.0, 0), X.iloc[idx], y[idx], w)
    sample = X.iloc[:300]
    ours = contributions(model, sample)
    if algorithm == "xgboost":
        theirs = shap.TreeExplainer(model).shap_values(sample)
        assert np.allclose(ours, theirs, atol=1e-4)
        margin = model.predict(sample, output_margin=True)
    else:
        # Reference point = the scaler's training mean (z = 0), which is what our formula uses.
        zero = shap.maskers.Independent(np.zeros((1, X.shape[1])))
        theirs = shap.LinearExplainer(model[-1], zero).shap_values(model[:-1].transform(sample))
        assert np.allclose(ours, theirs, atol=1e-6)
        margin = model.decision_function(sample)
        # Linear SHAP is relative to the mean prediction; differences must still add up.
    base = margin - ours.sum(axis=1)
    assert np.allclose(base, base[0], atol=1e-3)


def test_platt_calibration_is_monotone_and_preserves_ranking():
    rng = np.random.default_rng(0)
    raw = rng.uniform(0, 1, 5000)
    y = (rng.uniform(0, 1, 5000) < raw**3).astype(int)
    cal = PlattCalibrator.fit(raw, y)
    p = cal.predict(np.sort(raw))
    assert np.all(np.diff(p) >= -1e-12)
    assert abs(p.mean() - y.mean()) < 0.05


def test_thresholds_follow_analyst_capacity():
    rng = np.random.default_rng(1)
    p = rng.beta(0.5, 50, 30 * 1000)  # 30 days × 1,000 transactions/day
    thr = choose_thresholds(p, days=30, capacity=20)
    per_day = (p >= thr["high"]).sum() / 30
    assert per_day == pytest.approx(20, abs=1)
    assert thr["medium"] <= thr["high"] <= thr["critical"]
    bands = band_of(np.array([0.0, thr["medium"], thr["high"], 0.99]), thr)
    assert bands.tolist() == ["low", "medium", "high", "critical"]


def test_recall_at_capacity():
    y = np.array([1, 0, 1, 0, 0, 1])
    score = np.array([0.9, 0.8, 0.7, 0.1, 0.2, 0.05])
    amount = np.array([100.0, 1, 50, 1, 1, 10])
    r = recall_at_capacity(y, score, amount, capacity=3, days=1)
    assert r["recall"] == pytest.approx(2 / 3) and r["precision"] == pytest.approx(2 / 3)
    assert r["dollar_recall"] == pytest.approx(150 / 160)


def test_psi_detects_shift_and_ignores_noise():
    rng = np.random.default_rng(2)
    ref = rng.normal(0, 1, 5000)
    edges = psi_edges(ref)
    assert psi(ref, rng.normal(0, 1, 5000), edges) < 0.02
    assert psi(ref, rng.normal(1.5, 1, 5000), edges) > 0.25
    with_nan = np.where(rng.random(5000) < 0.5, np.nan, rng.normal(0, 1, 5000))
    assert psi(ref, with_nan, edges) > 0.25  # a jump in missing values is drift too


def test_explanations_group_by_family_and_skip_missing_values():
    names = list(FEATURE_NAMES)
    values = [float("nan")] * len(names)
    contrib = [0.0] * len(names)

    def setv(n: str, v: float, c: float) -> None:
        values[names.index(n)] = v
        contrib[names.index(n)] = c

    setv("cnt_10m", 6, 1.2)
    setv("cnt_1h", 7, -0.3)  # offsets within the velocity family are summed
    setv("km_from_home", float("nan"), 2.0)  # missing → no sentence from this feature
    setv("ip_mismatch", 1, 0.4)
    setv("amount", 900, 0.5)
    out = explain.top_reasons(names, values, contrib)
    signals = [r["signal"] for r in out]
    assert signals[0] == "location" and out[0]["feature"] == "ip_mismatch"
    vel = next(r for r in out if r["signal"] == "velocity")
    assert vel["contribution"] == pytest.approx(0.9) and "10 minutes" in vel["text"]
    assert all(r["source"] == "model" for r in out)
    assert explain.describe("amount", math.nan, {}) is None


# ----------------------------------------------------------------------------- rules
def _f(**kw: float) -> dict[str, float | None]:
    return {n: 0.0 for n in FEATURE_NAMES} | kw


def test_rules_fire_and_only_raise_the_band():
    cfg = rules.default_rule_config()
    fired = rules.evaluate(_f(small_cnt_10m=4, amount=500, cnt_10m=6), {}, cfg)
    assert {f["rule"] for f in fired} == {"CARD_TESTING", "VELOCITY_BURST"}
    assert all(f["source"] == "rule" for f in fired)
    assert rules.floor_band(fired) == "high"
    assert rules.max_band("critical", rules.floor_band(fired)) == "critical"
    assert rules.max_band("low", "low") == "low"
    assert rules.evaluate(_f(speed_kmh=None), {}, cfg) == []  # missing value never fires a rule
    assert rules.evaluate(_f(), {"merchant_ref": "M-1"}, cfg) == []
    blocked = [dict(r) for r in cfg]
    for r in blocked:
        if r["code"] == "BLOCKED_MERCHANT":
            r["params"] = {"merchant_refs": ["M-1"]}
    assert rules.floor_band(rules.evaluate(_f(), {"merchant_ref": "M-1"}, blocked)) == "critical"


def test_disabled_rules_do_not_fire():
    cfg = [{**r, "enabled": r["code"] != "VELOCITY_BURST"} for r in rules.default_rule_config()]
    assert rules.evaluate(_f(cnt_10m=10), {}, cfg) == []


@pytest.mark.parametrize(
    "bad",
    [
        [{"code": "NOPE"}],
        [{"code": "VELOCITY_BURST", "min_band": "low"}],
        [{"code": "VELOCITY_BURST", "params": {"unknown": 1}}],
        [{"code": "VELOCITY_BURST", "params": {"min_count_10m": -1}}],
        [{"code": "BLOCKED_MERCHANT", "params": {"merchant_refs": "M-1"}}],
        [{"code": "VELOCITY_BURST"}, {"code": "VELOCITY_BURST"}],
    ],
)
def test_rule_config_validation(bad):
    with pytest.raises(ValueError):
        rules.validate_rule_config(bad)


# ----------------------------------------------------------------------------- evaluation
@pytest.mark.slow
def test_evaluation_report_is_complete(data):
    frame, labels, X = data
    deploy = frame["occurred_at"].max() - pd.Timedelta(days=45)
    r = evaluate(frame, X, labels, deploy, capacity=5, seed=0)
    assert set(r["test_models"]) == {"logistic_regression", "random_forest", "xgboost", "rules_baseline"}
    for m in r["test_models"].values():
        assert 0 <= m["pr_auc"] <= 1
    prod = r["production"]
    assert prod["thresholds"]["medium"] <= prod["thresholds"]["high"]
    assert set(prod["recall_by_scenario"]) <= set(SCENARIOS)
    assert r["validation_pitfalls"]["random_split_with_ground_truth_pr_auc"] >= 0
    assert r["drift"]["by_month"]


@pytest.mark.parametrize("value", [0.0, 1.0, 95.0])
def test_every_template_returns_a_sentence(value):
    for name in FEATURE_NAMES:
        text = explain.describe(name, value, {})
        assert text is None or (isinstance(text, str) and text), name
