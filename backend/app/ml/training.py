"""Model training, calibration, thresholds and leakage-safe evaluation.

Every metric reported anywhere in this project is produced by ``evaluate`` below.

Protocol (see ``dataset.time_splits``): models are trained on transactions from
more than 60 days before a deployment date D, using only labels reported by D;
calibrated and thresholded on D−60…D−30 days; and tested on everything after D
against the simulator's full ground truth (including fraud that was never
reported). Model selection uses 5-fold cross-validation grouped by card inside the
training period.

Class imbalance: fraud is well under 1% of transactions. Models use class weights
(not SMOTE), and training keeps every positive but only a 25% sample of negatives,
weighted ×4, which keeps training fast without changing what the model learns.
Probabilities are re-calibrated (Platt scaling) on untouched data afterwards.
"""

from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
import sklearn
import xgboost
from sklearn.ensemble import IsolationForest, RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss, precision_recall_curve, roc_auc_score
from sklearn.model_selection import GroupKFold
from sklearn.pipeline import Pipeline, make_pipeline
from sklearn.preprocessing import StandardScaler
from xgboost import XGBClassifier

from app.ml import rules

logger = logging.getLogger(__name__)

NEG_SAMPLE = 0.25
MODEL_NAMES = ("logistic_regression", "random_forest", "xgboost")
PRODUCTION_CANDIDATES = ("logistic_regression", "xgboost")
DEFAULT_CAPACITY_PER_DAY = 25  # cases analysts can review per day
ANOMALY_FEATURES = (
    "amount_z",
    "amount_ratio_max",
    "log_secs_since_prev",
    "cnt_1h",
    "cnt_24h",
    "hour_familiarity",
    "km_from_home",
    "new_device",
    "new_payee",
    "ip_mismatch",
    "foreign_merchant",
)


# --------------------------------------------------------------------------- models
def make_model(name: str, pos_weight: float, seed: int) -> Any:
    if name == "logistic_regression":
        return make_pipeline(
            SimpleImputer(strategy="median", keep_empty_features=True),
            StandardScaler(),
            LogisticRegression(C=0.1, class_weight={0: 1.0, 1: pos_weight}, max_iter=3000),
        )
    if name == "random_forest":
        return make_pipeline(
            SimpleImputer(strategy="median", keep_empty_features=True),
            RandomForestClassifier(
                n_estimators=200,
                min_samples_leaf=5,
                class_weight={0: 1.0, 1: pos_weight},
                n_jobs=-1,
                random_state=seed,
            ),
        )
    if name == "xgboost":
        return XGBClassifier(
            n_estimators=400,
            learning_rate=0.05,
            max_depth=6,
            subsample=0.8,
            colsample_bytree=0.8,
            min_child_weight=3,
            scale_pos_weight=pos_weight,
            eval_metric="aucpr",
            tree_method="hist",
            n_jobs=4,
            random_state=seed,
        )
    raise ValueError(name)


def _pos_weight(y: np.ndarray, w: np.ndarray) -> float:
    pos = max(float(w[y == 1].sum()), 1.0)
    return float(min(w[y == 0].sum() / pos, 200.0))


def _fit(model: Any, X: pd.DataFrame, y: np.ndarray, w: np.ndarray) -> Any:
    if isinstance(model, Pipeline):
        model.fit(X, y, **{f"{model.steps[-1][0]}__sample_weight": w})
    else:
        model.fit(X, y, sample_weight=w)
    return model


def negative_sample(y: np.ndarray, seed: int) -> tuple[np.ndarray, np.ndarray]:
    """Indices to train on (all positives, NEG_SAMPLE of negatives) and their weights."""
    rng = np.random.default_rng(seed)
    keep = (y == 1) | (rng.random(len(y)) < NEG_SAMPLE)
    idx = np.flatnonzero(keep)
    w = np.where(y[idx] == 1, 1.0, 1.0 / NEG_SAMPLE)
    return idx, w


def rule_baseline_score(X: pd.DataFrame, merchant_refs: pd.Series | None = None) -> np.ndarray:
    """Score = highest band the default rules would force (0–3). Every model must beat this."""
    cfg = rules.default_rule_config()
    cols = X.to_dict("records")
    refs = merchant_refs.to_numpy() if merchant_refs is not None else [None] * len(X)
    return np.array(
        [
            rules.BAND_ORDER[rules.floor_band(rules.evaluate(f, {"merchant_ref": r}, cfg))]
            for f, r in zip(cols, refs, strict=True)
        ],
        dtype=float,
    )


