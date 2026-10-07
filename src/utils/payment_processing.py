"""Durable YooKassa reconciliation and transactional notification outbox.

Only trusted server GET responses may be passed to apply_payment_status.
Telegram delivery is at least once: a crash after send but before commit can
repeat a notification, but never credit a payment twice.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Awaitable, Callable

from sqlalchemy import func, or_, update

from utils.db import Payment, PaymentNotification, SessionLocal, User
from utils.yookassa_client import get_payment

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PaymentResult:
    payment_id: int
    status: str
    newly_credited: bool
    user_id: int
    fish_amount: int
    balance: int


def _result(session, payment, credited=False):
    balance = session.query(User.fish_balance).filter(User.id == payment.user_id).scalar() or 0
    return PaymentResult(payment.id, payment.status, credited, payment.user_id,
                         payment.fish_amount, balance)


def apply_payment_status(payment_id: int, provider_data: dict, *, session_factory=None) -> PaymentResult:
    """Credit once, increment atomically and enqueue both notifications together."""
    factory = session_factory or SessionLocal
    with factory.begin() as session:
        payment = session.get(Payment, payment_id)
        if payment is None:
            raise ValueError("Unknown payment")
        if payment.status == "succeeded":
            return _result(session, payment)
        status = provider_data.get("status")
        method = (provider_data.get("payment_method") or {}).get("type")
        now = datetime.utcnow()
        if status == "succeeded" and provider_data.get("paid") is True:
            # Validate amount and identity against the order, not metadata supplied
            # by a webhook caller or the browser return URL.
            try:
                amount = provider_data["amount"]
                valid = (provider_data["id"] == payment.yookassa_payment_id
                         and amount["currency"] == "RUB"
                         and Decimal(amount["value"]) == Decimal(payment.amount_rub))
            except (KeyError, TypeError, InvalidOperation):
                valid = False
            if not valid:
                raise ValueError(f"YooKassa payment identity/amount mismatch: {payment_id}")
            won = session.execute(update(Payment).where(
                Payment.id == payment_id, Payment.status != "succeeded",
            ).values(status="succeeded", method=method or payment.method,
                     updated_at=now), execution_options={"synchronize_session": False}).rowcount
            if not won:
                session.refresh(payment)
                return _result(session, payment)
            # Existing Telegram users are expected, but support old/imported orders.
            # Payment row CAS serializes same-payment calls; SQL increment avoids
            # overwriting concurrent credits or reading debits for this user.
            user = session.query(User).filter(User.id == payment.user_id).with_for_update().first()
            if user is None:
                user = User(id=payment.user_id, fish_balance=0)
                session.add(user)
                session.flush()
            session.execute(update(User).where(User.id == payment.user_id).values(
                fish_balance=func.coalesce(User.fish_balance, 0) + payment.fish_amount,
            ), execution_options={"synchronize_session": False})
            for channel in ("main", "payment"):
                session.add(PaymentNotification(payment_id=payment_id, channel=channel, event_type="succeeded"))
            session.flush()
            session.refresh(payment)
            result = _result(session, payment, True)
            logger.info("Payment credited id=%s user=%s fish=%s", payment_id, payment.user_id, payment.fish_amount)
            return result
        if status == "canceled":
            won = session.execute(update(Payment).where(
                Payment.id == payment_id,
                Payment.status.notin_(("succeeded", "canceled")),
            ).values(status="canceled", method=method or payment.method, updated_at=now),
                execution_options={"synchronize_session": False}).rowcount
            if won:
                session.add(PaymentNotification(
                    payment_id=payment_id, channel="payment", event_type="canceled",
                ))
                session.flush()
            session.refresh(payment)
            return _result(session, payment)
        # A success without paid must stay eligible for later reconciliation.
        next_status = status if status in ("pending", "waiting_for_capture", "canceled") else payment.status
        session.execute(update(Payment).where(
            Payment.id == payment_id, Payment.status.notin_(("succeeded", "canceled")),
        ).values(status=next_status, method=method or payment.method, updated_at=now),
            execution_options={"synchronize_session": False})
        session.refresh(payment)
        return _result(session, payment)


async def reconcile_pending_payments(*, session_factory=None, fetch=None, batch_size=100):
    """No expiration window: restart recovers every unfinished persisted order."""
    factory = session_factory or SessionLocal
    fetch = fetch or get_payment
    with factory() as session:
        orders = session.query(Payment.id, Payment.yookassa_payment_id).filter(
            Payment.status.in_(("pending", "waiting_for_capture", "error")),
        ).order_by(Payment.updated_at, Payment.id).limit(batch_size).all()
    for payment_id, provider_id in orders:
        try:
            data = await fetch(provider_id)
            apply_payment_status(payment_id, data, session_factory=factory)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Payment reconciliation failed id=%s", payment_id)
            # Rotate failed orders to prevent starving newer orders in large queues.
            with factory.begin() as session:
                session.execute(update(Payment).where(
                    Payment.id == payment_id,
                    Payment.status.in_(("pending", "waiting_for_capture", "error")),
                ).values(updated_at=datetime.utcnow()))


async def deliver_pending_notifications(
    deliver: Callable[[str, PaymentResult], Awaitable[None]], *,
    session_factory=None, payment_id=None, channels=("main", "payment"), batch_size=100,
):
    factory = session_factory or SessionLocal
    now = datetime.utcnow()
    with factory() as session:
        query = session.query(PaymentNotification.id).filter(
            PaymentNotification.sent_at.is_(None),
            PaymentNotification.channel.in_(channels),
            or_(PaymentNotification.claimed_until.is_(None), PaymentNotification.claimed_until <= now),
        )
        if payment_id is not None:
            query = query.filter(PaymentNotification.payment_id == payment_id)
        ids = [row[0] for row in query.order_by(PaymentNotification.id).limit(batch_size).all()]
    for notification_id in ids:
        lease = datetime.utcnow() + timedelta(seconds=90)
        with factory.begin() as session:
            claimed = session.execute(update(PaymentNotification).where(
                PaymentNotification.id == notification_id, PaymentNotification.sent_at.is_(None),
                or_(PaymentNotification.claimed_until.is_(None), PaymentNotification.claimed_until <= datetime.utcnow()),
            ).values(claimed_until=lease, attempts=PaymentNotification.attempts + 1),
                execution_options={"synchronize_session": False}).rowcount
            if not claimed:
                continue
            notification = session.get(PaymentNotification, notification_id)
            payment = session.get(Payment, notification.payment_id)
            current = _result(session, payment)
            # Deliver the recorded event, even if a later reconciliation changed
            # the payment status before this notification could be delivered.
            result = PaymentResult(current.payment_id, notification.event_type,
                current.newly_credited, current.user_id, current.fish_amount, current.balance)
            channel = notification.channel
        try:
            await deliver(channel, result)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Payment notification failed id=%s channel=%s", notification_id, channel)
            with factory.begin() as session:
                session.execute(update(PaymentNotification).where(
                    PaymentNotification.id == notification_id, PaymentNotification.claimed_until == lease,
                ).values(claimed_until=datetime.utcnow() + timedelta(minutes=5)))
        else:
            with factory.begin() as session:
                session.execute(update(PaymentNotification).where(
                    PaymentNotification.id == notification_id, PaymentNotification.claimed_until == lease,
                ).values(sent_at=datetime.utcnow(), claimed_until=None))


async def payment_worker(deliver, *, session_factory=None, interval_seconds=10):
    """Runs throughout payment service lifetime; polling shortcuts are optional."""
    async def reconcile_loop():
        while True:
            try:
                await reconcile_pending_payments(session_factory=session_factory)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Durable payment reconciliation iteration failed")
            await asyncio.sleep(interval_seconds)

    async def notification_loop():
        while True:
            try:
                await deliver_pending_notifications(deliver, session_factory=session_factory)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Durable payment notification iteration failed")
            await asyncio.sleep(interval_seconds)

    # A slow provider must not delay delivery of already credited purchases.
    tasks = [asyncio.create_task(reconcile_loop()), asyncio.create_task(notification_loop())]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
