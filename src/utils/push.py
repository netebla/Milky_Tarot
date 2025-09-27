from __future__ import annotations
import logging
from datetime import date

from aiogram import Bot
from aiogram.types import InputFile
from sqlalchemy.orm import Session

from .db import SessionLocal, User
from .cards_loader import load_cards, choose_random_card

logger = logging.getLogger(__name__)

async def send_push_card(bot: Bot, user_id: int) -> None:
    """Отправить пользователю ежедневный пуш о том, что можно вытянуть карту дня."""
    session: Session = SessionLocal()
    try:
        user = session.query(User).filter(User.id == user_id).first()
        if not user or not user.push_enabled:
            return

        today = date.today()
        user.last_activity_date = today
        session.commit()

        try:
            await bot.send_message(
                chat_id=user_id,
                text="Привет! Сегодня вы снова можете вытянуть свою карту дня. Нажми кнопку в боте."
            )
        except Exception as e:
            logger.warning("Не удалось отправить пуш %s: %s", user_id, e)
    finally:
        session.close()