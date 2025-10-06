"""Утилиты загрузки карт Таро через GitHub."""

from __future__ import annotations

import csv
import os
import random
from dataclasses import dataclass
from datetime import datetime
from typing import List
from urllib.parse import quote

import pytz

# Базовый URL для картинок в GitHub
GITHUB_RAW_BASE = "https://raw.githubusercontent.com/netebla/Milky_Tarot/main/src/data/images"

# Путь к CSV локально (он нужен только для названий и описаний)
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
CARDS_PATH = os.path.join(DATA_DIR, "cards.csv")

MOSCOW_TZ = pytz.timezone("Europe/Moscow")


def _normalize_title(title: str) -> str:
    return quote(title.strip().replace(" ", "_"))


@dataclass
class Card:
    title: str
    description: str

    def image_url(self) -> str:
        """Вернуть URL к изображению в GitHub с корректным кодированием имени."""
        return f"{GITHUB_RAW_BASE}/{_normalize_title(self.title)}.jpg"


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

CARDS_ADVICE_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "cards_advice.csv")

def load_advice_cards() -> List[Card]:
    cards = []
    with open(CARDS_ADVICE_PATH, newline="", encoding="utf-8") as csvfile:
        reader = csv.DictReader(csvfile)
        for row in reader:
            cards.append(Card(title=row["title"], description=row["description"]))
    return cards





import random
from datetime import datetime
from typing import List
from sqlalchemy.orm import Session
from .db import User
from .cards_loader import Card, MOSCOW_TZ  # предполагаю, что Card и часовой пояс уже есть

def choose_random_card(user: User, cards: List[Card], db: Session) -> Card:
    """
    Выбрать карту дня для пользователя.
    - Если карта уже выбрана сегодня, вернуть её.
    - Иначе выбрать случайную, обновить last_card, last_card_date и draw_count в базе.
    """
    now_moscow = datetime.now(MOSCOW_TZ).date()

    # Проверяем, есть ли карта сегодня
    if user.last_card_date and user.last_card_date == now_moscow and user.last_card:
        return next((c for c in cards if c.title == user.last_card), cards[0])

    # Выбираем новую карту
    new_card = random.choice(cards)
    user.last_card = new_card.title
    user.last_card_date = now_moscow
    user.draw_count = (user.draw_count or 0) + 1

    # Обновляем дату последней активности
    user.last_activity_date = now_moscow

    # Сохраняем изменения в базе
    db.add(user)
    db.commit()
    db.refresh(user)

    return new_card
