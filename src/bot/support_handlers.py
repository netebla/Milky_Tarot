from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from html import escape
from io import BytesIO

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BufferedInputFile,
    CallbackQuery,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import desc, func
from sqlalchemy.exc import IntegrityError

from utils.admin_ids import get_admin_ids, is_admin
from utils.db import Payment, ProductPrice, SessionLocal, SupportMessage, SupportRelay, SupportTicket, User
from utils.pricing import ensure_default_prices

logger = logging.getLogger(__name__)

router = Router(name="support")
admin_router = Router(name="admin")


class AdminPanelStates(StatesGroup):
    waiting_price = State()
    waiting_broadcast = State()


def _admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💰 Цены", callback_data="admin:prices")],
            [InlineKeyboardButton(text="📣 Рассылка", callback_data="admin:broadcast")],
            [InlineKeyboardButton(text="📊 Использование", callback_data="admin:usage")],
            [InlineKeyboardButton(text="🧾 Финансы", callback_data="admin:finance")],
        ]
    )


def _period_keyboard(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Сегодня", callback_data=f"admin:{prefix}:1"),
                InlineKeyboardButton(text="7 дней", callback_data=f"admin:{prefix}:7"),
                InlineKeyboardButton(text="30 дней", callback_data=f"admin:{prefix}:30"),
            ],
            [InlineKeyboardButton(text="Всё время", callback_data=f"admin:{prefix}:0")],
            [InlineKeyboardButton(text="← Админка", callback_data="admin:menu")],
        ]
    )


def _back_to_admin() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="← Админка", callback_data="admin:menu")]]
    )


def _admin_ids() -> list[int]:
    result: list[int] = []
    for raw_id in get_admin_ids():
        try:
            result.append(int(raw_id))
        except ValueError:
            logger.error("Некорректный Telegram ID администратора в ADMIN_ID: %r", raw_id)
    return result


def _content_type(message: Message) -> str:
    value = message.content_type
    return getattr(value, "value", str(value))


def _preview(message: Message) -> str | None:
    value = (message.text or message.caption or "").strip()
    return value[:1000] or None


def _ticket_actions(ticket_id: int, *, closed: bool = False) -> InlineKeyboardMarkup:
    if closed:
        rows = [[InlineKeyboardButton(text="Открыть снова", callback_data=f"support_reopen:{ticket_id}")]]
    else:
        rows = [
            [InlineKeyboardButton(text="Ответить", callback_data=f"support_reply:{ticket_id}")],
            [InlineKeyboardButton(text="Закрыть обращение", callback_data=f"support_close:{ticket_id}")],
        ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _takeover_action(ticket_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Перехватить обращение", callback_data=f"support_take:{ticket_id}")]
        ]
    )


def _user_close_action(ticket_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Вопрос решён", callback_data=f"support_user_close:{ticket_id}")]
        ]
    )


def _display_user(ticket: SupportTicket) -> str:
    name = escape(ticket.display_name or "Без имени")
    username = f"@{escape(ticket.username)}" if ticket.username else "не указан"
    return (
        f'<a href="tg://user?id={ticket.user_id}">{name}</a>\n'
        f"Username: {username}\n"
        f"Telegram ID: <code>{ticket.user_id}</code>"
    )


def _save_relay(ticket_id: int, admin_id: int, admin_message_id: int) -> None:
    with SessionLocal() as db:
        db.add(
            SupportRelay(
                ticket_id=ticket_id,
                admin_id=admin_id,
                admin_message_id=admin_message_id,
            )
        )
        db.commit()


async def _notify_admins_closed_by_user(bot: Bot, ticket_id: int) -> None:
    for admin_id in _admin_ids():
        try:
            await bot.send_message(admin_id, f"Пользователь закрыл обращение #{ticket_id}.")
        except (TelegramBadRequest, TelegramForbiddenError):
            logger.warning("Не удалось сообщить администратору %s о закрытии обращения %s", admin_id, ticket_id)


def _find_ticket_by_admin_reply(admin_id: int, replied_message_id: int) -> SupportTicket | None:
    with SessionLocal() as db:
        ticket = (
            db.query(SupportTicket)
            .join(SupportRelay, SupportRelay.ticket_id == SupportTicket.id)
            .filter(
                SupportRelay.admin_id == admin_id,
                SupportRelay.admin_message_id == replied_message_id,
            )
            .order_by(desc(SupportRelay.id))
            .first()
        )
        if not ticket:
            return None
        db.expunge(ticket)
        return ticket


