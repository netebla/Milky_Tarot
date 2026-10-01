from __future__ import annotations

"""Точка входа отдельного бота поддержки Milky."""

import asyncio
import logging
import os

from utils.proxy import configure_process_proxy, create_aiogram_session

configure_process_proxy()

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from utils.db import init_db
from .support_handlers import configure_support_commands, router as support_router

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SUPPORT_BOT_TOKEN = os.getenv("SUPPORT_BOT_TOKEN")
if not SUPPORT_BOT_TOKEN:
    raise RuntimeError("SUPPORT_BOT_TOKEN is not set")


async def on_startup(bot: Bot) -> None:
    init_db()
    await configure_support_commands(bot)
    me = await bot.get_me()
    logger.info("Бот поддержки @%s запущен", me.username)


async def main() -> None:
    bot = Bot(
        token=SUPPORT_BOT_TOKEN,
        session=create_aiogram_session(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(support_router)
    dispatcher.startup.register(on_startup)
    await dispatcher.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
