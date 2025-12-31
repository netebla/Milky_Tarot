from __future__ import annotations

import csv
import asyncio
import logging
import os
import random
import secrets
import time
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote

import httpx
import pytz
from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import func
from sqlalchemy.orm import Session

from utils.app_state import get_bot, get_scheduler
from utils.cards_loader import (
    GITHUB_RAW_BASE,
    IMAGES_DIR,
    ALT_DESCRIPTIONS,
    choose_random_card,
    load_cards,
)
from utils.year_energy_loader import load_year_energy_archetypes, get_archetype_by_card
from utils.db import SessionLocal, User
from utils.push import send_push_card
from utils.scheduler import DEFAULT_PUSH_TIME
from llm.three_cards import generate_three_card_reading
from llm.new_year_reading import generate_new_year_reading, NEW_YEAR_QUESTIONS
from utils.fish import tariff_to_amounts
from .keyboards import (
    advice_draw_kb,
    choose_time_kb,
    main_menu_kb,
    settings_inline_kb,
    choose_tz_offset_kb,
    onboarding_name_kb,
    choose_tz_mode_kb,
    fish_balance_kb,
    fish_tariff_kb,
    fish_payment_method_kb,
    admin_push_with_reading_kb,
    admin_push_type_kb,
    admin_push_year_energy_kb,
)

logger = logging.getLogger(__name__)

router = Router()


# Загружаем карты один раз при импорте модуля
try:
    CARDS = load_cards()
except Exception as e:
    logger.error("Не удалось загрузить карты: %s", e)
    CARDS = []


_ADMIN_RAW = os.getenv("ADMIN_ID") or os.getenv("ADMIN_IDS") or ""
ADMIN_IDS = {s.strip() for s in _ADMIN_RAW.split(",") if s.strip()}
PENDING_PUSHES: dict[str, dict[str, object]] = {}


class ThreeCardsStates(StatesGroup):
    waiting_context = State()
    waiting_question = State()


class NewYearReadingStates(StatesGroup):
    in_progress = State()


class YearEnergyStates(StatesGroup):
    waiting_selection = State()


class OnboardingStates(StatesGroup):
    asking_name = State()
    waiting_name_manual = State()
    asking_birth_date = State()
    asking_tz = State()


class FishPaymentStates(StatesGroup):
    viewing_balance = State()
    choosing_tariff = State()
    choosing_payment_method = State()


class AdminPushStates(StatesGroup):
    waiting_text = State()
    waiting_push_type = State()


def _is_admin(user_id: int) -> bool:
    return str(user_id) in ADMIN_IDS


async def _fetch_image_bytes(url: str) -> bytes:
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.content


def _get_or_create_user(session: Session, user_id: int, username: str | None) -> User:
    user = session.query(User).filter(User.id == user_id).first()
    if not user:
        user = User(id=user_id)
        session.add(user)

    user.username = username
    if not user.push_time:
        user.push_time = DEFAULT_PUSH_TIME

    user.last_activity_date = date.today()

    session.commit()
    session.refresh(user)
    return user


def _choose_year_energy_card(user_id: int, archetypes: dict[str, str]) -> tuple[str, bool]:
    """
    Выбирает карту для расклада "Энергия года".
    
    Если у пользователя уже есть сохраненная карта, возвращает её.
    Иначе выбирает случайную карту и сохраняет её в БД.
    
    Returns:
        tuple[название_карты, была_ли_карта_уже_сохранена]
    """
    with SessionLocal() as session:
        db_user = _get_or_create_user(session, user_id, None)
        
        # Если карта уже сохранена, возвращаем её
        if db_user.year_energy_card and db_user.year_energy_card in archetypes:
            return db_user.year_energy_card, True
        
        # Иначе выбираем случайную карту из доступных архетипов
        available_cards = [card for card in CARDS if card.title in archetypes]
        if not available_cards:
            raise ValueError("Не найдено карт для расклада.")
        
        selected_card = random.choice(available_cards)
        card_title = selected_card.title
        
        # Сохраняем выбранную карту
        db_user.year_energy_card = card_title
        session.commit()
        
        return card_title, False


async def _send_card_image(message_or_cb: Message | CallbackQuery, card_title: str) -> bool:
    """
    Отправляет изображение карты.
    
    Returns:
        True если изображение было успешно отправлено, False иначе
    """
    # Находим карту по названию
    selected_card = next((card for card in CARDS if card.title == card_title), None)
    if not selected_card:
        return False
    
    # Определяем объект для отправки сообщения
    if isinstance(message_or_cb, CallbackQuery):
        send_func = message_or_cb.message.answer_photo
    else:
        send_func = message_or_cb.answer_photo
    
    # Пытаемся отправить локальное изображение
    local_path = getattr(selected_card, "image_path", None)
    if callable(local_path):
        path = local_path()
        if path.exists():
            try:
                await send_func(
                    photo=BufferedInputFile(path.read_bytes(), filename=path.name),
                    caption=selected_card.title,
                )
                return True
            except TelegramBadRequest:
                pass
    
    # Пытаемся загрузить изображение по URL
    try:
        image_bytes = await _fetch_image_bytes(selected_card.image_url())
        await send_func(
            photo=BufferedInputFile(image_bytes, filename=f"{selected_card.title}.jpg"),
            caption=selected_card.title,
        )
        return True
    except (httpx.HTTPError, TelegramBadRequest, TelegramNetworkError):
        pass
    
    # Если не удалось отправить изображение, отправляем только название
    if isinstance(message_or_cb, CallbackQuery):
        await message_or_cb.message.answer(selected_card.title)
    else:
        await message_or_cb.answer(selected_card.title)
    
    return False


async def _start_three_cards_flow(message: Message, state: FSMContext) -> None:
    if len(CARDS) < 3:
        await message.answer("Недостаточно карт для расклада.")
        await state.clear()
        return

    await state.clear()

    user = message.from_user
    user_id = user.id if user else None
    username = user.username if user else None

    if user_id is not None:
        with SessionLocal() as session:
            _get_or_create_user(session, user_id, username)

    selected_cards = random.sample(CARDS, 3)
    await state.set_state(ThreeCardsStates.waiting_context)
    await state.update_data(three_cards=[card.title for card in selected_cards])


async def _send_card_of_the_day(message: Message, user_id: int) -> None:
    """Выдать карту дня, обновить статистику в Postgres через SQLAlchemy."""
    session = SessionLocal()
    try:
        username = message.from_user.username if message.from_user else None
        user = _get_or_create_user(session, user_id, username)

        today = date.today()
        cards = CARDS or load_cards()

        if user.last_card and user.last_card_date == today:
            # Уже тянули карту сегодня
            card = next((c for c in cards if c.title == user.last_card), None)
            if card:
                user.last_activity_date = today
                session.commit()
                await _send_card_message(message, card)
                return

        # Выбираем новую карту и сохраняем в базе
        card = choose_random_card(user, cards, db=session)
        await _send_card_message(message, card)
    finally:
        session.close()



async def _send_card_message(message: Message, card) -> None:
    # Описание выбираем случайно: основное или альтернативное (если есть во втором CSV)
    alt_desc = ALT_DESCRIPTIONS.get(card.title)
    if alt_desc:
        description = random.choice([card.description, alt_desc])
    else:
        description = card.description

    caption = f"Карта дня: {card.title}\n\n{description}"
    local_path = getattr(card, "image_path", None)
    if callable(local_path):
        path = local_path()
        if path.exists():
            try:
                await message.answer_photo(
                    BufferedInputFile(path.read_bytes(), filename=path.name),
                    caption=caption,
                )
                return
            except TelegramBadRequest:
                pass

    try:
        image_bytes = await _fetch_image_bytes(card.image_url())
        await message.answer_photo(
            BufferedInputFile(image_bytes, filename=f"{card.title}.jpg"),
            caption=caption,
        )
    except (httpx.HTTPError, TelegramBadRequest, TelegramNetworkError):
        await message.answer(caption)