# --------------------------------------------------------------------------- metrics
def ranking_metrics(y: np.ndarray, score: np.ndarray, w: np.ndarray | None = None) -> dict[str, float]:
    if y.sum() == 0 or y.sum() == len(y):
        return {"pr_auc": float("nan"), "roc_auc": float("nan"), "best_f1": float("nan")}
    precision, recall, _ = precision_recall_curve(y, score, sample_weight=w)
    f1 = 2 * precision * recall / np.clip(precision + recall, 1e-12, None)
    return {
        "pr_auc": float(average_precision_score(y, score, sample_weight=w)),
        "roc_auc": float(roc_auc_score(y, score, sample_weight=w)),
        "best_f1": float(f1.max()),
    }


def at_threshold(y: np.ndarray, score: np.ndarray, thr: float, amount: np.ndarray, days: float) -> dict[str, float]:
    flagged = score >= thr
    tp = float((flagged & (y == 1)).sum())
    precision = tp / max(float(flagged.sum()), 1.0)
    recall = tp / max(float(y.sum()), 1.0)
    return {
        "threshold": float(thr),
        "alerts": int(flagged.sum()),
        "alerts_per_day": float(flagged.sum() / max(days, 1e-9)),
        "precision": precision,
        "recall": recall,
        "f1": 2 * precision * recall / max(precision + recall, 1e-12),
        "dollar_recall": float(amount[flagged & (y == 1)].sum() / max(amount[y == 1].sum(), 1e-9)),
    }


def recall_at_capacity(
    y: np.ndarray, score: np.ndarray, amount: np.ndarray, capacity: float, days: float
) -> dict[str, float]:
    """If analysts can review ``capacity`` transactions a day, what do the top-ranked ones catch?"""
    k = round(capacity * days)
    order = np.argsort(-score, kind="mergesort")[:k]
    hit = y[order] == 1
    return {
        "capacity_per_day": capacity,
        "reviewed": k,
        "precision": float(hit.mean()) if k else 0.0,
        "recall": float(hit.sum() / max(y.sum(), 1)),
        "dollar_recall": float(amount[order][hit].sum() / max(amount[y == 1].sum(), 1e-9)),
    }


def reliability_table(
    y: np.ndarray, p: np.ndarray, edges: tuple[float, ...] = (0, 0.01, 0.05, 0.1, 0.2, 0.4, 0.6, 0.8, 1.0001)
) -> list[dict[str, float]]:
    out = []
    for lo, hi in itertools.pairwise(edges):
        m = (p >= lo) & (p < hi)
        if m.sum():
            out.append(
                {
                    "bin_low": lo,
                    "bin_high": min(hi, 1.0),
                    "count": int(m.sum()),
                    "mean_predicted": float(p[m].mean()),
                    "observed_rate": float(y[m].mean()),
                }
            )
    return out


def psi(expected: np.ndarray, actual: np.ndarray, edges: np.ndarray) -> float:
    """Population Stability Index between two samples.

    ``edges`` are the *interior* bin edges (from ``psi_edges``); the outer bins are open
    (−∞, +∞) and missing values (NaN) get a bin of their own, because a jump in missing
    values is drift too.
    """
    bins = np.concatenate([[-np.inf], np.asarray(edges, dtype=float), [np.inf]])

    def dist(x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=float)
        nan = np.isnan(x)
        counts = np.histogram(x[~nan], bins=bins)[0].astype(float)
        counts = np.append(counts, nan.sum())
        return (counts + 0.5) / (counts.sum() + 0.5 * len(counts))

    e, a = dist(expected), dist(actual)
    return float(np.sum((a - e) * np.log(a / e)))


def psi_edges(x: np.ndarray, bins: int = 10) -> np.ndarray:
    """Interior decile edges of the non-missing values (JSON-safe: no infinities)."""
    x = np.asarray(x, dtype=float)
    x = x[~np.isnan(x)]
    if not len(x):
        return np.array([])
    q = np.unique(np.quantile(x, np.linspace(0, 1, bins + 1)))
    return q[1:-1] if len(q) > 2 else q[:1] + 1e-9


