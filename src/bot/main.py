from __future__ import annotations

import asyncio
import logging
import os
import signal

from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.default import DefaultBotProperties

from utils.scheduler import PushScheduler, DEFAULT_PUSH_TIME
from utils.app_state import set_bot, set_scheduler
from utils.push import send_push_card
from utils.db import SessionLocal, User
from .handlers import router as handlers_router

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

# Глобальный планировщик
push_scheduler = PushScheduler()


async def reschedule_user_pushes(bot: Bot) -> None:
    """Пересоздать задания по пользователям согласно настройкам в базе."""
    with SessionLocal() as session:
        users = [
            {
                "id": user.id,
                "push_time": user.push_time or DEFAULT_PUSH_TIME,
                "push_enabled": bool(user.push_enabled),
            }
            for user in session.query(User).all()
        ]

    for user in users:
        if user["push_enabled"]:
            push_scheduler.schedule_daily(
                user["id"],
                user["push_time"],
                lambda user_id, _bot=bot: send_push_card(_bot, user_id),
            )
        else:
            push_scheduler.remove(user["id"])


async def on_startup(bot: Bot) -> None:
    push_scheduler.start()
    set_bot(bot)
    set_scheduler(push_scheduler)
    await reschedule_user_pushes(bot)
    logger.info("Бот запущен, планировщик активен, задания восстановлены")


async def on_shutdown(bot: Bot) -> None:
    push_scheduler.shutdown()
    logger.info("Бот остановлен")


async def main() -> None:
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())

    # Инициализация общего состояния ДО старта поллинга
    loop = asyncio.get_running_loop()
    push_scheduler.configure(loop)
    set_bot(bot)
    set_scheduler(push_scheduler)

    dp.include_router(handlers_router)

    # Регистрируем события старта и завершения
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    # Корректное завершение для локального запуска
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(dp.stop_polling()))

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
