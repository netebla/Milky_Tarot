from __future__ import annotations

"""Точка входа отдельного закрытого админ-бота Milky."""

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
from utils.pricing import ensure_default_prices
from .admin_handlers import configure_admin_commands, router as admin_router

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ADMIN_BOT_TOKEN = os.getenv("ADMIN_BOT_TOKEN")
if not ADMIN_BOT_TOKEN:
    raise RuntimeError("ADMIN_BOT_TOKEN is not set")

MAIN_BOT_TOKEN = os.getenv("BOT_TOKEN")
if not MAIN_BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set for admin broadcasts")


async def on_startup(bot: Bot) -> None:
    init_db()
    ensure_default_prices()
    await configure_admin_commands(bot)
    me = await bot.get_me()
    logger.info("Админ-бот @%s запущен", me.username)


async def main() -> None:
    bot = Bot(
        token=ADMIN_BOT_TOKEN,
        session=create_aiogram_session(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    main_bot = Bot(
        token=MAIN_BOT_TOKEN,
        session=create_aiogram_session(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher["main_bot"] = main_bot
    dispatcher.include_router(admin_router)
    dispatcher.startup.register(on_startup)
    try:
        await dispatcher.start_polling(bot)
    finally:
        await main_bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