# --------------------------------------------------------------------------- explainability
def contributions(model: Any, X: pd.DataFrame) -> np.ndarray:
    """Exact SHAP values in log-odds space, shape (n_rows, n_features).

    * XGBoost: TreeSHAP computed by XGBoost itself (``pred_contribs=True``), the same
      algorithm as ``shap.TreeExplainer`` (``tests/test_ml.py`` checks they agree).
    * Logistic regression: for a linear model on standardised inputs, the SHAP value
      of feature i is ``coef_i × z_i`` (``z_i`` has training mean 0).
    """
    if isinstance(model, XGBClassifier):
        dm = xgboost.DMatrix(X, feature_names=list(X.columns))
        return np.asarray(model.get_booster().predict(dm, pred_contribs=True))[:, :-1]
    if isinstance(model, Pipeline) and isinstance(model[-1], LogisticRegression):
        z = model[:-1].transform(X)
        return np.asarray(z * model[-1].coef_[0])
    raise TypeError(f"no SHAP implementation for {type(model).__name__}")


def global_importance(model: Any, X: pd.DataFrame, top: int = 15) -> list[dict[str, float | str]]:
    imp = np.abs(contributions(model, X)).mean(axis=0)
    order = np.argsort(imp)[::-1][:top]
    return [{"feature": X.columns[i], "mean_abs_shap": float(imp[i])} for i in order]


# --------------------------------------------------------------------------- production bundle
def _logit(p: np.ndarray) -> np.ndarray:
    p = np.clip(np.asarray(p, dtype=float), 1e-7, 1 - 1e-7)
    return np.log(p / (1 - p)).reshape(-1, 1)


@dataclass
class PlattCalibrator:
    """Sigmoid (Platt) calibration on the model's log-odds.

    Chosen over isotonic regression because the calibration window holds only a few
    hundred frauds: isotonic regression then produces a staircase with large ties
    (many transactions get the identical probability), which makes band thresholds
    coarse. Platt scaling is smooth and monotone, so the ranking is preserved exactly.
    """

    lr: LogisticRegression

    @classmethod
    def fit(cls, raw: np.ndarray, y: np.ndarray) -> PlattCalibrator:
        return cls(LogisticRegression(C=1e6, max_iter=1000).fit(_logit(raw), y))

    def predict(self, raw: np.ndarray) -> np.ndarray:
        return np.asarray(self.lr.predict_proba(_logit(raw))[:, 1])


@dataclass
class CalibratedClassifier:
    model: Any
    calibrator: PlattCalibrator

    def raw(self, X: pd.DataFrame) -> np.ndarray:
        return np.asarray(self.model.predict_proba(X)[:, 1])

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        return np.clip(self.calibrator.predict(self.raw(X)), 0.0, 1.0)


@dataclass
class AnomalyDetector:
    """Isolation Forest on behavioural features; output is a 0–1 percentile vs. training data."""

    imputer: SimpleImputer
    forest: IsolationForest
    reference: np.ndarray  # sorted training anomaly scores

    def score(self, X: pd.DataFrame) -> np.ndarray:
        s = -self.forest.score_samples(self.imputer.transform(X[list(ANOMALY_FEATURES)]))
        return np.searchsorted(self.reference, s, side="right") / len(self.reference)


def fit_anomaly(X: pd.DataFrame, seed: int, max_rows: int = 100_000) -> AnomalyDetector:
    rng = np.random.default_rng(seed)
    idx = rng.choice(len(X), size=min(len(X), max_rows), replace=False)
    Xa = X.iloc[idx][list(ANOMALY_FEATURES)]
    imp = SimpleImputer(strategy="median", keep_empty_features=True).fit(Xa)
    forest = IsolationForest(n_estimators=200, random_state=seed, n_jobs=-1).fit(imp.transform(Xa))
    ref = np.sort(-forest.score_samples(imp.transform(Xa)))
    return AnomalyDetector(imp, forest, ref)


@dataclass
class ModelBundle:
    algorithm: str
    classifier: CalibratedClassifier
    anomaly: AnomalyDetector
    feature_names: tuple[str, ...]
    thresholds: dict[str, float]
    reference: dict[str, Any]  # drift reference: per-feature PSI bin edges and the calibration sample
    trained_on: dict[str, Any]
    library_versions: dict[str, str]