@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    user_id = message.from_user.id
    username = message.from_user.username if message.from_user else None
    today = date.today()

    with SessionLocal() as session:
        user = _get_or_create_user(session, user_id, username)
        push_enabled = bool(user.push_enabled)
        push_time = user.push_time or DEFAULT_PUSH_TIME
        tz_offset = getattr(user, "tz_offset_hours", 0) or 0
        display_name = getattr(user, "display_name", None)
        birth_date = getattr(user, "birth_date", None)
        show_three_cards = _is_admin(user_id)

    # Планируем ежедневный пуш с учётом смещения
    if push_enabled:
        scheduler = get_scheduler()
        bot = get_bot()
        scheduler.schedule_daily_with_offset(
            user_id,
            push_time,
            tz_offset,
            lambda user_id, _bot=bot: send_push_card(_bot, user_id),
        )

    welcome_path = Path("/app/src/data/images/welcome.jpg")
    welcome_text = (
        "Привет! Я Милки, твой спутник в мире карт🪐\n\n"
        "Я помогу тебе настроиться на день, а также дам ответы на самые волнующие вопросы ☀️\n\n"
        "Но для начала, давай познакомимся?"
    )

    if welcome_path.exists():
        try:
            await message.answer_photo(
                photo=BufferedInputFile(welcome_path.read_bytes(), filename=welcome_path.name),
                caption=welcome_text,
                reply_markup=None,
            )
        except TelegramBadRequest:
            pass
    else:
        await message.answer(welcome_text)

    # Если не заполнены обязательные поля — запускаем онбординг
    if not display_name or birth_date is None:
        has_username = bool(message.from_user and (message.from_user.username or message.from_user.full_name))
        await message.answer(
            "Твое имя?",
            reply_markup=onboarding_name_kb(has_username),
        )
        await state.set_state(OnboardingStates.asking_name)
        return
    # Иначе сразу в меню
    await message.answer(
        "Готово. Чем займёмся?",
        reply_markup=main_menu_kb(show_three_cards),
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


@router.message(F.text == "Мои рыбки")
async def btn_my_fish(message: Message, state: FSMContext) -> None:
    user = message.from_user
    if not user:
        await message.answer("Сначала нажми /start 🚀")
        return

    with SessionLocal() as session:
        db_user = session.query(User).filter(User.id == user.id).first()
        if not db_user:
            db_user = User(id=user.id)
            session.add(db_user)
            session.commit()
        balance = getattr(db_user, "fish_balance", 0) or 0

    await state.set_state(FishPaymentStates.viewing_balance)
    await message.answer(
        f"На твоем балансе сейчас {balance} 🐟\n\n"
        "Рыбки — это внутренняя валюта за расклады.\n"
        "Можешь пополнить баланс или вернуться в главное меню.",
        reply_markup=fish_balance_kb(),
    )


@router.message(F.text == "Мои настройки")
async def btn_settings(message: Message) -> None:
    with SessionLocal() as session:
        user = session.query(User).filter(User.id == message.from_user.id).first()

    if not user:
        await message.answer("Сначала нажми /start 🚀")
        return

    push_enabled = bool(user.push_enabled)
    push_time = user.push_time or DEFAULT_PUSH_TIME
    await message.answer(
        f"Настройки пушей:\n\nСостояние: {'Включены' if push_enabled else 'Выключены'}\nВремя: {push_time}",
        reply_markup=settings_inline_kb(push_enabled),
    )


@router.message(F.text == "Задать свой вопрос")
async def btn_three_cards(message: Message, state: FSMContext) -> None:
    user = message.from_user
    if not user:
        await message.answer("Сначала нажми /start 🚀")
        return

    await _start_three_cards_flow(message, state)

    intro_text_1 = (
        "Мяу, давай посмотрим глубже 🐈‍⬛\n"
        "«Задать свой вопрос» — это расклад из трёх карт, который показывает:\n"
        "• что сейчас происходит,\n"
        "• куда всё движется,\n"
        "• к чему это может привести.\n\n"
        "Один такой расклад я делаю бесплатно раз в день.\n"
        "Если захочешь ещё — можно будет сделать дополнительный за рыбки."
    )
    intro_text_2 = (
        "Перед тем как спросить, коротко опиши свою ситуацию — так я лучше почувствую, что происходит, и подберу точные ответы.\n"
        "Если не готов рассказывать историю, нажми на кнопку «Сразу к вопросу», и мы начнем! 🌟"
    )

    await message.answer(intro_text_1)
    await message.answer(
        intro_text_2,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Сразу к вопросу",
                        callback_data="three_keys_go_to_question",
                    )
                ]
            ]
        ),
    )


@router.callback_query(F.data == "change_push_time")
async def cb_change_time(cb: CallbackQuery) -> None:
    await cb.message.edit_text("Выберите время отправки уведомления:", reply_markup=choose_time_kb())
    await cb.answer()


@router.message(F.text == "Пополнить баланс 🐟")
async def msg_fish_topup(message: Message, state: FSMContext) -> None:
    user = message.from_user
    if not user:
        await message.answer("Сначала нажми /start 🚀")
        return

    await state.clear()
    await message.answer(
        "Чтобы пополнить баланс рыбок, перейди в бота оплаты.\n\n"
        "Там можно выбрать тариф, оплатить через ЮKassa и вернуться обратно в Милки.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Открыть бота оплаты",
                        url="https://t.me/Milky_payment_bot",
                    )
                ]
            ]
        ),
    )


@router.callback_query(F.data == "fish_topup")
async def cb_fish_topup(cb: CallbackQuery, state: FSMContext) -> None:
    """Инлайн-кнопка пополнения баланса из экрана 'Мои рыбки'."""
    user = cb.from_user
    if not user:
        await cb.answer()
        return

    await state.clear()
    await cb.message.answer(
        "Чтобы пополнить баланс рыбок, перейди в бота оплаты.\n\n"
        "Там можно выбрать тариф, оплатить через ЮKassa и вернуться обратно в Милки.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Открыть бота оплаты",
                        url="https://t.me/Milky_payment_bot",
                    )
                ]
            ]
        ),
    )
    await cb.answer()


@router.message(F.text == "Главное меню")
async def msg_main_menu_from_anywhere(message: Message, state: FSMContext) -> None:
    """Позволяет вернуться в главное меню с любой сцены FSM."""
    await state.clear()
    await message.answer(
        "Готово. Чем займёмся?",
        reply_markup=main_menu_kb(_is_admin(message.from_user.id)),
    )


@router.callback_query(F.data.startswith("set_time:"))
async def cb_set_time(cb: CallbackQuery) -> None:
    time_str = cb.data.split(":", 1)[1]
    user_id = cb.from_user.id

    # Сразу отвечаем на callback, чтобы убрать "крутилку" у пользователя
    try:
        await cb.answer("Время пуша обновляю ✨")
    except TelegramBadRequest:
        logger.exception("Не удалось ответить на callback при выборе времени пуша")

    # Обновляем настройки в БД и перепланируем пуши
    try:
        with SessionLocal() as session:
            user = session.query(User).filter(User.id == user_id).first()
            if not user:
                user = User(id=user_id)
                session.add(user)
            user.push_time = time_str
            user.push_enabled = True
            session.commit()

        scheduler = get_scheduler()
        bot = get_bot()
        # Пользователь изменил время -> планируем ежедневный пуш с учётом смещения
        scheduler.schedule_daily_with_offset(
            user_id,
            time_str,
            getattr(user, "tz_offset_hours", 0) or 0,
            lambda user_id, _bot=bot: send_push_card(_bot, user_id),
        )
    except Exception:
        logger.exception("Ошибка при обновлении времени пуша для пользователя %s", user_id)

    # Сообщаем пользователю, что время обновлено
    try:
        await cb.message.edit_text(f"Время пуша обновлено на {time_str}.")
    except TelegramBadRequest:
        # Если не удалось отредактировать сообщение (удалено/устарело),
        # просто отправим новое с подтверждением.
        try:
            await cb.message.answer(f"Время пуша обновлено на {time_str}.")
        except TelegramBadRequest:
            logger.exception("Не удалось отправить подтверждение об обновлении времени пуша")

    # После обновления времени возвращаем пользователя в главное меню
    try:
        await cb.message.answer(
            "Готово. Чем займёмся?",
            reply_markup=main_menu_kb(_is_admin(user_id)),
        )
    except TelegramBadRequest:
        # Если по какой-то причине ответить в это сообщение нельзя —
        # пробуем отправить меню напрямую пользователю
        try:
            bot = get_bot()
            await bot.send_message(
                chat_id=user_id,
                text="Готово. Чем займёмся?",
                reply_markup=main_menu_kb(_is_admin(user_id)),
            )
        except Exception:
            logger.exception("Не удалось отправить главное меню после изменения времени пуша")


@router.callback_query(F.data.startswith("fish_tariff:"))
async def cb_fish_select_tariff(cb: CallbackQuery, state: FSMContext) -> None:
    user = cb.from_user
    if not user or not _is_admin(user.id):
        await cb.answer()
        return

    try:
        amount_str = cb.data.split(":", 1)[1]
        amount = int(amount_str)
    except (IndexError, ValueError):
        await cb.answer()
        return

    # Сохраняем выбранный тариф в состоянии
    await state.update_data(selected_tariff=amount)
    await state.set_state(FishPaymentStates.choosing_payment_method)

    await cb.message.edit_text(
        "Отличный выбор! Теперь выбери удобный способ оплаты:",
        reply_markup=fish_payment_method_kb(),
    )
    await cb.answer()


