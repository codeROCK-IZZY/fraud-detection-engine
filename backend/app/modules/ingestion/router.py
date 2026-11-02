"""Transaction ingestion: REST (machine clients with API keys) and CSV upload (leads)."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, File, Response, UploadFile
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from app.api.deps import DB, Lead, TransactionsKey
from app.core.config import get_settings
from app.core.errors import NotFound, UnprocessableData
from app.db.models import CaseTransaction, IngestionBatch, ModelVersion, Transaction, TransactionScore
from app.jobs import queue
from app.ml import scoring
from app.modules import audit
from app.modules.ingestion import uploads
from app.modules.ingestion.schemas import BatchResult, ModelRef, ScoreOut, TransactionBatch, TransactionIn
from app.modules.ingestion.service import Ingested, ingest

router = APIRouter(tags=["ingestion"])


def score_out(db: DB, item: Ingested, case_id: int | None = None) -> ScoreOut:
    txn = db.get(Transaction, item.transaction_id)
    assert txn is not None
    sc = db.get(TransactionScore, txn.current_score_id) if txn.current_score_id else None
    mv = db.get(ModelVersion, sc.model_version_id) if sc and sc.model_version_id else None
    if case_id is None and sc and sc.recommended_action == "review":
        case_id = db.scalar(select(CaseTransaction.case_id).where(CaseTransaction.transaction_id == txn.id).limit(1))
    return ScoreOut(
        transaction_id=txn.id,
        external_id=txn.external_id,
        status=item.status,
        risk_score=sc.score if sc else None,
        risk_band=sc.risk_band if sc else "low",
        model_band=sc.model_band if sc else None,
        recommended_action=(sc.recommended_action if sc else "allow"),
        sampled_for_review=sc.sampled_for_review if sc else False,
        anomaly_score=sc.anomaly_score if sc else None,
        model=ModelRef(name=mv.name, version=mv.version, algorithm=mv.algorithm) if mv else None,
        reasons=sc.explanation if sc else [],
        case_id=case_id,
        latency_ms=sc.latency_ms if sc else None,
    )


def ingest_and_score(db: DB, items: list[TransactionIn], source: str) -> BatchResult:
    results, errors = ingest(db, items, source)
    new_ids = [r.transaction_id for r in results if r.status == "created"]
    scored = {s.transaction_id: s for s in scoring.score_transactions(db, new_ids)}
    db.commit()
    out = [score_out(db, r, scored[r.transaction_id].case_id if r.transaction_id in scored else None) for r in results]
    return BatchResult(results=out, errors=errors)


@router.post(
    "/transactions",
    response_model=ScoreOut,
    status_code=201,
    responses={200: {"description": "Duplicate external_id: the original result is returned"}},
)
def submit_transaction(body: TransactionIn, response: Response, db: DB, _: TransactionsKey) -> ScoreOut:
    """Ingest one transaction and score it synchronously (advisory `allow` / `review`)."""
    res = ingest_and_score(db, [body], "api")
    if res.errors:
        raise UnprocessableData(res.errors[0].error, [e.model_dump() for e in res.errors])
    out = res.results[0]
    if out.status == "duplicate":
        response.status_code = 200
    return out


@router.post("/transactions:batch", response_model=BatchResult)
def submit_batch(body: TransactionBatch, db: DB, _: TransactionsKey) -> BatchResult:
    """Up to 1,000 transactions; per-item results and per-item reference errors."""
    return ingest_and_score(db, body.transactions, "api")


class BatchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    filename: str
    status: str
    rows_total: int
    rows_accepted: int
    rows_rejected: int
    rows_duplicate: int
    errors: list[dict[str, object]]
    created_at: datetime
    finished_at: datetime | None


@router.post("/ingestion/csv", response_model=BatchOut, status_code=202)
async def upload_csv(db: DB, user: Lead, file: UploadFile = File(...)) -> IngestionBatch:
    """CSV with the same columns as `POST /transactions`; validated and scored by the worker."""
    name = (file.filename or "upload.csv")[:255]
    if not name.lower().endswith(".csv"):
        raise UnprocessableData("Only .csv files are accepted")
    batch = IngestionBatch(filename=name, user_id=user.id)
    db.add(batch)
    db.flush()
    payload = await uploads.store(file, batch.id, get_settings().max_upload_mb)
    queue.enqueue(db, "csv_import", {"batch_id": str(batch.id), **payload})
    audit.record(db, user.id, "ingestion.csv_upload", "ingestion_batch", str(batch.id), filename=name)
    db.commit()
    return batch


@router.get("/ingestion/batches", response_model=list[BatchOut])
def list_batches(db: DB, _: Lead) -> list[IngestionBatch]:
    return list(db.scalars(select(IngestionBatch).order_by(IngestionBatch.created_at.desc()).limit(50)))


@router.get("/ingestion/batches/{batch_id}", response_model=BatchOut)
def get_batch(batch_id: uuid.UUID, db: DB, _: Lead) -> IngestionBatch:
    batch = db.get(IngestionBatch, batch_id)
    if batch is None:
        raise NotFound("Batch not found")
    return batch