def choose_thresholds(p_calib: np.ndarray, days: float, capacity: float) -> dict[str, float]:
    """Band thresholds on calibrated probability, from analyst capacity.

    * high:     the score above which ≈ ``capacity`` transactions per day are flagged
    * medium:   ≈ 3× that volume (watch-list, not reviewed)
    * critical: probability ≥ 0.5 (more likely fraud than not), and never below high
    """
    n = len(p_calib)

    def q(per_day: float) -> float:
        k = int(np.clip(round(per_day * days), 1, n))
        return float(np.sort(p_calib)[::-1][k - 1])

    high = max(q(capacity), 1e-6)
    medium = min(max(q(capacity * 3), 1e-6), high)
    critical = max(high, 0.5)
    return {"medium": medium, "high": high, "critical": critical, "capacity_per_day": capacity}


def band_of(p: np.ndarray, thr: dict[str, float]) -> np.ndarray:
    out = np.full(len(p), "low", dtype=object)
    out[p >= thr["medium"]] = "medium"
    out[p >= thr["high"]] = "high"
    out[p >= thr["critical"]] = "critical"
    return out


def _json_list(x: np.ndarray | pd.Series) -> list[float | None]:
    return [None if np.isnan(v) else float(v) for v in np.asarray(x, dtype=float)]


def drift_reference(X_ref: pd.DataFrame, p_ref: np.ndarray, features: list[str], n: int = 2000) -> dict[str, Any]:
    """What "normal" looked like at training time, stored with the model for drift checks.
    JSON-safe: missing values are stored as null."""
    rows = X_ref.sample(n=min(len(X_ref), n), random_state=0)
    scores = pd.Series(p_ref).sample(n=min(len(p_ref), n), random_state=0)
    return {
        "features": {f: psi_edges(X_ref[f].to_numpy()).tolist() for f in features},
        "score_edges": psi_edges(p_ref).tolist(),
        "sample": {f: _json_list(rows[f]) for f in features},
        "score_sample": _json_list(scores),
    }


def train_bundle(
    X: pd.DataFrame,
    y: np.ndarray,
    train: np.ndarray,
    calib: np.ndarray,
    *,
    algorithm: str,
    calib_days: float,
    capacity: float = DEFAULT_CAPACITY_PER_DAY,
    seed: int = 42,
    trained_on: dict[str, Any] | None = None,
) -> ModelBundle:
    if algorithm not in PRODUCTION_CANDIDATES:
        raise ValueError(f"production algorithm must be one of {PRODUCTION_CANDIDATES}")
    Xtr, ytr = X[train], y[train]
    idx, w = negative_sample(ytr, seed)
    model = _fit(make_model(algorithm, _pos_weight(ytr[idx], w), seed), Xtr.iloc[idx], ytr[idx], w)
    Xc, yc = X[calib], y[calib]
    raw = np.asarray(model.predict_proba(Xc)[:, 1])
    cal = PlattCalibrator.fit(raw, yc)
    clf = CalibratedClassifier(model, cal)
    p_cal = clf.predict_proba(Xc)
    thresholds = choose_thresholds(p_cal, calib_days, capacity)
    top = [
        str(f["feature"])
        for f in global_importance(model, Xc.sample(n=min(len(Xc), 20_000), random_state=seed), top=12)
    ]
    return ModelBundle(
        algorithm=algorithm,
        classifier=clf,
        anomaly=fit_anomaly(Xtr[ytr == 0], seed),
        feature_names=tuple(X.columns),
        thresholds=thresholds,
        reference=drift_reference(Xc, p_cal, top),
        trained_on=trained_on or {},
        library_versions={"xgboost": xgboost.__version__, "scikit-learn": sklearn.__version__},
    )


# --------------------------------------------------------------------------- evaluation
def cross_validate(X: pd.DataFrame, y: np.ndarray, groups: np.ndarray, seed: int, folds: int = 5) -> dict[str, Any]:
    idx_all, w_all = negative_sample(y, seed)
    Xs, ys, gs = X.iloc[idx_all], y[idx_all], groups[idx_all]
    out: dict[str, Any] = {}
    for name in MODEL_NAMES:
        scores = []
        for tr, te in GroupKFold(n_splits=folds).split(Xs, ys, gs):
            m = _fit(make_model(name, _pos_weight(ys[tr], w_all[tr]), seed), Xs.iloc[tr], ys[tr], w_all[tr])
            p = m.predict_proba(Xs.iloc[te])[:, 1]
            scores.append(average_precision_score(ys[te], p, sample_weight=w_all[te]))
        out[name] = {
            "pr_auc_mean": float(np.mean(scores)),
            "pr_auc_std": float(np.std(scores)),
            "folds": [float(s) for s in scores],
        }
        logger.info("cv %s pr_auc=%.3f", name, np.mean(scores))
    return out


