"""Kafka stream scorer: ``transactions.raw`` → score → ``transactions.scored``.

Delivery guarantees (see DESIGN.md §3.2):

* Offsets are committed manually, **after** the batch's results are committed to
  PostgreSQL. A crash before that re-delivers the batch (at-least-once).
* Re-delivered messages are harmless: ``external_id`` is unique, so a replayed
  transaction is reported as ``duplicate`` and not scored twice — the *effect* is
  exactly-once per transaction.
* Messages that can never succeed (malformed JSON, schema errors, unknown card or
  merchant) go to ``transactions.dlq`` with the reason, so one bad message cannot
  block its partition.
* If the database is unavailable the batch is retried with backoff and offsets are
  not committed — good messages are never dead-lettered because of an outage.

Messages are keyed by card token, so all of a card's transactions are in one
partition and are scored in order.
"""

from __future__ import annotations

import json
import logging
import signal
import time
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import log_extra
from app.db.session import new_session
from app.ml import scoring
from app.modules.ingestion.router import score_out
from app.modules.ingestion.schemas import TransactionIn
from app.modules.ingestion.service import ingest

logger = logging.getLogger(__name__)


@dataclass
class Outcome:
    scored: list[tuple[bytes | None, dict[str, Any]]] = field(default_factory=list)  # (key, ScoreOut json)
    dead: list[tuple[bytes | None, dict[str, Any]]] = field(default_factory=list)  # (key, {error, raw})


def process_messages(db: Session, messages: list[tuple[bytes | None, bytes | None]]) -> Outcome:
    """Validate, ingest and score one batch. Commits the DB transaction.

    Raises on infrastructure errors (e.g. database down) so the caller can retry
    without committing Kafka offsets.
    """
    out = Outcome()
    items: list[TransactionIn] = []
    keys: list[bytes | None] = []
    raws: list[str] = []
    for key, value in messages:
        raw = (value or b"").decode("utf-8", errors="replace")
        try:
            items.append(TransactionIn.model_validate_json(raw))
            keys.append(key)
            raws.append(raw)
        except ValidationError as exc:
            first = exc.errors()[0]
            out.dead.append(
                (key, {"error": f"{'.'.join(map(str, first.get('loc', [])))}: {first.get('msg')}", "raw": raw[:4000]})
            )
    if not items:
        return out
    results, errors = ingest(db, items, "stream")
    for e in errors:
        out.dead.append((keys[e.index], {"error": e.error, "raw": raws[e.index][:4000]}))
    created = [r.transaction_id for r in results if r.status == "created"]
    scored = {s.transaction_id: s for s in scoring.score_transactions(db, created)}
    db.commit()
    for r in results:
        so = score_out(db, r, scored[r.transaction_id].case_id if r.transaction_id in scored else None)
        out.scored.append((keys[r.index], so.model_dump(mode="json")))
    return out


def _db_ok() -> bool:
    try:
        with new_session() as db:
            db.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def run_forever() -> None:  # pragma: no cover - exercised by the Kafka integration test and in compose
    from confluent_kafka import Consumer, KafkaError, Producer

    s = get_settings()
    consumer = Consumer(
        {
            "bootstrap.servers": s.kafka_bootstrap,
            "group.id": s.kafka_group_id,
            "enable.auto.commit": False,
            "auto.offset.reset": "earliest",
            "max.poll.interval.ms": 600_000,
        }
    )
    producer = Producer({"bootstrap.servers": s.kafka_bootstrap, "enable.idempotence": True, "acks": "all"})
    consumer.subscribe([s.kafka_topic_raw])
    stopping = False

    def _stop(*_: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    logger.info("stream scorer started", extra=log_extra(topic=s.kafka_topic_raw, group=s.kafka_group_id))
    try:
        while not stopping:
            msgs = consumer.consume(num_messages=s.stream_batch_size, timeout=1.0)
            batch = []
            for m in msgs:
                err = m.error()
                if err is not None:
                    if err.code() != KafkaError._PARTITION_EOF:
                        logger.error("kafka error", extra=log_extra(error=str(err)))
                    continue
                batch.append((m.key(), m.value()))
            if not batch:
                continue
            started = time.perf_counter()
            attempt = 0
            while True:
                attempt += 1
                db = new_session()
                try:
                    outcome = process_messages(db, batch)
                    break
                except Exception:
                    db.rollback()
                    logger.exception("batch failed", extra=log_extra(attempt=attempt, size=len(batch)))
                    if attempt >= s.stream_max_attempts and _db_ok():
                        # The database is fine, so something in this batch is poisonous:
                        # process messages one by one and dead-letter only the failures.
                        outcome = _one_by_one(batch)
                        break
                    time.sleep(min(30.0, 2.0**attempt))
                finally:
                    db.close()
            for key, payload in outcome.scored:
                producer.produce(s.kafka_topic_scored, key=key, value=json.dumps(payload).encode())
            for key, payload in outcome.dead:
                producer.produce(s.kafka_topic_dlq, key=key, value=json.dumps(payload).encode())
            producer.flush(10)
            consumer.commit(asynchronous=False)
            logger.info(
                "batch scored",
                extra=log_extra(
                    size=len(batch),
                    scored=len(outcome.scored),
                    dead_lettered=len(outcome.dead),
                    ms=round((time.perf_counter() - started) * 1000, 1),
                ),
            )
    finally:
        consumer.close()
        producer.flush(10)


def _one_by_one(batch: list[tuple[bytes | None, bytes | None]]) -> Outcome:
    total = Outcome()
    for key, value in batch:
        db = new_session()
        try:
            o = process_messages(db, [(key, value)])
            total.scored += o.scored
            total.dead += o.dead
        except Exception as exc:
            db.rollback()
            total.dead.append(
                (
                    key,
                    {
                        "error": f"processing failed: {exc}"[:500],
                        "raw": (value or b"").decode("utf-8", errors="replace")[:4000],
                    },
                )
            )
        finally:
            db.close()
    return total