@router.callback_query(F.data == "fish_back_to_balance")
async def cb_fish_back_to_balance(cb: CallbackQuery, state: FSMContext) -> None:
    user = cb.from_user
    if not user or not _is_admin(user.id):
        await cb.answer()
        return

    with SessionLocal() as session:
        db_user = session.query(User).filter(User.id == user.id).first()
        balance = getattr(db_user, "fish_balance", 0) if db_user else 0

    await state.set_state(FishPaymentStates.viewing_balance)
    await cb.message.edit_text(
        f"На твоем балансе сейчас {balance} 🐟\n\n"
        "Можешь пополнить баланс или вернуться в главное меню.",
    )
    await cb.message.answer(
        "Что дальше?",
        reply_markup=fish_balance_kb(),
    )
    await cb.answer()


@router.callback_query(F.data == "fish_back_to_tariffs")
async def cb_fish_back_to_tariffs(cb: CallbackQuery, state: FSMContext) -> None:
    user = cb.from_user
    if not user or not _is_admin(user.id):
        await cb.answer()
        return

    await state.set_state(FishPaymentStates.choosing_tariff)
    await cb.message.edit_text(
        "Выберите, сколько рыбок хотите приобрести:\n"
        "50₽ – 350 🐟\n"
        "150₽ – 1050 🐟\n"
        "300₽ – 2100 🐟\n"
        "650₽ – 4550 🐟",
        reply_markup=fish_tariff_kb(),
    )
    await cb.answer()


@router.callback_query(F.data == "fish_main_menu")
async def cb_fish_main_menu(cb: CallbackQuery, state: FSMContext) -> None:
    user = cb.from_user
    if not user:
        await cb.answer()
        return

    await state.clear()
    try:
        await cb.message.edit_text("Возвращаю в главное меню.")
    except TelegramBadRequest:
        pass

    bot = get_bot()
    await bot.send_message(
        chat_id=user.id,
        text="Готово. Чем займёмся?",
        reply_markup=main_menu_kb(_is_admin(user.id)),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("fish_pay:"))
async def cb_fish_pay(cb: CallbackQuery, state: FSMContext) -> None:
    user = cb.from_user
    if not user or not _is_admin(user.id):
        await cb.answer()
        return

    data = await state.get_data()
    amount = int(data.get("selected_tariff", 0) or 0)
    total_fish, bonus_fish = tariff_to_amounts(amount)
    if total_fish == 0:
        await cb.answer("Не удалось определить тариф, попробуй ещё раз.")
        return

    # Начисляем рыбки
    with SessionLocal() as session:
        db_user = session.query(User).filter(User.id == user.id).first()
        if not db_user:
            db_user = User(id=user.id)
            session.add(db_user)
        current_balance = getattr(db_user, "fish_balance", 0) or 0
        db_user.fish_balance = current_balance + total_fish
        session.commit()
        new_balance = db_user.fish_balance

    method = cb.data.split(":", 1)[1]
    method_human = {
        "sbp": "СБП",
        "card": "картой",
        "stars": "звёздами Telegram",
    }.get(method, "выбранным способом")

    await state.clear()

    text_lines = [
        f"Оплата {method_human} прошла успешно ✨",
        f"Тебе начислено {total_fish} 🐟.",
    ]
    if bonus_fish > 0:
        text_lines.append(f"Из них {bonus_fish} рыбок — бонусные 🎁")
    text_lines.append(f"Твой новый баланс: {new_balance} 🐟")

    await cb.message.edit_text("\n".join(text_lines))
    await cb.message.answer(
        "Готово. Чем займёмся?",
        reply_markup=main_menu_kb(_is_admin(user.id)),
    )
    await cb.answer()


@router.callback_query(F.data == "cancel_time")
async def cb_cancel_time(cb: CallbackQuery) -> None:
    await cb.message.edit_text("Настройки обновлены.")
    await cb.answer()


@router.callback_query(F.data == "push_off")
async def cb_push_off(cb: CallbackQuery) -> None:
    with SessionLocal() as session:
        user = session.query(User).filter(User.id == cb.from_user.id).first()
        if user:
            user.push_enabled = False
            session.commit()

    scheduler = get_scheduler()
    scheduler.remove(cb.from_user.id)

    await cb.message.edit_text("Пуши отключены.")
    await cb.answer()


@router.callback_query(F.data == "push_on")
async def cb_push_on(cb: CallbackQuery) -> None:
    user_id = cb.from_user.id

    with SessionLocal() as session:
        user = session.query(User).filter(User.id == user_id).first()
        if not user:
            user = User(id=user_id)
            session.add(user)
        user.push_enabled = True
        if not user.push_time:
            user.push_time = DEFAULT_PUSH_TIME
        session.commit()

        push_time = user.push_time or DEFAULT_PUSH_TIME
        tz_offset = getattr(user, "tz_offset_hours", 0) or 0

    scheduler = get_scheduler()
    bot = get_bot()
    # Включаем пуши: ежедневно с учётом смещения
    scheduler.schedule_daily_with_offset(
        user_id,
        push_time,
        tz_offset,
        lambda user_id, _bot=bot: send_push_card(_bot, user_id),
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

        # активные сегодня — пользователи, которые сегодня вытянули хотя бы одну карту
        today = date.today()
        active_today = (
            session.query(User)
            .filter(
                User.draw_count > 0,
                User.last_activity_date == today,
            )
            .count()
        )

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


@router.message(Command("admin_push"))
async def admin_push(message: Message, state: FSMContext) -> None:
    if str(message.from_user.id) not in ADMIN_IDS:
        await message.answer("Недостаточно прав.")
        return

    parts = (message.text or "").split(" ", 1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer(
            "Пришли текст рассылки отдельным сообщением.\n"
            "Форматирование (включая спойлеры) сохранится."
        )
        await state.set_state(AdminPushStates.waiting_text)
        return

    push_text_html = parts[1].strip()
    token = secrets.token_urlsafe(8)
    PENDING_PUSHES[token] = {
        "text_html": push_text_html,
        "created_at": time.time(),
        "push_type": None,
    }

    await message.answer(
        f"Проверь текст пуша и выбери тип:\n\n{push_text_html}",
        parse_mode="HTML",
        reply_markup=admin_push_type_kb(token),
    )


@router.message(AdminPushStates.waiting_text)
async def admin_push_text(message: Message, state: FSMContext) -> None:
    if str(message.from_user.id) not in ADMIN_IDS:
        await message.answer("Недостаточно прав.")
        await state.clear()
        return

    push_text_html = (message.html_text or message.text or "").strip()
    if not push_text_html:
        await message.answer("Пожалуйста, отправь текст одним сообщением.")
        return

    token = secrets.token_urlsafe(8)
    PENDING_PUSHES[token] = {
        "text_html": push_text_html,
        "created_at": time.time(),
        "push_type": None,
    }
    await state.clear()

    await message.answer(
        f"Проверь текст пуша и выбери тип:\n\n{push_text_html}",
        parse_mode="HTML",
        reply_markup=admin_push_type_kb(token),
    )


@router.callback_query(F.data.startswith("admin_push_confirm:"))
async def cb_admin_push_confirm(cb: CallbackQuery) -> None:
    if str(cb.from_user.id) not in ADMIN_IDS:
        await cb.answer()
        return

    try:
        await cb.answer()
    except TelegramBadRequest:
        logger.warning("Admin push confirm callback expired for admin %s", cb.from_user.id)

    token = cb.data.split(":", 1)[1]
    payload = PENDING_PUSHES.pop(token, None)
    if not payload:
        await cb.message.edit_text("Заявка на рассылку не найдена или устарела.")
        return

    push_text_html = str(payload.get("text_html") or payload.get("text") or "").strip()
    if not push_text_html:
        await cb.message.edit_text("Пустой текст рассылки. Отмена.")
        return

    push_type = payload.get("push_type", "simple")

    await cb.message.edit_text("Запускаю рассылку…")

    with SessionLocal() as session:
        user_ids = [u.id for u in session.query(User.id).all()]

    # Выбираем клавиатуру в зависимости от типа пуша
    if push_type == "reading":
        reply_markup = admin_push_with_reading_kb()
    elif push_type == "year_energy":
        reply_markup = admin_push_year_energy_kb()
    else:  # simple или по умолчанию
        reply_markup = None  # Будет использовано главное меню

    sent = 0
    failed = 0
    for uid in user_ids:
        try:
            # Для обычного пуша используем главное меню
            if push_type == "simple" or reply_markup is None:
                await cb.bot.send_message(
                    chat_id=uid,
                    text=push_text_html,
                    parse_mode="HTML",
                    reply_markup=main_menu_kb(_is_admin(uid)),
                )
            else:
                await cb.bot.send_message(
                    chat_id=uid,
                    text=push_text_html,
                    parse_mode="HTML",
                    reply_markup=reply_markup,
                )
            sent += 1
        except TelegramBadRequest:
            logger.warning("Admin push failed (bad request) for user_id=%s", uid)
            failed += 1
        except TelegramNetworkError:
            logger.warning("Admin push failed (network) for user_id=%s", uid)
            failed += 1
        except Exception:
            logger.exception("Admin push failed (unexpected) for user_id=%s", uid)
            failed += 1
        await asyncio.sleep(0.05)

    await cb.bot.send_message(
        chat_id=cb.from_user.id,
        text=f"Готово. Отправлено: {sent}, ошибок: {failed}",
    )


@router.callback_query(F.data.startswith("admin_push_type:"))
async def cb_admin_push_type(cb: CallbackQuery) -> None:
    """Обработчик выбора типа пуша."""
    if str(cb.from_user.id) not in ADMIN_IDS:
        await cb.answer()
        return

    try:
        await cb.answer()
    except TelegramBadRequest:
        logger.warning("Admin push type callback expired for admin %s", cb.from_user.id)

    # Формат: admin_push_type:simple:token или admin_push_type:reading:token или admin_push_type:year_energy:token
    parts = cb.data.split(":", 2)
    if len(parts) < 3:
        await cb.message.edit_text("Ошибка в данных. Попробуй снова.")
        return

    push_type = parts[1]  # simple, reading, year_energy
    token = parts[2]

    payload = PENDING_PUSHES.get(token)
    if not payload:
        await cb.message.edit_text("Заявка на рассылку не найдена или устарела.")
        return

    payload["push_type"] = push_type

    push_type_names = {
        "simple": "Обычный пуш (главное меню)",
        "reading": "С раскладом 'Задать вопрос'",
        "year_energy": "С раскладом 'Энергия года'",
    }

    push_text_html = str(payload.get("text_html") or "").strip()
    await cb.message.edit_text(
        f"Тип пуша: {push_type_names.get(push_type, push_type)}\n\n"
        f"Текст:\n{push_text_html}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Подтвердить отправку",
                        callback_data=f"admin_push_confirm:{token}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="Изменить тип",
                        callback_data=f"admin_push_type_back:{token}",
                    ),
                    InlineKeyboardButton(
                        text="Отменить",
                        callback_data=f"admin_push_cancel:{token}",
                    )
                ],
            ]
        ),
    )


