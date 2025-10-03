"""Мини-скрипт для проверки интеграции с Qwen API."""

from __future__ import annotations

import csv
import random

from llm.client import ask_llm

CSV_FILE = "src/data/cards_advice.csv"


def get_random_cards(n: int = 3) -> list[str]:
    cards: list[str] = []
    with open(CSV_FILE, "r", encoding="utf-8") as f:
        reader = csv.reader(f, delimiter=";")
        for row in reader:
            if row:
                cards.append(row[0].strip())
    if len(cards) < n:
        raise ValueError("Недостаточно карт для генерации расклада")
    return random.sample(cards, n)


async def get_three_card_reading() -> str:
    cards = get_random_cards()
    prompt = f"Сделай трактовку расклада три карты: {', '.join(cards)}"
    return await ask_llm(prompt)
