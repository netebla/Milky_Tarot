"""Промпты и генерация для новогоднего расклада на 2026 год."""

from __future__ import annotations

from typing import Sequence

from utils.cards_loader import Card
from .client import ask_llm
from .rag import build_rag_prompt

MAX_LENGTH = 800

# Вопросы новогоднего расклада
NEW_YEAR_QUESTIONS = [
    {
        "category": "Про меня",
        "question": "Что будет главным для меня в этом году?",
    },
    {
        "category": "Про деньги",
        "question": "Что будет с моими доходами в этом году?",
    },
    {
        "category": "Про общение и дела",
        "question": "Что будет происходить в социальной сфере?",
    },
    {
        "category": "Про дом и семью",
        "question": "Что будет происходить дома и в семье в этом году?",
    },
    {
        "category": "Про любовь и удовольствие",
        "question": "Что будет в личной жизни и радостях в этом году?",
    },
    {
        "category": "Про здоровье и режим",
        "question": "Что будет с моим здоровьем и повседневным режимом в этом году?",
    },
    {
        "category": "Про партнёрство",
        "question": "Что будет в серьёзных отношениях и сотрудничествах в этом году?",
    },
    {
        "category": "Внутреннее года",
        "question": "Что мне в себе лучше всего проработать в этом году?",
    },
    {
        "category": "Про поездки и расширение",
        "question": "Какие важные поездки возможны в этом году?",
    },
    {
        "category": "Про работу и статус",
        "question": "Как будет развиваться моя карьера и статус в этом году?",
    },
    {
        "category": "Про друзей и круг общения",
        "question": "Какие люди придут в мою жизнь в этом году?",
    },
    {
        "category": "Про скрытое",
        "question": "Что будет \"за кадром\" и сильнее всего влиять на меня в этом году?",
    },
    {
        "category": "Итог года",
        "question": "Какой будет общий итог этого года для меня?",
    },
]


def _build_new_year_prompt(card: Card, question_data: dict[str, str], question_index: int, total_questions: int) -> str:
    """Собрать промпт для одного вопроса новогоднего расклада."""
    category = question_data["category"]
    question = question_data["question"]
    
    return (
        "Ты — таролог, делающий ясные и земные объяснения для новогоднего расклада на 2026 год. "
        "Используй только обычный связный текст без Markdown, списков, эмодзи или символов форматирования. "
        "Ответ должен быть разделён на несколько абзацев с завершёнными мыслями. "
        f"Это вопрос {question_index} из {total_questions} в новогоднем раскладе. "
        f"Категория: {category}. "
        f"Вопрос: {question}. "
        f"Выпавшая карта: {card.title}. "
        "Учитывай контекст нового года 2026 — это время новых возможностей, изменений и роста. "
        "Сделай трактовку карты в контексте этого конкретного вопроса о годе. "
        "Объясни, что карта говорит именно об этой сфере жизни в 2026 году. "
        "Будь конкретным и практичным, избегай общих фраз. "
        f"Уложись примерно в {MAX_LENGTH} символов и избегай эзотерических терминов, которые могут быть непонятны новичку."
    )


async def generate_new_year_reading(card: Card, question_data: dict[str, str], question_index: int, total_questions: int) -> str:
    """Сгенерировать трактовку для одного вопроса новогоднего расклада."""
    base_prompt = _build_new_year_prompt(card, question_data, question_index, total_questions)
    prompt = build_rag_prompt(base_prompt, [card])
    return await ask_llm(prompt)

