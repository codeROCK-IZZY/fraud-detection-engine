"""Command-line tools: ``python -m app.cli <command>``.

Commands
  migrate                         apply database migrations
  create-admin --email E          create an admin user (password from FDE_ADMIN_PASSWORD or prompt)
  create-user --email E --role R  create a staff user (password from FDE_USER_PASSWORD or prompt)
  create-api-key --name N --scope S [--scope S ...]   print a new API key (shown once)
  load-demo [--customers --days --seed]   simulate a year of activity and load it
  train [--capacity]              train, evaluate, register and activate a model
  backfill [--days]               score recent history with the active model (opens recent cases)
  drift-report [--days]           compute a drift report for the active model
  evaluate --out DIR              offline evaluation on simulated data (writes evaluation.json/.md)
  export-sample-csv --out F       write a small CSV in the upload format
  worker                          run the background job worker
  stream-scorer                   run the Kafka stream scorer
  simulate-stream [--rate --seconds]  publish live synthetic transactions to Kafka
  demo                            migrate + demo users + demo data + train + backfill (one-command setup)
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from app.core.config import get_settings
from app.core.logging import configure_logging

DEMO_USERS = (
    ("lead@example.com", "lead_analyst", "Lead Analyst"),
    ("analyst@example.com", "analyst", "Analyst One"),
    ("analyst2@example.com", "analyst", "Analyst Two"),
    ("auditor@example.com", "auditor", "Auditor"),
)


def _migrate() -> None:
    from alembic import command
    from alembic.config import Config

    root = Path(__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.attributes["database_url"] = get_settings().database_url
    cfg.attributes["skip_logging_config"] = True
    command.upgrade(cfg, "head")
    print("migrations applied")


def _create_user(email: str, role: str, full_name: str, password: str | None) -> None:
    from sqlalchemy import select

    from app.core.security import hash_password
    from app.db.models import User
    from app.db.session import session_scope

    password = password or getpass.getpass(f"Password for {email} (min 12 chars): ")
    if len(password) < 12:
        sys.exit("password must be at least 12 characters")
    with session_scope() as db:
        if db.scalar(select(User).where(User.email == email.lower())):
            print(f"user {email} already exists — skipped")
            return
        db.add(User(email=email.lower(), full_name=full_name, password_hash=hash_password(password), role=role))
    print(f"{role} {email} created")


def _create_api_key(name: str, scopes: list[str]) -> None:
    from app.db.session import session_scope
    from app.modules.api_keys.router import create_key

    with session_scope() as db:
        _, raw = create_key(db, name, scopes, None)
    print(raw)


def _load_demo(customers: int, days: int, seed: int) -> None:
    from app.db.session import session_scope
    from app.ml.demo import default_demo_config, load_demo

    with session_scope() as db:
        stats = load_demo(db, default_demo_config(customers, days, seed))
    print(json.dumps(stats, default=str))


def _train(capacity: float) -> None:
    from app.db.session import session_scope
    from app.ml.pipeline import train_from_db

    with session_scope() as db:
        mv = train_from_db(db, capacity=capacity, activate=True)
        print(
            json.dumps(
                {
                    "version": mv.version,
                    "algorithm": mv.algorithm,
                    "thresholds": mv.thresholds,
                    "calibration_window": mv.metrics.get("calibration_window"),
                },
                default=str,
            )
        )


def _backfill(days: int) -> None:
    from app.db.session import session_scope
    from app.ml.backfill import backfill

    with session_scope() as db:
        print(json.dumps(backfill(db, days=days)))


def _drift(days: int) -> None:
    from app.db.session import session_scope
    from app.modules.monitoring.service import run_drift_report

    with session_scope() as db:
        r = run_drift_report(db, days=days)
        print(json.dumps({"status": r.status, "score_psi": r.score_psi, "feature_psi": r.feature_psi}))


def _evaluate(out_dir: Path, customers: int, days: int, seed: int, test_days: int) -> None:
    import pandas as pd

    from app.ml.dataset import add_features, sim_frame
    from app.ml.pipeline import clean_nans
    from app.ml.report import write_markdown
    from app.ml.synthetic import SimConfig, generate
    from app.ml.training import evaluate

    cfg = SimConfig(
        n_customers=customers,
        days=days,
        seed=seed,
        end=datetime(2026, 11, 1, tzinfo=UTC),
        drift_start_day=max(days - 65, 1),
    )
    sim = generate(cfg)
    frame, labels = sim_frame(sim)
    X = add_features(frame, labels)
    X_leaky = add_features(frame, labels, leaky=True)
    deploy = pd.Timestamp(cfg.end) - pd.Timedelta(days=test_days)
    report = evaluate(frame, X, labels, deploy, seed=seed, X_leaky=X_leaky)
    report["simulation"] = sim.summary()
    report = clean_nans(json.loads(json.dumps(report, default=str)))
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "evaluation.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    write_markdown(report, out_dir / "evaluation.md")
    print(f"wrote {out_dir / 'evaluation.json'} and {out_dir / 'evaluation.md'}")


def _export_sample_csv(out: Path) -> None:
    """A few transactions for cards/merchants that exist in the demo dataset (seed 42)."""
    from app.ml.demo import default_demo_config
    from app.ml.synthetic import generate

    sim = generate(default_demo_config(n_customers=20, days=30))
    tx = sim.transactions.tail(25).copy()
    tx["occurred_at"] = tx["occurred_at"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    tx["external_id"] = "CSV-" + tx["external_id"]
    cols = {
        "external_id": "external_id",
        "card_token": "card_token",
        "merchant_ref": "merchant_ref",
        "amount": "amount",
        "channel": "channel",
        "entry_mode": "entry_mode",
        "occurred_at": "occurred_at",
        "device": "device_fingerprint",
        "payee": "payee_ref",
        "ip_country": "ip_country",
        "lat": "lat",
        "lon": "lon",
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    tx[list(cols)].rename(columns=cols).to_csv(out, index=False, float_format="%.4f")
    print(f"wrote {len(tx)} rows to {out} (cards from a 20-customer simulation; load matching reference data first)")


def main(argv: list[str] | None = None) -> None:
    configure_logging(get_settings().log_level, as_json=False)
    p = argparse.ArgumentParser(
        prog="python -m app.cli", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate")
    a = sub.add_parser("create-admin")
    a.add_argument("--email", required=True)
    a.add_argument("--name", default="Administrator")
    u = sub.add_parser("create-user")
    u.add_argument("--email", required=True)
    u.add_argument("--role", required=True, choices=["admin", "lead_analyst", "analyst", "auditor"])
    u.add_argument("--name", default="")
    k = sub.add_parser("create-api-key")
    k.add_argument("--name", required=True)
    k.add_argument(
        "--scope", action="append", required=True, choices=["transactions:write", "reference:write", "labels:write"]
    )
    d = sub.add_parser("load-demo")
    d.add_argument("--customers", type=int, default=1500)
    d.add_argument("--days", type=int, default=365)
    d.add_argument("--seed", type=int, default=42)
    t = sub.add_parser("train")
    t.add_argument("--capacity", type=float, default=25)
    bf = sub.add_parser("backfill")
    bf.add_argument("--days", type=int, default=14)
    dr = sub.add_parser("drift-report")
    dr.add_argument("--days", type=int, default=7)
    ev = sub.add_parser("evaluate")
    ev.add_argument("--out", type=Path, default=Path("../ml/reports"))
    ev.add_argument("--customers", type=int, default=2000)
    ev.add_argument("--days", type=int, default=365)
    ev.add_argument("--seed", type=int, default=42)
    ev.add_argument("--test-days", type=int, default=92)
    e = sub.add_parser("export-sample-csv")
    e.add_argument("--out", type=Path, default=Path("../data/sample/sample_transactions.csv"))
    sub.add_parser("worker")
    sub.add_parser("stream-scorer")
    ss = sub.add_parser("simulate-stream")
    ss.add_argument("--rate", type=float, default=20, help="transactions per second")
    ss.add_argument("--seconds", type=float, default=60)
    ss.add_argument("--fraud-share", type=float, default=0.02)
    demo = sub.add_parser("demo")
    demo.add_argument("--customers", type=int, default=1500)
    demo.add_argument("--admin-email", default=os.environ.get("FDE_ADMIN_EMAIL", "admin@example.com"))
    args = p.parse_args(argv)

    if args.cmd == "migrate":
        _migrate()
    elif args.cmd == "create-admin":
        _create_user(args.email, "admin", args.name, os.environ.get("FDE_ADMIN_PASSWORD"))
    elif args.cmd == "create-user":
        _create_user(args.email, args.role, args.name, os.environ.get("FDE_USER_PASSWORD"))
    elif args.cmd == "create-api-key":
        _create_api_key(args.name, args.scope)
    elif args.cmd == "load-demo":
        _load_demo(args.customers, args.days, args.seed)
    elif args.cmd == "train":
        _train(args.capacity)
    elif args.cmd == "backfill":
        _backfill(args.days)
    elif args.cmd == "drift-report":
        _drift(args.days)
    elif args.cmd == "evaluate":
        _evaluate(args.out, args.customers, args.days, args.seed, args.test_days)
    elif args.cmd == "export-sample-csv":
        _export_sample_csv(args.out)
    elif args.cmd == "worker":
        from app.jobs.worker import run_forever

        run_forever()
    elif args.cmd == "stream-scorer":
        from app.streaming.scorer import run_forever as run_scorer

        run_scorer()
    elif args.cmd == "simulate-stream":
        from app.streaming.simulate import run as simulate

        simulate(rate=args.rate, seconds=args.seconds, fraud_share=args.fraud_share)
    elif args.cmd == "demo":
        _migrate()
        password = os.environ.get("FDE_ADMIN_PASSWORD")
        if not password:
            sys.exit("set FDE_ADMIN_PASSWORD (demo users share it)")
        _create_user(args.admin_email, "admin", "Administrator", password)
        for email, role, name in DEMO_USERS:
            _create_user(email, role, name, password)
        _load_demo(args.customers, 365, 42)
        _train(25)
        _backfill(56)
        _drift(7)


if __name__ == "__main__":
    main()
