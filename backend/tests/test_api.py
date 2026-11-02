"""End-to-end API tests against a trained model on a small simulated world (real PostgreSQL)."""

import itertools
from datetime import timedelta

import pandas as pd
import pytest
from sqlalchemy import func, select, text

from app.db.models import AuditLog, ModelVersion, TransactionScore
from app.db.session import new_session
from app.jobs.worker import drain
from tests.conftest import WORLD_END, api_key_headers

_ids = itertools.count(1)
LEGIT_TIME = WORLD_END + timedelta(hours=2)


def ext() -> str:
    return f"TEST-{next(_ids):06d}"


def busy_card(world) -> pd.Series:
    """A card with plenty of legitimate online history and a known device."""
    tx = world.sim.transactions
    online = tx[(tx.channel == "ecommerce") & ~tx.is_fraud & tx.device.notna()]
    token = online.card_token.value_counts().index[0]
    return online[online.card_token == token].iloc[-1]


def body_from(row: pd.Series, **over: object) -> dict:
    b = {
        "external_id": ext(),
        "card_token": row.card_token,
        "merchant_ref": row.merchant_ref,
        "amount": f"{row.amount:.2f}",
        "channel": row.channel,
        "entry_mode": row.entry_mode,
        "occurred_at": LEGIT_TIME.isoformat(),
        "device_fingerprint": row.device,
        "ip_country": row.ip_country,
    }
    b.update(over)
    return {k: v for k, v in b.items() if v is not None}


def post(world, body: dict, expect: int = 201) -> dict:
    r = world.client.post("/api/v1/transactions", json=body, headers=world.key)
    assert r.status_code == expect, r.text
    return r.json()


def test_legit_transaction_is_scored_and_idempotent(trained_world):
    w = trained_world
    body = body_from(busy_card(w))
    first = post(w, body)
    assert first["status"] == "created"
    assert first["model"]["version"] == w.model_version
    assert 0 <= first["risk_score"] <= 1
    assert first["recommended_action"] == "allow" and first["risk_band"] in ("low", "medium")
    again = post(w, body, expect=200)
    assert again["status"] == "duplicate" and again["transaction_id"] == first["transaction_id"]
    with new_session() as s:
        n = s.scalar(select(func.count()).where(TransactionScore.transaction_id == first["transaction_id"]))
    assert n == 1  # a retried request is never scored twice


def test_card_testing_burst_is_flagged_and_grouped_into_one_case(trained_world):
    w = trained_world
    row = busy_card(w)
    tiny = w.sim.merchants[w.sim.merchants.category == "digital_goods"].external_ref.iloc[0]
    big = w.sim.merchants[(w.sim.merchants.category == "electronics") & w.sim.merchants.is_online].external_ref.iloc[0]
    t = LEGIT_TIME + timedelta(hours=1)
    common = {"device_fingerprint": "fraudster-device-1", "ip_country": "ZZ"}
    for k in range(5):
        post(
            w,
            body_from(
                row, merchant_ref=tiny, amount="1.00", occurred_at=(t + timedelta(seconds=40 * k)).isoformat(), **common
            ),
        )
    res = post(
        w,
        body_from(row, merchant_ref=big, amount="899.00", occurred_at=(t + timedelta(minutes=5)).isoformat(), **common),
    )
    assert res["recommended_action"] == "review" and res["risk_band"] in ("high", "critical")
    sources = {r["source"] for r in res["reasons"]}
    assert {"model", "rule"} <= sources
    assert any(r.get("rule") == "CARD_TESTING" for r in res["reasons"])
    assert res["case_id"] is not None
    res2 = post(
        w,
        body_from(row, merchant_ref=big, amount="950.00", occurred_at=(t + timedelta(minutes=7)).isoformat(), **common),
    )
    assert res2["case_id"] == res["case_id"]  # one open case per card

    lead = w.auth("lead_analyst")
    case = w.client.get(f"/api/v1/cases/{res['case_id']}", headers=lead).json()
    assert case["status"] == "open" and case["transactions"] >= 2
    assert case["flagged"][-1]["score"]["reasons"]
    assert all("card_token" not in str(t) for t in case["flagged"])  # tokens never sent to the UI


