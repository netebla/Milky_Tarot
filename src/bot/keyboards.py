from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton


def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Вытянуть карту дня")],
            [KeyboardButton(text="Мои настройки"), KeyboardButton(text="Помощь")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите действие",
    )


def settings_inline_kb(push_enabled: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Изменить время пуша", callback_data="change_push_time")],
            [InlineKeyboardButton(
                text=("Выключить пуши" if push_enabled else "Включить пуши"),
                callback_data=("push_off" if push_enabled else "push_on"),
            )],
            [InlineKeyboardButton(text="Помощь", callback_data="help")],
        ]
    )


def choose_time_kb() -> InlineKeyboardMarkup:
    # Predefined times for simplicity
    times = ["08:00", "09:00", "10:00", "11:00", "12:00", "18:00", "21:00"]
    rows = []
    row = []
    for idx, t in enumerate(times, start=1):
        row.append(InlineKeyboardButton(text=t, callback_data=f"set_time:{t}"))
        if idx % 3 == 0:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="cancel_time")])
    return InlineKeyboardMarkup(inline_keyboard=rows) 