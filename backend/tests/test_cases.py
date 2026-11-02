"""Case workflow, permissions, labels from dispositions and the append-only audit trail.

These tests need no trained model: with no active model, deterministic rules alone
still raise the band and open cases.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.db.models import FraudLabel
from app.db.session import new_session
from tests.conftest import api_key_headers

T0 = datetime(2026, 9, 1, 12, tzinfo=UTC)


@pytest.fixture
def setup(client, auth):
    key = api_key_headers("transactions:write", "reference:write")
    client.post(
        "/api/v1/reference/merchants",
        json={
            "merchants": [
                {
                    "external_ref": "M-GAMES",
                    "name": "Games",
                    "mcc": "5815",
                    "category": "digital_goods",
                    "country": "US",
                    "is_online": True,
                },
                {
                    "external_ref": "M-TV",
                    "name": "TV shop",
                    "mcc": "5732",
                    "category": "electronics",
                    "country": "US",
                    "is_online": True,
                },
            ]
        },
        headers=key,
    ).raise_for_status()
    client.post(
        "/api/v1/reference/customers",
        json={
            "customers": [
                {
                    "external_ref": "CUST-1",
                    "home_country": "US",
                    "home_lat": 40.7,
                    "home_lon": -74.0,
                    "cards": [
                        {
                            "token": "tok_card_one",
                            "last4": "1234",
                            "network": "visa",
                            "issued_at": "2024-01-01T00:00:00Z",
                        }
                    ],
                },
            ]
        },
        headers=key,
    ).raise_for_status()
    return key


def flag_card(client, key, prefix: str = "C") -> dict:
    """A card-testing burst: the rules force 'high' and a case opens."""
    for k in range(4):
        client.post(
            "/api/v1/transactions",
            headers=key,
            json={
                "external_id": f"{prefix}-small-{k}",
                "card_token": "tok_card_one",
                "merchant_ref": "M-GAMES",
                "amount": "1.00",
                "channel": "ecommerce",
                "entry_mode": "online",
                "occurred_at": (T0 + timedelta(seconds=30 * k)).isoformat(),
                "device_fingerprint": "dev-x",
            },
        ).raise_for_status()
    r = client.post(
        "/api/v1/transactions",
        headers=key,
        json={
            "external_id": f"{prefix}-big",
            "card_token": "tok_card_one",
            "merchant_ref": "M-TV",
            "amount": "700.00",
            "channel": "ecommerce",
            "entry_mode": "online",
            "occurred_at": (T0 + timedelta(minutes=3)).isoformat(),
            "device_fingerprint": "dev-x",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_rules_alone_open_a_case_when_no_model_is_active(client, setup):
    res = flag_card(client, setup)
    assert res["model"] is None and res["risk_score"] is None
    assert res["risk_band"] == "high" and res["recommended_action"] == "review"
    assert {r["rule"] for r in res["reasons"]} >= {"CARD_TESTING"}
    assert res["case_id"] is not None


def test_full_workflow_and_permissions(client, auth, setup):
    case_id = flag_card(client, setup)["case_id"]
    a1, a2, lead, auditor = auth("analyst", "a1"), auth("analyst", "a2"), auth("lead_analyst"), auth("auditor")
    base = f"/api/v1/cases/{case_id}"

    assert client.post(f"{base}/claim", headers=auditor).status_code == 403
    assert (
        client.post(
            f"{base}/disposition", headers=a1, json={"disposition": "legitimate", "note": "too early to close"}
        ).status_code
        == 409
    )
    assert client.post(f"{base}/claim", headers=a1).json()["status"] == "in_review"
    assert client.post(f"{base}/claim", headers=a2).status_code == 409  # already claimed
    assert client.post(f"{base}/notes", headers=a2, json={"body": "not mine"}).status_code == 403
    assert client.post(f"{base}/notes", headers=a1, json={"body": "Customer called, card in wallet"}).status_code == 201
    pan = client.post(f"{base}/notes", headers=a1, json={"body": "card 4111 1111 1111 1111 was used"})
    assert pan.status_code == 422 and "card number" in pan.json()["detail"]
    assert client.post(f"{base}/status", headers=a1, json={"to": "escalated"}).json()["status"] == "escalated"
    closing = {"disposition": "confirmed_fraud", "note": "Burst of tiny tests then a TV purchase; customer denies."}
    assert client.post(f"{base}/disposition", headers=a1, json=closing).status_code == 403  # escalated: lead only
    done = client.post(f"{base}/disposition", headers=lead, json=closing)
    assert done.status_code == 200 and done.json()["labels_written"] == 1  # only the flagged purchase
    assert client.post(f"{base}/notes", headers=a1, json={"body": "late"}).status_code == 409

    detail = client.get(base, headers=lead).json()
    assert detail["status"] == "closed" and detail["disposition"] == "confirmed_fraud"
    assert [e["event"] for e in detail["events"]] == ["opened", "claimed", "note_added", "escalated", "closed"]
    assert detail["closed_by"] == "lead_analyst@example.com"
    with new_session() as s:
        labels = list(s.scalars(select(FraudLabel)))
    assert {lab.label for lab in labels} == {"fraud"} and {lab.source for lab in labels} == {"analyst"}


def test_new_flagged_activity_after_closure_opens_a_new_case(client, auth, setup):
    first = flag_card(client, setup, "A")["case_id"]
    a1 = auth("analyst")
    client.post(f"/api/v1/cases/{first}/claim", headers=a1)
    client.post(
        f"/api/v1/cases/{first}/disposition",
        headers=a1,
        json={"disposition": "legitimate", "note": "Customer confirmed these purchases."},
    )
    second = flag_card(client, setup, "B")["case_id"]
    assert second != first


def test_disposition_overrides_per_transaction(client, auth, setup):
    res = flag_card(client, setup)
    a1 = auth("analyst")
    base = f"/api/v1/cases/{res['case_id']}"
    client.post(f"{base}/claim", headers=a1)
    bad = client.post(
        f"{base}/disposition",
        headers=a1,
        json={
            "disposition": "confirmed_fraud",
            "note": "Only the TV purchase was fraud.",
            "transaction_labels": {"999999": "legitimate"},
        },
    )
    assert bad.status_code == 422
    ok = client.post(
        f"{base}/disposition",
        headers=a1,
        json={
            "disposition": "inconclusive",
            "note": "Only the TV purchase is confirmed.",
            "transaction_labels": {str(res["transaction_id"]): "fraud"},
        },
    )
    assert ok.json()["labels_written"] == 1


def test_lead_assigns_and_release_returns_case_to_queue(client, auth, setup):
    case_id = flag_card(client, setup)["case_id"]
    lead = auth("lead_analyst")
    analyst = auth("analyst")
    users = client.get("/api/v1/users/directory", headers=lead).json()
    analyst_id = next(u["id"] for u in users if u["role"] == "analyst")
    assert (
        client.post(f"/api/v1/cases/{case_id}/assign", headers=analyst, json={"assignee_id": analyst_id}).status_code
        == 403
    )
    assert (
        client.post(f"/api/v1/cases/{case_id}/assign", headers=lead, json={"assignee_id": analyst_id}).json()["status"]
        == "in_review"
    )
    mine = client.get("/api/v1/cases", params={"assignee": "me"}, headers=analyst).json()
    assert mine["total"] == 1 and mine["items"][0]["assignee"]["id"] == analyst_id
    assert client.post(f"/api/v1/cases/{case_id}/status", headers=analyst, json={"to": "open"}).status_code == 200
    queue = client.get("/api/v1/cases", params={"assignee": "unassigned", "status": ["open"]}, headers=lead).json()
    assert queue["total"] == 1
    bad = client.post(f"/api/v1/cases/{case_id}/status", headers=lead, json={"to": "escalated"})
    assert bad.status_code == 422  # open → escalated is not a valid transition


def test_case_reads_are_audited_and_audit_log_is_append_only(client, auth, setup):
    case_id = flag_card(client, setup)["case_id"]
    client.post(f"/api/v1/cases/{case_id}/claim", headers=auth("analyst"))
    client.post(f"/api/v1/cases/{case_id}/notes", headers=auth("analyst"), json={"body": "checked"})
    client.get(f"/api/v1/cases/{case_id}", headers=auth("analyst"))
    log = client.get("/api/v1/audit-log", params={"action": "case.view"}, headers=auth("auditor")).json()["items"]
    assert log and log[0]["entity_id"] == str(case_id) and log[0]["user"] == "analyst@example.com"
    assert client.get("/api/v1/audit-log", headers=auth("analyst", "a9")).status_code == 403
    with new_session() as s:
        for stmt in ("UPDATE audit_log SET action = 'x'", "DELETE FROM case_events", "UPDATE case_notes SET body = ''"):
            with pytest.raises(DBAPIError, match="append-only"):
                s.execute(text(stmt))
            s.rollback()


def test_reference_data_refuses_personal_fields_and_raw_card_numbers(client, setup):
    r = client.post(
        "/api/v1/reference/customers",
        headers=setup,
        json={"customers": [{"external_ref": "C2", "home_country": "US", "name": "Jane Doe"}]},
    )
    assert r.status_code == 422
    r = client.post(
        "/api/v1/reference/customers",
        headers=setup,
        json={
            "customers": [
                {
                    "external_ref": "C2",
                    "home_country": "US",
                    "cards": [
                        {
                            "token": "5555555555554444",
                            "last4": "4444",
                            "network": "mc",
                            "issued_at": "2024-01-01T00:00:00Z",
                        }
                    ],
                }
            ]
        },
    )
    assert r.status_code == 422 and "raw card number" in r.text
