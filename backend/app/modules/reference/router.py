"""Reference data (customers, cards, merchants) and card investigation views.

Customers are pseudonymous: an external reference and a home location only. The
API has no fields for names, birth dates or demographic attributes, so they
cannot be stored even by mistake (``extra="forbid"``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Query
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.api.deps import DB, Analyst, ReferenceKey
from app.core.errors import NotFound
from app.db.models import Card, Customer, Merchant, Transaction
from app.ml.synthetic import CATEGORY_CODES
from app.modules import audit
from app.modules.ingestion.validation import looks_like_pan
from app.modules.transactions.views import transaction_rows

router = APIRouter(tags=["reference"])


class CardIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=8, max_length=64)
    last4: str = Field(pattern=r"^\d{4}$")
    network: str = Field(min_length=2, max_length=20)
    issued_at: AwareDatetime

    @field_validator("token")
    @classmethod
    def _not_pan(cls, v: str) -> str:
        if looks_like_pan(v):
            raise ValueError("token looks like a raw card number")
        return v


class CustomerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    external_ref: str = Field(min_length=1, max_length=64)
    home_country: str = Field(pattern=r"^[A-Z]{2}$")
    home_city: str | None = Field(default=None, max_length=100)
    home_lat: float | None = Field(default=None, ge=-90, le=90)
    home_lon: float | None = Field(default=None, ge=-180, le=180)
    cards: list[CardIn] = Field(default_factory=list, max_length=20)


class MerchantIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    external_ref: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    mcc: str = Field(pattern=r"^\d{4}$")
    category: str
    country: str = Field(pattern=r"^[A-Z]{2}$")
    city: str | None = Field(default=None, max_length=100)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    is_online: bool = False

    @field_validator("category")
    @classmethod
    def _known_category(cls, v: str) -> str:
        if v not in CATEGORY_CODES:
            raise ValueError(f"category must be one of {list(CATEGORY_CODES)}")
        return v


class CustomersBody(BaseModel):
    customers: list[CustomerIn] = Field(min_length=1, max_length=1000)


class MerchantsBody(BaseModel):
    merchants: list[MerchantIn] = Field(min_length=1, max_length=1000)


def upsert_customers(db: Session, items: list[CustomerIn]) -> dict[str, int]:
    rows = [i.model_dump(exclude={"cards"}) for i in items]
    base = pg_insert(Customer).values(rows)
    stmt = base.on_conflict_do_update(
        index_elements=[Customer.external_ref],
        set_={c: base.excluded[c] for c in ("home_country", "home_city", "home_lat", "home_lon")},
    ).returning(Customer.external_ref, Customer.id)
    ids: dict[str, int] = dict(db.execute(stmt).all())
    cards = [{**c.model_dump(), "customer_id": ids[i.external_ref]} for i in items for c in i.cards]
    if cards:
        cstmt = pg_insert(Card).values(cards)
        db.execute(
            cstmt.on_conflict_do_update(
                index_elements=[Card.token],
                set_={c: cstmt.excluded[c] for c in ("customer_id", "last4", "network", "issued_at")},
            )
        )
    return {"customers": len(ids), "cards": len(cards)}


def upsert_merchants(db: Session, items: list[MerchantIn]) -> int:
    stmt = pg_insert(Merchant).values([i.model_dump() for i in items])
    db.execute(
        stmt.on_conflict_do_update(
            index_elements=[Merchant.external_ref],
            set_={
                c: stmt.excluded[c] for c in ("name", "mcc", "category", "country", "city", "lat", "lon", "is_online")
            },
        )
    )
    return len(items)


@router.post("/reference/customers")
def post_customers(body: CustomersBody, db: DB, _: ReferenceKey) -> dict[str, int]:
    out = upsert_customers(db, body.customers)
    db.commit()
    return out


@router.post("/reference/merchants")
def post_merchants(body: MerchantsBody, db: DB, _: ReferenceKey) -> dict[str, int]:
    n = upsert_merchants(db, body.merchants)
    db.commit()
    return {"merchants": n}


@router.get("/cards/{card_id}")
def get_card(card_id: int, db: DB, user: Analyst) -> dict[str, Any]:
    card = db.get(Card, card_id)
    if card is None:
        raise NotFound("Card not found")
    cust = db.get(Customer, card.customer_id)
    assert cust is not None
    audit.record(db, user.id, "card.view", "card", str(card_id))
    db.commit()
    return {
        "id": card.id,
        "last4": card.last4,
        "network": card.network,
        "issued_at": card.issued_at,
        "status": card.status,
        "customer": {
            "id": cust.id,
            "external_ref": cust.external_ref,
            "home_country": cust.home_country,
            "home_city": cust.home_city,
            "home_lat": cust.home_lat,
            "home_lon": cust.home_lon,
        },
    }


@router.get("/cards/{card_id}/history")
def card_history(
    card_id: int,
    db: DB,
    user: Analyst,
    until: datetime | None = None,
    hours: int = Query(default=72, ge=1, le=24 * 90),
) -> list[dict[str, Any]]:
    """The card's transactions in the ``hours`` before ``until`` (default: now), newest first."""
    if db.get(Card, card_id) is None:
        raise NotFound("Card not found")
    end = until or datetime.now(UTC)
    q = (
        select(Transaction)
        .where(
            Transaction.card_id == card_id,
            Transaction.occurred_at <= end,
            Transaction.occurred_at >= end - timedelta(hours=hours),
        )
        .order_by(Transaction.occurred_at.desc())
        .limit(500)
    )
    audit.record(db, user.id, "card.history_view", "card", str(card_id), hours=hours)
    db.commit()
    return transaction_rows(db, list(db.scalars(q)))
