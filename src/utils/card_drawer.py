"""Случайная карта из основной колоды для расклада «Живой диалог» (не карта дня)."""

from __future__ import annotations

import random
from typing import List, Sequence

from utils.cards_loader import Card, load_cards


def draw_random_card(cards: Sequence[Card] | None = None) -> tuple[str, bool]:
    """
    Вернуть (название карты, перевёрнута ли).

    Использует ту же колоду, что и остальной бот (cards.csv).
    """
    deck: List[Card] = list(cards) if cards is not None else load_cards()
    if not deck:
        raise ValueError("Колода пуста")
    card = random.choice(deck)
    is_reversed = random.random() < 0.5
    return card.title, is_reversed
