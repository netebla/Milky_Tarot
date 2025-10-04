import asyncio
from sqlalchemy.orm import Session
from utils.db import SessionLocal, User
from aiogram import Bot
from bot.keyboards import main_menu_kb  # твоя функция для клавиатуры
import os

BOT_TOKEN = os.getenv("BOT_TOKEN")
bot = Bot(token=BOT_TOKEN)

_ADMIN_RAW = os.getenv("ADMIN_ID") or os.getenv("ADMIN_IDS") or ""
ADMIN_IDS = {s.strip() for s in _ADMIN_RAW.split(",") if s.strip()}


def _is_admin(user_id: int) -> bool:
    return str(user_id) in ADMIN_IDS

async def update_keyboards():
    async with bot:
        with SessionLocal() as session:
            users = session.query(User).all()  # получаем всех пользователей

            for user in users:
                try:
                    # Отправляем новое меню или обновляем старое сообщение
                    # Здесь можно либо редактировать старое сообщение, либо отправить новое
                    await bot.send_message(
                        chat_id=user.id,
                        text="",
                        reply_markup=main_menu_kb(_is_admin(user.id))
                    )
                except Exception as e:
                    print(f"Не удалось обновить пользователя {user.id}: {e}")
