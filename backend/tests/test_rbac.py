"""Role × endpoint matrix: every protected endpoint refuses roles it shouldn't serve.

Expected codes are what the *guard* returns; 2xx/404/422 all mean "let through".
"""

import pytest

ROLES = ("admin", "lead_analyst", "analyst", "auditor")

# (method, path, body, roles allowed)
MATRIX = [
    ("GET", "/api/v1/users", None, {"admin"}),
    ("POST", "/api/v1/users", {"email": "n@example.com", "password": "x" * 12, "role": "analyst"}, {"admin"}),
    ("GET", "/api/v1/api-keys", None, {"admin"}),
    ("GET", "/api/v1/transactions", None, set(ROLES)),
    ("GET", "/api/v1/transactions/1", None, {"admin", "lead_analyst", "analyst"}),
    ("GET", "/api/v1/cards/1", None, {"admin", "lead_analyst", "analyst"}),
    ("GET", "/api/v1/cases", None, set(ROLES)),
    ("GET", "/api/v1/cases/1", None, {"admin", "lead_analyst", "analyst"}),
    ("POST", "/api/v1/cases/1/claim", None, {"admin", "lead_analyst", "analyst"}),
    (
        "POST",
        "/api/v1/cases/1/assign",
        {"assignee_id": "00000000-0000-0000-0000-000000000000"},
        {"admin", "lead_analyst"},
    ),
    ("GET", "/api/v1/models", None, set(ROLES)),
    ("POST", "/api/v1/models/train", {}, {"admin"}),
    ("POST", "/api/v1/models/rollback", None, {"admin"}),
    ("GET", "/api/v1/monitoring/performance", None, {"admin", "lead_analyst"}),
    ("POST", "/api/v1/monitoring/drift", None, {"admin"}),
    ("GET", "/api/v1/settings/risk", None, set(ROLES)),
    ("GET", "/api/v1/rules", None, set(ROLES)),
    ("PUT", "/api/v1/rules", {"rules": [{"code": "VELOCITY_BURST"}], "comment": "abc"}, {"admin"}),
    ("GET", "/api/v1/analytics/summary", None, set(ROLES)),
    ("GET", "/api/v1/reports/cases.csv", None, {"admin", "lead_analyst"}),
    ("GET", "/api/v1/reports/flagged.csv", None, {"admin", "auditor"}),
    ("GET", "/api/v1/audit-log", None, {"admin", "auditor"}),
    ("GET", "/api/v1/ingestion/batches", None, {"admin", "lead_analyst"}),
    ("GET", "/api/v1/system/info", None, set(ROLES)),
]


@pytest.mark.parametrize(("method", "path", "body", "allowed"), MATRIX, ids=[f"{m} {p}" for m, p, _, _ in MATRIX])
def test_role_matrix(client, auth, method, path, body, allowed):
    assert client.request(method, path, json=body).status_code == 401  # no token at all
    for role in ROLES:
        r = client.request(method, path, json=body, headers=auth(role))
        if role in allowed:
            assert r.status_code not in (401, 403), (role, r.status_code, r.text)
        else:
            assert r.status_code == 403, (role, r.status_code, r.text)


@pytest.mark.parametrize(
    ("path", "scope"),
    [
        ("/api/v1/transactions", "transactions:write"),
        ("/api/v1/transactions:batch", "transactions:write"),
        ("/api/v1/reference/customers", "reference:write"),
        ("/api/v1/labels/chargebacks", "labels:write"),
    ],
)
def test_machine_endpoints_reject_staff_tokens(client, auth, path, scope):
    from tests.conftest import api_key_headers

    assert client.post(path, json={}, headers=auth("admin")).status_code == 401
    other = next(s for s in ("transactions:write", "reference:write", "labels:write") if s != scope)
    assert client.post(path, json={}, headers=api_key_headers(other)).status_code == 403
    assert client.post(path, json={}, headers=api_key_headers(scope)).status_code == 422  # authorised, bad body


def test_admin_cannot_demote_self(client, auth):
    admin = auth("admin")
    me = client.get("/api/v1/auth/me", headers=admin).json()
    r = client.patch(f"/api/v1/users/{me['id']}", json={"role": "analyst"}, headers=admin)
    assert r.status_code == 422
