from __future__ import annotations

"""
Обработчики второго бота (@Milky_payment_bot), отвечающего за оплату.

Сценарий:
1. Админ заходит в бота и выбирает тариф (количество рублей).
2. Создаём платёж в ЮKassa, сохраняем его в БД.
3. Отправляем ссылку на оплату (confirmation_url) и кнопку «Проверить оплату».
4. После нажатия «Проверить оплату» запрашиваем статус в ЮKassa:
   - если succeeded — начисляем рыбки и обновляем баланс пользователя;
   - если ещё pending — просим подождать и проверить позже;
   - если canceled — пишем, что платёж не прошёл.
"""

import asyncio
import logging
import os
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, Message, BufferedInputFile
from pathlib import Path
from sqlalchemy.orm import Session

from utils.db import SessionLocal, User, Payment
from utils.fish import tariff_to_amounts
from utils.yookassa_client import create_payment, get_payment, YooKassaError

logger = logging.getLogger(__name__)

router = Router()


_ADMIN_RAW = os.getenv("ADMIN_ID") or os.getenv("ADMIN_IDS") or ""
ADMIN_IDS = {s.strip() for s in _ADMIN_RAW.split(",") if s.strip()}


def _is_admin(user_id: int) -> bool:
    """Проверяем, является ли пользователь админом бота."""
    return str(user_id) in ADMIN_IDS


def _tariffs_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура с тарифами пополнения."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="50₽ – 350 🐟", callback_data="pay_tariff:50"),
            ],
            [
                InlineKeyboardButton(text="150₽ – 1050 🐟", callback_data="pay_tariff:150"),
            ],
            [
                InlineKeyboardButton(text="300₽ – 2100 🐟", callback_data="pay_tariff:300"),
            ],
            [
                InlineKeyboardButton(text="650₽ – 4550 🐟", callback_data="pay_tariff:650"),
            ],
        ]
    )


