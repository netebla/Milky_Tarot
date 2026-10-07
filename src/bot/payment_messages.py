"""Подтверждение пополнения после начисления: одно сообщение в каждом боте."""

import logging
from pathlib import Path

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from utils.pending_readings import get_pending_reading

logger = logging.getLogger(__name__)
IMAGES_DIR = Path(__file__).resolve().parent.parent / "data" / "images"
MAIN_BOT_URL = "https://t.me/Milky_Tarot_Bot"


def success_keyboard(user_id, channel):
    reading = get_pending_reading(user_id)
    if channel == "main":
        primary = InlineKeyboardButton(
            text="Продолжить вопрос" if reading else "Задать вопрос Милки",
            callback_data=f"resume_reading:{reading.id}" if reading else "daily_card_ask_question",
        )
        # Пополнение доступно в платёжном боте; основное действие ведёт к ответу.
        secondary = InlineKeyboardButton(text="Пополнить ещё", url="https://t.me/Milky_payment_bot?start=topup_balance")
    elif channel == "payment":
        primary = InlineKeyboardButton(
            text="Продолжить вопрос в Милки" if reading else "Вернуться в Милки",
            url=f"{MAIN_BOT_URL}?start=resume_{reading.id}" if reading else MAIN_BOT_URL,
        )
        secondary = InlineKeyboardButton(text="Пополнить ещё", callback_data="show_tariffs")
    else:
        raise ValueError(f"Unknown notification channel: {channel}")
    return InlineKeyboardMarkup(inline_keyboard=[[primary], [secondary]])


async def send_success_notification(bot, result, channel):
    """Доставка подтверждённого события оплаты; ошибки повторяются из outbox."""
    if result.status == "canceled" and channel == "payment":
        await bot.send_message(
            chat_id=result.user_id,
            text=(
                "Платёж отменён — рыбки за него не начислены 🐾\n"
                "Можно попробовать пополнить баланс ещё раз или вернуться в Милки.\n"
                "Если деньги всё же списались, напиши в поддержку через «Помощь» в Милки."
            ),
            reply_markup=success_keyboard(result.user_id, channel),
        )
        return
    if result.status != "succeeded":
        raise ValueError("Cannot announce a payment before confirmed crediting")
    keyboard = success_keyboard(result.user_id, channel)
    has_question = keyboard.inline_keyboard[0][0].text.startswith("Продолжить вопрос")
    if channel == "main":
        text = (
            "Мяу, рыбки уже в мисочке — спасибо! 🐟💖\n"
            f"Баланс пополнен на {result.fish_amount} 🐟. Теперь у тебя {result.balance} 🐟.\n\n"
            "Теперь я сытая и готова разбирать твои вопросы 😻\n"
            + ("Твой вопрос сохранён — нажми «Продолжить вопрос», и продолжим с того места, где остановились."
               if has_question else "Задавай свой вопрос — посмотрим, что подскажут карты.")
        )
        await bot.send_message(chat_id=result.user_id, text=text, reply_markup=keyboard)
        return
    text = (
        "Оплата прошла успешно ✨\n"
        f"Тебе начислено {result.fish_amount} 🐟. Твой баланс: {result.balance} 🐟.\n\n"
        "Мяу, рыбки уже в мисочке — спасибо! 🐟💖\nТеперь я сытая и готова разбирать твои вопросы 😻\n"
        + ("Твой вопрос сохранён. Возвращайся в Милки — продолжим расклад."
           if has_question else "Возвращайся в Милки — я готова отвечать на твои вопросы.")
    )
    image = IMAGES_DIR / "fed_milky.jpg"
    if image.exists():
        try:
            await bot.send_photo(
                chat_id=result.user_id,
                photo=BufferedInputFile(image.read_bytes(), filename=image.name),
                caption=text, reply_markup=keyboard,
            )
            return
        except TelegramBadRequest:
            logger.warning("Фото благодарности не принято Telegram; отправляем текст", exc_info=True)
    await bot.send_message(chat_id=result.user_id, text=text, reply_markup=keyboard)
