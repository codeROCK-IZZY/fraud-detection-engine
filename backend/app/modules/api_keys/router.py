"""API keys for machine clients (payment systems, chargeback feeds).

The raw key is returned exactly once, at creation. Only its SHA-256 hash is
stored, so the database cannot leak usable keys.
"""

import uuid
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.api.deps import DB, Admin
from app.core.errors import NotFound
from app.core.security import new_api_key
from app.db.models import ApiKey
from app.modules import audit

router = APIRouter(prefix="/api-keys", tags=["api-keys"])

Scope = Literal["transactions:write", "reference:write", "labels:write"]


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    scopes: list[Scope] = Field(min_length=1)


class ApiKeyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    key_prefix: str
    scopes: list[str]
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


class ApiKeyCreated(ApiKeyOut):
    key: str = Field(description="Shown once. Store it in your secret manager; it cannot be retrieved again.")


def create_key(db: DB, name: str, scopes: list[str], created_by: uuid.UUID | None) -> tuple[ApiKey, str]:
    raw, digest = new_api_key()
    key = ApiKey(name=name, key_hash=digest, key_prefix=raw[:12], scopes=sorted(set(scopes)), created_by=created_by)
    db.add(key)
    db.flush()
    return key, raw


@router.get("", response_model=list[ApiKeyOut])
def list_keys(db: DB, _: Admin) -> list[ApiKey]:
    return list(db.scalars(select(ApiKey).order_by(ApiKey.created_at.desc())))


@router.post("", response_model=ApiKeyCreated, status_code=201)
def create(body: ApiKeyCreate, db: DB, admin: Admin) -> ApiKeyCreated:
    key, raw = create_key(db, body.name, list(body.scopes), admin.id)
    audit.record(db, admin.id, "api_key.create", "api_key", str(key.id), name=body.name, scopes=key.scopes)
    db.commit()
    return ApiKeyCreated(**ApiKeyOut.model_validate(key).model_dump(), key=raw)


@router.delete("/{key_id}", status_code=204)
def revoke(key_id: uuid.UUID, db: DB, admin: Admin) -> None:
    key = db.get(ApiKey, key_id)
    if key is None:
        raise NotFound("API key not found")
    if key.revoked_at is None:
        key.revoked_at = datetime.now(UTC)
        audit.record(db, admin.id, "api_key.revoke", "api_key", str(key.id))
        db.commit()