@pytest.mark.parametrize(
    ("override", "fragment"),
    [
        ({"card_token": "4111111111111111"}, "raw card number"),
        ({"card_token": "tok_does_not_exist"}, "unknown card_token"),
        ({"merchant_ref": "M-NOPE"}, "unknown merchant_ref"),
        ({"occurred_at": "2099-01-01T00:00:00Z"}, "future"),
        ({"amount": "-5"}, ""),
        ({"amount": "1.234"}, ""),
        ({"currency": "EUR"}, ""),
        ({"channel": "transfer"}, "payee_ref is required"),
        ({"lat": 10.0}, "lat and lon"),
        ({"name": "Jane Doe"}, ""),  # extra fields (e.g. personal data) are refused
    ],
)
def test_invalid_transactions_are_rejected(trained_world, override, fragment):
    w = trained_world
    r = w.client.post("/api/v1/transactions", json=body_from(busy_card(w), **override), headers=w.key)
    assert r.status_code == 422, r.text
    assert fragment in r.text


def test_machine_clients_need_a_scoped_api_key(trained_world):
    w = trained_world
    body = body_from(busy_card(w))
    assert w.client.post("/api/v1/transactions", json=body).status_code == 401
    assert w.client.post("/api/v1/transactions", json=body, headers=w.auth("admin")).status_code == 401
    labels_only = api_key_headers("labels:write")
    assert w.client.post("/api/v1/transactions", json=body, headers=labels_only).status_code == 403
    admin = w.auth("admin")
    created = w.client.post("/api/v1/api-keys", json={"name": "tmp", "scopes": ["transactions:write"]}, headers=admin)
    assert created.status_code == 201
    key = {"X-API-Key": created.json()["key"]}
    assert "key" not in w.client.get("/api/v1/api-keys", headers=admin).json()[0]
    assert w.client.post("/api/v1/transactions", json=body, headers=key).status_code == 201
    assert w.client.delete(f"/api/v1/api-keys/{created.json()['id']}", headers=admin).status_code == 204
    assert w.client.post("/api/v1/transactions", json=body_from(busy_card(w)), headers=key).status_code == 401


def test_batch_reports_per_item_results_and_errors(trained_world):
    w = trained_world
    row = busy_card(w)
    batch = {"transactions": [body_from(row), body_from(row, merchant_ref="M-UNKNOWN"), body_from(row)]}
    r = w.client.post("/api/v1/transactions:batch", json=batch, headers=w.key)
    assert r.status_code == 200
    out = r.json()
    assert len(out["results"]) == 2 and out["errors"] == [
        {"index": 1, "external_id": batch["transactions"][1]["external_id"], "error": "unknown merchant_ref"}
    ]


def test_chargebacks_become_labels_with_report_time(trained_world):
    w = trained_world
    res = post(w, body_from(busy_card(w)))
    r = w.client.post(
        "/api/v1/labels/chargebacks",
        json={"chargebacks": [{"external_id": res["external_id"]}, {"external_id": "NOPE"}]},
        headers=w.key,
    )
    assert r.json() == {"accepted": 1, "unknown_external_ids": ["NOPE"]}
    detail = w.client.get(f"/api/v1/transactions/{res['transaction_id']}", headers=w.auth("analyst")).json()
    assert detail["labels"][0]["label"] == "fraud" and detail["labels"][0]["source"] == "chargeback"


