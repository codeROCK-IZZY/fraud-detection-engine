from datetime import UTC, datetime, timedelta

import jwt
import pytest
from pydantic import ValidationError

from app.core import security
from app.core.config import Settings, get_settings
from tests.conftest import PASSWORD, create_user, login

LOGIN = "/api/v1/auth/login"


# ---------------------------------------------------------------- unit: security helpers
def test_password_hash_roundtrip():
    h = security.hash_password("s3cret-password!")
    assert h.startswith("$argon2id$")
    assert security.verify_password("s3cret-password!", h)
    assert not security.verify_password("wrong", h)
    assert not security.verify_password("x", "not-a-hash")


def test_access_token_roundtrip_and_type_check():
    import uuid

    uid = uuid.uuid4()
    claims = security.decode_access_token(security.create_access_token(uid, "auditor"))
    assert claims["sub"] == str(uid) and claims["role"] == "auditor"
    s = get_settings()
    wrong_type = jwt.encode(
        {"sub": str(uid), "type": "refresh", "exp": datetime.now(UTC) + timedelta(minutes=5)},
        s.jwt_secret.get_secret_value(),
        algorithm="HS256",
    )
    with pytest.raises(jwt.InvalidTokenError):
        security.decode_access_token(wrong_type)
    expired = jwt.encode(
        {"sub": str(uid), "type": "access", "exp": datetime.now(UTC) - timedelta(seconds=1)},
        s.jwt_secret.get_secret_value(),
        algorithm="HS256",
    )
    with pytest.raises(jwt.ExpiredSignatureError):
        security.decode_access_token(expired)


def test_weak_jwt_secret_rejected():
    with pytest.raises(ValidationError):
        Settings(jwt_secret="short", pseudonym_secret="p" * 40)  # type: ignore[arg-type]


def test_refresh_token_only_hash_stored():
    raw, digest = security.new_refresh_token()
    assert raw != digest and len(digest) == 64 and security.hash_token(raw) == digest


# ---------------------------------------------------------------- API
def test_login_success_sets_httponly_refresh_cookie(client):
    create_user("a@example.com", "auditor")
    r = client.post(LOGIN, json={"email": "A@Example.com", "password": PASSWORD})
    assert r.status_code == 200
    assert r.json()["token_type"] == "bearer"
    cookie = r.headers["set-cookie"]
    assert "fde_refresh=" in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie


def test_login_wrong_password_and_unknown_user_same_message(client):
    create_user("a@example.com", "auditor")
    bad = client.post(LOGIN, json={"email": "a@example.com", "password": "nope"})
    unknown = client.post(LOGIN, json={"email": "ghost@example.com", "password": "nope"})
    assert bad.status_code == unknown.status_code == 401
    assert bad.json()["detail"] == unknown.json()["detail"]
    assert bad.headers["content-type"].startswith("application/problem+json")


def test_lockout_after_repeated_failures(client):
    create_user("a@example.com", "auditor")
    for _ in range(get_settings().max_failed_logins):
        assert client.post(LOGIN, json={"email": "a@example.com", "password": "wrong"}).status_code == 401
    # Correct password is now refused while locked.
    assert client.post(LOGIN, json={"email": "a@example.com", "password": PASSWORD}).status_code == 401


def test_login_rate_limit(client):
    limit = get_settings().login_rate_limit_per_minute
    codes = [client.post(LOGIN, json={"email": "x@example.com", "password": "p"}).status_code for _ in range(limit + 1)]
    assert codes[-1] == 429 and 429 not in codes[:-1]


def test_inactive_user_cannot_login(client):
    create_user("off@example.com", "auditor", active=False)
    assert client.post(LOGIN, json={"email": "off@example.com", "password": PASSWORD}).status_code == 401


def test_me_requires_valid_token(client):
    create_user("a@example.com", "analyst")
    assert client.get("/api/v1/auth/me").status_code == 401
    assert client.get("/api/v1/auth/me", headers={"Authorization": "Bearer garbage"}).status_code == 401
    r = client.get("/api/v1/auth/me", headers=login(client, "a@example.com"))
    assert r.status_code == 200 and r.json()["role"] == "analyst"
    assert "password_hash" not in r.json()


def test_refresh_rotation_and_reuse_detection(client):
    create_user("a@example.com", "auditor")
    client.post(LOGIN, json={"email": "a@example.com", "password": PASSWORD})
    old = client.cookies.get("fde_refresh")
    r1 = client.post("/api/v1/auth/refresh")
    assert r1.status_code == 200
    new = client.cookies.get("fde_refresh")
    assert new and new != old
    # Replaying the old (rotated) token is treated as theft: everything is revoked.
    client.cookies.set("fde_refresh", old, path="/api/v1/auth")
    assert client.post("/api/v1/auth/refresh").status_code == 401
    client.cookies.set("fde_refresh", new, path="/api/v1/auth")
    assert client.post("/api/v1/auth/refresh").status_code == 401


def test_logout_revokes_refresh_token(client):
    create_user("a@example.com", "auditor")
    client.post(LOGIN, json={"email": "a@example.com", "password": PASSWORD})
    token = client.cookies.get("fde_refresh")
    assert client.post("/api/v1/auth/logout").status_code == 204
    client.cookies.set("fde_refresh", token, path="/api/v1/auth")
    assert client.post("/api/v1/auth/refresh").status_code == 401


def test_refresh_without_cookie(client):
    assert client.post("/api/v1/auth/refresh").status_code == 401


def test_weak_pseudonym_secret_rejected():
    with pytest.raises(ValidationError):
        Settings(jwt_secret="j" * 40, pseudonym_secret="short")  # type: ignore[arg-type]


def test_pseudonymise_is_keyed_and_stable():
    a = security.pseudonymise("PAYEE-123")
    assert a == security.pseudonymise("PAYEE-123") and len(a) == 64
    import hashlib

    assert a != hashlib.sha256(b"PAYEE-123").hexdigest()  # not a plain (guessable) hash
