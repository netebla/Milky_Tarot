"""Сохранение вопроса при нехватке рыбок, независимо от памяти FSM."""

import json
import uuid
from datetime import datetime, timedelta

from utils.db import PendingReading, SessionLocal


def save_pending_reading(session, user_id, question, context, card_titles):
    now = datetime.utcnow()
    session.query(PendingReading).filter(
        PendingReading.user_id == user_id, PendingReading.expires_at <= now,
    ).delete(synchronize_session=False)
    session.query(PendingReading).filter(
        PendingReading.user_id == user_id, PendingReading.status == "pending",
    ).update({
        PendingReading.status: "superseded", PendingReading.question: "",
        PendingReading.context: "", PendingReading.card_titles: "[]",
    }, synchronize_session=False)
    reading = PendingReading(
        id=uuid.uuid4().hex, user_id=user_id, question=question, context=context,
        card_titles=json.dumps(card_titles, ensure_ascii=False),
        status="pending", created_at=now, expires_at=now + timedelta(hours=24),
    )
    session.add(reading)
    session.commit()
    return reading.id


def get_pending_reading(user_id, reading_id=None):
    with SessionLocal() as session:
        query = session.query(PendingReading).filter(
            PendingReading.user_id == user_id,
            PendingReading.status == "pending",
            PendingReading.expires_at > datetime.utcnow(),
        )
        if reading_id is not None:
            query = query.filter(PendingReading.id == reading_id)
        reading = query.order_by(PendingReading.created_at.desc(), PendingReading.id.desc()).first()
        if reading:
            session.expunge(reading)
        return reading
