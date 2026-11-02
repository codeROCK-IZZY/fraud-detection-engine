"""Admin user management."""

import uuid
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.api.deps import DB, Admin
from app.core.errors import Conflict, NotFound, UnprocessableData
from app.core.security import hash_password
from app.db.models import RefreshToken, User
from app.modules import audit
from app.modules.auth.schemas import UserOut

router = APIRouter(prefix="/users", tags=["users"])

Role = Literal["admin", "lead_analyst", "analyst", "auditor"]


class UserCreate(BaseModel):
    email: EmailStr
    full_name: str = Field(default="", max_length=200)
    password: str = Field(min_length=12, max_length=256)
    role: Role


class UserUpdate(BaseModel):
    full_name: str | None = Field(default=None, max_length=200)
    role: Role | None = None
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=12, max_length=256)


def create_user(db: DB, email: str, password: str, role: str, full_name: str = "") -> User:
    user = User(email=email.lower(), full_name=full_name, password_hash=hash_password(password), role=role)
    db.add(user)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise Conflict("A user with this email already exists") from exc
    return user


@router.get("", response_model=list[UserOut])
def list_users(db: DB, _: Admin) -> list[User]:
    return list(db.scalars(select(User).order_by(User.created_at)))


@router.post("", response_model=UserOut, status_code=201)
def create(body: UserCreate, db: DB, admin: Admin) -> User:
    user = create_user(db, body.email, body.password, body.role, body.full_name)
    audit.record(db, admin.id, "user.create", "user", str(user.id), role=body.role)
    db.commit()
    return user


@router.patch("/{user_id}", response_model=UserOut)
def update_user(user_id: uuid.UUID, body: UserUpdate, db: DB, admin: Admin) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise NotFound("User not found")
    if user.id == admin.id and (body.role not in (None, "admin") or body.is_active is False):
        raise UnprocessableData("Admins cannot demote or deactivate themselves")
    changes = body.model_dump(exclude_unset=True, exclude={"password"})
    for field, value in changes.items():
        setattr(user, field, value)
    if body.password:
        user.password_hash = hash_password(body.password)
    if body.is_active is False or body.password:
        # Force re-login everywhere.
        db.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=func.now())
        )
    audit.record(db, admin.id, "user.update", "user", str(user.id), fields=sorted(body.model_fields_set))
    db.commit()
    return user


@router.delete("/{user_id}", status_code=204)
def deactivate_user(user_id: uuid.UUID, db: DB, admin: Admin) -> None:
    """Soft delete: users are deactivated, never removed, to preserve audit history."""
    update_user(user_id, UserUpdate(is_active=False), db, admin)
