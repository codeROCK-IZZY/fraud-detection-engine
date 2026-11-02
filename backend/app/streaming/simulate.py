"""Live transaction simulator: publishes synthetic transactions to ``transactions.raw``.

It replays patterns from the demo dataset's last 30 days (saved by ``load-demo``):
legitimate transactions with their own cards, devices and merchants, and whole
fraud episodes (bursts keep their internal timing). Each replayed transaction
gets a new ``external_id`` and a current timestamp, so the stream scorer sees
realistic "live" traffic for cards that exist in the database.
"""

from __future__ import annotations

import json
import logging
import random
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import joblib
import pandas as pd

from app.core.config import get_settings

logger = logging.getLogger(__name__)


def replay_path() -> Path:
    return get_settings().artifact_dir / "demo" / "replay.joblib"


def save_replay_pool(tx: pd.DataFrame, days: int = 30) -> Path:
    cols = [
        "card_token",
        "merchant_ref",
        "amount",
        "channel",
        "entry_mode",
        "device",
        "payee",
        "ip_country",
        "lat",
        "lon",
        "occurred_at",
        "is_fraud",
        "episode",
    ]
    recent = tx[tx["occurred_at"] > tx["occurred_at"].max() - pd.Timedelta(days=days)][cols].reset_index(drop=True)
    path = replay_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(recent, path)
    return path


def _message(row: Any, when: datetime, seq: int) -> dict[str, Any]:
    msg: dict[str, Any] = {
        "external_id": f"LIVE-{int(when.timestamp())}-{seq:07d}",
        "card_token": row.card_token,
        "merchant_ref": row.merchant_ref,
        "amount": f"{max(0.5, float(row.amount) * random.uniform(0.9, 1.1)):.2f}",
        "channel": row.channel,
        "entry_mode": row.entry_mode,
        "occurred_at": when.isoformat(),
    }
    for src, dst in (("device", "device_fingerprint"), ("payee", "payee_ref"), ("ip_country", "ip_country")):
        v = getattr(row, src)
        if isinstance(v, str):
            msg[dst] = v
    if pd.notna(row.lat) and pd.notna(row.lon):
        msg["lat"], msg["lon"] = float(row.lat), float(row.lon)
    return msg


def run(rate: float = 20, seconds: float = 60, fraud_share: float = 0.02) -> int:  # pragma: no cover - needs Kafka
    from confluent_kafka import Producer

    s = get_settings()
    pool: pd.DataFrame = joblib.load(replay_path())
    legit = pool[~pool["is_fraud"]]
    episodes = [g.sort_values("occurred_at") for _, g in pool[pool["is_fraud"]].groupby("episode")]
    producer = Producer({"bootstrap.servers": s.kafka_bootstrap, "enable.idempotence": True, "acks": "all"})
    pending: list[tuple[float, Any]] = []  # (send_at, row) for fraud episode follow-ups
    sent, seq = 0, 0
    started = time.monotonic()
    while time.monotonic() - started < seconds:
        tick = time.monotonic()
        for _ in range(max(1, int(rate))):
            if episodes and random.random() < fraud_share:
                ep = random.choice(episodes)
                t0 = ep["occurred_at"].iloc[0]
                for r in ep.itertuples(index=False):
                    # Compress long episodes so they play out within a few minutes.
                    delay = min((r.occurred_at - t0).total_seconds(), 300.0)
                    pending.append((tick + delay, r))
            else:
                pending.append((tick, legit.iloc[random.randrange(len(legit))]))
        due = [p for p in pending if p[0] <= time.monotonic()]
        pending = [p for p in pending if p[0] > time.monotonic()]
        for _, r in due:
            seq += 1
            msg = _message(r, datetime.now(UTC), seq)
            producer.produce(s.kafka_topic_raw, key=msg["card_token"].encode(), value=json.dumps(msg).encode())
            sent += 1
        producer.poll(0)
        time.sleep(max(0.0, 1.0 - (time.monotonic() - tick)))
    producer.flush(30)
    logger.info("simulation finished", extra={"extra_fields": {"sent": sent}})
    print(f"published {sent} transactions to {s.kafka_topic_raw}")
    return sent