def _claim_ticket(ticket_id: int, admin_id: int, *, force: bool = False) -> tuple[SupportTicket | None, bool]:
    """Возвращает (ticket, claimed). claimed=False означает, что тикет у другого админа."""
    with SessionLocal() as db:
        ticket = db.query(SupportTicket).filter(SupportTicket.id == ticket_id).with_for_update().first()
        if not ticket:
            return None, False
        if ticket.status != "open":
            db.expunge(ticket)
            return ticket, False
        if ticket.assigned_admin_id not in (None, admin_id) and not force:
            db.expunge(ticket)
            return ticket, False
        ticket.assigned_admin_id = admin_id
        ticket.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(ticket)
        db.expunge(ticket)
        return ticket, True


async def configure_support_commands(bot: Bot) -> None:
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Открыть поддержку"),
            BotCommand(command="status", description="Статус обращения"),
            BotCommand(command="close", description="Закрыть обращение"),
        ]
    )
    for admin_id in _admin_ids():
        try:
            await bot.set_my_commands(
                [
                    BotCommand(command="start", description="Открыть панель поддержки"),
                    BotCommand(command="tickets", description="Открытые обращения"),
                ],
                scope=BotCommandScopeChat(chat_id=admin_id),
            )
        except (TelegramBadRequest, TelegramForbiddenError):
            logger.warning("Не удалось настроить команды поддержки для администратора %s", admin_id)


@router.message(CommandStart())
async def support_start(message: Message) -> None:
    if not message.from_user:
        return
    if is_admin(message.from_user.id):
        await message.answer(
            "Панель поддержки Milky готова.\n\n"
            "Новые обращения будут приходить сюда. Чтобы ответить, нажми «Ответить» "
            "или сделай reply на карточку/сообщение пользователя.\n\n"
            "Открытые обращения: /tickets"
        )
        return
    await message.answer(
        "Привет! Это поддержка Milky 🐾\n\n"
        "Опиши вопрос одним или несколькими сообщениями. Можно прислать фото, документ "
        "или голосовое — команда поддержки увидит их и ответит здесь."
    )


@router.message(Command("status"))
async def support_status(message: Message) -> None:
    if not message.from_user or is_admin(message.from_user.id):
        return
    with SessionLocal() as db:
        ticket = (
            db.query(SupportTicket)
            .filter(SupportTicket.user_id == message.from_user.id, SupportTicket.status == "open")
            .first()
        )
        if not ticket:
            await message.answer("Сейчас у тебя нет открытого обращения. Просто напиши вопрос, чтобы создать его.")
            return
        status = "в работе у специалиста" if ticket.assigned_admin_id else "ожидает ответа"
        ticket_id = ticket.id
    await message.answer(f"Обращение #{ticket_id}: {status}.")


@router.message(Command("close"))
async def support_user_close_command(message: Message) -> None:
    if not message.from_user or is_admin(message.from_user.id):
        return
    with SessionLocal() as db:
        ticket = (
            db.query(SupportTicket)
            .filter(SupportTicket.user_id == message.from_user.id, SupportTicket.status == "open")
            .first()
        )
        if not ticket:
            await message.answer("Открытых обращений нет.")
            return
        ticket.status = "closed"
        ticket.closed_at = datetime.utcnow()
        ticket.updated_at = datetime.utcnow()
        ticket_id = ticket.id
        db.commit()
    await message.answer(f"Обращение #{ticket_id} закрыто. Если понадобится помощь, просто напиши снова.")
    await _notify_admins_closed_by_user(message.bot, ticket_id)


@admin_router.message(CommandStart())
@admin_router.message(Command("admin"))
async def admin_panel(message: Message, state: FSMContext) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        return
    await state.clear()
    await message.answer("Управление Milky:", reply_markup=_admin_menu())


@admin_router.callback_query(F.data == "admin:menu")
async def admin_panel_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.clear()
    await callback.message.edit_text("Управление Milky:", reply_markup=_admin_menu())
    await callback.answer()


