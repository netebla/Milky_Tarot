from __future__ import annotations

import csv
import logging
import os
import random
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
from utils.db import SessionLocal, User
from utils.push import send_push_card
from utils.scheduler import DEFAULT_PUSH_TIME
from llm.three_cards import generate_three_card_reading
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


class ThreeCardsStates(StatesGroup):
    waiting_question = State()


class OnboardingStates(StatesGroup):
    asking_name = State()
    waiting_name_manual = State()
    asking_birth_date = State()
    asking_tz = State()


class FishPaymentStates(StatesGroup):
    viewing_balance = State()
    choosing_tariff = State()
    choosing_payment_method = State()


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
    await state.set_state(ThreeCardsStates.waiting_question)
    await state.update_data(three_cards=[card.title for card in selected_cards])
    await message.answer(
        'Задай вопрос к колоде и отправь его сообщением для расклада "Три карты".'
    )


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
    if not user or not _is_admin(user.id):
        await message.answer("Эта функция пока доступна только администраторам.")
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


@router.message(F.text == '"Три карты"')
async def btn_three_cards(message: Message, state: FSMContext) -> None:
    user = message.from_user
    if not user or not _is_admin(user.id):
        await message.answer("Эта кнопка доступна только администраторам.")
        return

    await _start_three_cards_flow(message, state)


@router.callback_query(F.data == "change_push_time")
async def cb_change_time(cb: CallbackQuery) -> None:
    await cb.message.edit_text("Выберите время отправки уведомления:", reply_markup=choose_time_kb())
    await cb.answer()


@router.message(F.text == "Пополнить баланс 🐟")
async def msg_fish_topup(message: Message, state: FSMContext) -> None:
    user = message.from_user
    if not user or not _is_admin(user.id):
        await message.answer("Эта функция пока доступна только администраторам.")
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

    user = message.from_user
    user_id = user.id if user else None
    username = user.username if user else None

    if user_id is not None:
        with SessionLocal() as session:
            _get_or_create_user(session, user_id, username)

    await message.answer("Колода тасуется... Подожди несколько секунд ✨")

    try:
        interpretation = await generate_three_card_reading(selected_cards, question)
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
        'Расклад "Три карты"\n'
        f"Вопрос: {question}\n"
        f"Карты: {cards_titles}\n\n"
        f"{interpretation}"
    )

    await message.answer(response_text)
    await state.clear()


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
