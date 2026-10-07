from __future__ import annotations

"""
Обработчики второго бота (@Milky_payment_bot), отвечающего за оплату.

Сценарий:
1. Пользователь заходит в бота и выбирает тариф (количество рублей).
2. Создаём платёж в ЮKassa, сохраняем его в БД.
3. Отправляем ссылку на оплату (confirmation_url) и кнопку «Проверить оплату».
4. После нажатия «Проверить оплату» запрашиваем статус в ЮKassa:
   - если succeeded — начисляем рыбки и обновляем баланс пользователя;
   - если ещё pending — просим подождать и проверить позже;
   - если canceled — пишем, что платёж не прошёл.
"""

import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, Message

from utils.db import SessionLocal, User, Payment
from utils.fish import tariff_to_amounts
from utils.pricing import get_tariffs
from utils.pending_readings import get_pending_reading
from utils.yookassa_client import create_payment, get_payment, YooKassaError
from utils.payment_processing import apply_payment_status, deliver_pending_notifications
from .payment_messages import send_success_notification

logger = logging.getLogger(__name__)

router = Router()


async def _remove_payment_actions(message: Message) -> None:
    """Убрать кнопки с обработанного платежа; параллельный клик может их опередить."""
    try:
        await message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        logger.debug("Кнопки платежа уже удалены или сообщение нельзя изменить")


def _return_to_main_button(user_id: int | None = None) -> InlineKeyboardButton:
    reading = get_pending_reading(user_id) if user_id is not None else None
    if reading:
        return InlineKeyboardButton(
            text="Продолжить вопрос в Милки",
            url=f"https://t.me/Milky_Tarot_Bot?start=resume_{reading.id}",
        )
    return InlineKeyboardButton(text="Вернуться в Милки", url="https://t.me/Milky_Tarot_Bot")


def _tariffs_keyboard(user_id: int | None = None) -> InlineKeyboardMarkup:
    """Клавиатура с тарифами пополнения."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text=f"{tariff.amount_rub}₽ – {tariff.fish_amount} 🐟",
                callback_data=f"pay_tariff:{tariff.amount_rub}",
            )]
            for tariff in get_tariffs()
        ] + [[_return_to_main_button(user_id)]]
    )


def _payment_actions_kb(
    payment_db_id: int,
    include_back_to_main: bool = True,
    confirmation_url: str | None = None,
) -> InlineKeyboardMarkup:
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
    if confirmation_url:
        buttons.insert(0, [InlineKeyboardButton(text="Перейти к оплате", url=confirmation_url)])
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


def _new_payment_kb(user_id: int | None = None) -> InlineKeyboardMarkup:
    """Дать понятный путь к новому платежу после завершения предыдущего."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_return_to_main_button(user_id)],
            [InlineKeyboardButton(text="Пополнить ещё", callback_data="show_tariffs")],
        ]
    )


async def send_tariffs(bot: Bot, user_id: int) -> None:
    with SessionLocal() as session:
        user = session.query(User).filter(User.id == user_id).first()
        balance = (user.fish_balance or 0) if user else 0
    await bot.send_message(
        chat_id=user_id,
        text=(f"Здесь можно пополнить баланс рыбок 🐟\nСейчас у тебя {balance} 🐟.\n\n"
              "Выбери сумму пополнения:"),
        reply_markup=_tariffs_keyboard(user_id),
    )


@router.callback_query(F.data == "show_tariffs")
async def cb_show_tariffs(cb: CallbackQuery) -> None:
    await cb.answer()
    await cb.message.answer(
        "Выбери сумму нового пополнения:",
        reply_markup=_tariffs_keyboard(cb.from_user.id),
    )


async def _deliver_payment_notification(bot: Bot, payment_db_id: int) -> None:
    async def deliver(channel, result):
        await send_success_notification(bot, result, channel)
    await deliver_pending_notifications(
        deliver, session_factory=SessionLocal, payment_id=payment_db_id, channels=("payment",),
    )


async def _auto_check_payment(bot: Bot, payment_db_id: int, user_id: int) -> None:
    """Совместимый однократный check; постоянную проверку выполняет payment_worker."""
    with SessionLocal() as session:
        payment = session.get(Payment, payment_db_id)
        if payment is None or payment.user_id != user_id:
            return
        provider_id = payment.yookassa_payment_id
    data = await get_payment(provider_id)
    result = apply_payment_status(payment_db_id, data, session_factory=SessionLocal)
    if result.status in ("succeeded", "canceled"):
        await _deliver_payment_notification(bot, payment_db_id)


@router.message(CommandStart())
@router.message(Command("topup"))
@router.message(F.text.in_({"Пополнить баланс 🐟", "Пополнить ещё", "Тарифы"}))
async def cmd_start(message: Message) -> None:
    """
    Точка входа во второй бот.
    """
    user = message.from_user
    if not user:
        return

    await send_tariffs(message.bot, user.id)