async def _show_prices(message: Message, *, edit: bool = False) -> None:
    ensure_default_prices()
    with SessionLocal() as db:
        prices = db.query(ProductPrice).order_by(ProductPrice.kind, ProductPrice.code).all()
        rows = []
        lines = ["💰 <b>Цены и тарифы</b>"]
        for item in prices:
            if item.kind == "tariff":
                value = f"{item.amount_rub} ₽ → {item.fish_amount} 🐟"
                if item.bonus_fish:
                    value += f" (бонус {item.bonus_fish})"
            else:
                value = f"{item.fish_amount} 🐟"
            lines.append(f"• {escape(item.title)}: {value}")
            rows.append(
                [InlineKeyboardButton(text=f"Изменить: {item.title}", callback_data=f"admin:price:{item.code}")]
            )
    rows.append([InlineKeyboardButton(text="← Админка", callback_data="admin:menu")])
    kwargs = {"reply_markup": InlineKeyboardMarkup(inline_keyboard=rows)}
    if edit:
        await message.edit_text("\n".join(lines), **kwargs)
    else:
        await message.answer("\n".join(lines), **kwargs)


@admin_router.message(Command("prices"))
async def admin_prices_command(message: Message) -> None:
    if message.from_user and is_admin(message.from_user.id):
        await _show_prices(message)


