"""Учёт реальных действий пользователей основного бота по дням МСК."""

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy.dialects.postgresql import insert

from utils.db import SessionLocal, UserActivity

MOSCOW_TZ = ZoneInfo("Europe/Moscow")


def moscow_today():
    return datetime.now(MOSCOW_TZ).date()


def record_user_activity(user_id: int) -> None:
    # Отдельная таблица исключает старые даты, записанные автоматическими пушами.
    # Без FK на users: /start приходит до создания пользователя обработчиком.
    with SessionLocal() as db:
        db.execute(
            insert(UserActivity)
            .values(user_id=user_id, activity_date=moscow_today())
            .on_conflict_do_nothing(index_elements=["user_id", "activity_date"])
        )
        db.commit()


def count_active_today(db) -> int:
    return db.query(UserActivity).filter(UserActivity.activity_date == moscow_today()).count()
