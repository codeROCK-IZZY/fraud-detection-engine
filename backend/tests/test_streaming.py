"""Stream processing: validation, dead-lettering, idempotent redelivery, and (optionally) real Kafka."""

import json
import os
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.db.session import new_session
from app.streaming.scorer import process_messages
from tests.conftest import BACKEND

T0 = datetime(2026, 9, 1, 12, tzinfo=UTC)


@pytest.fixture
def reference(client):
    from tests.conftest import api_key_headers

    key = api_key_headers("reference:write")
    client.post(
        "/api/v1/reference/merchants",
        headers=key,
        json={
            "merchants": [{"external_ref": "M-1", "name": "Shop", "mcc": "5311", "category": "retail", "country": "US"}]
        },
    ).raise_for_status()
    client.post(
        "/api/v1/reference/customers",
        headers=key,
        json={
            "customers": [
                {
                    "external_ref": "C-1",
                    "home_country": "US",
                    "cards": [
                        {
                            "token": "tok_stream_1",
                            "last4": "0001",
                            "network": "visa",
                            "issued_at": "2024-01-01T00:00:00Z",
                        }
                    ],
                }
            ]
        },
    ).raise_for_status()


def msg(ext: str, **over: object) -> tuple[bytes, bytes]:
    body = {
        "external_id": ext,
        "card_token": "tok_stream_1",
        "merchant_ref": "M-1",
        "amount": "12.50",
        "channel": "pos",
        "entry_mode": "chip",
        "occurred_at": T0.isoformat(),
        "lat": 40.7,
        "lon": -74.0,
    }
    body.update(over)
    return b"tok_stream_1", json.dumps(body).encode()


def test_process_messages_scores_and_dead_letters(reference):
    batch = [
        msg("S-1"),
        (b"k", b"{not json"),
        msg("S-2", amount="-1"),
        msg("S-3", card_token="tok_unknown_card"),
        msg("S-1"),  # duplicate within the batch
    ]
    with new_session() as db:
        out = process_messages(db, batch)
    assert [p["external_id"] for _, p in out.scored] == ["S-1", "S-1"]
    assert [p["status"] for _, p in out.scored] == ["created", "duplicate"]
    errors = [p["error"] for _, p in out.dead]
    assert len(errors) == 3 and any("unknown card_token" in e for e in errors)
    assert all("raw" in p for _, p in out.dead)


def test_redelivery_is_idempotent(reference):
    with new_session() as db:
        first = process_messages(db, [msg("S-9")])
    with new_session() as db:
        again = process_messages(db, [msg("S-9")])  # e.g. scorer crashed before committing the offset
    assert first.scored[0][1]["transaction_id"] == again.scored[0][1]["transaction_id"]
    assert again.scored[0][1]["status"] == "duplicate"


KAFKA = os.environ.get("FDE_TEST_KAFKA_BOOTSTRAP")


@pytest.mark.kafka
@pytest.mark.skipif(not KAFKA, reason="set FDE_TEST_KAFKA_BOOTSTRAP to run against a real broker")
def test_end_to_end_through_kafka(reference):
    """Produce to a fresh raw topic, run the real scorer process, read results and the DLQ."""
    from confluent_kafka import Consumer, Producer

    suffix = uuid.uuid4().hex[:8]
    topics = {
        "FDE_KAFKA_TOPIC_RAW": f"raw-{suffix}",
        "FDE_KAFKA_TOPIC_SCORED": f"scored-{suffix}",
        "FDE_KAFKA_TOPIC_DLQ": f"dlq-{suffix}",
    }
    producer = Producer({"bootstrap.servers": KAFKA})
    for k in range(5):
        key, value = msg(f"K-{suffix}-{k}", occurred_at=(T0 + timedelta(minutes=k)).isoformat())
        producer.produce(topics["FDE_KAFKA_TOPIC_RAW"], key=key, value=value)
    producer.produce(topics["FDE_KAFKA_TOPIC_RAW"], key=b"x", value=b"garbage")
    producer.flush(10)

    env = {
        **os.environ,
        **topics,
        "FDE_KAFKA_BOOTSTRAP": KAFKA,
        "FDE_KAFKA_GROUP_ID": f"g-{suffix}",
        "FDE_STREAMING_ENABLED": "true",
    }
    proc = subprocess.Popen([sys.executable, "-m", "app.cli", "stream-scorer"], cwd=BACKEND, env=env)
    try:
        consumer = Consumer(
            {"bootstrap.servers": KAFKA, "group.id": f"check-{suffix}", "auto.offset.reset": "earliest"}
        )
        consumer.subscribe([topics["FDE_KAFKA_TOPIC_SCORED"], topics["FDE_KAFKA_TOPIC_DLQ"]])
        seen: dict[str, list[dict]] = {"scored": [], "dlq": []}
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline and (len(seen["scored"]) < 5 or not seen["dlq"]):
            m = consumer.poll(1.0)
            if m is None or m.error():
                continue
            kind = "dlq" if m.topic().startswith("dlq") else "scored"
            seen[kind].append(json.loads(m.value()))
        consumer.close()
    finally:
        proc.terminate()
        proc.wait(30)
    assert sorted(s["external_id"] for s in seen["scored"]) == [f"K-{suffix}-{k}" for k in range(5)]
    assert seen["dlq"] and "garbage" in seen["dlq"][0]["raw"]