@router.callback_query(F.data.startswith("admin_push_type_back:"))
async def cb_admin_push_type_back(cb: CallbackQuery) -> None:
    """Возврат к выбору типа пуша."""
    if str(cb.from_user.id) not in ADMIN_IDS:
        await cb.answer()
        return

    try:
        await cb.answer()
    except TelegramBadRequest:
        pass

    token = cb.data.split(":", 1)[1]
    payload = PENDING_PUSHES.get(token)
    if not payload:
        await cb.message.edit_text("Заявка на рассылку не найдена или устарела.")
        return

    push_text_html = str(payload.get("text_html") or "").strip()
    await cb.message.edit_text(
        f"Проверь текст пуша и выбери тип:\n\n{push_text_html}",
        parse_mode="HTML",
        reply_markup=admin_push_type_kb(token),
    )


@router.callback_query(F.data.startswith("admin_push_cancel:"))
async def cb_admin_push_cancel(cb: CallbackQuery) -> None:
    if str(cb.from_user.id) not in ADMIN_IDS:
        await cb.answer()
        return

    token = cb.data.split(":", 1)[1]
    PENDING_PUSHES.pop(token, None)
    await cb.message.edit_text("Рассылка отменена.")
    await cb.answer()


@router.callback_query(F.data == "admin_push_start_reading")
async def cb_admin_push_start_reading(cb: CallbackQuery, state: FSMContext) -> None:
    """Обработчик кнопки 'Начать расклад' из единоразового пуша."""
    user = cb.from_user
    if not user:
        await cb.answer()
        return

    await cb.answer()
    await _start_three_cards_flow(cb.message, state)

    intro_text_1 = (
        "Мяу, давай посмотрим глубже 🐈‍⬛\n"
        "«Задать свой вопрос» — это расклад из трёх карт, который показывает:\n"
        "• что сейчас происходит,\n"
        "• куда всё движется,\n"
        "• к чему это может привести.\n\n"
        "Один такой расклад я делаю бесплатно раз в день.\n"
        "Если захочешь ещё — можно будет сделать дополнительный за рыбки."
    )
    intro_text_2 = (
        "Перед тем как спросить, коротко опиши свою ситуацию — так я лучше почувствую, что происходит, и подберу точные ответы.\n"
        "Если не готов рассказывать историю, нажми на кнопку «Сразу к вопросу», и мы начнем! 🌟"
    )

    await cb.message.answer(intro_text_1)
    await cb.message.answer(
        intro_text_2,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Сразу к вопросу",
                        callback_data="three_keys_go_to_question",
                    )
                ]
            ]
        ),
    )


@router.callback_query(F.data == "admin_push_year_energy")
async def cb_admin_push_year_energy(cb: CallbackQuery, state: FSMContext) -> None:
    """Обработчик кнопки 'Узнать энергию года' из единоразового пуша."""
    user = cb.from_user
    if not user:
        await cb.answer()
        return

    await cb.answer()

    # Загружаем архетипы
    archetypes = load_year_energy_archetypes()
    if not archetypes:
        await cb.message.answer("К сожалению, данные для расклада временно недоступны.")
        return

    try:
        # Выбираем карту (или получаем сохраненную)
        card_title, was_saved = _choose_year_energy_card(user.id, archetypes)
        archetype_description = archetypes[card_title]

        # Отправляем карту
        await _send_card_image(cb, card_title)

        # Отправляем трактовку архетипа
        await cb.message.answer(
            f"✨ Энергия года: {card_title} ✨\n\n{archetype_description}"
        )

        # Отправляем сообщение с предложением платного расклада
        await cb.message.answer(
            "Отлично, Архетип года пойман. 😈\n"
            "Хочешь разобрать его глубже? Могу сделать подробный расклад на год: где будет рост, где проверка, что станет твоей опорой и какой шанс важно не пропустить.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Разобрать глубже (101 🐟)",
                            callback_data="year_energy_deep_reading",
                        )
                    ]
                ]
            ),
        )

        # Обновляем статистику (только если карта была выбрана впервые)
        if not was_saved:
            with SessionLocal() as session:
                db_user = _get_or_create_user(session, user.id, user.username)
                db_user.draw_count = (db_user.draw_count or 0) + 1
                db_user.last_activity_date = date.today()
                session.commit()
    except ValueError as e:
        await cb.message.answer(str(e))


class AdviceCard:
    def __init__(self, title: str, description: str):
        self.title = title.replace("\ufeff", "").strip()
        self.description = description

    def image_url(self) -> str:
        normalized = self.title.replace(" ", "_")
        return f"{GITHUB_RAW_BASE}/{quote(normalized)}.jpg"

    def image_path(self) -> Path:
        return IMAGES_DIR / f"{self.title.strip().replace(' ', '_')}.jpg"


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
async def send_advice(message: Message) -> None:
    user_id = message.from_user.id
    username = message.from_user.username if message.from_user else None
    today = date.today()

    with SessionLocal() as session:
        user = session.query(User).filter(User.id == user_id).first()
        if not user:
            user = User(id=user_id)
            session.add(user)

        user.username = username
        user.last_activity_date = today
        if not user.push_time:
            user.push_time = DEFAULT_PUSH_TIME
        session.commit()

    await message.answer(
        "Подумай, о чем ты хочешь спросить карты и жми 'Вытянуть карту'.",
        reply_markup=advice_draw_kb(),
    )


@router.callback_query(F.data == "advice_draw")
async def cb_advice_draw(cb: CallbackQuery) -> None:
    today = date.today()
    user_id = cb.from_user.id
    username = cb.from_user.username if cb.from_user else None

    with SessionLocal() as session:
        user = session.query(User).filter(User.id == user_id).first()
        if not user:
            user = User(id=user_id)
            session.add(user)

        user.username = username
        user.last_activity_date = today
        if not user.push_time:
            user.push_time = DEFAULT_PUSH_TIME

        if user.advice_last_date != today:
            user.daily_advice_count = 0
            user.advice_last_date = today

        if user.daily_advice_count >= 2:
            session.commit()
            await cb.answer()
            await cb.message.answer("⚠️ Лимит советов на сегодня исчерпан. Следующие будут доступны завтра 🌙")
            return

        card = random.choice(ADVICE_CARDS)
        user.daily_advice_count += 1
        user.advice_last_date = today
        session.commit()

    await cb.answer()
    local_path = getattr(card, "image_path", None)
    if callable(local_path):
        path = local_path()
        if path.exists():
            try:
                await cb.message.answer_photo(
                    photo=BufferedInputFile(path.read_bytes(), filename=path.name),
                    caption=f"✨ Совет карт: {card.title}\n\n{card.description}"
                )
                await cb.answer()
                return
            except TelegramBadRequest:
                pass

    try:
        image_bytes = await _fetch_image_bytes(card.image_url())
        await cb.message.answer_photo(
            photo=BufferedInputFile(image_bytes, filename=f"{card.title}.jpg"),
            caption=f"✨ Совет карт: {card.title}\n\n{card.description}"
        )
    except (httpx.HTTPError, TelegramBadRequest, TelegramNetworkError):
        await cb.message.answer(
            f"✨ Совет карт: {card.title}\n\n{card.description}"
        )


