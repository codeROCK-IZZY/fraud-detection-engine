"""Shared FastAPI dependencies: DB session, current user, role guards, API keys.

Roles are *sets*, not a ladder: the auditor can read the audit log but cannot
work cases, and an analyst can work cases but cannot read the audit log.

| Guard        | Roles admitted                       |
|--------------|--------------------------------------|
| ``Staff``    | admin, lead_analyst, analyst, auditor |
| ``Analyst``  | admin, lead_analyst, analyst          |
| ``Lead``     | admin, lead_analyst                   |
| ``Admin``    | admin                                 |
| ``Auditing`` | admin, auditor                        |

Machine clients use ``X-API-Key`` with scopes (``require_scope``).
"""

import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Annotated

import jwt
from fastapi import Depends, Header
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import Forbidden, Unauthorized
from app.core.security import decode_access_token, hash_token
from app.db.models import ApiKey, User
from app.db.session import new_session

_bearer = HTTPBearer(auto_error=False)

STAFF = frozenset({"admin", "lead_analyst", "analyst", "auditor"})
ANALYSTS = frozenset({"admin", "lead_analyst", "analyst"})
LEADS = frozenset({"admin", "lead_analyst"})
ADMINS = frozenset({"admin"})
AUDITING = frozenset({"admin", "auditor"})


def get_db() -> Iterator[Session]:
    """One session per request. Services call ``commit()`` explicitly; anything
    left uncommitted is rolled back when the request ends."""
    session = new_session()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


DB = Annotated[Session, Depends(get_db)]


def get_current_user(db: DB, creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]) -> User:
    if creds is None or creds.scheme.lower() != "bearer":
        raise Unauthorized("Missing bearer token")
    try:
        claims = decode_access_token(creds.credentials)
        user_id = uuid.UUID(claims["sub"])
    except (jwt.PyJWTError, ValueError, KeyError) as exc:
        raise Unauthorized("Invalid or expired token") from exc
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise Unauthorized("User is inactive or no longer exists")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_roles(allowed: frozenset[str]) -> Callable[[User], User]:
    def _guard(user: CurrentUser) -> User:
        if user.role not in allowed:
            raise Forbidden(f"Requires one of the roles: {', '.join(sorted(allowed))}")
        return user

    return _guard


Staff = Annotated[User, Depends(require_roles(STAFF))]
Analyst = Annotated[User, Depends(require_roles(ANALYSTS))]
Lead = Annotated[User, Depends(require_roles(LEADS))]
Admin = Annotated[User, Depends(require_roles(ADMINS))]
Auditing = Annotated[User, Depends(require_roles(AUDITING))]


def require_scope(scope: str) -> Callable[..., ApiKey]:
    """Authenticate a machine client by ``X-API-Key`` and check it has ``scope``."""

    def _guard(db: DB, x_api_key: Annotated[str | None, Header()] = None) -> ApiKey:
        if not x_api_key:
            raise Unauthorized("Missing X-API-Key header")
        key = db.scalar(select(ApiKey).where(ApiKey.key_hash == hash_token(x_api_key)))
        if key is None or key.revoked_at is not None:
            raise Unauthorized("Invalid or revoked API key")
        if scope not in key.scopes:
            raise Forbidden(f"API key lacks scope '{scope}'")
        now = datetime.now(UTC)
        # Throttled write: at most once a minute per key, to keep the hot path cheap.
        if key.last_used_at is None or now - key.last_used_at > timedelta(minutes=1):
            key.last_used_at = now
            db.commit()
        return key

    return _guard


TransactionsKey = Annotated[ApiKey, Depends(require_scope("transactions:write"))]
ReferenceKey = Annotated[ApiKey, Depends(require_scope("reference:write"))]
LabelsKey = Annotated[ApiKey, Depends(require_scope("labels:write"))]
