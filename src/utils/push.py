from __future__ import annotations
import logging
import random
from datetime import date
from pathlib import Path

from aiogram import Bot
from sqlalchemy.orm import Session

from bot.keyboards import push_card_kb
from .db import SessionLocal, User

logger = logging.getLogger(__name__)

# Загрузка текстов пушей из файла src/data/pushes.txt
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
PUSHES_PATH = DATA_DIR / "pushes.txt"

def _load_push_texts() -> list[str]:
    texts: list[str] = []
    try:
        if PUSHES_PATH.exists():
            with PUSHES_PATH.open("r", encoding="utf-8") as f:
                for line in f:
                    t = line.strip()
                    if t:
                        texts.append(t)
    except Exception as e:
        logger.warning("Не удалось загрузить pushes.txt: %s", e)
    return texts

PUSH_TEXTS = _load_push_texts()
DEFAULT_PUSH_TEXT = (
    "Привет! Сегодня можно вытянуть свою карту дня. Открой бота и нажми кнопку."
)


async def send_push_card(bot: Bot, user_id: int) -> None:
    """Отправить пользователю ежедневный пуш с текстом из pushes.txt (случайная строка)."""
    session: Session = SessionLocal()
    try:
        user = session.query(User).filter(User.id == user_id).first()
        if not user or not user.push_enabled:
            return

        today = date.today()
        user.last_activity_date = today
        session.commit()

        text = random.choice(PUSH_TEXTS) if PUSH_TEXTS else DEFAULT_PUSH_TEXT
        try:
            await bot.send_message(
                chat_id=user_id,
                text=text,
                reply_markup=push_card_kb(),
            )
        except Exception as e:
            logger.warning("Не удалось отправить пуш %s: %s", user_id, e)
    finally:
        session.close()
