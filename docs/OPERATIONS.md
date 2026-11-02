# Operations runbook

## Start, demo, stop
```bash
cp .env.example .env            # set the CHANGE_ME values
docker compose up -d --build    # db, kafka (+ topic init), migrate, api, worker, scorer, web
docker compose run --rm api python -m app.cli demo     # ~5–10 min: users, 1 year of data, train, backfill, drift
make stream                     # 2 minutes of live synthetic transactions through Kafka
docker compose down             # keeps volumes;  docker compose down -v  deletes all data
```
Demo users (password = `FDE_ADMIN_PASSWORD`): `admin@`, `lead@`, `analyst@`, `analyst2@`, `auditor@example.com`.

## Health
- `GET /api/health/live` — process up. `GET /api/health/ready` — DB reachable, active model, streaming flag.
- Logs are JSON with `request_id`. Useful messages: `batch scored` (stream scorer, with size/ms/dead_lettered), `job failed`, `model loaded`.

## Routine tasks
| Task | How |
|---|---|
| Machine-client API key | Administration → API keys, or `python -m app.cli create-api-key --name psp --scope transactions:write` |
| Retrain | Models → *Train new version* (or `POST /api/v1/models/train`). Backfills 14 days with the new model. |
| Roll back a model | Models → *Roll back*. Activation re-verifies the artifact's SHA-256 before switching. |
| Drift check | Monitoring → *Run drift check*. PSI > 0.25 on the score or a top feature = investigate, consider retraining. |
| Change rules | Administration → Rules → *Save as new version*. Old versions are kept; every score records its version. |
| Analyst capacity | Administration → Risk settings. Takes effect at the next training (thresholds are set from capacity). |

## Kafka
- Topics: `transactions.raw` (6 partitions, keyed by card token), `transactions.scored`, `transactions.dlq`.
- Inspect the DLQ: `docker compose exec kafka /opt/kafka/bin/kafka-console-consumer.sh --bootstrap-server kafka:9092 --topic transactions.dlq --from-beginning`
- Consumer lag: `docker compose exec kafka /opt/kafka/bin/kafka-consumer-groups.sh --bootstrap-server kafka:9092 --describe --group fde-stream-scorer`
- Replay after a new model: stop `scorer`, reset offsets with `kafka-consumer-groups.sh --reset-offsets --to-datetime …`, start it. Replays are idempotent (duplicates are reported, not re-inserted).
- Database outage: the scorer retries with backoff and does not commit offsets, so nothing is lost or dead-lettered.

## Incidents
| Symptom | Likely cause / action |
|---|---|
| Every transaction `rules only` | No active model → train one |
| Case queue flooding | Thresholds too low for traffic → lower capacity or retrain; check drift |
| Growing DLQ | Producer sending bad data (see `error` in each DLQ message) |
| `/health/ready` 503 | Database unreachable |

## Data protection
Synthetic data only. Card numbers are tokens (raw PANs rejected at ingestion and in notes); device
fingerprints and payees are keyed hashes. Case notes, case events and the audit log are append-only
(database triggers). Reads of case and transaction detail are audit-logged.
