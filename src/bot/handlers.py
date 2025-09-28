from __future__ import annotations


import csv
import random
import logging
import os
from datetime import date
from typing import Optional
import asyncio

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InputFile, FSInputFile

from src.utils.cards_loader import GITHUB_RAW_BASE
from utils.storage import UserStorage
from utils.cards_loader import load_cards, choose_random_card
from utils.app_state import get_scheduler, get_bot
from utils.push import send_push_card
from .keyboards import main_menu_kb, settings_inline_kb, choose_time_kb

logger = logging.getLogger(__name__)

router = Router()


import os
from aiogram.filters import Command
from aiogram.types import Message
from datetime import date
from sqlalchemy.orm import Session
from sqlalchemy import func
from utils.db import SessionLocal, User

ADMIN_IDS = os.getenv("ADMIN_ID", "")
ADMIN_IDS = [x.strip() for x in ADMIN_IDS.split(",") if x.strip()]



# Загружаем карты один раз при импорте модуля
try:
    CARDS = load_cards()
except Exception as e:
    logger.error("Не удалось загрузить карты: %s", e)
    CARDS = []

# Поддержка нескольких админов: ADMIN_ID может содержать список ID через запятую
_ADMIN_RAW = os.getenv("ADMIN_ID") or os.getenv("ADMIN_IDS") or ""
ADMIN_IDS = {s.strip() for s in _ADMIN_RAW.split(",") if s.strip()}

from utils.db import SessionLocal, User
from utils.cards_loader import load_cards, choose_random_card
from datetime import date
async def _send_card_of_the_day(message: Message, user_id: int) -> None:
    """Выдать карту дня, обновить статистику в Postgres через SQLAlchemy."""
    session = SessionLocal()
    try:
        user = session.query(User).filter(User.id == user_id).first()
        if not user:
            # Создаём нового пользователя
            user = User(
                id=user_id,
                username=message.from_user.username if message.from_user else None
            )
            session.add(user)
            session.commit()
            session.refresh(user)

        today = date.today()
        cards = load_cards()

        if user.last_card and user.last_card_date == today:
            # Уже тянули карту сегодня
            card = next((c for c in cards if c.title == user.last_card), None)
            if card:
                await _send_card_message(message, card)
                return

        # Выбираем новую карту и сохраняем в базе
        card = choose_random_card(user, cards, db=session)
        await _send_card_message(message, card)
    finally:
        session.close()



async def _send_card_message(message: Message, card) -> None:
    caption = f"Карта дня: {card.title}\n\n{card.description}"
    image_url = card.image_url()  # берём ссылку на GitHub
    if image_url:
        await message.answer_photo(image_url, caption=caption)
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

    photo = FSInputFile("/app/src/data/images/welcome.jpg")
    await message.answer_photo(
        photo=photo,
        caption=(
            "👋 Привет! Рада познакомиться и видеть тебя здесь. Я — Милки, твой спутник в мире карт. "
            "Каждый день я буду присылать твою персональную карту и показывать, на что стоит обратить внимание, "
            "какие скрытые возможности рядом и где сосредоточена твоя энергия. 🌟 С чего начнем сегодня? ❤️"
        ),
        reply_markup=main_menu_kb(),
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "Для связи с админом пишите @netebla"
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
    if str(message.from_user.id) not in ADMIN_IDS:
        await message.answer("Недостаточно прав.")
        return

    session: Session = SessionLocal()
    try:
        # всего пользователей
        total_users = session.query(User).count()

        # активные сегодня (у кого last_activity_date = сегодня)
        active_today = session.query(User).filter(User.last_activity_date == date.today()).count()

        # всего вытянуто карт (поле draw_count)
        total_draws = session.query(func.coalesce(func.sum(User.draw_count), 0)).scalar()

        await message.answer(
            f"📊 Статистика:\n"
            f"👥 Пользователей: {total_users}\n"
            f"🔥 Активны сегодня: {active_today}\n"
            f"🃏 Вытянуто карт (всего): {total_draws}"
        )
    finally:
        session.close()

class AdviceCard:
    def __init__(self, title: str, description: str):
        self.title = title
        self.description = description

    def image_url(self) -> str:
        normalized = self.title.strip().replace(" ", "_")
        return f"{GITHUB_RAW_BASE}/{normalized}.jpg"


def load_advice_cards() -> list[AdviceCard]:
    cards = []
    with open("src/data/cards_advice.csv", "r", encoding="utf-8") as f:
        reader = csv.reader(f, delimiter=";")  # без заголовка
        for row in reader:
            if len(row) < 2:
                continue
            title, description = row[0], row[1]
            cards.append(AdviceCard(title, description))
    return cards


ADVICE_CARDS = load_advice_cards()


@router.message(lambda msg: msg.text == "Узнать совет карт")
async def send_advice(message: Message):
    today = date.today()
    session: Session = SessionLocal()
    try:
        user = session.query(User).filter(User.id == message.from_user.id).first()
        if not user:
            await message.answer("Сначала нажми /start 🚀")
            return

        # сброс при новом дне
        if user.advice_last_date != today:
            user.daily_advice_count = 0
            user.advice_last_date = today

        if user.daily_advice_count >= 2:
            await message.answer("⚠️ Лимит советов на сегодня исчерпан. Следующие будут доступны завтра 🌙")
            return

        card = random.choice(ADVICE_CARDS)
        user.daily_advice_count += 1
        user.advice_last_date = today
        session.commit()

        await message.answer_photo(
            photo=card.image_url(),
            caption=f"✨ Совет карт: {card.title}\n\n{card.description}"
        )
    finally:
        session.close()
