"""Детерминированная проверка текста от внешней LLM перед отправкой в Telegram."""

from __future__ import annotations

import re

# В раскладах ссылки не являются частью полезного ответа. Запрет на них —
# намеренно строгий последний рубеж на случай сбоя или чужого текста от провайдера.
_URL_RE = re.compile(
    r"(?:https?://|www\.|(?:t|bit)\.co/|telegram\.me/|t\.me/)", re.IGNORECASE
)
_PROMOTIONAL_SIGNATURE_RE = re.compile(
    r"(?:only1sam|getsmarter|join\s+(?:us|now|today)|exclusive\s+(?:news|insights))",
    re.IGNORECASE,
)
_SOCIAL_POST_METADATA_RE = re.compile(r"(?:日期時間|內容|圖片|網址)\s*:")


def unsafe_output_reason(text: str) -> str | None:
    """Вернуть причину, по которой ответ LLM нельзя показать в Telegram."""
    if len(text) > 3_500:
        return "response_too_long"
    if _URL_RE.search(text):
        return "external_link"
    if _PROMOTIONAL_SIGNATURE_RE.search(text):
        return "promotional_signature"
    if _SOCIAL_POST_METADATA_RE.search(text):
        return "social_post_metadata"
    return None


def safe_generated_label(value: object, *, fallback: str, max_length: int = 120) -> str:
    """Нормализовать короткое поле из tool call, которое показывается пользователю."""
    label = " ".join(str(value or "").split())[:max_length]
    return fallback if not label or unsafe_output_reason(label) else label