def test_transaction_search_and_audited_detail(trained_world):
    w = trained_world
    analyst = w.auth("analyst")
    page = w.client.get("/api/v1/transactions", params={"band": ["high", "critical"], "limit": 5}, headers=analyst)
    assert page.status_code == 200
    items = page.json()["items"]
    assert items and all(i["risk_band"] in ("high", "critical") for i in items)
    tid = items[0]["id"]
    detail = w.client.get(f"/api/v1/transactions/{tid}", headers=analyst).json()
    assert detail["score"]["model"]["version"] == w.model_version
    with new_session() as s:
        logged = s.scalar(
            select(func.count()).where(AuditLog.action == "transaction.view", AuditLog.entity_id == str(tid))
        )
    assert logged == 1
    # Auditors may search but not open case/transaction detail (they review the audit trail instead).
    assert w.client.get(f"/api/v1/transactions/{tid}", headers=w.auth("auditor")).status_code == 403


def test_csv_upload_is_validated_ingested_and_scored(trained_world):
    w = trained_world
    row = busy_card(w)
    good = body_from(row)
    lines = [
        "external_id,card_token,merchant_ref,amount,channel,entry_mode,occurred_at,device_fingerprint,ip_country",
        ",".join(
            str(good[k])
            for k in (
                "external_id",
                "card_token",
                "merchant_ref",
                "amount",
                "channel",
                "entry_mode",
                "occurred_at",
                "device_fingerprint",
                "ip_country",
            )
        ),
        f"{ext()},{row.card_token},{row.merchant_ref},abc,ecommerce,online,{LEGIT_TIME.isoformat()},,US",
        f"{ext()},{row.card_token},M-NOPE,5.00,ecommerce,online,{LEGIT_TIME.isoformat()},,US",
    ]
    lead = w.auth("lead_analyst")
    r = w.client.post(
        "/api/v1/ingestion/csv", files={"file": ("tx.csv", "\n".join(lines).encode(), "text/csv")}, headers=lead
    )
    assert r.status_code == 202, r.text
    assert (
        w.client.post("/api/v1/ingestion/csv", files={"file": ("tx.txt", b"x", "text/plain")}, headers=lead).status_code
        == 422
    )
    assert drain() >= 1
    batch = w.client.get(f"/api/v1/ingestion/batches/{r.json()['id']}", headers=lead).json()
    assert batch["status"] == "completed", batch
    assert (batch["rows_total"], batch["rows_accepted"], batch["rows_rejected"]) == (3, 1, 2)
    assert {e["row"] for e in batch["errors"]} == {3, 4}
    found = w.client.get("/api/v1/transactions", params={"external_id": good["external_id"]}, headers=lead).json()
    assert found["items"][0]["risk_band"] is not None


def test_rules_are_versioned_and_recorded_on_scores(trained_world):
    w = trained_world
    admin = w.auth("admin")
    current = w.client.get("/api/v1/rules", headers=admin).json()
    bad = w.client.put("/api/v1/rules", json={"rules": [{"code": "NOPE"}], "comment": "bad"}, headers=admin)
    assert bad.status_code == 422
    rules = [{k: r[k] for k in ("code", "enabled", "min_band", "params")} for r in current["rules"]]
    for r in rules:
        if r["code"] == "BLOCKED_MERCHANT":
            r["params"]["merchant_refs"] = [busy_card(w).merchant_ref]
    r = w.client.put("/api/v1/rules", json={"rules": rules, "comment": "block a merchant"}, headers=admin)
    assert r.status_code == 200 and r.json()["active_version"] == current["active_version"] + 1
    res = post(w, body_from(busy_card(w)))
    assert res["risk_band"] == "critical"
    assert any(x.get("rule") == "BLOCKED_MERCHANT" for x in res["reasons"])
    with new_session() as s:
        sc = s.scalar(select(TransactionScore).where(TransactionScore.transaction_id == res["transaction_id"]))
        assert sc is not None and sc.rule_set_version == current["active_version"] + 1
    # Restore so later tests are unaffected.
    for r in rules:
        if r["code"] == "BLOCKED_MERCHANT":
            r["params"]["merchant_refs"] = []
    w.client.put("/api/v1/rules", json={"rules": rules, "comment": "unblock"}, headers=admin)