@router.callback_query(F.data.startswith("pay_tariff:"))
async def cb_pay_tariff(cb: CallbackQuery) -> None:
    """
    Пользователь выбрал тариф — создаём платёж в ЮKassa и отправляем ссылку на оплату.
    """
    user = cb.from_user
    if not user:
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

    await cb.answer("Готовлю ссылку на оплату…")

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
            "Не удалось создать платёж в ЮKassa. Попробуй немного позже.",
            reply_markup=_new_payment_kb(user.id),
        )
        return

    yookassa_id = payment_data.get("id")
    confirmation = payment_data.get("confirmation") or {}
    confirmation_url = confirmation.get("confirmation_url")

    immediate_success = payment_data.get("status") == "succeeded" and payment_data.get("paid") is True
    if not yookassa_id or (not confirmation_url and not immediate_success):
        logger.error("Некорректный ответ ЮKassa: %s", payment_data)
        await cb.message.answer(
            "Не удалось получить ссылку на оплату. Попробуй немного позже.",
            reply_markup=_new_payment_kb(user.id),
        )
        return

    # Сохраняем платёж в нашей базе
    with SessionLocal() as session:
        db_payment = Payment(
            user_id=user.id,
            yookassa_payment_id=yookassa_id,
            amount_rub=amount_rub,
            fish_amount=total_fish,
            status="pending",
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

    logger.info(
        "[payment] created db_id=%s yookassa_id=%s user_id=%s amount_rub=%s fish=%s",
        payment_db_id,
        yookassa_id,
        user.id,
        amount_rub,
        total_fish,
    )

    if immediate_success:
        # Даже немедленный ответ POST не объявляем успехом до проверенного GET.
        # Заказ уже сохранён pending: worker восстановит проверку при сетевой ошибке.
        try:
            verified_data = await get_payment(yookassa_id)
            result = apply_payment_status(payment_db_id, verified_data, session_factory=SessionLocal)
        except (YooKassaError, ValueError):
            logger.exception("Не удалось подтвердить немедленную оплату %s", yookassa_id)
            result = None
        if result is not None and result.status == "succeeded":
            await _deliver_payment_notification(cb.message.bot, payment_db_id)
            return
        if not confirmation_url:
            await cb.message.answer(
                "Проверяю подтверждение оплаты. Сообщу, как только рыбки окажутся на балансе.",
                reply_markup=_payment_actions_kb(payment_db_id),
            )
            return

    # Заказ подхватит постоянный worker из БД, в том числе после перезапуска.

    text_lines = [
        f"Ты выбрал тариф на {amount_rub}₽.",
        f"После успешной оплаты будет начислено {total_fish} 🐟"
        + (f" (из них {bonus_fish} — бонусные 🎁)" if bonus_fish > 0 else ""),
        "",
        "Нажми кнопку ниже, чтобы перейти на страницу оплаты ЮKassa:",
        "После оплаты я проверю платёж автоматически. Если подтверждение не пришло, нажми «Я оплатил, проверить».",
    ]
    await cb.message.answer(
        "\n".join(text_lines),
        reply_markup=_payment_actions_kb(payment_db_id, confirmation_url=confirmation_url),
    )


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

        already_succeeded = payment.status == "succeeded"

        yookassa_id = payment.yookassa_payment_id

    if already_succeeded:
        await cb.answer("Оплата уже подтверждена, рыбки на балансе ✅")
        await _deliver_payment_notification(cb.message.bot, payment_db_id)
        await _remove_payment_actions(cb.message)
        return

    await cb.answer("Проверяю статус платежа…")
    try:
        payment_data = await get_payment(yookassa_id)
        result = apply_payment_status(payment_db_id, payment_data, session_factory=SessionLocal)
    except (YooKassaError, ValueError):
        logger.exception("Не удалось подтвердить платёж %s в ЮKassa", yookassa_id)
        await cb.message.answer(
            "Пока не удалось подтвердить оплату. Я продолжу проверку автоматически. Можно проверить ещё раз через минуту.",
            reply_markup=_payment_actions_kb(payment_db_id),
        )
        return

    if result.status == "succeeded":
        # Outbox сохраняет уведомление при ошибке Telegram и повторяет доставку.
        await _deliver_payment_notification(cb.message.bot, payment_db_id)
        await _remove_payment_actions(cb.message)
    elif result.status == "canceled":
        await _deliver_payment_notification(cb.message.bot, payment_db_id)
        await _remove_payment_actions(cb.message)
    else:
        await cb.message.answer(
            "Платёж ещё не завершён. Я продолжу проверку автоматически и сообщу, когда рыбки окажутся на балансе. Можно проверить ещё раз через минуту.",
            reply_markup=_payment_actions_kb(payment_db_id),
        )
