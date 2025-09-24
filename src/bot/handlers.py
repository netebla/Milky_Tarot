from __future__ import annotations

import logging
import os
from datetime import date
from typing import Optional
import asyncio

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InputFile

from utils.storage import UserStorage
from utils.cards_loader import load_cards, choose_random_card
from utils.app_state import get_scheduler, get_bot
from utils.push import send_push_card
from .keyboards import main_menu_kb, settings_inline_kb, choose_time_kb

logger = logging.getLogger(__name__)

router = Router()

# Загружаем карты один раз при импорте модуля
try:
    CARDS = load_cards()
except Exception as e:
    logger.error("Не удалось загрузить карты: %s", e)
    CARDS = []

# Поддержка нескольких админов: ADMIN_ID может содержать список ID через запятую
_ADMIN_RAW = os.getenv("ADMIN_ID") or os.getenv("ADMIN_IDS") or ""
ADMIN_IDS = {s.strip() for s in _ADMIN_RAW.split(",") if s.strip()}


async def _send_card_of_the_day(message: Message, user_id: int) -> None:
    storage = UserStorage()
    user = storage.ensure_user(user_id, message.from_user.username if message.from_user else None)
    today = date.today().isoformat()

    # Если сегодня уже тянули — возвращаем сохранённую
    if user.get("last_card") and user.get("last_card_date") == today:
        title = user["last_card"]
        card = next((c for c in CARDS if c.title == title), None)
        if card:
            await _send_card_message(message, card)
            return

    if not CARDS:
        await message.answer("Карты не загружены. Обратитесь к администратору.")
        return

    card = choose_random_card(CARDS)
    storage.set_last_card(user_id, card.title)
    await _send_card_message(message, card)


async def _send_card_message(message: Message, card) -> None:
    caption = f"Карта дня: {card.title}\n\n{card.description}"
    image_path = card.image_path()
    if image_path:
        await message.answer_photo(InputFile(image_path), caption=caption)
    else:
        await message.answer(caption)


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    storage = UserStorage()
    user = storage.ensure_user(message.from_user.id, message.from_user.username if message.from_user else None)

    # Гарантируем расписание при первом старте
    scheduler = get_scheduler()
    if user.get("push_enabled", True):
        scheduler.schedule_daily(
            message.from_user.id,
            user.get("push_time", UserStorage.DEFAULT_PUSH_TIME),
            lambda user_id: asyncio.create_task(send_push_card(get_bot(), user_id)),
        )

    await message.answer(
        "Привет! Я Таролог. Нажми кнопку, чтобы вытянуть карту дня.",
        reply_markup=main_menu_kb(),
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "Бот тянет карту дня, отправляет ежедневные напоминания и хранит настройки."
    )


@router.message(F.text == "Вытянуть карту дня")
async def btn_card(message: Message) -> None:
    await _send_card_of_the_day(message, message.from_user.id)


@router.message(F.text == "Помощь")
async def btn_help(message: Message) -> None:
    await cmd_help(message)


@router.message(F.text == "Мои настройки")
async def btn_settings(message: Message) -> None:
    storage = UserStorage()
    user = storage.get_user(message.from_user.id) or {}
    push_enabled = bool(user.get("push_enabled", True))
    push_time = user.get("push_time", UserStorage.DEFAULT_PUSH_TIME)
    await message.answer(
        f"Настройки пушей:\n\nСостояние: {'Включены' if push_enabled else 'Выключены'}\nВремя: {push_time}",
        reply_markup=settings_inline_kb(push_enabled),
    )


@router.callback_query(F.data == "change_push_time")
async def cb_change_time(cb: CallbackQuery) -> None:
    await cb.message.edit_text("Выберите время отправки уведомления:", reply_markup=choose_time_kb())
    await cb.answer()


@router.callback_query(F.data.startswith("set_time:"))
async def cb_set_time(cb: CallbackQuery) -> None:
    time_str = cb.data.split(":", 1)[1]
    storage = UserStorage()
    storage.set_push_time(cb.from_user.id, time_str)

    # Перепланируем пуш на новое время
    scheduler = get_scheduler()
    scheduler.schedule_daily(
        cb.from_user.id,
        time_str,
        lambda user_id: asyncio.create_task(send_push_card(get_bot(), user_id)),
    )

    await cb.message.edit_text(f"Время пуша обновлено на {time_str}.")
    await cb.answer()


@router.callback_query(F.data == "cancel_time")
async def cb_cancel_time(cb: CallbackQuery) -> None:
    await cb.message.edit_text("Настройки обновлены.")
    await cb.answer()


@router.callback_query(F.data == "push_off")
async def cb_push_off(cb: CallbackQuery) -> None:
    storage = UserStorage()
    storage.set_push_enabled(cb.from_user.id, False)

    scheduler = get_scheduler()
    scheduler.remove(cb.from_user.id)

    await cb.message.edit_text("Пуши отключены.")
    await cb.answer()


@router.callback_query(F.data == "push_on")
async def cb_push_on(cb: CallbackQuery) -> None:
    storage = UserStorage()
    storage.set_push_enabled(cb.from_user.id, True)

    user = storage.get_user(cb.from_user.id) or {}
    scheduler = get_scheduler()
    scheduler.schedule_daily(
        cb.from_user.id,
        user.get("push_time", UserStorage.DEFAULT_PUSH_TIME),
        lambda user_id: asyncio.create_task(send_push_card(get_bot(), user_id)),
    )

    await cb.message.edit_text("Пуши включены.")
    await cb.answer()


@router.message(Command("admin_stats"))
async def admin_stats(message: Message) -> None:
    # Доступ только для админов (список в ADMIN_ID через запятую)
    if str(message.from_user.id) not in ADMIN_IDS:
        await message.answer("Недостаточно прав.")
        return
    storage = UserStorage()
    total_users, active_today, total_draws = storage.get_stats()
    await message.answer(
        f"Статистика:\nПользователей: {total_users}\nАктивны сегодня: {active_today}\nВытянуто карт (всего): {total_draws}"
    ) 