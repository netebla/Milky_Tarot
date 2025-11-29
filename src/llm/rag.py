"""Общий вспомогательный модуль для "псевдо-RAG" контекста.

Задача: на вход получать список карт расклада и готовый базовый промпт,
а на выходе возвращать промпт, дополненный трактовками только тех карт,
которые реально выпали (rag_cards.csv).

LLM не должен упоминать, что этот контекст был передан отдельно.
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Sequence

from utils.cards_loader import Card

logger = logging.getLogger(__name__)

# Путь к данным
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
RAG_CARDS_PATH = DATA_DIR / "rag_cards.csv"


def _clean_title(raw: str) -> str:
    return raw.replace("\ufeff", "").strip()


def _load_rag_card_meanings() -> dict[str, str]:
    meanings: dict[str, str] = {}
    if not RAG_CARDS_PATH.exists():
        logger.warning("RAG cards CSV not found: %s", RAG_CARDS_PATH)
        return meanings

    try:
        with RAG_CARDS_PATH.open("r", encoding="utf-8") as f:
            reader = csv.reader(f, delimiter=";")
            for row in reader:
                if len(row) < 2:
                    continue
                title_raw, meaning = row[0], row[1]
                title = _clean_title(title_raw)
                meaning = meaning.strip()
                if title and meaning:
                    meanings[title] = meaning
    except OSError as exc:
        logger.warning("Failed to read RAG cards CSV %s: %s", RAG_CARDS_PATH, exc)

    return meanings


RAG_CARD_MEANINGS = _load_rag_card_meanings()


def build_rag_prompt(base_prompt: str, cards: Sequence[Card]) -> str:
    """Вернуть промпт, дополненный RAG-контекстом по выпавшим картам.

    - base_prompt: уже собранный промпт (инструкции + вопрос + список карт);
    - cards: объекты Card, которые реально участвуют в раскладе.
    """
    pieces: list[str] = []

    card_snippets: list[str] = []
    for card in cards:
        title = _clean_title(card.title)
        meaning = RAG_CARD_MEANINGS.get(title)
        if meaning:
            card_snippets.append(f"{card.title}: {meaning}")

    if card_snippets:
        pieces.append(
            "Дополнительные трактовки только для выпавших в раскладе карт:\n"
            + "\n\n".join(card_snippets)
        )

    if not pieces:
        # Нет трактовок — просто возвращаем исходный промпт.
        return base_prompt

    rag_context = (
        "Ниже приведены дополнительные трактовки карт, которые выпали в раскладе. "
        "Не ссылайся на этот контекст напрямую и не упоминай, что он был отдельно передан — "
        "просто используй его смысл внутри живой, человечной трактовки.\n\n"
        + "\n\n".join(pieces)
    )

    return f"{rag_context}\n\n---\n\n{base_prompt}"


__all__ = ["build_rag_prompt"]
