"""Доставка свежего прайса в payment-чат с fallback для первого запуска."""

import logging
import os

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from utils.proxy import create_aiogram_session
from .payment_handlers import send_tariffs

logger = logging.getLogger(__name__)


async def open_payment_chat(message, user_id: int, source: str) -> None:
    token = os.getenv("PAYMENT_BOT_TOKEN")
    delivered = False
    if token:
        try:
            async with Bot(token=token, session=create_aiogram_session()) as payment_bot:
                await send_tariffs(payment_bot, user_id)
            delivered = True
            logger.info("[payment] tariffs_delivered user_id=%s source=%s", user_id, source)
        except TelegramForbiddenError:
            logger.info("[payment] start_required user_id=%s source=%s", user_id, source)
        except (TelegramAPIError, ValueError) as exc:
            # Не выводим токен или пользовательский вопрос в логи.
            logger.warning("[payment] tariffs_delivery_failed user_id=%s error_type=%s", user_id, type(exc).__name__)
    else:
        logger.warning("[payment] PAYMENT_BOT_TOKEN missing in main bot; using deep link")

    url = "https://t.me/Milky_payment_bot"
    if delivered:
        text = "Прайс уже отправлен в бота оплаты 🐟\nВыбери сумму пополнения — затем можно вернуться в Милки."
    else:
        url += f"?start={source}"
        text = (
            "Открой бота оплаты, чтобы выбрать сумму пополнения 🐟\n"
            "Если Telegram покажет кнопку «Начать», нажми её — появится прайс."
        )
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Открыть бота оплаты", url=url)],
    ]))
