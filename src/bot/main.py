from __future__ import annotations

import asyncio
import logging
import os
import signal

from aiogram import Bot, Dispatcher
from aiogram.enums.parse_mode import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from utils.storage import UserStorage
from utils.scheduler import PushScheduler
from utils.app_state import set_bot, set_scheduler
from utils.push import send_push_card
from .handlers import router as handlers_router

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

# Глобальный планировщик
push_scheduler = PushScheduler()


async def reschedule_user_pushes(bot: Bot) -> None:
    """Пересоздать задания по всем пользователям согласно их настройкам."""
    storage = UserStorage()
    for user_id_str, user in storage.get_users().items():
        user_id = int(user_id_str)
        if user.get("push_enabled", True):
            push_scheduler.schedule_daily(
                user_id,
                user.get("push_time", UserStorage.DEFAULT_PUSH_TIME),
                lambda user_id: asyncio.create_task(send_push_card(bot, user_id)),
            )
        else:
            push_scheduler.remove(user_id)


async def on_startup(bot: Bot) -> None:
    # На этапе старта только пересоздаём задания (инициализация уже выполнена в main())
    await reschedule_user_pushes(bot)
    logger.info("Бот запущен, планировщик активен, задания восстановлены")


async def on_shutdown(bot: Bot) -> None:
    push_scheduler.shutdown()
    logger.info("Бот остановлен")


async def main() -> None:
    bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
    dp = Dispatcher(storage=MemoryStorage())

    # Инициализация общего состояния ДО старта поллинга, чтобы хендлеры могли использовать планировщик
    set_bot(bot)
    set_scheduler(push_scheduler)
    push_scheduler.start()

    dp.include_router(handlers_router)

    # Регистрируем только пересоздание заданий и корректное завершение
    dp.startup.register(lambda: on_startup(bot))
    dp.shutdown.register(lambda: on_shutdown(bot))

    # Корректное завершение для локального запуска
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(dp.stop_polling()))

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main()) // Test
