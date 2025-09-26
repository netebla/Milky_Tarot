"""
Утилиты загрузки карт Таро через GitHub.

- Загружает карты из CSV по пути src/data/cards.csv (из репозитория локально)
- Формат CSV: title;description
- Для картинок формирует URL в GitHub
- Предоставляет функцию выбора карты дня
"""

from __future__ import annotations
import csv
import os
import random
from dataclasses import dataclass
from typing import List
from datetime import datetime
import pytz

# Базовый URL для картинок в GitHub
GITHUB_RAW_BASE = "https://raw.githubusercontent.com/netebla/Milky_Tarot/main/src/data/images"

# Путь к CSV локально (он нужен только для названий и описаний)
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
CARDS_PATH = os.path.join(DATA_DIR, "cards.csv")

MOSCOW_TZ = pytz.timezone("Europe/Moscow")


@dataclass
class Card:
    title: str
    description: str

    def image_url(self) -> str:
        """Вернуть URL к изображению в GitHub."""
        normalized = self.title.strip().replace(" ", "_")
        return f"{GITHUB_RAW_BASE}/{normalized}.jpg"


def load_cards() -> List[Card]:
    """Загрузить все карты из CSV."""
    if not os.path.exists(CARDS_PATH):
        raise FileNotFoundError(f"Не найден CSV с картами: {CARDS_PATH}")

    cards: List[Card] = []
    with open(CARDS_PATH, "r", encoding="utf-8") as f:
        reader = csv.reader(f, delimiter=";")
        for row in reader:
            if not row or len(row) < 2:
                continue
            title, description = row[0].strip(), row[1].strip()
            if title and description:
                cards.append(Card(title=title, description=description))

    if not cards:
        raise ValueError("В CSV нет валидных записей. Требуется формат 'title;description'.")

    return cards


def choose_random_card(user: dict, cards: List[Card]) -> Card:
    """
    Выбрать карту дня для пользователя.
    - Если карта уже выбрана сегодня, вернуть её.
    - Иначе выбрать случайную, обновить last_card и last_card_date.
    """
    now_moscow = datetime.now(MOSCOW_TZ).date()
    last_card_date_str = user.get("last_card_date")
    if last_card_date_str:
        last_card_date = datetime.fromisoformat(last_card_date_str).date()
        if last_card_date == now_moscow and user.get("last_card"):
            return next((c for c in cards if c.title == user["last_card"]), cards[0])

    # Выбираем новую карту
    new_card = random.choice(cards)
    user["last_card"] = new_card.title
    user["last_card_date"] = now_moscow.isoformat()
    user["draw_count"] = int(user.get("draw_count", 0)) + 1
    return new_card
