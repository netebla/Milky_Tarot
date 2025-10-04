"""Prompt construction for three-card tarot reading."""

from __future__ import annotations

from typing import Sequence

from utils.cards_loader import Card
from .client import ask_llm

MAX_LENGTH = 1200


def _build_prompt(cards: Sequence[Card], question: str) -> str:
    titles = ", ".join(card.title for card in cards)
    question = question.strip()
    question_clause = (
        f"Вопрос клиента: {question}. " if question else "Вопрос клиента не указан. "
    )
    return (
        "Ты — таролог, делающий ясные и земные объяснения. "
        "Используй только обычный связный текст без Markdown, списков, эмодзи или символов форматирования. "
        "Ответ должен быть разделён на несколько абзацев с завершёнными мыслями. "
        "Сделай трактовку расклада 'Три карты'. "
        f"Карты: {titles}. "
        f"{question_clause}"
        f"Объясни общую энергию расклада, коротко опиши роль каждой карты и заверши практическим советом. "
        f"Уложись примерно в {MAX_LENGTH} символов и избегай эзотерических терминов, которые могут быть непонятны новичку."
    )


async def generate_three_card_reading(cards: Sequence[Card], question: str) -> str:
    prompt = _build_prompt(cards, question)
    return await ask_llm(prompt)