@router.message(Command("three_cards_test"))
async def cmd_three_cards_test(message: Message, state: FSMContext) -> None:
    await _start_three_cards_flow(message, state)


@router.message(ThreeCardsStates.waiting_context)
async def handle_three_cards_context(message: Message, state: FSMContext) -> None:
    """
    Пользователь описывает ситуацию перед формулировкой вопроса.
    Сообщение сохраняется в состоянии и передаётся в LLM как контекст.
    После этого сразу просим сформулировать явный вопрос.
    """
    text = (message.text or message.caption or "").strip()
    if not text:
        await message.answer(
            "Если хочешь, опиши свою ситуацию словами. "
            "Когда будешь готов — задай свой главный вопрос одним сообщением."
        )
        return

    data = await state.get_data()
    prev_context = (data.get("three_keys_context") or "").strip()
    new_context = f"{prev_context}\n\n{text}" if prev_context else text
    await state.update_data(three_keys_context=new_context)
    await state.set_state(ThreeCardsStates.waiting_question)
    await message.answer(
        "Записала твою историю 🐾\n"
        "Теперь сформулируй свой главный вопрос к раскладу «Задать свой вопрос» "
        "и отправь его одним сообщением."
    )


@router.callback_query(F.data == "three_keys_go_to_question")
async def cb_three_keys_go_to_question(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ThreeCardsStates.waiting_question)
    await cb.message.answer(
        "Теперь сформулируй свой главный вопрос к раскладу «Задать свой вопрос» "
        "и отправь его одним сообщением."
    )
    await cb.answer()


@router.message(ThreeCardsStates.waiting_question)
async def handle_three_cards_question(message: Message, state: FSMContext) -> None:
    if len(CARDS) < 3:
        await message.answer("Недостаточно карт для расклада.")
        await state.clear()
        return

    data = await state.get_data()
    stored_titles = data.get("three_cards") or []

    if stored_titles and len(stored_titles) >= 3:
        selected_cards = []
        for title in stored_titles:
            card = next((c for c in CARDS if c.title == title), None)
            if card:
                selected_cards.append(card)
        if len(selected_cards) < 3:
            selected_cards = random.sample(CARDS, 3)
    else:
        selected_cards = random.sample(CARDS, 3)

    question = (message.text or message.caption or "").strip()
    if not question:
        await message.answer("Пожалуйста, сформулируй вопрос текстом.")
        return

    context_text = (data.get("three_keys_context") or "").strip()

    user = message.from_user
    user_id = user.id if user else None
    username = user.username if user else None

    # Учёт бесплатного расклада и списание рыбок за повторные расклады
    if user_id is not None:
        today = date.today()
        with SessionLocal() as session:
            user_obj = session.query(User).filter(User.id == user_id).first()
            if not user_obj:
                user_obj = User(id=user_id, username=username)
                session.add(user_obj)

            last_date = getattr(user_obj, "three_keys_last_date", None)
            daily_count = getattr(user_obj, "three_keys_daily_count", 0) or 0
            if last_date != today:
                daily_count = 0

            # Первый расклад за день — бесплатный.
            # Начиная со второго — списываем 69 рыбок, если хватает.
            FREE_PER_DAY = 1
            PRICE_FISH = 69

            if daily_count >= FREE_PER_DAY:
                balance = getattr(user_obj, "fish_balance", 0) or 0
                if balance < PRICE_FISH:
                    # Недостаточно рыбок — показываем голодную Милки и выходим.
                    hungry_path = Path("src/data/images/hungry_milky.jpg")
                    text = (
                        "Мяу… Похоже, мои силы закончились.\n"
                        "Вся моя магия на сегодня уже исчерпана, лапки устали, "
                        "а в мисочке совсем нет рыбок 😿\n"
                        "Если пополнишь баланс, я смогу продолжить прямо сейчас.\n"
                        "А если нет — приходи завтра. К этому времени я отдохну, "
                        "подкреплюсь и снова с радостью вытяну карты для тебя❤️"
                    )
                    kb_buy_fish = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(
                                    text="Купить рыбки",
                                    callback_data="three_keys_buy_fish",
                                )
                            ]
                        ]
                    )
                    if hungry_path.exists():
                        try:
                            await message.answer_photo(
                                photo=BufferedInputFile(hungry_path.read_bytes(), filename=hungry_path.name),
                                caption=text,
                                reply_markup=kb_buy_fish,
                            )
                        except TelegramBadRequest:
                            await message.answer(text, reply_markup=kb_buy_fish)
                    else:
                        await message.answer(text, reply_markup=kb_buy_fish)
                    await state.clear()
                    return

                # Списываем рыбки за расклад
                user_obj.fish_balance = balance - PRICE_FISH

            # Фиксируем факт расклада на сегодня
            daily_count += 1
            user_obj.three_keys_last_date = today
            user_obj.three_keys_daily_count = daily_count
            # Считаем количество вытянутых карт
            user_obj.draw_count = (user_obj.draw_count or 0) + len(selected_cards)
            user_obj.last_activity_date = today

            session.commit()

    await message.answer("Колода тасуется... Подожди несколько секунд ✨")

    try:
        interpretation = await generate_three_card_reading(selected_cards, question, context=context_text)
    except Exception as exc:
        logger.exception("Ошибка при обращении к LLM: %s", exc)
        await message.answer("Не удалось получить трактовку. Попробуй чуть позже.")
        await state.clear()
        return

    for card in selected_cards:
        sent = False
        local_path = getattr(card, "image_path", None)
        if callable(local_path):
            path = local_path()
            if path.exists():
                try:
                    await message.answer_photo(
                        photo=BufferedInputFile(path.read_bytes(), filename=path.name),
                        caption=card.title,
                    )
                    sent = True
                except TelegramBadRequest:
                    sent = False
        if not sent:
            try:
                image_bytes = await _fetch_image_bytes(card.image_url())
                await message.answer_photo(
                    photo=BufferedInputFile(image_bytes, filename=f"{card.title}.jpg"),
                    caption=card.title,
                )
                sent = True
            except (httpx.HTTPError, TelegramBadRequest, TelegramNetworkError):
                sent = False
        if not sent:
            await message.answer(card.title)

    cards_titles = ", ".join(card.title for card in selected_cards)
    response_text = (
        'Расклад "Задать свой вопрос"\n'
        f"Вопрос: {question}\n"
        f"Карты: {cards_titles}\n\n"
        f"{interpretation}"
    )

    await message.answer(response_text)
    # После трактовки отправляем кастомный эмодзи с выбором следующего шага
    await message.answer(
        '<tg-emoji emoji-id="5413703918947413540">🐈‍⬛</tg-emoji>',
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Хочу ещё расклад",
                        callback_data="three_keys_again",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="Спасибо, Милки!",
                        callback_data="three_keys_thanks",
                    )
                ],
            ]
        ),
    )
    await state.clear()


@router.callback_query(F.data == "three_keys_again")
async def cb_three_keys_again(cb: CallbackQuery, state: FSMContext) -> None:
    """Повторный запуск расклада 'Задать свой вопрос' по кнопке."""
    user = cb.from_user
    if not user:
        await cb.answer()
        return

    await _start_three_cards_flow(cb.message, state)

    intro_text_1 = (
        "Мяу, давай посмотрим, что подскажет тебе ещё один расклад из трёх карт! 😼\n"
        "Напоминаю: один расклад в день — бесплатно, дальше - 69 рыбок."
    )
    intro_text_2 = (
        "Если хочешь, коротко опиши свою ситуацию, а потом задавай главный вопрос.\n"
        "Если нет — жми «Сразу к вопросу»."
    )

    await cb.message.answer(intro_text_1)
    await cb.message.answer(
        intro_text_2,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Сразу к вопросу",
                        callback_data="three_keys_go_to_question",
                    )
                ]
            ]
        ),
    )
    await cb.answer()


@router.callback_query(F.data == "three_keys_thanks")
async def cb_three_keys_thanks(cb: CallbackQuery, state: FSMContext) -> None:
    """Завершение диалога после расклада и переход в главное меню."""
    user = cb.from_user
    if not user:
        await cb.answer()
        return

    await state.clear()
    await cb.message.answer(
        "Мяу! Пожалуйста! Рада буду видеть тебя снова💖😎"
    )

    bot = get_bot()
    await bot.send_message(
        chat_id=user.id,
        text="Готово. Чем займёмся?",
        reply_markup=main_menu_kb(_is_admin(user.id)),
    )
    await cb.answer()