def evaluate(
    frame: pd.DataFrame,
    X: pd.DataFrame,
    labels: pd.DataFrame,
    deploy_at: pd.Timestamp,
    *,
    capacity: float = DEFAULT_CAPACITY_PER_DAY,
    seed: int = 42,
    X_leaky: pd.DataFrame | None = None,
) -> dict[str, Any]:
    """Full evaluation report (dict). ``frame`` must include ground truth ``is_fraud``."""
    from app.ml.dataset import CALIB_GAP_DAYS, TRAIN_GAP_DAYS, labels_as_of, time_splits

    sp = time_splits(frame, deploy_at)
    y_known = labels_as_of(frame, labels, deploy_at)  # what was known when the model was built
    truth = frame["is_fraud"].to_numpy(dtype=np.int8)
    amount = frame["amount"].to_numpy(dtype=float)
    t = frame["occurred_at"]
    test_days = float((t[sp.test].max() - deploy_at).total_seconds() / 86400) or 1.0
    calib_days = float(TRAIN_GAP_DAYS - CALIB_GAP_DAYS)

    report: dict[str, Any] = {"splits": sp.describe(frame), "capacity_per_day": capacity, "seed": seed}
    report["label_quality"] = {
        "train_true_fraud": int(truth[sp.train].sum()),
        "train_known_fraud_at_deploy": int(y_known[sp.train].sum()),
        "train_known_but_not_fraud": int(((y_known == 1) & (truth == 0))[sp.train].sum()),
        "test_true_fraud": int(truth[sp.test].sum()),
        "test_fraud_rate": float(truth[sp.test].mean()),
    }

    report["cross_validation"] = cross_validate(
        X[sp.train], y_known[sp.train], frame["card_id"].to_numpy()[sp.train], seed
    )

    Xte, yte, ate = X[sp.test], truth[sp.test], amount[sp.test]
    models: dict[str, Any] = {}
    fitted: dict[str, Any] = {}
    Xtr, ytr = X[sp.train], y_known[sp.train]
    idx, w = negative_sample(ytr, seed)
    for name in MODEL_NAMES:
        m = _fit(make_model(name, _pos_weight(ytr[idx], w), seed), Xtr.iloc[idx], ytr[idx], w)
        fitted[name] = m
        s = m.predict_proba(Xte)[:, 1]
        models[name] = {**ranking_metrics(yte, s), "at_capacity": recall_at_capacity(yte, s, ate, capacity, test_days)}
    rb = rule_baseline_score(Xte, frame.loc[sp.test, "merchant_ref"])
    models["rules_baseline"] = {
        **ranking_metrics(yte, rb),
        "at_threshold_high": at_threshold(yte, rb, rules.BAND_ORDER["high"], ate, test_days),
    }
    report["test_models"] = models

    best = max(PRODUCTION_CANDIDATES, key=lambda n: report["cross_validation"][n]["pr_auc_mean"])
    report["production_algorithm"] = best
    bundle = train_bundle(
        X, y_known, sp.train, sp.calib, algorithm=best, calib_days=calib_days, capacity=capacity, seed=seed
    )
    p = bundle.classifier.predict_proba(Xte)
    thr = bundle.thresholds
    prod: dict[str, Any] = {
        **ranking_metrics(yte, p),
        "brier": float(brier_score_loss(yte, p)),
        "thresholds": thr,
        "at_high": at_threshold(yte, p, thr["high"], ate, test_days),
        "at_critical": at_threshold(yte, p, thr["critical"], ate, test_days),
        "at_capacity": recall_at_capacity(yte, p, ate, capacity, test_days),
        "reliability": reliability_table(yte, p),
    }
    # Final band = max(model band, rule floor), as in production.
    rule_floor = np.array([rules.BANDS[int(v)] for v in rb], dtype=object)
    final = np.array([rules.max_band(a, b) for a, b in zip(band_of(p, thr), rule_floor, strict=True)])
    reviewed = np.isin(final, ["high", "critical"])
    prod["with_rules"] = {
        "alerts_per_day": float(reviewed.sum() / test_days),
        "precision": float(yte[reviewed].mean()) if reviewed.any() else 0.0,
        "recall": float(yte[reviewed].sum() / max(yte.sum(), 1)),
        "dollar_recall": float(ate[reviewed & (yte == 1)].sum() / max(ate[yte == 1].sum(), 1e-9)),
    }
    scen = frame.loc[sp.test, "scenario"].to_numpy()
    prod["recall_by_scenario"] = {
        s: {
            "fraud_transactions": int((scen == s).sum()),
            "recall_model_high": float((p[scen == s] >= thr["high"]).mean()),
            "recall_with_rules": float(reviewed[scen == s].mean()),
        }
        for s in sorted(set(scen) - {"legit"})
    }
    weeks = t[sp.test].dt.to_period("W").astype(str).to_numpy()
    prod["weekly"] = [
        {
            "week": wk,
            "transactions": int(m.sum()),
            "fraud": int(yte[m].sum()),
            **ranking_metrics(yte[m], p[m]),
            "alert_precision": float(yte[m & reviewed].mean()) if (m & reviewed).any() else None,
        }
        for wk in sorted(set(weeks))
        if (m := weeks == wk).sum()
    ]
    imp_sample = Xte.sample(n=min(len(Xte), 20_000), random_state=seed)
    prod["global_importance"] = global_importance(bundle.classifier.model, imp_sample)
    report["production"] = prod

    an = bundle.anomaly.score(Xte)
    dc = scen == "device_compromise"
    report["anomaly"] = {
        **ranking_metrics(yte, an),
        "device_compromise_vs_legit_pr_auc": ranking_metrics(yte[dc | (yte == 0)], an[dc | (yte == 0)])["pr_auc"]
        if dc.any()
        else None,
        "model_device_compromise_vs_legit_pr_auc": ranking_metrics(yte[dc | (yte == 0)], p[dc | (yte == 0)])["pr_auc"]
        if dc.any()
        else None,
    }

    months = t[sp.test].dt.to_period("M").astype(str).to_numpy()
    ref = bundle.reference
    drift: list[dict[str, Any]] = []
    for mo in sorted(set(months)):
        m = months == mo
        feats = {
            f: psi(np.array(ref["sample"][f]), Xte.loc[m, f].to_numpy(), np.array(ref["features"][f]))
            for f in ref["features"]
        }
        drift.append(
            {
                "month": mo,
                "score_psi": psi(np.array(ref["score_sample"]), p[m], np.array(ref["score_edges"])),
                "max_feature_psi": max(feats.values()),
                "feature_psi": feats,
            }
        )
    cat_cols = [c for c in X.columns if c.startswith("cat_")]
    report["drift"] = {
        "monitored_features": list(ref["features"]),
        "by_month": drift,
        "category_mix_psi_by_month": {
            mo: psi_categorical(X[sp.calib][cat_cols], Xte.loc[months == mo, cat_cols]) for mo in sorted(set(months))
        },
    }

    # Validation pitfalls: what a careless setup would have reported.
    pit: dict[str, Any] = {}
    rng = np.random.default_rng(seed)
    rand_test = rng.random(len(frame)) < 0.25
    ridx, rw = negative_sample(truth[~rand_test], seed)
    Xr = X[~rand_test]
    mr = _fit(
        make_model(best, _pos_weight(truth[~rand_test][ridx], rw), seed), Xr.iloc[ridx], truth[~rand_test][ridx], rw
    )
    pit["random_split_with_ground_truth_pr_auc"] = ranking_metrics(
        truth[rand_test], mr.predict_proba(X[rand_test])[:, 1]
    )["pr_auc"]
    if X_leaky is not None:
        ml = _fit(make_model(best, _pos_weight(ytr[idx], w), seed), X_leaky[sp.train].iloc[idx], ytr[idx], w)
        pit["leaky_merchant_rate_test_pr_auc"] = ranking_metrics(yte, ml.predict_proba(X_leaky[sp.test])[:, 1])[
            "pr_auc"
        ]
    pit["point_in_time_test_pr_auc"] = models[best]["pr_auc"]
    report["validation_pitfalls"] = pit
    return report


def psi_categorical(ref: pd.DataFrame, cur: pd.DataFrame) -> float:
    e = (ref.sum().to_numpy() + 0.5) / (ref.to_numpy().sum() + 0.5 * ref.shape[1])
    a = (cur.sum().to_numpy() + 0.5) / (cur.to_numpy().sum() + 0.5 * cur.shape[1])
    return float(np.sum((a - e) * np.log(a / e)))
