"""Load a simulated year of activity into PostgreSQL (``python -m app.cli load-demo``).

Bulk path for ~1M rows: PostgreSQL ``COPY`` instead of the API. Device fingerprints
and payee references are pseudonymised with the same keyed hash the API uses, and
only labels **reported by the end of the simulation** are loaded: fraud that will
be reported "next month" is not known yet, exactly as in production.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import pandas as pd
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.security import pseudonymise
from app.ml.synthetic import SimConfig, SimData, generate

logger = logging.getLogger(__name__)


def _copy(db: Session, table: str, columns: list[str], rows: Any) -> int:
    raw = db.connection().connection.dbapi_connection
    assert raw is not None
    n = 0
    with raw.cursor() as cur, cur.copy(f"COPY {table} ({', '.join(columns)}) FROM STDIN") as copy:  # type: ignore[attr-defined]
        for r in rows:
            copy.write_row(r)
            n += 1
    return n


def _none(v: Any) -> Any:
    return None if v is None or (isinstance(v, float) and v != v) else v


def load_sim(db: Session, sim: SimData) -> dict[str, Any]:
    if db.execute(text("SELECT EXISTS (SELECT 1 FROM transactions)")).scalar():
        raise ValueError("The database already contains transactions; the demo loader only fills an empty database")
    tx = sim.transactions
    _copy(
        db,
        "customers",
        ["external_ref", "home_country", "home_city", "home_lat", "home_lon"],
        sim.customers[["external_ref", "home_country", "home_city", "home_lat", "home_lon"]].itertuples(index=False),
    )
    cust_ids: dict[str, int] = dict(db.execute(text("SELECT external_ref, id FROM customers")).all())
    _copy(
        db,
        "cards",
        ["customer_id", "token", "last4", "network", "issued_at"],
        (
            (cust_ids[r.customer_ref], r.token, r.last4, r.network, r.issued_at.to_pydatetime())
            for r in sim.cards.itertuples(index=False)
        ),
    )
    _copy(
        db,
        "merchants",
        ["external_ref", "name", "mcc", "category", "country", "city", "lat", "lon", "is_online"],
        (
            (
                r.external_ref,
                r.name,
                r.mcc,
                r.category,
                r.country,
                _none(r.city),
                _none(r.lat),
                _none(r.lon),
                bool(r.is_online),
            )
            for r in sim.merchants.itertuples(index=False)
        ),
    )
    card_ids: dict[str, int] = dict(db.execute(text("SELECT token, id FROM cards")).all())
    merchant_ids: dict[str, int] = dict(db.execute(text("SELECT external_ref, id FROM merchants")).all())

    dev = tx.dropna(subset=["device"]).groupby("device")["occurred_at"].min()
    dev_hash = {fp: pseudonymise(fp) for fp in dev.index}
    _copy(
        db,
        "devices",
        ["fingerprint_hash", "first_seen_at"],
        ((dev_hash[fp], t.to_pydatetime()) for fp, t in dev.items()),
    )
    device_ids: dict[str, int] = dict(db.execute(text("SELECT fingerprint_hash, id FROM devices")).all())
    payee_hash = {p: pseudonymise(p) for p in tx["payee"].dropna().unique()}

    cols = [
        "external_id",
        "card_id",
        "merchant_id",
        "device_id",
        "amount",
        "currency",
        "channel",
        "entry_mode",
        "payee_hash",
        "ip_country",
        "lat",
        "lon",
        "occurred_at",
        "received_at",
        "source",
    ]

    def rows() -> Any:
        for r in tx.itertuples(index=False):
            t = r.occurred_at.to_pydatetime()
            yield (
                r.external_id,
                card_ids[r.card_token],
                merchant_ids[r.merchant_ref],
                device_ids[dev_hash[r.device]] if isinstance(r.device, str) else None,
                f"{r.amount:.2f}",
                "USD",
                r.channel,
                r.entry_mode,
                payee_hash[r.payee] if isinstance(r.payee, str) else None,
                _none(r.ip_country),
                _none(r.lat),
                _none(r.lon),
                t,
                t,
                "csv",
            )

    n_tx = _copy(db, "transactions", cols, rows())
    known = sim.labels[sim.labels["reported_at"] <= pd.Timestamp(sim.config.end)]
    txn_ids: dict[str, int] = dict(db.execute(text("SELECT external_id, id FROM transactions")).all())
    n_lab = _copy(
        db,
        "fraud_labels",
        ["transaction_id", "label", "source", "reported_at"],
        (
            (txn_ids[r.external_id], r.label, r.source, r.reported_at.to_pydatetime())
            for r in known.itertuples(index=False)
        ),
    )
    db.execute(text("ANALYZE"))
    from app.streaming.simulate import save_replay_pool

    save_replay_pool(tx)
    summary = sim.summary()
    return {
        **summary,
        "loaded_transactions": n_tx,
        "labels_known_now": n_lab,
        "labels_reported_later": len(sim.labels) - n_lab,
    }


def default_demo_config(n_customers: int = 1500, days: int = 365, seed: int = 42) -> SimConfig:
    """Simulation ending at the current hour, so the demo data looks like "today"."""
    end = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    return SimConfig(n_customers=n_customers, days=days, end=end, seed=seed, drift_start_day=max(days - 65, 1))


def load_demo(db: Session, cfg: SimConfig) -> dict[str, Any]:
    sim = generate(cfg)
    logger.info("simulated", extra={"extra_fields": sim.summary()})
    return load_sim(db, sim)