@admin_router.callback_query(F.data == "admin:prices")
async def admin_prices_callback(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    await _show_prices(callback.message, edit=True)
    await callback.answer()


@admin_router.callback_query(F.data.startswith("admin:price:"))
async def admin_price_select(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    code = callback.data.split(":", 2)[2]
    with SessionLocal() as db:
        item = db.get(ProductPrice, code)
        if not item:
            await callback.answer("Цена не найдена", show_alert=True)
            return
        kind = item.kind
        title = item.title
    await state.set_state(AdminPanelStates.waiting_price)
    await state.update_data(price_code=code, price_kind=kind)
    if kind == "tariff":
        prompt = (
            f"Новые значения для «{escape(title)}» одним сообщением:\n"
            "<code>рубли рыбки бонус</code>\n\nНапример: <code>199 500 50</code>"
        )
    else:
        prompt = f"Новая стоимость «{escape(title)}» в рыбках. Например: <code>79</code>"
    await callback.message.answer(prompt)
    await callback.answer()


@admin_router.message(AdminPanelStates.waiting_price)
async def admin_price_value(message: Message, state: FSMContext) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        await state.clear()
        return
    data = await state.get_data()
    code = str(data.get("price_code") or "")
    kind = str(data.get("price_kind") or "")
    try:
        values = [int(part) for part in (message.text or "").split()]
        if kind == "tariff":
            if len(values) != 3:
                raise ValueError
            amount_rub, fish_amount, bonus_fish = values
            if amount_rub <= 0 or fish_amount <= 0 or bonus_fish < 0 or bonus_fish > fish_amount:
                raise ValueError
        else:
            if len(values) != 1 or values[0] < 0:
                raise ValueError
            fish_amount = values[0]
    except ValueError:
        await message.answer("Не удалось разобрать значения. Проверь формат и отправь ещё раз.")
        return

    with SessionLocal() as db:
        item = db.get(ProductPrice, code)
        if not item:
            await message.answer("Цена больше не существует.")
            await state.clear()
            return
        if kind == "tariff":
            duplicate = (
                db.query(ProductPrice)
                .filter(ProductPrice.kind == "tariff", ProductPrice.amount_rub == amount_rub, ProductPrice.code != code)
                .first()
            )
            if duplicate:
                await message.answer("Уже есть другой тариф с такой суммой в рублях.")
                return
            item.amount_rub = amount_rub
            item.bonus_fish = bonus_fish
        item.fish_amount = fish_amount
        item.updated_at = datetime.utcnow()
        db.commit()
    await state.clear()
    await message.answer("Цена обновлена. Новые платежи и расклады сразу используют это значение.")
    await _show_prices(message)


def _period_start(days: int) -> datetime | None:
    if days <= 0:
        return None
    today = datetime.utcnow().date()
    return datetime.combine(today - timedelta(days=days - 1), datetime.min.time())


async def _send_usage_stats(message: Message, days: int) -> None:
    start = _period_start(days)
    start_date = start.date() if start else None
    with SessionLocal() as db:
        total_users = db.query(User).count()
        card_query = db.query(User).filter(User.last_card_date.is_not(None))
        question_query = db.query(User).filter(User.three_keys_last_date.is_not(None))
        if start_date:
            card_query = card_query.filter(User.last_card_date >= start_date)
            question_query = question_query.filter(User.three_keys_last_date >= start_date)
        card_users = card_query.count()
        question_users = question_query.count()
        active_today = db.query(User).filter(User.last_activity_date == date.today()).count()
    label = "за всё время" if not days else ("сегодня" if days == 1 else f"за {days} дней")
    await message.answer(
        f"📊 <b>Использование — {label}</b>\n\n"
        f"👥 Всего пользователей: {total_users}\n"
        f"🃏 Тянули карту дня: {card_users}\n"
        f"🔮 Использовали «Задать свой вопрос»: {question_users}\n"
        f"🔥 Активны сегодня: {active_today}\n\n"
        "Показатели сценариев — уникальные пользователи, у которых последнее использование попало в период.",
        reply_markup=_period_keyboard("usage"),
    )


@admin_router.message(Command("stats"))
async def admin_stats_command(message: Message) -> None:
    if message.from_user and is_admin(message.from_user.id):
        await _send_usage_stats(message, 30)


@admin_router.callback_query(F.data == "admin:usage")
async def admin_usage_callback(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    await callback.message.edit_text("Выбери период:", reply_markup=_period_keyboard("usage"))
    await callback.answer()


@admin_router.callback_query(F.data.startswith("admin:usage:"))
async def admin_usage_period(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    await _send_usage_stats(callback.message, int(callback.data.rsplit(":", 1)[1]))
    await callback.answer()


async def _send_finance_stats(message: Message, days: int) -> None:
    start = _period_start(days)
    with SessionLocal() as db:
        succeeded = db.query(Payment).filter(Payment.status == "succeeded")
        pending = db.query(Payment).filter(Payment.status == "pending")
        canceled = db.query(Payment).filter(Payment.status.in_(("canceled", "error")))
        if start:
            # Для выручки период определяется моментом последнего обновления статуса,
            # то есть максимально близко к фактическому подтверждению оплаты.
            succeeded = succeeded.filter(Payment.updated_at >= start)
            pending = pending.filter(Payment.created_at >= start)
            canceled = canceled.filter(Payment.updated_at >= start)
        succeeded_count = succeeded.count()
        revenue = int(succeeded.with_entities(func.coalesce(func.sum(Payment.amount_rub), 0)).scalar() or 0)
        fish_sold = int(succeeded.with_entities(func.coalesce(func.sum(Payment.fish_amount), 0)).scalar() or 0)
        payers = succeeded.with_entities(Payment.user_id).distinct().count()
        pending_count = pending.count()
        canceled_count = canceled.count()
    average = revenue / succeeded_count if succeeded_count else 0
    label = "за всё время" if not days else ("сегодня" if days == 1 else f"за {days} дней")
    await message.answer(
        f"🧾 <b>Финансы — {label}</b>\n\n"
        f"💳 Успешных платежей: {succeeded_count}\n"
        f"💵 Выручка: {revenue:,} ₽\n"
        f"📈 Средний чек: {average:,.0f} ₽\n"
        f"👤 Уникальных плательщиков: {payers}\n"
        f"🐟 Начислено рыбок: {fish_sold:,}\n"
        f"⏳ Ожидают оплаты: {pending_count}\n"
        f"❌ Отменены/ошибка: {canceled_count}".replace(",", " "),
        reply_markup=_period_keyboard("finance"),
    )


@admin_router.message(Command("finance"))
async def admin_finance_command(message: Message) -> None:
    if message.from_user and is_admin(message.from_user.id):
        await _send_finance_stats(message, 30)


@admin_router.callback_query(F.data == "admin:finance")
async def admin_finance_callback(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    await callback.message.edit_text("Выбери период:", reply_markup=_period_keyboard("finance"))
    await callback.answer()


@admin_router.callback_query(F.data.startswith("admin:finance:"))
async def admin_finance_period(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    await _send_finance_stats(callback.message, int(callback.data.rsplit(":", 1)[1]))
    await callback.answer()


@router.message(Command("tickets"))
async def support_open_tickets(message: Message) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        return
    with SessionLocal() as db:
        tickets = (
            db.query(SupportTicket)
            .filter(SupportTicket.status == "open")
            .order_by(desc(SupportTicket.updated_at))
            .limit(20)
            .all()
        )
        rows = [
            [
                InlineKeyboardButton(
                    text=f"#{ticket.id} · {ticket.display_name or ticket.username or ticket.user_id}",
                    callback_data=f"support_ticket:{ticket.id}",
                )
            ]
            for ticket in tickets
        ]
    if not rows:
        await message.answer("Открытых обращений нет.")
        return
    await message.answer(
        "Открытые обращения (сначала новые):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("support_ticket:"))
async def support_ticket_details(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    ticket_id = int(callback.data.rsplit(":", 1)[1])
    with SessionLocal() as db:
        ticket = db.query(SupportTicket).filter(SupportTicket.id == ticket_id).first()
        if not ticket:
            await callback.answer("Обращение не найдено", show_alert=True)
            return
        text = (
            f"Обращение #{ticket.id}\n"
            f"Статус: {escape(ticket.status)}\n"
            f"Назначено: {ticket.assigned_admin_id or 'никому'}\n\n"
            f"{_display_user(ticket)}"
        )
        closed = ticket.status != "open"
    sent = await callback.bot.send_message(
        callback.from_user.id,
        text,
        reply_markup=_ticket_actions(ticket_id, closed=closed),
    )
    _save_relay(ticket_id, callback.from_user.id, sent.message_id)
    await callback.answer()


async def _prompt_admin_reply(bot: Bot, ticket_id: int, admin_id: int) -> None:
    prompt = await bot.send_message(
        admin_id,
        f"Ответь на это сообщение — текстом, фото, документом или голосовым.\nОбращение #{ticket_id}",
        reply_markup=ForceReply(
            selective=True,
            input_field_placeholder=f"Ответ пользователю по обращению #{ticket_id}",
        ),
    )
    _save_relay(ticket_id, admin_id, prompt.message_id)


@router.callback_query(F.data.startswith("support_reply:"))
async def support_reply_button(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    ticket_id = int(callback.data.rsplit(":", 1)[1])
    ticket, claimed = _claim_ticket(ticket_id, callback.from_user.id)
    if not ticket:
        await callback.answer("Обращение не найдено", show_alert=True)
        return
    if ticket.status != "open":
        await callback.answer("Обращение уже закрыто", show_alert=True)
        return
    if not claimed:
        await callback.bot.send_message(
            callback.from_user.id,
            f"Обращение #{ticket_id} уже взял другой администратор.",
            reply_markup=_takeover_action(ticket_id),
        )
        await callback.answer()
        return
    await _prompt_admin_reply(callback.bot, ticket_id, callback.from_user.id)
    await callback.answer("Обращение взято в работу")


@router.callback_query(F.data.startswith("support_take:"))
async def support_takeover(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    ticket_id = int(callback.data.rsplit(":", 1)[1])
    ticket, claimed = _claim_ticket(ticket_id, callback.from_user.id, force=True)
    if not ticket or not claimed:
        await callback.answer("Обращение недоступно", show_alert=True)
        return
    await _prompt_admin_reply(callback.bot, ticket_id, callback.from_user.id)
    await callback.answer("Обращение передано тебе")


@router.callback_query(F.data.startswith("support_close:"))
async def support_admin_close(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    ticket_id = int(callback.data.rsplit(":", 1)[1])
    with SessionLocal() as db:
        ticket = db.query(SupportTicket).filter(SupportTicket.id == ticket_id).with_for_update().first()
        if not ticket:
            await callback.answer("Обращение не найдено", show_alert=True)
            return
        if ticket.status != "open":
            await callback.answer("Обращение уже закрыто", show_alert=True)
            return
        if ticket.assigned_admin_id not in (None, callback.from_user.id):
            await callback.answer("Обращение ведёт другой администратор", show_alert=True)
            return
        ticket.status = "closed"
        ticket.closed_at = datetime.utcnow()
        ticket.updated_at = datetime.utcnow()
        user_id = ticket.user_id
        db.commit()
    try:
        await callback.bot.send_message(
            user_id,
            f"Обращение #{ticket_id} закрыто. Если останутся вопросы, просто отправь новое сообщение.",
        )
    except (TelegramForbiddenError, TelegramBadRequest):
        logger.info("Пользователь %s заблокировал support-бота", user_id)
    await callback.answer("Обращение закрыто")
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=_ticket_actions(ticket_id, closed=True))
        except TelegramBadRequest:
            pass


@router.callback_query(F.data.startswith("support_reopen:"))
async def support_reopen(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    ticket_id = int(callback.data.rsplit(":", 1)[1])
    with SessionLocal() as db:
        ticket = db.query(SupportTicket).filter(SupportTicket.id == ticket_id).with_for_update().first()
        if not ticket:
            await callback.answer("Обращение не найдено", show_alert=True)
            return
        existing = (
            db.query(SupportTicket)
            .filter(
                SupportTicket.user_id == ticket.user_id,
                SupportTicket.status == "open",
                SupportTicket.id != ticket.id,
            )
            .first()
        )
        if existing:
            await callback.answer(f"У пользователя уже открыто обращение #{existing.id}", show_alert=True)
            return
        ticket.status = "open"
        ticket.closed_at = None
        ticket.assigned_admin_id = callback.from_user.id
        ticket.updated_at = datetime.utcnow()
        db.commit()
    await _prompt_admin_reply(callback.bot, ticket_id, callback.from_user.id)
    await callback.answer("Обращение открыто снова")


@router.callback_query(F.data.startswith("support_user_close:"))
async def support_user_close_button(callback: CallbackQuery) -> None:
    ticket_id = int(callback.data.rsplit(":", 1)[1])
    with SessionLocal() as db:
        ticket = db.query(SupportTicket).filter(SupportTicket.id == ticket_id).with_for_update().first()
        if not ticket or ticket.user_id != callback.from_user.id:
            await callback.answer("Обращение не найдено", show_alert=True)
            return
        if ticket.status != "open":
            await callback.answer("Обращение уже закрыто")
            return
        ticket.status = "closed"
        ticket.closed_at = datetime.utcnow()
        ticket.updated_at = datetime.utcnow()
        db.commit()
    await callback.answer("Спасибо!")
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer("Рада, что вопрос решён 🐾 Если что-то понадобится — напиши снова.")
    await _notify_admins_closed_by_user(callback.bot, ticket_id)


async def _send_admin_response(message: Message, ticket: SupportTicket) -> None:
    admin_id = message.from_user.id
    claimed_ticket, claimed = _claim_ticket(ticket.id, admin_id)
    if not claimed_ticket or claimed_ticket.status != "open":
        await message.answer(
            f"Обращение #{ticket.id} закрыто.",
            reply_markup=_ticket_actions(ticket.id, closed=True),
        )
        return
    if not claimed:
        await message.answer(
            f"Обращение #{ticket.id} ведёт другой администратор.",
            reply_markup=_takeover_action(ticket.id),
        )
        return

    header = None
    try:
        header = await message.bot.send_message(
            claimed_ticket.user_id,
            f"Ответ поддержки по обращению #{ticket.id}:",
            reply_markup=_user_close_action(ticket.id),
        )
        await message.bot.copy_message(
            chat_id=claimed_ticket.user_id,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
        )
    except TelegramForbiddenError:
        await message.answer("Не удалось доставить ответ: пользователь заблокировал support-бота.")
        return
    except TelegramBadRequest:
        logger.exception("Не удалось скопировать ответ администратора по обращению %s", ticket.id)
        if header:
            try:
                await message.bot.delete_message(claimed_ticket.user_id, header.message_id)
            except TelegramBadRequest:
                pass
        await message.answer("Этот тип сообщения Telegram не дал отправить. Попробуй текстом или файлом.")
        return

    with SessionLocal() as db:
        current = db.query(SupportTicket).filter(SupportTicket.id == ticket.id).first()
        if current:
            current.updated_at = datetime.utcnow()
        db.add(
            SupportMessage(
                ticket_id=ticket.id,
                sender_role="admin",
                sender_id=admin_id,
                telegram_message_id=message.message_id,
                content_type=_content_type(message),
                preview=_preview(message),
            )
        )
        db.commit()

    ack = await message.answer(
        f"Ответ отправлен пользователю · обращение #{ticket.id}",
        reply_markup=_ticket_actions(ticket.id),
    )
    _save_relay(ticket.id, admin_id, ack.message_id)


@admin_router.message(Command("broadcast"))
async def admin_broadcast_command(message: Message, state: FSMContext) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        return
    await state.set_state(AdminPanelStates.waiting_broadcast)
    await message.answer(
        "Пришли сообщение для рассылки: форматированный текст или одну картинку с подписью. "
        "Перед отправкой я покажу предпросмотр и попрошу подтверждение.",
        reply_markup=_back_to_admin(),
    )


@admin_router.callback_query(F.data == "admin:broadcast")
async def admin_broadcast_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.set_state(AdminPanelStates.waiting_broadcast)
    await callback.message.answer(
        "Пришли сообщение для рассылки: форматированный текст или одну картинку с подписью. "
        "Форматирование Telegram сохранится."
    )
    await callback.answer()


@admin_router.message(AdminPanelStates.waiting_broadcast)
async def admin_broadcast_content(message: Message, state: FSMContext) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        await state.clear()
        return
    if message.photo:
        payload = {
            "broadcast_kind": "photo",
            "broadcast_file_id": message.photo[-1].file_id,
            "broadcast_html": message.html_caption or message.caption or "",
        }
    elif message.text:
        payload = {
            "broadcast_kind": "text",
            "broadcast_html": message.html_text or message.text,
        }
    else:
        await message.answer("Поддерживаются форматированный текст или одна фотография с подписью.")
        return
    await state.update_data(**payload)
    await state.set_state(None)
    await message.answer("Предпросмотр:")
    await message.bot.copy_message(message.chat.id, message.chat.id, message.message_id)
    await message.answer(
        "Отправить это сообщение всем пользователям основного бота?",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✅ Отправить", callback_data="admin:broadcast:confirm")],
                [InlineKeyboardButton(text="Отмена", callback_data="admin:broadcast:cancel")],
            ]
        ),
    )


@admin_router.callback_query(F.data == "admin:broadcast:cancel")
async def admin_broadcast_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.clear()
    await callback.message.edit_text("Рассылка отменена.", reply_markup=_back_to_admin())
    await callback.answer()


@admin_router.callback_query(F.data == "admin:broadcast:confirm")
async def admin_broadcast_confirm(callback: CallbackQuery, state: FSMContext, main_bot: Bot) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    data = await state.get_data()
    kind = data.get("broadcast_kind")
    html_text = str(data.get("broadcast_html") or "")
    if kind not in {"text", "photo"}:
        await callback.answer("Черновик не найден", show_alert=True)
        return

    photo_bytes: bytes | None = None
    if kind == "photo":
        destination = BytesIO()
        await callback.bot.download(str(data.get("broadcast_file_id")), destination=destination)
        photo_bytes = destination.getvalue()

    with SessionLocal() as db:
        user_ids = [row[0] for row in db.query(User.id).all()]

    await callback.message.edit_text(f"Рассылка запущена. Получателей: {len(user_ids)}")
    await callback.answer()
    sent = 0
    failed = 0
    for user_id in user_ids:
        try:
            if kind == "photo" and photo_bytes is not None:
                await main_bot.send_photo(
                    user_id,
                    BufferedInputFile(photo_bytes, filename="broadcast.jpg"),
                    caption=html_text or None,
                )
            else:
                await main_bot.send_message(user_id, html_text)
            sent += 1
        except TelegramRetryAfter as exc:
            await asyncio.sleep(float(exc.retry_after))
            try:
                if kind == "photo" and photo_bytes is not None:
                    await main_bot.send_photo(
                        user_id,
                        BufferedInputFile(photo_bytes, filename="broadcast.jpg"),
                        caption=html_text or None,
                    )
                else:
                    await main_bot.send_message(user_id, html_text)
                sent += 1
            except Exception:
                failed += 1
        except (TelegramForbiddenError, TelegramBadRequest, TelegramNetworkError):
            failed += 1
        except Exception:
            logger.exception("Неожиданная ошибка рассылки пользователю %s", user_id)
            failed += 1
        await asyncio.sleep(0.05)

    await state.clear()
    logger.info("Админ %s завершил рассылку: sent=%s failed=%s", callback.from_user.id, sent, failed)
    await callback.bot.send_message(
        callback.from_user.id,
        f"Рассылка завершена.\n✅ Доставлено: {sent}\n❌ Ошибок: {failed}",
        reply_markup=_admin_menu(),
    )


@router.message()
async def support_any_message(message: Message) -> None:
    if not message.from_user:
        return

    if is_admin(message.from_user.id):
        if not message.reply_to_message:
            await message.answer(
                "Чтобы ответить пользователю, сделай reply на карточку обращения или нажми «Ответить».\n"
                "Список обращений: /tickets"
            )
            return
        ticket = _find_ticket_by_admin_reply(message.from_user.id, message.reply_to_message.message_id)
        if not ticket:
            await message.answer("Не нашла обращение для этого reply. Открой актуальную карточку через /tickets.")
            return
        await _send_admin_response(message, ticket)
        return

    now = datetime.utcnow()
    with SessionLocal() as db:
        ticket = (
            db.query(SupportTicket)
            .filter(SupportTicket.user_id == message.from_user.id, SupportTicket.status == "open")
            .with_for_update()
            .first()
        )
        is_new = ticket is None
        if ticket is None:
            ticket = SupportTicket(
                user_id=message.from_user.id,
                username=message.from_user.username,
                display_name=message.from_user.full_name,
                status="open",
                created_at=now,
                updated_at=now,
            )
            db.add(ticket)
            try:
                db.flush()
            except IntegrityError:
                # Два сообщения пользователя могли одновременно попытаться создать тикет.
                # Уникальный partial index оставляет ровно один открытый тикет.
                db.rollback()
                ticket = (
                    db.query(SupportTicket)
                    .filter(SupportTicket.user_id == message.from_user.id, SupportTicket.status == "open")
                    .first()
                )
                if ticket is None:
                    raise
                is_new = False
                ticket.username = message.from_user.username
                ticket.display_name = message.from_user.full_name
                ticket.updated_at = now
        else:
            ticket.username = message.from_user.username
            ticket.display_name = message.from_user.full_name
            ticket.updated_at = now
        db.add(
            SupportMessage(
                ticket_id=ticket.id,
                sender_role="user",
                sender_id=message.from_user.id,
                telegram_message_id=message.message_id,
                content_type=_content_type(message),
                preview=_preview(message),
            )
        )
        db.commit()
        db.refresh(ticket)
        ticket_id = ticket.id
        assigned_admin_id = ticket.assigned_admin_id
        user_summary = _display_user(ticket)

    delivered = 0
    raw_preview = _preview(message)
    message_summary = (
        f"\n\nТекст: {escape(raw_preview[:500])}" if raw_preview else f"\n\nТип сообщения: {_content_type(message)}"
    )
    for admin_id in _admin_ids():
        try:
            label = "Новое обращение" if is_new else "Новое сообщение"
            assigned = f"\nВ работе у: <code>{assigned_admin_id}</code>" if assigned_admin_id else ""
            card = await message.bot.send_message(
                admin_id,
                f"{'🆕' if is_new else '💬'} {label} #{ticket_id}\n\n{user_summary}{assigned}\n\n"
                f"Ответь reply на эту карточку или на сообщение ниже.{message_summary}",
                reply_markup=_ticket_actions(ticket_id),
            )
            _save_relay(ticket_id, admin_id, card.message_id)
            delivered += 1
            try:
                copied = await message.bot.copy_message(
                    chat_id=admin_id,
                    from_chat_id=message.chat.id,
                    message_id=message.message_id,
                )
                _save_relay(ticket_id, admin_id, copied.message_id)
            except TelegramBadRequest:
                logger.exception(
                    "Telegram не дал скопировать %s из обращения %s администратору %s",
                    _content_type(message),
                    ticket_id,
                    admin_id,
                )
        except (TelegramForbiddenError, TelegramBadRequest):
            logger.exception(
                "Не удалось уведомить администратора %s об обращении %s. "
                "Администратор должен запустить support-бота через /start.",
                admin_id,
                ticket_id,
            )

    if delivered:
        if is_new:
            await message.answer(
                f"Обращение #{ticket_id} создано и передано команде поддержки. Ответ придёт сюда."
            )
        else:
            await message.answer("Сообщение добавлено к обращению.")
    else:
        await message.answer(
            "Сообщение сохранено, но команда поддержки пока недоступна. Мы увидим его, как только подключимся."
        )
