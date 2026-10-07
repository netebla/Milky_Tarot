"""Optional PostgreSQL checks with mocked providers in an isolated test schema.

Set MILKY_TEST_POSTGRES_URL to a disposable local database to run these tests.
"""

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from threading import Barrier

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from utils import reading_charge
from utils.db import Base, Payment, PaymentNotification, PendingReading, ReadingAttempt, User
from utils.payment_processing import apply_payment_status

pytestmark = pytest.mark.skipif(
    not os.getenv("MILKY_TEST_POSTGRES_URL"), reason="Requires disposable local PostgreSQL",
)


@pytest.fixture
def pg_sessions(monkeypatch):
    url = os.environ["MILKY_TEST_POSTGRES_URL"]
    schema = "milky_cjm_test_" + uuid.uuid4().hex
    admin = create_engine(url)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    migrations = [Path(__file__).resolve().parents[1] / "migrations" / name for name in (
        "006_pending_readings.sql", "007_payment_notifications.sql", "008_reading_attempts.sql",
    )]
    try:
        # Start from the preceding schema, then apply the actual deployment SQL twice.
        existing = [table for table in Base.metadata.sorted_tables if table.name not in (
            "pending_readings", "payment_notifications", "reading_attempts",
        )]
        Base.metadata.create_all(engine, tables=existing)
        for _ in range(2):
            with engine.begin() as conn:
                for migration in migrations:
                    conn.exec_driver_sql(migration.read_text())
        factory = sessionmaker(bind=engine)
        monkeypatch.setattr(reading_charge, "SessionLocal", factory)
        with factory.begin() as session:
            session.add(User(id=1, fish_balance=500,
                three_keys_last_date=date.today(), three_keys_daily_count=1))
            for idx in (1, 2):
                session.add(Payment(id=idx, user_id=1, amount_rub=150, fish_amount=350,
                    yookassa_payment_id=f"provider-{idx}", status="pending"))
        yield factory
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def provider_success(idx):
    return {"id": f"provider-{idx}", "status": "succeeded", "paid": True,
            "amount": {"value": "150.00", "currency": "RUB"}}


@pytest.mark.parametrize("payment_ids,expected", [((1, 1), 850), ((1, 2), 1200)])
def test_pg_concurrent_payments_credit_once_without_lost_balance(pg_sessions, payment_ids, expected):
    gate = Barrier(2)

    def credit(idx):
        gate.wait(timeout=10)
        return apply_payment_status(idx, provider_success(idx), session_factory=pg_sessions)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(credit, payment_ids))
    with pg_sessions() as session:
        assert session.get(User, 1).fish_balance == expected
        assert session.query(PaymentNotification).count() == 2 * len(set(payment_ids))
    assert sum(result.newly_credited for result in results) == len(set(payment_ids))


def test_pg_concurrent_reading_claims_start_one_generation(pg_sessions):
    gate = Barrier(2)

    def claim(_):
        gate.wait(timeout=10)
        return reading_charge.claim_reading(1, "test", "Question", "Context", ["A", "B", "C"], 69)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, (1, 2)))
    assert sorted(result["status"] for result in results) == ["busy", "claimed"]
    with pg_sessions() as session:
        assert session.query(ReadingAttempt).count() == 1
        assert session.get(User, 1).fish_balance == 500
        assert session.get(User, 1).three_keys_daily_count == 1


def test_pg_reading_completion_and_payment_credit_preserve_both_changes(pg_sessions):
    attempt = reading_charge.claim_reading(1, "test", "Question", "Context", ["A", "B", "C"], 69)
    reading_charge.cache_interpretation(attempt["id"], attempt["token"], "Delivered interpretation")
    gate = Barrier(2)

    def run(kind):
        gate.wait(timeout=10)
        if kind == "reading":
            return reading_charge.complete_reading(attempt["id"], attempt["token"])
        return apply_payment_status(1, provider_success(1), session_factory=pg_sessions)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(run, ("reading", "payment")))
    assert not reading_charge.complete_reading(attempt["id"], attempt["token"])
    with pg_sessions() as session:
        assert session.get(User, 1).fish_balance == 500 + 350 - 69
        assert session.get(User, 1).three_keys_daily_count == 2
        assert session.get(ReadingAttempt, attempt["id"]).status == "completed"
        assert session.get(PendingReading, attempt["pending_id"]).status == "consumed"
