# Development guide

## Prerequisites
Python 3.13, Node 22, Docker (PostgreSQL 16 and Kafka run in containers).

## Backend
```bash
cd backend
python -m venv .venv && . .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt

# A throwaway PostgreSQL for tests (port 5434):
docker run -d --name fde-test-db -e POSTGRES_USER=fde -e POSTGRES_PASSWORD=fde -e POSTGRES_DB=fde_test \
  -p 127.0.0.1:5434:5432 postgres:16-alpine

pytest                         # all tests; the schema is rebuilt from the migration each run
pytest -m "not slow"           # skip the two model-training tests
ruff check app tests migrations && ruff format --check app tests migrations && mypy app
```

Kafka integration test (otherwise skipped):
```bash
docker run -d --name fde-test-kafka -p 127.0.0.1:9096:9092 \
  -e KAFKA_NODE_ID=1 -e KAFKA_PROCESS_ROLES=broker,controller \
  -e KAFKA_LISTENERS=PLAINTEXT://:9092,CONTROLLER://:9093 -e KAFKA_ADVERTISED_LISTENERS=PLAINTEXT://127.0.0.1:9096 \
  -e KAFKA_LISTENER_SECURITY_PROTOCOL_MAP=CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT \
  -e KAFKA_CONTROLLER_LISTENER_NAMES=CONTROLLER -e KAFKA_CONTROLLER_QUORUM_VOTERS=1@localhost:9093 \
  -e KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR=1 apache/kafka:3.9.1
FDE_TEST_KAFKA_BOOTSTRAP=127.0.0.1:9096 pytest tests/test_streaming.py
```

Windows note: use `127.0.0.1`, not `localhost`, in connection strings — `localhost` resolves to IPv6 first
and Docker publishes on IPv4 only, which makes every connection wait for a 5-second timeout.

Run the API locally against your own database:
```bash
export FDE_DATABASE_URL=postgresql+psycopg://fde:fde@127.0.0.1:5434/fde_dev FDE_JWT_SECRET=... FDE_PSEUDONYM_SECRET=...
python -m app.cli migrate && FDE_ADMIN_PASSWORD=... python -m app.cli demo --customers 400
uvicorn app.main:app --reload     # http://localhost:8000/api/docs
python -m app.cli worker          # second terminal
```

## Frontend
```bash
cd frontend && npm install && npm run dev     # http://localhost:5173 (proxies /api to :8000)
npm run lint && npm run typecheck && npm test && npm run build
```

## Code map
| Path | What lives there |
|---|---|
| `app/ml/synthetic.py` | Simulated customers, merchants, legitimate behaviour and five fraud scenarios |
| `app/ml/features.py` | Point-in-time features (shared by training and live scoring) |
| `app/ml/dataset.py`, `training.py` | Labels-as-of, out-of-time splits, models, calibration, thresholds, evaluation |
| `app/ml/scoring.py` | The single scoring path (REST, CSV, stream) |
| `app/ml/rules.py`, `explain.py` | Deterministic rules; SHAP → plain-language reasons |
| `app/modules/cases` | Case workflow and dispositions → labels |
| `app/streaming` | Kafka stream scorer and live simulator |
| `app/modules/monitoring` | PSI drift and matured-label performance |

## Exercise ideas (learning)
1. Delete the `closed="left"` in `features._rolling` and run `pytest tests/test_features.py`. Which test fails, and why does that matter for model accuracy claims?
2. Add a rule `NIGHT_HIGH_AMOUNT` (amount ≥ 1,500 between 00:00–06:00). Where do you add the logic, the default config and a test?
3. In the evaluation report, the deliberately leaky merchant fraud rate did *not* raise the test PR-AUC. Look at `merchant_fraud_rates` and the importance table and explain why this particular leak is weak here, and why the random split still is a big leak.
