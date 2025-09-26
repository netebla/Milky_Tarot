"""
Утилиты загрузки карт Таро.

- Загружает карты из CSV по пути src/data/cards.csv
- Формат CSV: title;description
- Предоставляет выбор карты дня с кэшированием в UserStorage
"""
from __future__ import annotations

import csv
import os
import random
from dataclasses import dataclass
from typing import List, Optional

from utils.storage import UserStorage

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
CARDS_PATH = os.path.join(DATA_DIR, "cards.csv")
IMAGES_DIR = os.path.join(DATA_DIR, "images")

# Хранилище пользователей
user_storage = UserStorage()

@dataclass
class Card:
    title: str
    description: str

    def image_path(self) -> Optional[str]:
        """Вернуть путь к изображению при наличии, иначе None.
        Ожидаемое имя файла: images/<название>.jpg (поддержка русских букв)
        """
        candidate = os.path.join(IMAGES_DIR, f"{self.title}.jpg")
        return candidate if os.path.exists(candidate) else None


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


# Загружаем карты один раз при старте
CARDS = load_cards()
TITLE_TO_CARD = {card.title: card for card in CARDS}


def get_card_by_title(title: str) -> Card:
    """Найти карту по названию."""
    return TITLE_TO_CARD.get(title)  # вернёт None, если нет


def draw_card_for_user(user_id: int) -> Card:
    """Выдать карту дня для пользователя, фиксируя её на день."""
    user = user_storage.get_user(user_id)
    today = user_storage._today_str()

    if user and user.get("last_card_date") == today:
        card_title = user["last_card"]
        card = get_card_by_title(card_title)
        if card:
            return card

    # Иначе выбираем случайную карту
    card = random.choice(CARDS)
    user_storage.set_last_card(user_id, card.title)
    return card