def test_risk_settings_validation(trained_world):
    w = trained_world
    admin = w.auth("admin")
    s = w.client.get("/api/v1/settings/risk", headers=admin).json()
    bad = {**s, "threshold_overrides": {"medium": 0.5, "high": 0.2, "critical": 0.9}}
    assert w.client.put("/api/v1/settings/risk", json=bad, headers=admin).status_code == 422
    assert (
        w.client.put("/api/v1/settings/risk", json={**s, "review_sample_rate": 0.5}, headers=admin).status_code == 422
    )
    ok = w.client.put("/api/v1/settings/risk", json={**s, "capacity_per_day": 12}, headers=admin)
    assert ok.status_code == 200 and ok.json()["capacity_per_day"] == 12
    assert w.client.put("/api/v1/settings/risk", json=s, headers=w.auth("lead_analyst")).status_code == 403


def test_analytics_and_reports(trained_world):
    w = trained_world
    lead = w.auth("lead_analyst")
    summary = w.client.get("/api/v1/analytics/summary", headers=lead).json()
    assert summary["last_24h"]["transactions"] >= 1 and summary["cases"]["open"] >= 1
    trends = w.client.get("/api/v1/analytics/trends", params={"days": 60}, headers=lead).json()
    assert trends["daily"] and trends["by_category"] and trends["by_channel"]
    csv_text = w.client.get("/api/v1/reports/cases.csv", headers=lead)
    assert csv_text.status_code == 200 and csv_text.text.startswith("case_id,")
    assert w.client.get("/api/v1/reports/cases.csv", headers=w.auth("analyst")).status_code == 403
    assert w.client.get("/api/v1/reports/flagged.csv", headers=w.auth("auditor")).status_code == 200


def test_monitoring_drift_and_performance(trained_world):
    w = trained_world
    admin = w.auth("admin")
    job = w.client.post("/api/v1/monitoring/drift", params={"days": 14}, headers=admin)
    assert job.status_code == 202
    drain()
    status = w.client.get(f"/api/v1/jobs/{job.json()['job_id']}", headers=admin).json()
    assert status["status"] == "succeeded", status
    reports = w.client.get("/api/v1/monitoring/drift", headers=admin).json()
    assert reports[0]["status"] in ("ok", "warning", "alert") and reports[0]["feature_psi"]
    perf = w.client.get("/api/v1/monitoring/performance", headers=admin).json()
    assert perf["maturity_days"] == 30 and isinstance(perf["weeks"], list)
    assert w.client.get("/api/v1/monitoring/latency", headers=admin).json()["scores_24h"] >= 1


@pytest.mark.slow
def test_train_activate_rollback_and_integrity_check(trained_world):
    w = trained_world
    admin = w.auth("admin")
    job = w.client.post("/api/v1/models/train", json={"capacity_per_day": 10, "backfill_days": 3}, headers=admin)
    assert job.status_code == 202
    drain()
    result = w.client.get(f"/api/v1/jobs/{job.json()['job_id']}", headers=admin).json()
    assert result["status"] == "succeeded", result
    models = w.client.get("/api/v1/models", headers=admin).json()
    assert models[0]["version"] == w.model_version + 1 and models[0]["is_active"]
    full = w.client.get(f"/api/v1/models/{models[0]['id']}", headers=w.auth("auditor")).json()
    assert full["metrics"]["cross_validation"] and full["monitored_features"]
    back = w.client.post("/api/v1/models/rollback", headers=admin)
    assert back.status_code == 200 and back.json()["version"] == w.model_version
    # Tamper with the stored artifact hash: activation must refuse to load it.
    with new_session() as s:
        s.execute(
            text("UPDATE model_versions SET artifact_sha256 = repeat('0', 64) WHERE id = :i"), {"i": models[0]["id"]}
        )
        s.commit()
    from app.ml import registry

    registry._cache.clear()
    assert w.client.post(f"/api/v1/models/{models[0]['id']}/activate", headers=admin).status_code == 503
    with new_session() as s:
        active = s.scalar(select(ModelVersion).where(ModelVersion.is_active))
        assert active is not None and active.version == w.model_version