def _payment_actions_kb(payment_db_id: int, include_back_to_main: bool = True) -> InlineKeyboardMarkup:
    """
    Клавиатура под сообщением с оплатой:
    - кнопка «Я оплатил, проверить» — дергает статус платежа;
    - опционально — кнопка «Вернуться в Милки».
    """
    buttons = [
        [
            InlineKeyboardButton(
                text="Я оплатил, проверить",
                callback_data=f"check_payment:{payment_db_id}",
            )
        ]
    ]
    if include_back_to_main:
        buttons.append(
            [
                InlineKeyboardButton(
                    text="Вернуться в Милки",
                    url="https://t.me/Milky_Tarot_Bot",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def _auto_check_payment(bot: Bot, payment_db_id: int, user_id: int) -> None:
    """
    Фоновая проверка статуса платежа в ЮKassa.

    Периодически опрашивает ЮKassa и:
    - при успешной оплате начисляет рыбки и отправляет сообщение пользователю;
    - при отмене сообщает пользователю;
    - если по таймауту платёж всё ещё pending, предлагает проверить вручную.
    """
    max_attempts = 18  # ~3 минуты при шаге 10 секунд
    delay_seconds = 10

    for _ in range(max_attempts):
        with SessionLocal() as session:
            payment: Payment | None = session.query(Payment).filter(Payment.id == payment_db_id).first()
            if not payment:
                return

            # Если платёж уже обработан вручную
            if payment.status == "succeeded":
                user_obj = session.query(User).filter(User.id == user_id).first()
                balance = getattr(user_obj, "fish_balance", 0) if user_obj else 0
                await bot.send_message(
                    chat_id=user_id,
                    text=(
                        "Оплата уже была успешно проведена ✅\n"
                        f"Текущий баланс: {balance} 🐟"
                    ),
                )
                return

            yookassa_id = payment.yookassa_payment_id

        try:
            payment_data = await get_payment(yookassa_id)
        except YooKassaError:
            logger.exception("Не удалось получить статус платежа %s в ЮKassa", yookassa_id)
            await asyncio.sleep(delay_seconds)
            continue

        status = payment_data.get("status")
        paid = bool(payment_data.get("paid"))
        payment_method = payment_data.get("payment_method") or {}
        method_type = payment_method.get("type")

        with SessionLocal() as session:
            payment: Payment | None = session.query(Payment).filter(Payment.id == payment_db_id).first()
            if not payment:
                return

            payment.status = status or payment.status
            payment.method = method_type or payment.method
            payment.updated_at = datetime.utcnow()

            if status == "succeeded" and paid:
                user_obj = session.query(User).filter(User.id == user_id).first()
                if not user_obj:
                    user_obj = User(id=user_id)
                    session.add(user_obj)

                current_balance = getattr(user_obj, "fish_balance", 0) or 0
                user_obj.fish_balance = current_balance + payment.fish_amount
                session.commit()
                new_balance = user_obj.fish_balance

                text_lines = [
                    "Оплата прошла успешно ✨",
                    f"Тебе начислено {payment.fish_amount} 🐟.",
                    f"Твой новый баланс: {new_balance} 🐟",
                ]
                await bot.send_message(chat_id=user_id, text="\n".join(text_lines))
                return

            session.commit()

        if status in {"canceled"}:
            await bot.send_message(
                chat_id=user_id,
                text=(
                    "Платёж находится в статусе «отменён» или не был завершён.\n"
                    "Если деньги всё же списались, напиши, пожалуйста, администратору."
                ),
            )
            return

        await asyncio.sleep(delay_seconds)

    # Если после всех попыток платёж всё ещё не завершён
    await bot.send_message(
        chat_id=user_id,
        text=(
            "Платёж всё ещё в ожидании.\n"
            "Если ты уже оплатил и деньги списались, вернись в этого бота "
            "и нажми кнопку «Я оплатил, проверить» под последним сообщением об оплате."
        ),
    )


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    """
    Точка входа во второй бот.

    Пока доступен только администраторам, чтобы протестировать оплаты.
    """
    user = message.from_user
    if not user or not _is_admin(user.id):
        await message.answer(
            "Этот бот сейчас доступен только администраторам для тестирования оплат."
        )
        return

    await message.answer(
        "Привет! Здесь можно пополнить баланс рыбок 🐟\n\n"
        "Выбери, на сколько хочешь пополнить баланс:",
        reply_markup=_tariffs_keyboard(),
    )


@router.callback_query(F.data.startswith("pay_tariff:"))
async def cb_pay_tariff(cb: CallbackQuery) -> None:
    """
    Пользователь выбрал тариф — создаём платёж в ЮKassa и отправляем ссылку на оплату.
    """
    user = cb.from_user
    if not user or not _is_admin(user.id):
        await cb.answer()
        return

    try:
        amount_rub = int(cb.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await cb.answer("Не удалось определить тариф. Попробуй ещё раз.")
        return

    total_fish, bonus_fish = tariff_to_amounts(amount_rub)
    if total_fish == 0:
        await cb.answer("Неизвестный тариф, выбери другой.")
        return

    # Создаём платёж в ЮKassa
    description = f"Пополнение баланса на {total_fish} рыбок (user_id={user.id})"
    metadata = {
        "telegram_user_id": user.id,
        "amount_rub": amount_rub,
        "fish_total": total_fish,
        "fish_bonus": bonus_fish,
    }

    try:
        payment_data = await create_payment(amount_rub=amount_rub, description=description, metadata=metadata)
    except YooKassaError as e:
        logger.exception("Не удалось создать платёж в ЮKassa")
        await cb.message.answer(
            "Не удалось создать платёж в ЮKassa. Попробуй немного позже или напиши администратору."
        )
        await cb.answer()
        return

    yookassa_id = payment_data.get("id")
    confirmation = payment_data.get("confirmation") or {}
    confirmation_url = confirmation.get("confirmation_url")

    if not yookassa_id or not confirmation_url:
        logger.error("Некорректный ответ ЮKassa: %s", payment_data)
        await cb.message.answer(
            "Не удалось получить ссылку на оплату. Напиши, пожалуйста, администратору."
        )
        await cb.answer()
        return

    # Сохраняем платёж в нашей базе
    with SessionLocal() as session:
        db_payment = Payment(
            user_id=user.id,
            yookassa_payment_id=yookassa_id,
            amount_rub=amount_rub,
            fish_amount=total_fish,
            status=payment_data.get("status", "pending"),
            description=description,
        )
        session.add(db_payment)

        # На всякий случай убеждаемся, что пользователь есть в таблице users
        db_user = session.query(User).filter(User.id == user.id).first()
        if not db_user:
            db_user = User(id=user.id, username=user.username)
            session.add(db_user)

        session.commit()
        session.refresh(db_payment)
        payment_db_id = db_payment.id

    # Запускаем фоновую проверку статуса платежа
    bot = cb.message.bot
    asyncio.create_task(_auto_check_payment(bot, payment_db_id, user.id))

    text_lines = [
        f"Ты выбрал тариф на {amount_rub}₽.",
        f"После успешной оплаты будет начислено {total_fish} 🐟"
        + (f" (из них {bonus_fish} — бонусные 🎁)" if bonus_fish > 0 else ""),
        "",
        "Нажми кнопку ниже, чтобы перейти на страницу оплаты ЮKassa:",
    ]
    await cb.message.answer(
        "\n".join(text_lines),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Перейти к оплате",
                        url=confirmation_url,
                    )
                ]
            ],
        ),
    )

    await cb.message.answer(
        "После того как оплатишь, вернись в этот чат и нажми «Я оплатил, проверить».",
        reply_markup=_payment_actions_kb(payment_db_id),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("check_payment:"))
