from __future__ import annotations

"""
Точка входа второго бота оплаты (@Milky_payment_bot).

Бот поднимается отдельным процессом/сервисом и использует ту же БД,
что и основной бот Милки.
"""

import asyncio
import logging
import os
from contextlib import suppress

from utils.proxy import configure_process_proxy, create_aiogram_session
from utils.db import init_db
from utils.pricing import ensure_default_prices
from utils.payment_processing import payment_worker

configure_process_proxy()

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from .payment_handlers import router as payment_router
from .payment_messages import send_success_notification

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


PAYMENT_BOT_TOKEN = os.getenv("PAYMENT_BOT_TOKEN")
if not PAYMENT_BOT_TOKEN:
    raise RuntimeError("PAYMENT_BOT_TOKEN is not set")


async def on_startup() -> None:
    init_db()
    ensure_default_prices()


async def main() -> None:
    """
    Запуск второго бота-оплатника.
    """
    bot = Bot(
        token=PAYMENT_BOT_TOKEN,
        session=create_aiogram_session(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())

    dp.include_router(payment_router)

    main_token = os.getenv("BOT_TOKEN")
    if not main_token:
        raise RuntimeError("BOT_TOKEN is required for durable payment notifications in Milky")
    main_bot = Bot(
        token=main_token,
        session=create_aiogram_session(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    async def deliver(channel, result):
        target = main_bot if channel == "main" else bot
        await send_success_notification(target, result, channel)

    # Initialise tables before reconciliation and before accepting updates.
    await on_startup()
    worker = asyncio.create_task(payment_worker(deliver))
    logger.info("Запускаю бота оплаты (@Milky_payment_bot) со сверкой платежей из БД")
    try:
        await dp.start_polling(bot)
    finally:
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker
        await main_bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
