"""Утилиты для загрузки карт Таро и выбора карт дня."""

from __future__ import annotations

import csv
import random
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List
from urllib.parse import quote

import pytz
from sqlalchemy.orm import Session

from .db import User

# Пути к данным и изображениям
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CARDS_PATH = DATA_DIR / "cards.csv"
IMAGES_DIR = DATA_DIR / "images"

# Публичный fallback (на случай, если потребуется URL)
GITHUB_RAW_BASE = "https://raw.githubusercontent.com/netebla/Milky_Tarot/main/src/data/images"

MOSCOW_TZ = pytz.timezone("Europe/Moscow")


def _normalized_filename(title: str) -> str:
    return quote(title.strip().replace(" ", "_"))


def _normalized_title(title: str) -> str:
    return title.strip()


@dataclass
class Card:
    title: str
    description: str

    def image_path(self) -> Path:
        return IMAGES_DIR / f"{_normalized_title(self.title).replace(' ', '_')}.jpg"

    def image_url(self) -> str:
        return f"{GITHUB_RAW_BASE}/{_normalized_filename(self.title)}.jpg"


def load_cards() -> List[Card]:
    if not CARDS_PATH.exists():
        raise FileNotFoundError(f"Не найден CSV с картами: {CARDS_PATH}")

    cards: List[Card] = []
    with CARDS_PATH.open("r", encoding="utf-8") as f:
        reader = csv.reader(f, delimiter=";")
        for row in reader:
            if len(row) < 2:
                continue
            title, description = row[0].strip(), row[1].strip()
            if title and description:
                cards.append(Card(title=title, description=description))

    if not cards:
        raise ValueError("В CSV нет валидных записей. Требуется формат 'title;description'.")

    return cards


CARDS_ADVICE_PATH = DATA_DIR / "cards_advice.csv"


def load_advice_cards() -> List[Card]:
    if not CARDS_ADVICE_PATH.exists():
        raise FileNotFoundError(f"Не найден CSV с советами: {CARDS_ADVICE_PATH}")

    cards: List[Card] = []
    with CARDS_ADVICE_PATH.open("r", encoding="utf-8") as f:
        reader = csv.reader(f, delimiter=";")
        for row in reader:
            if len(row) < 2:
                continue
            cards.append(Card(title=row[0].strip(), description=row[1].strip()))
    return cards


def choose_random_card(user: User, cards: List[Card], db: Session) -> Card:
    """Выбрать карту дня. Если уже тянули сегодня — вернуть прежнюю."""
    now_moscow = datetime.now(MOSCOW_TZ).date()

    if user.last_card_date and user.last_card_date == now_moscow and user.last_card:
        return next((c for c in cards if c.title == user.last_card), cards[0])

    new_card = random.choice(cards)
    user.last_card = new_card.title
    user.last_card_date = now_moscow
    user.draw_count = (user.draw_count or 0) + 1
    user.last_activity_date = now_moscow

    db.add(user)
    db.commit()
    db.refresh(user)
    return new_card


__all__ = [
    "Card",
    "load_cards",
    "load_advice_cards",
    "choose_random_card",
    "MOSCOW_TZ",
]
