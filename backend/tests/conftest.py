"""Test fixtures.

Integration tests run against a REAL PostgreSQL database (``fde_test``), not mocks
or SQLite: partial unique indexes, ON CONFLICT, SKIP LOCKED, COPY, JSONB and the
append-only triggers are exactly what needs verifying. The schema is rebuilt from
the Alembic migration at the start of the session, so the migration is tested too.

``trained_world`` (module scope) loads a small simulated dataset and trains a real
model once; tests that use it share that state and must use unique external ids.
"""

import os
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path

_tmp = Path(tempfile.mkdtemp(prefix="fde-test-"))
os.environ.setdefault("FDE_DATABASE_URL", "postgresql+psycopg://fde:fde@127.0.0.1:5434/fde_test")
os.environ["FDE_JWT_SECRET"] = "test-secret-not-for-production-0123456789abcdef"
os.environ["FDE_PSEUDONYM_SECRET"] = "test-pseudonym-not-for-production-0123456789"
os.environ["FDE_ENV"] = "test"
os.environ["FDE_COOKIE_SECURE"] = "false"
os.environ["FDE_LOG_JSON"] = "false"
os.environ["FDE_LOG_LEVEL"] = "WARNING"
os.environ["FDE_ARTIFACT_DIR"] = str(_tmp / "artifacts")
os.environ["FDE_UPLOAD_DIR"] = str(_tmp / "uploads")

from dataclasses import dataclass  # noqa: E402
from datetime import UTC, datetime  # noqa: E402

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.db.models import User  # noqa: E402
from app.db.session import get_engine, new_session  # noqa: E402
from app.main import app  # noqa: E402
from app.ml import registry  # noqa: E402
from app.modules.auth.router import login_limiter  # noqa: E402

BACKEND = Path(__file__).resolve().parent.parent
PASSWORD = "Correct-Horse-Battery-9"
TABLES = (
    "audit_log, jobs, ingestion_batches, drift_reports, app_settings, case_events, case_notes, case_transactions, "
    "fraud_labels, cases, transaction_scores, transactions, model_versions, rule_sets, devices, cards, customers, "
    "merchants, api_keys, refresh_tokens, users"
)


@pytest.fixture(scope="session", autouse=True)
def migrated_db() -> None:
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public; GRANT ALL ON SCHEMA public TO public"))
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "migrations"))
    cfg.attributes["database_url"] = get_settings().database_url
    cfg.attributes["skip_logging_config"] = True
    command.upgrade(cfg, "head")


def reset_database() -> None:
    with get_engine().begin() as conn:
        conn.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
    registry._cache.clear()
    login_limiter.reset()


@pytest.fixture
def clean_db() -> None:
    reset_database()


@pytest.fixture
def db(clean_db: None) -> Iterator[Session]:
    session = new_session()
    yield session
    session.rollback()
    session.close()


@pytest.fixture
def client(clean_db: None) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def create_user(email: str, role: str, password: str = PASSWORD, active: bool = True) -> User:
    with new_session() as s:
        user = User(
            email=email, full_name=role.title(), password_hash=hash_password(password), role=role, is_active=active
        )
        s.add(user)
        s.commit()
        s.refresh(user)
        return user


def login(client: TestClient, email: str, password: str = PASSWORD) -> dict[str, str]:
    r = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def make_auth(client: TestClient) -> Callable[..., dict[str, str]]:
    made: dict[str, dict[str, str]] = {}

    def _auth(role: str, name: str | None = None) -> dict[str, str]:
        key = name or role
        if key not in made:
            email = f"{key}@example.com"
            create_user(email, role)
            made[key] = login(client, email)
        return made[key]

    return _auth


@pytest.fixture
def auth(client: TestClient) -> Callable[..., dict[str, str]]:
    """``auth("analyst")`` → headers for a fresh user with that role."""
    return make_auth(client)


def api_key_headers(*scopes: str) -> dict[str, str]:
    from app.modules.api_keys.router import create_key

    with new_session() as s:
        _, raw = create_key(s, f"test-{'-'.join(scopes)}", list(scopes), None)  # type: ignore[arg-type]
        s.commit()
    return {"X-API-Key": raw}


# ----------------------------------------------------------------------------- shared trained world
WORLD_END = datetime(2026, 10, 1, tzinfo=UTC)


@dataclass
class World:
    client: TestClient
    sim: object
    model_version: int
    key: dict[str, str]
    auth: Callable[..., dict[str, str]]


def build_world(n_customers: int = 160, days: int = 200) -> World:
    from app.ml.demo import load_sim
    from app.ml.pipeline import train_from_db
    from app.ml.synthetic import SimConfig, generate

    reset_database()
    sim = generate(SimConfig(n_customers=n_customers, days=days, end=WORLD_END, seed=11, drift_start_day=days - 40))
    with new_session() as s:
        load_sim(s, sim)
        s.commit()
    with new_session() as s:
        mv = train_from_db(s, capacity=3)  # ≈1.5% of ~210 txns/day, like the real default ratio
        s.commit()
        version = mv.version
    c = TestClient(app)
    return World(
        c, sim, version, api_key_headers("transactions:write", "reference:write", "labels:write"), make_auth(c)
    )


@pytest.fixture(scope="module")
def trained_world() -> Iterator[World]:
    w = build_world()
    with w.client:
        yield w
    reset_database()
