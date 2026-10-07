"""Durable payments: no external Telegram or YooKassa calls."""
import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from utils.db import User, Payment, PaymentNotification
from utils.payment_processing import apply_payment_status, reconcile_pending_payments, deliver_pending_notifications


@pytest.fixture
def sessions(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'payments.db'}", connect_args={"timeout": 30})
    for model in (User, Payment, PaymentNotification):
        model.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    with factory.begin() as session:
        session.add(User(id=1, fish_balance=10))
        for idx in (1, 2):
            session.add(Payment(id=idx, user_id=1, yookassa_payment_id=f"provider-{idx}",
                amount_rub=150, fish_amount=350, status="pending",
                created_at=datetime.utcnow() - timedelta(days=1)))
    yield factory
    engine.dispose()


def success(idx=1):
    return {"id": f"provider-{idx}", "status": "succeeded", "paid": True,
            "amount": {"value": "150.00", "currency": "RUB"}}


def test_repeated_credit_and_outbox_are_atomic(sessions):
    assert apply_payment_status(1, success(), session_factory=sessions).newly_credited
    assert not apply_payment_status(1, success(), session_factory=sessions).newly_credited
    with sessions() as session:
        assert session.get(User, 1).fish_balance == 360
        assert session.query(PaymentNotification).count() == 2


def test_concurrent_distinct_payments_keep_all_credits(sessions):
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda idx: apply_payment_status(idx, success(idx), session_factory=sessions), (1, 2)))
    assert all(result.newly_credited for result in results)
    with sessions() as session:
        assert session.get(User, 1).fish_balance == 710
        assert session.query(PaymentNotification).count() == 4


def test_concurrent_same_payment_is_credited_once(sessions):
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: apply_payment_status(1, success(), session_factory=sessions), (1, 2)))
    assert sum(result.newly_credited for result in results) == 1
    with sessions() as session:
        assert session.get(User, 1).fish_balance == 360
        assert session.query(PaymentNotification).count() == 2


@pytest.mark.parametrize("override", [
    {"id": "wrong"}, {"amount": {"value": "1.00", "currency": "RUB"}},
    {"amount": {"value": "150.00", "currency": "USD"}},
])
def test_rejects_wrong_provider_identity_or_amount(sessions, override):
    data = success()
    data.update(override)
    with pytest.raises(ValueError):
        apply_payment_status(1, data, session_factory=sessions)
    with sessions() as session:
        assert session.get(User, 1).fish_balance == 10
        assert session.get(Payment, 1).status == "pending"
        assert session.query(PaymentNotification).count() == 0


def test_not_paid_success_stays_reconcilable(sessions):
    data = success()
    data["paid"] = False
    result = apply_payment_status(1, data, session_factory=sessions)
    assert result.status == "pending"
    assert result.balance == 10


def test_restart_recovers_old_order_and_failed_notification(sessions):
    async def scenario():
        fetch = AsyncMock(side_effect=lambda provider: success(int(provider.split('-')[-1])))
        await reconcile_pending_payments(session_factory=sessions, fetch=fetch)
        assert fetch.await_count == 2
        # Failure is persisted, success in other bot is recorded independently.
        async def broken(channel, result):
            if channel == 'main':
                raise RuntimeError('network')
        await deliver_pending_notifications(broken, session_factory=sessions)
        with sessions.begin() as session:
            rows = session.query(PaymentNotification).filter_by(channel='main').all()
            assert all(row.sent_at is None and row.attempts == 1 for row in rows)
            for row in rows:
                row.claimed_until = datetime.utcnow() - timedelta(seconds=1)
        # New invocation (new worker/restart) retries only unfinished delivery.
        deliver = AsyncMock()
        await deliver_pending_notifications(deliver, session_factory=sessions)
        assert deliver.await_count == 2
        assert all(call.args[0] == 'main' for call in deliver.call_args_list)
        await deliver_pending_notifications(deliver, session_factory=sessions)
        assert deliver.await_count == 2
        with sessions() as session:
            assert session.get(User, 1).fish_balance == 710
            assert all(row.sent_at for row in session.query(PaymentNotification))
    asyncio.run(scenario())


