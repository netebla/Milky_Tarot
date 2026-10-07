"""Durable three-card attempts: cache output, charge only after delivery."""
import json
import logging
import uuid
from datetime import date, datetime, timedelta

from sqlalchemy import text
from utils.db import PendingReading, ReadingAttempt, SessionLocal, User

logger = logging.getLogger(__name__)
LEASE_SECONDS = 600


def _locked_session():
    session = SessionLocal()
    if session.bind.dialect.name == "sqlite":
        session.execute(text("BEGIN IMMEDIATE"))
    return session


def claim_reading(user_id, username, question, context, titles, service_price,
                  pending_id=None, confirmed_price=None):
    now = datetime.utcnow()
    with _locked_session() as session:
        user = session.query(User).filter(User.id == user_id).with_for_update().first()
        if not user:
            user = User(id=user_id, username=username, fish_balance=0)
            session.add(user)
            session.flush()
        active = session.query(ReadingAttempt).filter(
            ReadingAttempt.user_id == user_id, ReadingAttempt.status.in_(["generating", "ready"]),
        ).order_by(ReadingAttempt.created_at.desc()).first()
        if active:
            active_pending = session.query(PendingReading).filter(PendingReading.id == active.pending_reading_id).first()
            if not active_pending or active_pending.status != "pending" or active_pending.expires_at <= now:
                active.status = "expired"
                active.lease_token = None
                active.lease_expires_at = None
                active.question = ""
                active.context = ""
                active.card_titles = "[]"
                active.interpretation = None
                session.flush()
                active = None
        if active and active.lease_expires_at and active.lease_expires_at > now:
            return {"status": "busy"}
        # A cached result is resumed as the same attempt, irrespective of tariff changes.
        if active and (active.pending_reading_id == pending_id or (not pending_id and active.question == question)):
            active.lease_token = uuid.uuid4().hex
            active.lease_expires_at = now + timedelta(seconds=LEASE_SECONDS)
            session.commit()
            return _snapshot(active)
        if active:
            return {"status": "unfinished", "pending_id": active.pending_reading_id}
        pending = None
        if pending_id:
            pending = session.query(PendingReading).filter(
                PendingReading.id == pending_id, PendingReading.user_id == user_id,
            ).with_for_update().first()
            if not pending or pending.status != "pending" or pending.expires_at <= now:
                return {"status": "unavailable"}
        count = (user.three_keys_daily_count or 0) if user.three_keys_last_date == date.today() else 0
        price = service_price if count >= 1 else 0
        if pending and confirmed_price != price:
            return {"status": "price_changed", "pending_id": pending.id}
        if not pending:
            pending = PendingReading(
                id=uuid.uuid4().hex, user_id=user_id, question=question, context=context,
                card_titles=json.dumps(titles, ensure_ascii=False), status="pending",
                created_at=now, expires_at=now + timedelta(hours=24),
            )
            session.add(pending)
            session.flush()
        if (user.fish_balance or 0) < price:
            session.commit()
            return {"status": "insufficient", "pending_id": pending.id,
                    "price": price, "balance": user.fish_balance or 0}
        attempt = ReadingAttempt(
            id=uuid.uuid4().hex, pending_reading_id=pending.id, user_id=user_id,
            status="generating", question=pending.question, context=pending.context,
            card_titles=pending.card_titles, price_fish=price, reading_date=date.today(),
            lease_token=uuid.uuid4().hex,
            lease_expires_at=now + timedelta(seconds=LEASE_SECONDS), created_at=now,
        )
        session.add(attempt)
        session.commit()
        return _snapshot(attempt)


def _snapshot(attempt):
    return {"status": "claimed", "id": attempt.id, "token": attempt.lease_token,
            "pending_id": attempt.pending_reading_id, "question": attempt.question,
            "context": attempt.context, "titles": json.loads(attempt.card_titles),
            "interpretation": attempt.interpretation, "price": attempt.price_fish}


def cache_interpretation(attempt_id, token, interpretation):
    if not isinstance(interpretation, str) or not interpretation.strip():
        raise ValueError("Empty LLM interpretation")
    with SessionLocal() as session:
        changed = session.query(ReadingAttempt).filter(
            ReadingAttempt.id == attempt_id, ReadingAttempt.lease_token == token,
            ReadingAttempt.status.in_(["generating", "ready"]),
        ).update({ReadingAttempt.interpretation: interpretation,
                  ReadingAttempt.status: "ready"}, synchronize_session=False)
        session.commit()
        return bool(changed)


def release_reading(attempt_id, token):
    with SessionLocal() as session:
        session.query(ReadingAttempt).filter(
            ReadingAttempt.id == attempt_id, ReadingAttempt.lease_token == token,
            ReadingAttempt.status.in_(["generating", "ready"]),
        ).update({ReadingAttempt.lease_token: None, ReadingAttempt.lease_expires_at: None},
                 synchronize_session=False)
        session.commit()


def complete_reading(attempt_id, token):
    """Run only after Telegram accepted the interpretation. Never charge twice."""
    with _locked_session() as session:
        attempt_user_id = session.query(ReadingAttempt.user_id).filter(ReadingAttempt.id == attempt_id).scalar()
        if attempt_user_id is None:
            return False
        user = session.query(User).filter(User.id == attempt_user_id).with_for_update().first()
        attempt = session.query(ReadingAttempt).filter(
            ReadingAttempt.id == attempt_id,
        ).with_for_update().first()
        if not attempt or attempt.status not in ("generating", "ready"):
            return False
        if attempt.lease_token != token or not attempt.interpretation:
            return False
        # Other paid products may spend this balance during generation. Never go negative;
        # if that happens the already delivered reading is a courtesy, not a debt.
        today = date.today()
        count = (user.three_keys_daily_count or 0) if user.three_keys_last_date == today else 0
        actual_price = attempt.price_fish if count >= 1 else 0
        charged = min(actual_price, user.fish_balance or 0)
        if charged != actual_price:
            logger.warning("Delivered reading has insufficient final balance user=%s attempt=%s; waived charge", attempt.user_id, attempt.id)
            charged = 0
        user.fish_balance = (user.fish_balance or 0) - charged
        user.three_keys_daily_count = count + 1
        user.three_keys_last_date = today
        user.draw_count = (user.draw_count or 0) + len(json.loads(attempt.card_titles))
        user.last_activity_date = today
        attempt.status = "completed"
        attempt.lease_token = None
        attempt.lease_expires_at = None
        pending = session.query(PendingReading).filter(PendingReading.id == attempt.pending_reading_id).first()
        if pending:
            pending.status = "consumed"
            pending.question = ""
            pending.context = ""
            pending.card_titles = "[]"
        # Keep the result only while delivery can still be retried.
        attempt.question = ""
        attempt.context = ""
        attempt.card_titles = "[]"
        attempt.interpretation = None
        session.commit()
        return True
