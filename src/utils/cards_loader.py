"""
Утилиты загрузки карт Таро.

- Загружает карты из CSV по пути src/data/cards.csv
- Формат CSV: title;description
- Предоставляет функцию выбора случайной карты
"""
from __future__ import annotations

import csv
import os
import random
from dataclasses import dataclass
from typing import List, Optional

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
CARDS_PATH = os.path.join(DATA_DIR, "cards.csv")
IMAGES_DIR = os.path.join(DATA_DIR, "images")


@dataclass
class Card:
    title: str
    description: str

    def image_path(self) -> Optional[str]:
        # 1. Пробуем точное совпадение
        candidate = os.path.join(IMAGES_DIR, f"{self.title}.jpg")
        if os.path.exists(candidate):
            return candidate

        # 2. Пробуем нормализованное имя
        normalized = self.title.strip().lower().replace(" ", "_")
        candidate_norm = os.path.join(IMAGES_DIR, f"{normalized}.jpg")
        return candidate_norm if os.path.exists(candidate_norm) else None


def load_cards() -> List[Card]:
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


def choose_random_card(cards: List[Card]) -> Card:
    return random.choice(cards) 