def test_failed_api_order_does_not_block_next_order(sessions):
    async def scenario():
        async def fetch(provider):
            if provider == 'provider-1':
                raise RuntimeError('temporary API failure')
            return success(2)
        await reconcile_pending_payments(session_factory=sessions, fetch=fetch)
        with sessions() as session:
            assert session.get(Payment, 1).status == 'pending'
            assert session.get(Payment, 2).status == 'succeeded'
            assert session.get(User, 1).fish_balance == 360
    asyncio.run(scenario())


def test_credit_rolls_back_if_outbox_cannot_be_persisted(sessions):
    from sqlalchemy import event
    def reject(*args):
        raise RuntimeError('outbox write failed')
    event.listen(PaymentNotification, 'before_insert', reject)
    try:
        with pytest.raises(RuntimeError):
            apply_payment_status(1, success(), session_factory=sessions)
    finally:
        event.remove(PaymentNotification, 'before_insert', reject)
    with sessions() as session:
        assert session.get(User, 1).fish_balance == 10
        assert session.get(Payment, 1).status == 'pending'
        assert session.query(PaymentNotification).count() == 0
    assert apply_payment_status(1, success(), session_factory=sessions).newly_credited


def test_parallel_notification_workers_claim_one_delivery(sessions):
    apply_payment_status(1, success(), session_factory=sessions)
    async def scenario():
        deliver = AsyncMock(side_effect=lambda *args: None)
        await asyncio.gather(
            deliver_pending_notifications(deliver, session_factory=sessions),
            deliver_pending_notifications(deliver, session_factory=sessions),
        )
        assert deliver.await_count == 2  # one per channel
    asyncio.run(scenario())


def test_canceled_order_enqueues_one_payment_notification(sessions):
    canceled = {'id': 'provider-1', 'status': 'canceled', 'paid': False}
    apply_payment_status(1, canceled, session_factory=sessions)
    apply_payment_status(1, canceled, session_factory=sessions)
    with sessions() as session:
        assert session.get(User, 1).fish_balance == 10
        assert session.get(Payment, 1).status == 'canceled'
        notifications = session.query(PaymentNotification).all()
        assert len(notifications) == 1
        assert notifications[0].channel == 'payment'
        assert notifications[0].event_type == 'canceled'
    # Even if a late verified success supersedes the cancellation, the recorded
    # cancellation event is never interpreted as a success notification.
    apply_payment_status(1, success(), session_factory=sessions)
    deliver = AsyncMock()
    asyncio.run(deliver_pending_notifications(deliver, session_factory=sessions))
    assert [call.args[1].status for call in deliver.call_args_list] == ['canceled', 'succeeded', 'succeeded']


def test_stale_canceled_response_cannot_overwrite_success(sessions):
    apply_payment_status(1, success(), session_factory=sessions)
    result = apply_payment_status(1, {'status': 'canceled', 'paid': False}, session_factory=sessions)
    assert result.status == 'succeeded'
    with sessions() as session:
        assert session.query(PaymentNotification).count() == 2
        assert session.get(User, 1).fish_balance == 360


def test_reconciliation_delivers_canceled_order_after_restart(sessions):
    async def scenario():
        await reconcile_pending_payments(session_factory=sessions,
            fetch=AsyncMock(return_value={'status': 'canceled', 'paid': False}))
        deliver = AsyncMock()
        await deliver_pending_notifications(deliver, session_factory=sessions)
        assert deliver.await_count == 2
        assert all(call.args[0] == 'payment' and call.args[1].status == 'canceled'
                   for call in deliver.call_args_list)
        await deliver_pending_notifications(deliver, session_factory=sessions)
        assert deliver.await_count == 2
    asyncio.run(scenario())


def test_concurrent_cancellation_has_one_outbox_event(sessions):
    canceled = {'status': 'canceled', 'paid': False}
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(lambda _: apply_payment_status(1, canceled, session_factory=sessions), (1, 2)))
    # An older in-flight pending response must not reopen a terminal order.
    apply_payment_status(1, {'status': 'pending', 'paid': False}, session_factory=sessions)
    apply_payment_status(1, canceled, session_factory=sessions)
    with sessions() as session:
        assert session.get(Payment, 1).status == 'canceled'
        assert session.get(User, 1).fish_balance == 10
        assert session.query(PaymentNotification).count() == 1
