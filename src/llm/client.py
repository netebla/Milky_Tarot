"""Асинхронный клиент для обращения к Google Gemini API."""

from __future__ import annotations

import os
from typing import Any, Dict

import aiohttp

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY") or "AIzaSyA6umGCHmYuMR2m62xOL5oTvQFTzyqKqho"
GEMINI_MODEL = "models/gemini-1.5-flash"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/{GEMINI_MODEL}:generateContent"


class GeminiClientError(RuntimeError):
    """Ошибки взаимодействия с Google Gemini."""


def _build_payload(prompt: str) -> Dict[str, Any]:
    return {
        "contents": [
            {
                "parts": [{"text": prompt}],
            }
        ]
    }


async def ask_llm(prompt: str) -> str:
    """Отправить запрос в Gemini и вернуть текстовый ответ."""
    if not GOOGLE_API_KEY or GOOGLE_API_KEY == "":
        raise GeminiClientError("GOOGLE_API_KEY не задан. Укажите ключ в переменных окружения или client.py.")

    params = {"key": GOOGLE_API_KEY}
    payload = _build_payload(prompt)

    async with aiohttp.ClientSession() as session:
        async with session.post(GEMINI_URL, params=params, json=payload) as resp:
            if resp.status >= 400:
                text = await resp.text()
                raise GeminiClientError(f"Gemini API error {resp.status}: {text}")
            data = await resp.json()

    candidates = data.get("candidates") or []
    if not candidates:
        raise GeminiClientError("Пустой ответ от Gemini")

    content = candidates[0].get("content", {})
    parts = content.get("parts") or []
    if not parts:
        raise GeminiClientError("В ответе Gemini отсутствуют parts")

    text = parts[0].get("text")
    if not text:
        raise GeminiClientError("В ответе Gemini отсутствует текст")

    return text
