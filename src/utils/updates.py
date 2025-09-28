import asyncio
from aiogram import Bot
from src.utils.db import get_all_users 
from src.bot.keyboards import main_menu_kb
import os

BOT_TOKEN = os.getenv("BOT_TOKEN")

async def notify_users():
    bot = Bot(token=BOT_TOKEN)
    users = await get_all_users()  # [{id: 1, feature_version: 2}, ...]
    
    for user in users:
        keyboard = main_menu_kb(user)
        try:
            await bot.send_message(
                chat_id=user['id'],
                text="",
                reply_markup=keyboard
            )
        except Exception as e:
            print(f"Не удалось отправить пользователю {user['id']}: {e}")
    
    await bot.session.close()