async def cb_check_payment(cb: CallbackQuery) -> None:
    """
    Проверяем статус платежа в ЮKassa и при необходимости начисляем рыбки.
    """
    user = cb.from_user
    if not user:
        await cb.answer()
        return

    try:
        payment_db_id = int(cb.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await cb.answer("Не удалось найти платёж.")
        return

    with SessionLocal() as session:
        payment: Payment | None = session.query(Payment).filter(Payment.id == payment_db_id).first()
        if not payment:
            await cb.answer("Платёж не найден. Напиши, пожалуйста, администратору.")
            return

        # Чтобы пользователь не мог проверить чужой платёж
        if payment.user_id != user.id:
            await cb.answer("Этот платёж привязан к другому пользователю.")
            return

        # Если уже зафиксирован успешный платёж — просто показываем результат
        if payment.status == "succeeded":
            db_user = session.query(User).filter(User.id == user.id).first()
            balance = getattr(db_user, "fish_balance", 0) if db_user else 0
            await cb.message.answer(
                f"Этот платёж уже был успешно проведён ранее ✅\n"
                f"Текущий баланс: {balance} 🐟",
                reply_markup=_payment_actions_kb(payment_db_id),
            )
            await cb.answer()
            return

        yookassa_id = payment.yookassa_payment_id

    await cb.answer("Проверяю статус платежа…")

    # Запрашиваем статус в ЮKassa
    try:
        payment_data = await get_payment(yookassa_id)
    except YooKassaError:
        logger.exception("Не удалось получить статус платежа %s в ЮKassa", yookassa_id)
        await cb.message.answer(
            "Не удалось получить статус платежа. Попробуй ещё раз через минуту."
        )
        return

    status = payment_data.get("status")
    paid = bool(payment_data.get("paid"))
    payment_method = payment_data.get("payment_method") or {}
    method_type = payment_method.get("type")

    with SessionLocal() as session:
        payment: Payment | None = session.query(Payment).filter(Payment.id == payment_db_id).first()
        if not payment:
            await cb.message.answer("Платёж не найден. Напиши, пожалуйста, администратору.")
            return

        payment.status = status or payment.status
        payment.method = method_type or payment.method
        payment.updated_at = datetime.utcnow()

            if status == "succeeded" and paid:
                # Начисляем рыбки пользователю один раз
                user_obj = session.query(User).filter(User.id == user.id).first()
                if not user_obj:
                    user_obj = User(id=user.id, username=user.username)
                    session.add(user_obj)

                current_balance = getattr(user_obj, "fish_balance", 0) or 0
                user_obj.fish_balance = current_balance + payment.fish_amount

                session.commit()
                new_balance = user_obj.fish_balance

                text_lines = [
                    "Оплата прошла успешно ✨",
                    f"Тебе начислено {payment.fish_amount} 🐟.",
                    f"Твой новый баланс: {new_balance} 🐟",
                ]
                await cb.message.answer(
                    "\n".join(text_lines),
                    reply_markup=_payment_actions_kb(payment_db_id),
                )
                # Дополнительное сообщение после пополнения баланса
                fed_text = (
                    "Спасибо за рыбки!💖💖💖\n"
                    "Теперь я снова в порядке — сытая, собранная и готовая продолжать 😻\n"
                    "Пиши свой вопрос — я уже готова вытянуть карты для тебя 🐈‍⬛"
                )
                fed_path = Path("src/data/images/fed_milky.jpg")
                if fed_path.exists():
                    try:
                        await cb.message.answer_photo(
                            photo=BufferedInputFile(fed_path.read_bytes(), filename=fed_path.name),
                            caption=fed_text,
                        )
                    except TelegramBadRequest:
                        await cb.message.answer(fed_text)
                else:
                    await cb.message.answer(fed_text)
                return

        session.commit()

    if status in {"canceled"}:
        await cb.message.answer(
            "Платёж находится в статусе «отменён» или не был завершён.\n"
            "Если деньги всё же списались, напиши, пожалуйста, администратору.",
            reply_markup=_payment_actions_kb(payment_db_id),
        )
    else:
        await cb.message.answer(
            "Платёж ещё не завершён. Если ты только что оплатил, подожди 1–2 минуты и нажми «Я оплатил, проверить» ещё раз.",
            reply_markup=_payment_actions_kb(payment_db_id),
        )
