"""Audit trail helper for administrative and safety-relevant actions."""

import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import AuditLog


def record(
    db: Session,
    user_id: uuid.UUID | None,
    action: str,
    entity: str,
    entity_id: str | None = None,
    **details: Any,
) -> None:
    """Adds an audit row to the current transaction (committed by the caller)."""
    db.add(AuditLog(user_id=user_id, action=action, entity=entity, entity_id=entity_id, details=details))
