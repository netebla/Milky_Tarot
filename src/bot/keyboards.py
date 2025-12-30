from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton


def main_menu_kb(show_admin_features: bool = False) -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton(text="Вытянуть карту дня")],
        [KeyboardButton(text="Узнать совет карт")],
    ]

    # Премиальный расклад и баланс доступны всем пользователям
    keyboard.append([KeyboardButton(text="Задать свой вопрос")])
    keyboard.append([KeyboardButton(text="Мои рыбки")])

    # Новогодний расклад и Энергия года только для админов
    if show_admin_features:
        keyboard.append([KeyboardButton(text="Новогодний расклад 2026")])
        keyboard.append([KeyboardButton(text="Энергия года")])

    keyboard.append([KeyboardButton(text="Мои настройки"), KeyboardButton(text="Помощь")])

    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        input_field_placeholder="Выберите действие",
    )

def settings_inline_kb(push_enabled: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Изменить время пуша", callback_data="change_push_time")],
            [InlineKeyboardButton(text="Сменить часовой пояс", callback_data="change_tz")],
            [InlineKeyboardButton(
                text=("Выключить пуши" if push_enabled else "Включить пуши"),
                callback_data=("push_off" if push_enabled else "push_on"),
            )],
            [InlineKeyboardButton(text="Помощь", callback_data="help")],
        ]
    )


def fish_balance_kb() -> ReplyKeyboardMarkup:
    """Инлайн-клавиатура под сообщением с балансом рыбок."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Пополнить баланс 🐟", callback_data="fish_topup")],
            [InlineKeyboardButton(text="Главное меню", callback_data="fish_main_menu")],
        ]
    )


def fish_tariff_kb() -> InlineKeyboardMarkup:
    """Инлайн-клавиатура с вариантами тарифов."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="50₽ – 350 🐟", callback_data="fish_tariff:50"),
            ],
            [
                InlineKeyboardButton(text="150₽ – 1050 🐟", callback_data="fish_tariff:150"),
            ],
            [
                InlineKeyboardButton(text="300₽ – 2100 🐟", callback_data="fish_tariff:300"),
            ],
            [
                InlineKeyboardButton(text="650₽ – 4550 🐟", callback_data="fish_tariff:650"),
            ],
            [
                InlineKeyboardButton(text="Назад", callback_data="fish_back_to_balance"),
                InlineKeyboardButton(text="Главное меню", callback_data="fish_main_menu"),
            ],
        ]
    )


def fish_payment_method_kb() -> InlineKeyboardMarkup:
    """Инлайн-клавиатура выбора способа оплаты."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="СБП", callback_data="fish_pay:sbp"),
                InlineKeyboardButton(text="Картой", callback_data="fish_pay:card"),
            ],
            [
                InlineKeyboardButton(text="Звёздами Telegram", callback_data="fish_pay:stars"),
            ],
            [
                InlineKeyboardButton(text="Назад", callback_data="fish_back_to_tariffs"),
                InlineKeyboardButton(text="Главное меню", callback_data="fish_main_menu"),
            ],
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


def advice_draw_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Вытянуть карту", callback_data="advice_draw")],
        ]
    )


def push_card_kb() -> InlineKeyboardMarkup:
    """Кнопка под пушем для вытягивания карты дня."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Вытянуть карту дня", callback_data="push_draw_card")],
        ]
    )


def choose_tz_offset_kb() -> InlineKeyboardMarkup:
    """Клавиатура выбора смещения относительно МСК (-12..+14)."""
    rows = []
    offsets = list(range(-12, 15))
    row = []
    for idx, off in enumerate(offsets, start=1):
        sign = "+" if off >= 0 else ""
        label = f"МСК{sign}{off}"
        row.append(InlineKeyboardButton(text=label, callback_data=f"set_tz:{off}"))
        if idx % 5 == 0:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="cancel_tz")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def onboarding_name_kb(has_username: bool) -> InlineKeyboardMarkup:
    buttons = []
    if has_username:
        buttons.append([InlineKeyboardButton(text="Взять из профиля", callback_data="use_profile_name")])
    buttons.append([InlineKeyboardButton(text="Ввести вручную", callback_data="enter_name_manual")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def choose_tz_mode_kb() -> InlineKeyboardMarkup:
    """Первая ступень выбора часового пояса: МСК сразу или выбрать другое."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Московское время (МСК)", callback_data="set_tz_moscow")],
            [InlineKeyboardButton(text="Другой часовой пояс", callback_data="change_tz_other")],
            [InlineKeyboardButton(text="Отмена", callback_data="cancel_tz")],
        ]
    )
