"""Authentication logic: login with lockout, refresh-token rotation, logout.

Refresh-token rotation: each refresh revokes the presented token and issues a new
one. If a *revoked* token is presented again, that indicates theft/replay, so all
of that user's refresh tokens are revoked.
"""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import Unauthorized
from app.core.logging import log_extra
from app.core.security import (
    DUMMY_HASH,
    create_access_token,
    hash_token,
    new_refresh_token,
    verify_password,
)
from app.db.models import RefreshToken, User

logger = logging.getLogger(__name__)

INVALID_CREDENTIALS = "Invalid email or password"


def authenticate(db: Session, email: str, password: str) -> User:
    s = get_settings()
    now = datetime.now(UTC)
    user = db.scalar(select(User).where(User.email == email.lower()))
    if user is None:
        verify_password(password, DUMMY_HASH)  # equalise timing
        raise Unauthorized(INVALID_CREDENTIALS)
    if user.locked_until and user.locked_until > now:
        # Same message as bad credentials: do not reveal lockout to an attacker.
        logger.warning("login while locked", extra=log_extra(user_id=str(user.id)))
        raise Unauthorized(INVALID_CREDENTIALS)
    if not verify_password(password, user.password_hash) or not user.is_active:
        user.failed_login_count += 1
        if user.failed_login_count >= s.max_failed_logins:
            user.locked_until = now + timedelta(minutes=s.lockout_minutes)
            user.failed_login_count = 0
            logger.warning("account locked", extra=log_extra(user_id=str(user.id)))
        db.commit()
        raise Unauthorized(INVALID_CREDENTIALS)
    user.failed_login_count = 0
    user.locked_until = None
    return user


def issue_tokens(db: Session, user: User) -> tuple[str, str]:
    """Returns (access_token, raw_refresh_token) and commits."""
    raw, digest = new_refresh_token()
    db.add(
        RefreshToken(
            user_id=user.id,
            token_hash=digest,
            expires_at=datetime.now(UTC) + timedelta(days=get_settings().refresh_token_days),
        )
    )
    db.commit()
    return create_access_token(user.id, user.role), raw


def rotate_refresh_token(db: Session, raw: str) -> tuple[User, str, str]:
    now = datetime.now(UTC)
    token = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == hash_token(raw)).with_for_update())
    if token is None:
        raise Unauthorized("Invalid refresh token")
    if token.revoked_at is not None:
        db.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == token.user_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        db.commit()
        logger.warning("refresh token reuse detected", extra=log_extra(user_id=str(token.user_id)))
        raise Unauthorized("Invalid refresh token")
    if token.expires_at <= now:
        raise Unauthorized("Refresh token expired")
    user = db.get(User, token.user_id)
    if user is None or not user.is_active:
        raise Unauthorized("User is inactive")
    token.revoked_at = now
    access, new_raw = issue_tokens(db, user)
    return user, access, new_raw


def revoke_refresh_token(db: Session, raw: str) -> None:
    db.execute(
        update(RefreshToken)
        .where(RefreshToken.token_hash == hash_token(raw), RefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )
    db.commit()