@router.callback_query(F.data == "three_keys_buy_fish")
async def cb_three_keys_buy_fish(cb: CallbackQuery, state: FSMContext) -> None:
    """
    Кнопка из сообщения о нехватке рыбок в раскладе «Задать свой вопрос» —
    сразу предлагает перейти в бота оплаты.
    """
    user = cb.from_user
    if not user:
        await cb.answer()
        return

    await state.clear()
    await cb.message.answer(
        "Чтобы пополнить баланс рыбок, перейди в бота оплаты.\n\n"
        "Там можно выбрать тариф, оплатить через ЮKassa и вернуться обратно в Милки.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Открыть бота оплаты",
                        url="https://t.me/Milky_payment_bot",
                    )
                ]
            ]
        ),
    )
    await cb.answer()


# -------- Онбординг: имя, ДР, часовой пояс --------

@router.callback_query(F.data == "use_profile_name")
async def cb_use_profile_name(cb: CallbackQuery, state: FSMContext) -> None:
    user = cb.from_user
    # Предпочтём реальное имя из профиля, затем username
    full_name = (user.first_name or "").strip()
    if getattr(user, "last_name", None):
        ln = (user.last_name or "").strip()
        if ln:
            full_name = f"{full_name} {ln}" if full_name else ln
    name = full_name or user.username or ""
    with SessionLocal() as session:
        db_user = session.query(User).filter(User.id == user.id).first()
        if db_user:
            db_user.display_name = name
            session.commit()
    greet = f"Приятно познакомиться, {name}!" if name else "Приятно познакомиться!"
    await cb.message.answer(f"{greet} Теперь укажи дату рождения в формате ДД.ММ.ГГГГ")
    await state.set_state(OnboardingStates.asking_birth_date)
    await cb.answer()


@router.callback_query(F.data == "enter_name_manual")
async def cb_enter_name_manual(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.message.answer("Напиши, как к тебе обращаться (одно сообщение).")
    await state.set_state(OnboardingStates.waiting_name_manual)
    await cb.answer()


@router.message(OnboardingStates.asking_name)
async def msg_name_direct(message: Message, state: FSMContext) -> None:
    """
    Поддержка ввода имени напрямую, без нажатия кнопки.

    Если пользователь просто отвечает сообщением на вопрос «Твоё имя?»
    вместо выбора «Ввести вручную», обрабатываем это как ручной ввод имени.
    """
    await msg_name_manual(message, state)


@router.message(OnboardingStates.waiting_name_manual)
async def msg_name_manual(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name:
        await message.answer("Пожалуйста, отправь имя текстом.")
        return
    with SessionLocal() as session:
        user = session.query(User).filter(User.id == message.from_user.id).first()
        if user:
            user.display_name = name
            session.commit()
    await message.answer(f"Рада знакомству, {name}! Теперь укажи дату рождения в формате ДД.ММ.ГГГГ")
    await state.set_state(OnboardingStates.asking_birth_date)


def _parse_birth_date(text: str) -> date | None:
    import re
    from datetime import datetime as _dt
    s = text.strip()
    # Допускаем форматы: DD.MM.YYYY, DD-MM-YYYY, YYYY-MM-DD
    for fmt in ("%d.%m.%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return _dt.strptime(s, fmt).date()
        except ValueError:
            continue
    # Попытка вытащить цифры через regexp DD MM YYYY
    m = re.match(r"^(\d{1,2})[\s./-](\d{1,2})[\s./-](\d{4})$", s)
    if m:
        d, mth, y = map(int, m.groups())
        try:
            return date(y, mth, d)
        except ValueError:
            return None
    return None


@router.message(OnboardingStates.asking_birth_date)
async def msg_birth_date(message: Message, state: FSMContext) -> None:
    d = _parse_birth_date(message.text or "")
    if d is None:
        await message.answer("Не похоже на дату. Пример: 07.11.1993")
        return
    with SessionLocal() as session:
        user = session.query(User).filter(User.id == message.from_user.id).first()
        if user:
            user.birth_date = d
            session.commit()
    await message.answer(
        "Теперь настроим твой часовой пояс :)\n\n"
        "Укажи, какой у тебя сейчас час\n\n"
        "(Если на часах 14:40, то указывай 14)",
    )
    await state.set_state(OnboardingStates.asking_tz)


@router.callback_query(F.data == "change_tz")
async def cb_change_tz(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.message.edit_text(
        "Теперь настроим твой часовой пояс :)\n\n"
        "Укажи, какой у тебя сейчас час\n\n"
        "(Если на часах 14:40, то указывай 14)",
    )
    await state.set_state(OnboardingStates.asking_tz)
    await cb.answer()


@router.message(OnboardingStates.asking_tz)
async def msg_tz_hour(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer(
            "Пока ничего не вижу 🙈\n\n"
            "Напиши, какой сейчас час у тебя — просто число от 0 до 23.\n\n"
            "Например, если на часах 14:40, достаточно отправить 14."
        )
        return

    # Пытаемся вытащить число часа (0-23) даже если пользователь ввёл что-то вроде "14:40"
    import re

    m = re.search(r"\d{1,2}", text)
    if not m:
        await message.answer(
            "Кажется, я не нашла в сообщении число часа 🕰️\n\n"
            "Отправь, пожалуйста, только час в формате числа от 0 до 23.\n\n"
            "Пример: 8 или 14."
        )
        return

    try:
        hour = int(m.group(0))
    except ValueError:
        await message.answer(
            "Что-то пошло не так с числом часа ✨\n\n"
            "Попробуй ещё раз: отправь только одно число от 0 до 23.\n"
            "Например: 9 или 21."
        )
        return

    if not 0 <= hour <= 23:
        await message.answer(
            "Хм, такого часа на циферблате не бывает 🙂\n\n"
            "Час должен быть в диапазоне от 0 до 23.\n"
            "Например: 0, 7, 14 или 22."
        )
        return

    # Текущее время в Москве
    msk_tz = pytz.timezone("Europe/Moscow")
    msk_hour = datetime.now(msk_tz).hour

    # Смещение пользователя относительно МСК в часах
    diff = hour - msk_hour
    tz_offset_hours = ((diff + 12) % 24) - 12  # нормализуем в диапазон [-12, 11]

    user_id = message.from_user.id
    with SessionLocal() as session:
        user = session.query(User).filter(User.id == user_id).first()
        if not user:
            user = User(id=user_id)
            session.add(user)
        user.tz_offset_hours = tz_offset_hours
        if not user.push_time:
            user.push_time = DEFAULT_PUSH_TIME
        session.commit()
        push_time = user.push_time

    # Перепланируем уведомления с учётом нового смещения
    scheduler = get_scheduler()
    bot = get_bot()
    scheduler.schedule_daily_with_offset(
        user_id,
        push_time,
        tz_offset_hours,
        lambda user_id, _bot=bot: send_push_card(_bot, user_id),
    )

    await message.answer("Часовой пояс настроен. Настройки сохранены.")
    await message.answer(
        "Готово. Чем займёмся?",
        reply_markup=main_menu_kb(_is_admin(user_id)),
    )
    await state.clear()


@router.callback_query(F.data == "change_tz_other")
async def cb_change_tz_other(cb: CallbackQuery) -> None:
    await cb.message.edit_text(
        "Выбери смещение относительно Москвы (МСК):",
        reply_markup=choose_tz_offset_kb(),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("set_tz:"))
async def cb_set_tz(cb: CallbackQuery, state: FSMContext) -> None:
    try:
        off = int(cb.data.split(":", 1)[1])
    except ValueError:
        await cb.answer()
        return
    user_id = cb.from_user.id
    with SessionLocal() as session:
        user = session.query(User).filter(User.id == user_id).first()
        if not user:
            user = User(id=user_id)
            session.add(user)
        user.tz_offset_hours = off
        if not user.push_time:
            user.push_time = DEFAULT_PUSH_TIME
        session.commit()
        push_time = user.push_time

    # Перепланируем уведомления с учётом смещения
    scheduler = get_scheduler()
    bot = get_bot()
    scheduler.schedule_daily_with_offset(
        user_id,
        push_time,
        off,
        lambda user_id, _bot=bot: send_push_card(_bot, user_id),
    )
    await cb.message.edit_text("Часовой пояс обновлён. Настройки сохранены.")
    # Показать главное меню
    await cb.message.answer(
        "Готово. Чем займёмся?",
        reply_markup=main_menu_kb(_is_admin(user_id)),
    )
    await state.clear()
    await cb.answer()


@router.callback_query(F.data == "set_tz_moscow")
async def cb_set_tz_moscow(cb: CallbackQuery, state: FSMContext) -> None:
    user_id = cb.from_user.id
    off = 0
    with SessionLocal() as session:
        user = session.query(User).filter(User.id == user_id).first()
        if not user:
            user = User(id=user_id)
            session.add(user)
        user.tz_offset_hours = off
        if not user.push_time:
            user.push_time = DEFAULT_PUSH_TIME
        session.commit()
        push_time = user.push_time

    scheduler = get_scheduler()
    bot = get_bot()
    scheduler.schedule_daily_with_offset(
        user_id,
        push_time,
        off,
        lambda user_id, _bot=bot: send_push_card(_bot, user_id),
    )

    await cb.message.edit_text("Часовой пояс установлен: Московское время (МСК). Настройки сохранены.")
    await cb.message.answer(
        "Готово. Чем займёмся?",
        reply_markup=main_menu_kb(_is_admin(user_id)),
    )
    await state.clear()
    await cb.answer()


@router.callback_query(F.data == "cancel_tz")
async def cb_cancel_tz(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.message.edit_text("Настройки обновлены.")
    await state.clear()
    await cb.answer()


# -------- Пуш: кнопка "Вытянуть карту дня" --------

@router.callback_query(F.data == "push_draw_card")
async def cb_push_draw_card(cb: CallbackQuery) -> None:
    """Обработчик кнопки под пушем — вытянуть карту дня."""
    await _send_card_of_the_day(cb.message, cb.from_user.id)
    await cb.answer()


# -------- Новогодний расклад на 2026 год --------

NEW_YEAR_READING_PRICE = 101


@router.message(F.text == "Новогодний расклад 2026")
async def btn_new_year_reading(message: Message, state: FSMContext) -> None:
    """Начало новогоднего расклада."""
    user = message.from_user
    if not user:
        await message.answer("Сначала нажми /start 🚀")
        return

    user_id = user.id
    if not _is_admin(user_id):
        await message.answer("Этот расклад пока доступен только для админов.")
        return

    # Проверяем баланс
    with SessionLocal() as session:
        db_user = session.query(User).filter(User.id == user_id).first()
        if not db_user:
            db_user = User(id=user_id, username=user.username)
            session.add(db_user)
            session.commit()

        balance = getattr(db_user, "fish_balance", 0) or 0
        if balance < NEW_YEAR_READING_PRICE:
            hungry_path = Path("src/data/images/hungry_milky.jpg")
            text = (
                f"Мяу… Для новогоднего расклада нужно {NEW_YEAR_READING_PRICE} рыбок.\n"
                f"На твоем балансе сейчас {balance} 🐟\n"
                "Пополни баланс, чтобы начать расклад."
            )
            kb_buy_fish = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Пополнить баланс",
                            callback_data="new_year_buy_fish",
                        )
                    ]
                ]
            )
            if hungry_path.exists():
                try:
                    await message.answer_photo(
                        photo=BufferedInputFile(hungry_path.read_bytes(), filename=hungry_path.name),
                        caption=text,
                        reply_markup=kb_buy_fish,
                    )
                except TelegramBadRequest:
                    await message.answer(text, reply_markup=kb_buy_fish)
            else:
                await message.answer(text, reply_markup=kb_buy_fish)
            return

        # Списываем рыбки
        db_user.fish_balance = balance - NEW_YEAR_READING_PRICE
        session.commit()

    # Инициализируем расклад
    await state.set_state(NewYearReadingStates.in_progress)
    await state.update_data(
        new_year_question_index=0,
        new_year_cards=[],
        new_year_ready_answers={},
    )

    # Отправляем введение
    intro_text = (
        "🎄 Новогодний расклад на 2026 год 🎄\n\n"
        "Этот расклад поможет тебе понять, что ждёт тебя в новом году. "
        "Мы пройдём через 13 вопросов, которые охватят все важные сферы твоей жизни.\n\n"
        "После каждого вопроса ты получишь карту и её трактовку, а затем сможешь перейти к следующему вопросу.\n\n"
        "Готов начать? Нажми кнопку ниже, чтобы вытянуть первую карту!"
    )

    await message.answer(intro_text)
    await message.answer(
        "Вытянуть карту для первого вопроса",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Вытянуть карту",
                        callback_data="new_year_draw_card",
                    )
                ]
            ]
        ),
    )
    
    # Запускаем фоновую генерацию первого вопроса
    bot = get_bot()
    asyncio.create_task(_generate_next_question_background(
        user_id,
        0,
        state,
        bot,
    ))


