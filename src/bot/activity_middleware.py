"""Входящее сообщение или callback — действие; исходящий пуш — нет."""

import logging

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message

from utils.activity import record_user_activity

logger = logging.getLogger(__name__)


class UserActivityMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = event.from_user
        chat = event.chat if isinstance(event, Message) else (
            event.message.chat if isinstance(event, CallbackQuery) and event.message else None
        )
        if user and not user.is_bot and chat and chat.type == "private":
            try:
                record_user_activity(user.id)
            except Exception:
                # Ошибка аналитики не должна прерывать обработку действия пользователя.
                logger.exception("Не удалось записать активность user_id=%s", user.id)
        return await handler(event, data)
