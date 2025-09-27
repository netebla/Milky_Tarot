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
    """Отправить пользователю ежедневный пуш с «картой дня» через SQLAlchemy."""
    session: Session = SessionLocal()
    try:
        user = session.query(User).filter(User.id == user_id).first()
        if not user or not user.push_enabled:
            return

        cards = load_cards()
        today = date.today()

        if user.last_card and user.last_card_date == today:
            title = user.last_card
            card = next((c for c in cards if c.title == title), None)
            if not card:
                card = choose_random_card(cards)
                user.last_card = card.title
                user.last_card_date = today
                user.last_activity_date = today
                user.draw_count += 1
                session.commit()
        else:
            card = choose_random_card(cards)
            user.last_card = card.title
            user.last_card_date = today
            user.last_activity_date = today
            user.draw_count += 1
            session.commit()

        caption = f"Карта дня: {card.title}\n\n{card.description}"
        image_path = card.image_path()
        try:
            if image_path:
                await bot.send_photo(chat_id=user_id, photo=InputFile(image_path), caption=caption)
            else:
                await bot.send_message(chat_id=user_id, text=caption)
        except Exception as e:
            logger.warning("Не удалось отправить пуш %s: %s", user_id, e)
    finally:
        session.close()