from __future__ import annotations

import logging
from datetime import date

from aiogram import Bot
from aiogram.types import InputFile

from .storage import UserStorage
from .cards_loader import load_cards, choose_random_card

logger = logging.getLogger(__name__)


async def send_push_card(bot: Bot, user_id: int) -> None:
    """Отправить пользователю ежедневный пуш с «картой дня», если пуши включены.
    Берёт карту на сегодня (или тянет новую), обновляет статистику и отправляет сообщение.
    """
    storage = UserStorage()
    user = storage.get_user(user_id)
    if not user or not user.get("push_enabled", True):
        return

    cards = load_cards()
    today = date.today().isoformat()

    if user.get("last_card") and user.get("last_card_date") == today:
        title = user["last_card"]
        card = next((c for c in cards if c.title == title), None)
        if not card:
            card = choose_random_card(cards)
            storage.set_last_card(user_id, card.title)
    else:
        card = choose_random_card(cards)
        storage.set_last_card(user_id, card.title)

    caption = f"Карта дня: {card.title}\n\n{card.description}"
    image_path = card.image_path()
    try:
        if image_path:
            await bot.send_photo(chat_id=user_id, photo=InputFile(image_path), caption=caption)
        else:
            await bot.send_message(chat_id=user_id, text=caption)
    except Exception as e:
        logger.warning("Не удалось отправить пуш %s: %s", user_id, e) 