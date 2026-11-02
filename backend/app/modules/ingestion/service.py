"""Insert validated transactions (REST, CSV and stream all come through here).

Idempotency: ``external_id`` is unique. Re-sending a transaction (a client retry,
a Kafka redelivery) inserts nothing and reports ``duplicate``, so the same
transaction can never be scored or counted twice.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.security import pseudonymise
from app.db.models import Card, Device, Merchant, Transaction
from app.modules.ingestion.schemas import BatchItemError, TransactionIn


@dataclass
class Ingested:
    index: int
    external_id: str
    transaction_id: int
    status: str  # created | duplicate


def ingest(db: Session, items: list[TransactionIn], source: str) -> tuple[list[Ingested], list[BatchItemError]]:
    """Validate references and insert; returns per-item results and errors (caller commits)."""
    cards = dict(db.execute(select(Card.token, Card.id).where(Card.token.in_({i.card_token for i in items}))).all())
    merchants = dict(
        db.execute(
            select(Merchant.external_ref, Merchant.id).where(Merchant.external_ref.in_({i.merchant_ref for i in items}))
        ).all()
    )
    errors: list[BatchItemError] = []
    valid: list[tuple[int, TransactionIn]] = []
    for idx, it in enumerate(items):
        if it.card_token not in cards:
            errors.append(BatchItemError(index=idx, external_id=it.external_id, error="unknown card_token"))
        elif it.merchant_ref not in merchants:
            errors.append(BatchItemError(index=idx, external_id=it.external_id, error="unknown merchant_ref"))
        else:
            valid.append((idx, it))
    if not valid:
        return [], errors

    first_seen: dict[str, datetime] = {}
    for _, it in valid:
        if it.device_fingerprint:
            h = pseudonymise(it.device_fingerprint)
            first_seen[h] = min(first_seen.get(h, it.occurred_at), it.occurred_at)
    device_ids: dict[str, int] = {}
    if first_seen:
        base = pg_insert(Device).values([{"fingerprint_hash": h, "first_seen_at": t} for h, t in first_seen.items()])
        stmt = base.on_conflict_do_update(
            index_elements=[Device.fingerprint_hash],
            set_={"first_seen_at": func.least(Device.first_seen_at, base.excluded.first_seen_at)},
        ).returning(Device.fingerprint_hash, Device.id)
        device_ids = dict(db.execute(stmt).all())

    rows = [
        {
            "external_id": it.external_id,
            "card_id": cards[it.card_token],
            "merchant_id": merchants[it.merchant_ref],
            "device_id": device_ids[pseudonymise(it.device_fingerprint)] if it.device_fingerprint else None,
            "amount": it.amount,
            "currency": it.currency,
            "channel": it.channel,
            "entry_mode": it.entry_mode,
            "payee_hash": pseudonymise(it.payee_ref) if it.payee_ref else None,
            "ip_country": it.ip_country,
            "lat": it.lat,
            "lon": it.lon,
            "occurred_at": it.occurred_at,
            "source": source,
        }
        for _, it in valid
    ]
    created = dict(
        db.execute(
            pg_insert(Transaction)
            .values(rows)
            .on_conflict_do_nothing(index_elements=[Transaction.external_id])
            .returning(Transaction.external_id, Transaction.id)
        ).all()
    )
    missing = {it.external_id for _, it in valid} - set(created)
    existing = (
        dict(
            db.execute(
                select(Transaction.external_id, Transaction.id).where(Transaction.external_id.in_(missing))
            ).all()
        )
        if missing
        else {}
    )
    results: list[Ingested] = []
    seen: set[str] = set()
    for idx, it in valid:
        ext = it.external_id
        if ext in created and ext not in seen:
            results.append(Ingested(idx, ext, created[ext], "created"))
        else:  # already in the DB, or repeated within this batch
            results.append(Ingested(idx, ext, created.get(ext) or existing[ext], "duplicate"))
        seen.add(ext)
    return results, errors


CSV_COLUMNS = tuple(TransactionIn.model_fields)
CSV_CHUNK = 500
MAX_REPORTED_ERRORS = 100


@dataclass
class CsvReport:
    total: int = 0
    accepted: int = 0
    rejected: int = 0
    duplicates: int = 0
    errors: list[dict[str, object]] | None = None


def import_csv(db: Session, path: Path) -> CsvReport:
    """Validate every row with the same schema as the REST API, then ingest and score in
    chunks (each chunk committed, so a crash part-way keeps what was done; re-running the
    import is safe because ``external_id`` makes inserts idempotent)."""
    from app.ml import scoring  # local import: scoring depends on this module's package

    report = CsvReport(errors=[])
    assert report.errors is not None
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        header = set(reader.fieldnames or [])
        required = {n for n, f in TransactionIn.model_fields.items() if f.is_required()}
        if missing := required - header:
            raise ValueError(f"CSV is missing required columns: {sorted(missing)}")
        if unknown := header - set(CSV_COLUMNS):
            raise ValueError(f"CSV has unknown columns: {sorted(unknown)}")
        chunk: list[tuple[int, TransactionIn]] = []

        def flush() -> None:
            if not chunk:
                return
            results, errs = ingest(db, [c[1] for c in chunk], "csv")
            for e in errs:
                report.rejected += 1
                if len(report.errors) < MAX_REPORTED_ERRORS:  # type: ignore[arg-type]
                    report.errors.append({"row": chunk[e.index][0], "error": e.error})  # type: ignore[union-attr]
            created = [r.transaction_id for r in results if r.status == "created"]
            report.accepted += len(created)
            report.duplicates += len(results) - len(created)
            scoring.score_transactions(db, created)
            db.commit()
            chunk.clear()

        for line_no, raw in enumerate(reader, start=2):
            report.total += 1
            data = {k: (v.strip() if v and v.strip() else None) for k, v in raw.items() if k}
            try:
                chunk.append((line_no, TransactionIn.model_validate({k: v for k, v in data.items() if v is not None})))
            except ValidationError as exc:
                report.rejected += 1
                if len(report.errors) < MAX_REPORTED_ERRORS:
                    first = exc.errors()[0]
                    loc = ".".join(str(x) for x in first.get("loc", []))
                    report.errors.append({"row": line_no, "error": f"{loc}: {first.get('msg')}".strip(": ")})
            if len(chunk) >= CSV_CHUNK:
                flush()
        flush()
    return report
