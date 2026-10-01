from __future__ import annotations

import logging
from datetime import datetime
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    CallbackQuery,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import desc
from sqlalchemy.exc import IntegrityError

from utils.admin_ids import get_admin_ids, is_admin
from utils.db import SessionLocal, SupportMessage, SupportRelay, SupportTicket

logger = logging.getLogger(__name__)

router = Router(name="support")


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
