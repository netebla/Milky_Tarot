"""Мини-скрипт для проверки интеграции с Gemini через премиальный расклад."""

from __future__ import annotations

import random

from utils.cards_loader import load_cards
from llm.three_cards import generate_three_card_reading


async def get_three_card_reading(question: str = "") -> str:
    cards = random.sample(load_cards(), 3)
    return await generate_three_card_reading(cards, question)
