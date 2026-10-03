"""Обработчики отдельного закрытого админ-бота Milky."""

from aiogram import Bot
from aiogram.types import BotCommand

# Админская часть изолирована отдельным Router и не подключается к support-боту.
from .support_handlers import admin_router as router


async def configure_admin_commands(bot: Bot) -> None:
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Открыть админ-панель"),
            BotCommand(command="prices", description="Изменить цены"),
            BotCommand(command="broadcast", description="Создать рассылку"),
            BotCommand(command="stats", description="Статистика использования"),
            BotCommand(command="finance", description="Финансовая статистика"),
        ]
    )
