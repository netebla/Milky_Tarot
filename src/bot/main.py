from __future__ import annotations

import asyncio
import logging
import os
import signal
from datetime import datetime, timedelta
from pathlib import Path

from utils.proxy import configure_process_proxy, create_aiogram_session

configure_process_proxy()

from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.default import DefaultBotProperties

from utils.scheduler import PushScheduler, DEFAULT_PUSH_TIME
from utils.app_state import set_bot, set_scheduler
from utils.push import send_main_menu_refresh_all, send_push_card
from utils.db import SessionLocal, User
from utils import session_manager as dialogue_sm
from .handlers import router as handlers_router
from .live_dialogue import LiveDialogueMenuExitMiddleware, router as live_dialogue_router

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

# Глобальный планировщик
push_scheduler = PushScheduler()
SUPPORT_LINK_READY_FILE = Path("/tmp/milky_support_link_ready")


def _mark_support_link_ready() -> None:
    try:
        SUPPORT_LINK_READY_FILE.touch()
    except OSError:
        logger.exception("Не удалось создать readiness-маркер ссылки поддержки")


async def resolve_support_bot_username() -> None:
    """Получить username support-бота по токену, если он не задан явно."""
    try:
        SUPPORT_LINK_READY_FILE.unlink(missing_ok=True)
    except OSError:
        logger.exception("Не удалось сбросить readiness-маркер ссылки поддержки")

    configured_username = (os.getenv("SUPPORT_BOT_USERNAME") or "").strip().lstrip("@")
    if configured_username:
        # Нормализуем значение, чтобы обработчики всегда получали username без @.
        os.environ["SUPPORT_BOT_USERNAME"] = configured_username
        _mark_support_link_ready()
        logger.info("Support bot username загружен из окружения")
        return

    support_token = (os.getenv("SUPPORT_BOT_TOKEN") or "").strip()
    if not support_token:
        logger.error("SUPPORT_BOT_USERNAME и SUPPORT_BOT_TOKEN не заданы")
        return

    support_bot = Bot(
        token=support_token,
        session=create_aiogram_session(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    try:
        support_identity = await support_bot.get_me()
        if not support_identity.username:
            logger.error("Telegram не вернул username support-бота")
            return
        os.environ["SUPPORT_BOT_USERNAME"] = support_identity.username
        _mark_support_link_ready()
        logger.info("Support bot username автоматически получен через Telegram API")
    except Exception:
        logger.exception("Не удалось автоматически получить username support-бота")
    finally:
        await support_bot.session.close()


async def reschedule_user_pushes(bot: Bot) -> None:
    """Пересоздать задания по пользователям согласно настройкам в базе."""
    with SessionLocal() as session:
        users = [
            {
                "id": user.id,
                "push_time": user.push_time or DEFAULT_PUSH_TIME,
                "push_enabled": bool(user.push_enabled),
                "tz_offset_hours": getattr(user, "tz_offset_hours", 0) or 0,
            }
            for user in session.query(User).all()
        ]

    for user in users:
        if user["push_enabled"]:
            # Ежедневно с учётом смещения пользователя.
            push_scheduler.schedule_daily_with_offset(
                user["id"],
                user["push_time"],
                user["tz_offset_hours"],
                lambda user_id, _bot=bot: send_push_card(_bot, user_id),
            )
        else:
            push_scheduler.remove(user["id"])


def _expire_stale_live_dialogues() -> None:
    try:
        with SessionLocal() as db:
            n = dialogue_sm.expire_stale_sessions(db)
        if n:
            logger.info("Автозакрыто незавершённых живых диалогов: %s", n)
    except Exception:
        logger.exception("Не удалось закрыть просроченные живые диалоги")


async def on_startup(bot: Bot) -> None:
    await resolve_support_bot_username()
    push_scheduler.start()
    set_bot(bot)
    set_scheduler(push_scheduler)
    await reschedule_user_pushes(bot)
    push_scheduler.schedule_interval_hours(
        "live-dialogue-expire",
        1,
        _expire_stale_live_dialogues,
    )
    # Тихо обновляем reply-клавиатуру всем пользователям на следующий день,
    # чтобы старые пункты меню исчезли из интерфейса клиента.
    try:
        hour, minute = map(int, DEFAULT_PUSH_TIME.split(":"))
        now = datetime.now(push_scheduler.timezone)
        run_date = (now + timedelta(days=1)).replace(
            hour=hour,
            minute=minute,
            second=0,
            microsecond=0,
        )
        push_scheduler.schedule_once(
            job_id="main-menu-refresh-all",
            run_date=run_date,
            callback=send_main_menu_refresh_all,
            kwargs={"bot": bot},
        )
    except Exception:
        logger.exception("Не удалось запланировать обновление главного меню")
    logger.info("Бот запущен, планировщик активен, задания восстановлены")


async def on_shutdown(bot: Bot) -> None:
    push_scheduler.shutdown()
    logger.info("Бот остановлен")


async def main() -> None:
    bot = Bot(
        token=BOT_TOKEN,
        session=create_aiogram_session(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.middleware(LiveDialogueMenuExitMiddleware())
    dp.callback_query.middleware(LiveDialogueMenuExitMiddleware())

    # Инициализация общего состояния ДО старта поллинга
    loop = asyncio.get_running_loop()
    push_scheduler.configure(loop)
    set_bot(bot)
    set_scheduler(push_scheduler)

    dp.include_router(handlers_router)
    dp.include_router(live_dialogue_router)

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
