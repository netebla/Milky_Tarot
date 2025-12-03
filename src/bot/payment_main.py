from __future__ import annotations

"""
Точка входа второго бота оплаты (@Milky_payment_bot).

Бот поднимается отдельным процессом/сервисом и использует ту же БД,
что и основной бот Милки.
"""

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from .payment_handlers import router as payment_router

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


PAYMENT_BOT_TOKEN = os.getenv("PAYMENT_BOT_TOKEN")
if not PAYMENT_BOT_TOKEN:
    raise RuntimeError("PAYMENT_BOT_TOKEN is not set")


async def main() -> None:
    """
    Запуск второго бота-оплатника.
    """
    bot = Bot(
        token=PAYMENT_BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())

    dp.include_router(payment_router)

    logger.info("Запускаю бота оплаты (@Milky_payment_bot)")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