async def _generate_next_question_background(
    user_id: int,
    next_question_index: int,
    state: FSMContext,
    bot,
) -> None:
    """Фоновая генерация следующего вопроса новогоднего расклада."""
    if next_question_index >= len(NEW_YEAR_QUESTIONS):
        return

    question_data = NEW_YEAR_QUESTIONS[next_question_index]
    
    # Выбираем случайную карту
    if len(CARDS) < 1:
        return

    selected_card = random.choice(CARDS)
    
    # Генерируем трактовку
    try:
        interpretation = await generate_new_year_reading(
            selected_card,
            question_data,
            next_question_index + 1,
            len(NEW_YEAR_QUESTIONS),
        )
        
        # Сохраняем готовый результат в state (только название карты, не объект)
        data = await state.get_data()
        ready_answers = data.get("new_year_ready_answers", {})
        ready_answers[next_question_index] = {
            "card_title": selected_card.title,
            "interpretation": interpretation,
        }
        await state.update_data(new_year_ready_answers=ready_answers)
        
        logger.info("Фоновая генерация вопроса %d завершена для пользователя %d", next_question_index + 1, user_id)
    except Exception as exc:
        logger.exception("Ошибка при фоновой генерации вопроса %d для пользователя %d: %s", next_question_index + 1, user_id, exc)


@router.callback_query(F.data == "new_year_draw_card")
async def cb_new_year_draw_card(cb: CallbackQuery, state: FSMContext) -> None:
    """Обработчик вытягивания карты для текущего вопроса новогоднего расклада."""
    user = cb.from_user
    if not user or not _is_admin(user.id):
        await cb.answer()
        return

    data = await state.get_data()
    question_index = data.get("new_year_question_index", 0)
    
    if question_index >= len(NEW_YEAR_QUESTIONS):
        await cb.answer("Расклад завершён!")
        await state.clear()
        await cb.message.answer(
            "🎉 Поздравляю! Ты прошёл весь новогодний расклад на 2026 год!\n\n"
            "Надеюсь, карты помогли тебе лучше понять, что ждёт тебя в новом году. "
            "Используй эти знания для планирования и принятия решений.\n\n"
            "Удачи в 2026 году! ✨",
            reply_markup=main_menu_kb(_is_admin(user.id)),
        )
        return

    question_data = NEW_YEAR_QUESTIONS[question_index]
    ready_answers = data.get("new_year_ready_answers", {})
    
    # Проверяем, есть ли уже готовая трактовка для этого вопроса
    if question_index in ready_answers:
        # Используем готовую трактовку
        ready_answer = ready_answers[question_index]
        card_title = ready_answer["card_title"]
        interpretation = ready_answer["interpretation"]
        
        # Находим объект карты по названию
        selected_card = next((c for c in CARDS if c.title == card_title), None)
        if not selected_card:
            # Если карта не найдена, выбираем случайную
            selected_card = random.choice(CARDS)
        
        # Удаляем использованный ответ из кэша
        del ready_answers[question_index]
        await state.update_data(new_year_ready_answers=ready_answers)
        
        await cb.answer()
    else:
        # Генерируем трактовку на лету (если не была подготовлена заранее)
        if len(CARDS) < 1:
            await cb.answer("Недостаточно карт для расклада.")
            await state.clear()
            return

        selected_card = random.choice(CARDS)
        
        await cb.answer()
        await cb.message.answer("Колода тасуется... Подожди несколько секунд ✨")

        try:
            interpretation = await generate_new_year_reading(
                selected_card,
                question_data,
                question_index + 1,
                len(NEW_YEAR_QUESTIONS),
            )
        except Exception as exc:
            logger.exception("Ошибка при обращении к LLM для новогоднего расклада: %s", exc)
            await cb.message.answer("Не удалось получить трактовку. Попробуй чуть позже.")
            await state.clear()
            return

    # Сохраняем карту в список
    cards_list = data.get("new_year_cards", [])
    cards_list.append(selected_card.title)
    await state.update_data(new_year_cards=cards_list)

    # Обновляем счетчик вытянутых карт в БД
    with SessionLocal() as session:
        db_user = session.query(User).filter(User.id == user.id).first()
        if db_user:
            db_user.draw_count = (db_user.draw_count or 0) + 1
            db_user.last_activity_date = date.today()
            session.commit()

    # Отправляем карту
    sent = False
    local_path = getattr(selected_card, "image_path", None)
    if callable(local_path):
        path = local_path()
        if path.exists():
            try:
                await cb.message.answer_photo(
                    photo=BufferedInputFile(path.read_bytes(), filename=path.name),
                    caption=selected_card.title,
                )
                sent = True
            except TelegramBadRequest:
                sent = False
    if not sent:
        try:
            image_bytes = await _fetch_image_bytes(selected_card.image_url())
            await cb.message.answer_photo(
                photo=BufferedInputFile(image_bytes, filename=f"{selected_card.title}.jpg"),
                caption=selected_card.title,
            )
            sent = True
        except (httpx.HTTPError, TelegramBadRequest, TelegramNetworkError):
            sent = False
    if not sent:
        await cb.message.answer(selected_card.title)

    # Отправляем трактовку
    response_text = (
        f"📋 {question_data['category']}\n"
        f"❓ {question_data['question']}\n\n"
        f"{interpretation}"
    )
    await cb.message.answer(response_text)

    # Обновляем индекс вопроса
    question_index += 1
    await state.update_data(new_year_question_index=question_index)

    # Если это не последний вопрос, показываем кнопку для следующего
    if question_index < len(NEW_YEAR_QUESTIONS):
        next_question = NEW_YEAR_QUESTIONS[question_index]
        await cb.message.answer(
            f"Вопрос {question_index + 1} из {len(NEW_YEAR_QUESTIONS)}: {next_question['category']}",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Вытянуть карту для следующего вопроса",
                            callback_data="new_year_draw_card",
                        )
                    ]
                ]
            ),
        )
        
        # Запускаем фоновую генерацию следующего вопроса
        bot = get_bot()
        asyncio.create_task(_generate_next_question_background(
            user.id,
            question_index,
            state,
            bot,
        ))
    else:
        # Последний вопрос завершён
        await state.clear()
        await cb.message.answer(
            "🎉 Поздравляю! Ты прошёл весь новогодний расклад на 2026 год!\n\n"
            "Надеюсь, карты помогли тебе лучше понять, что ждёт тебя в новом году. "
            "Используй эти знания для планирования и принятия решений.\n\n"
            "Удачи в 2026 году! ✨",
            reply_markup=main_menu_kb(_is_admin(user.id)),
        )


@router.callback_query(F.data == "new_year_buy_fish")
async def cb_new_year_buy_fish(cb: CallbackQuery, state: FSMContext) -> None:
    """Кнопка пополнения баланса из новогоднего расклада."""
    user = cb.from_user
    if not user:
        await cb.answer()
        return

    with SessionLocal() as session:
        db_user = session.query(User).filter(User.id == user.id).first()
        if not db_user:
            db_user = User(id=user.id)
            session.add(db_user)
            session.commit()
        balance = getattr(db_user, "fish_balance", 0) or 0

    await state.set_state(FishPaymentStates.viewing_balance)
    await cb.message.answer(
        f"На твоем балансе сейчас {balance} 🐟\n\n"
        "Рыбки — это внутренняя валюта за расклады.\n"
        "Можешь пополнить баланс или вернуться в главное меню.",
        reply_markup=fish_balance_kb(),
    )
    await cb.answer()


# -------- Расклад "Энергия года" (бесплатный, только для админов) --------
#
# Расклад позволяет получить архетип года - случайную карту старших арканов
# с трактовкой того, какая энергия будет преобладать в году.
# После получения трактовки пользователю предлагается перейти к платному
# раскладу "Итоги года" (101 рыбка, 13 вопросов).
#
# Данные архетипов загружаются из CSV файла year_energy_archetypes.csv,
# который создаётся парсером parse_year_energy.py из docx файла.

@router.message(F.text == "Энергия года")
async def btn_year_energy(message: Message, state: FSMContext) -> None:
    """
    Обработчик кнопки 'Энергия года'.
    
    Выполняет бесплатный расклад:
    1. Выбирает карту из доступных архетипов (или возвращает сохраненную)
    2. Отправляет изображение карты
    3. Отправляет трактовку архетипа года
    4. Предлагает перейти к платному раскладу "Итоги года"
    
    Доступен только администраторам.
    """
    user = message.from_user
    if not user:
        return
    
    # Проверяем, что пользователь - админ
    if not _is_admin(user.id):
        await message.answer("Эта функция доступна только администраторам.")
        return
    
    # Загружаем архетипы
    archetypes = load_year_energy_archetypes()
    if not archetypes:
        await message.answer("К сожалению, данные для расклада временно недоступны.")
        return
    
    try:
        # Выбираем карту (или получаем сохраненную)
        card_title, was_saved = _choose_year_energy_card(user.id, archetypes)
        archetype_description = archetypes[card_title]
        
        # Отправляем карту
        await _send_card_image(message, card_title)
        
        # Отправляем трактовку архетипа
        await message.answer(
            f"✨ Энергия года: {card_title} ✨\n\n{archetype_description}"
        )
        
        # Отправляем сообщение с предложением платного расклада
        await message.answer(
            "Отлично, Архетип года пойман. 😈\n"
            "Хочешь разобрать его глубже? Могу сделать подробный расклад на год: где будет рост, где проверка, что станет твоей опорой и какой шанс важно не пропустить.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Разобрать глубже (101 🐟)",
                            callback_data="year_energy_deep_reading",
                        )
                    ]
                ]
            ),
        )
        
        # Обновляем статистику (только если карта была выбрана впервые)
        if not was_saved:
            with SessionLocal() as session:
                db_user = _get_or_create_user(session, user.id, user.username)
                db_user.draw_count = (db_user.draw_count or 0) + 1
                db_user.last_activity_date = date.today()
                session.commit()
    except ValueError as e:
        await message.answer(str(e))


@router.callback_query(F.data == "year_energy_deep_reading")
async def cb_year_energy_deep_reading(cb: CallbackQuery, state: FSMContext) -> None:
    """
    Обработчик кнопки перехода к платному раскладу 'Итоги года'.
    
    Проверяет баланс пользователя (нужно 101 рыбка) и запускает
    новогодний расклад с 13 вопросами, если баланс достаточен.
    Если баланса недостаточно, предлагает пополнить его.
    
    Доступен только администраторам.
    """
    user = cb.from_user
    if not user:
        await cb.answer()
        return
    
    # Проверяем, что пользователь - админ
    if not _is_admin(user.id):
        await cb.answer("Эта функция доступна только администраторам.")
        return
    
    await cb.answer()
    
    # Проверяем баланс и запускаем новогодний расклад
    user_id = user.id
    with SessionLocal() as session:
        db_user = _get_or_create_user(session, user_id, user.username)
        balance = getattr(db_user, "fish_balance", 0) or 0
        
        if balance < NEW_YEAR_READING_PRICE:
            text = (
                f"Мяу… Для подробного расклада на год нужно {NEW_YEAR_READING_PRICE} рыбок.\n"
                f"У тебя сейчас {balance} 🐟\n\n"
                "Пополни баланс, чтобы получить полный расклад на год с 13 вопросами!"
            )
            kb_buy_fish = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Пополнить баланс",
                            callback_data="new_year_buy_fish",
                        )
                    ]
                ]
            )
            hungry_path = Path("src/data/images/hungry_milky.jpg")
            if hungry_path.exists():
                try:
                    await cb.message.answer_photo(
                        photo=BufferedInputFile(hungry_path.read_bytes(), filename=hungry_path.name),
                        caption=text,
                        reply_markup=kb_buy_fish,
                    )
                except TelegramBadRequest:
                    await cb.message.answer(text, reply_markup=kb_buy_fish)
            else:
                await cb.message.answer(text, reply_markup=kb_buy_fish)
            return
        
        # Списываем рыбки
        db_user.fish_balance = balance - NEW_YEAR_READING_PRICE
        session.commit()
    
    # Инициализируем расклад
    await state.set_state(NewYearReadingStates.in_progress)
    await state.update_data(
        new_year_question_index=0,
        new_year_cards=[],
        new_year_ready_answers={},
    )
    
    # Отправляем введение
    intro_text = (
        "🎄 Подробный расклад на год 🎄\n\n"
        "Этот расклад поможет тебе понять, что ждёт тебя в новом году. "
        "Мы пройдём через 13 вопросов, которые охватят все важные сферы твоей жизни.\n\n"
        "После каждого вопроса ты получишь карту и её трактовку, а затем сможешь перейти к следующему вопросу.\n\n"
        "Готов начать? Нажми кнопку ниже, чтобы вытянуть первую карту!"
    )
    
    await cb.message.answer(intro_text)
    await cb.message.answer(
        "Вытянуть карту для первого вопроса",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Вытянуть карту",
                        callback_data="new_year_draw_card",
                    )
                ]
            ]
        ),
    )
    
    # Запускаем фоновую генерацию первого вопроса
    bot = get_bot()
    asyncio.create_task(_generate_next_question_background(
        user_id,
        0,
        state,
        bot,
    ))
