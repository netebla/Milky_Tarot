"""Клиент для взаимодействия с Google Gemini через пакет google-genai."""

from __future__ import annotations

import asyncio
import os
import logging
from typing import Optional

from google import genai

GEMINI_MODEL = os.getenv("GEMINI_MODEL") or "gemini-2.5-flash"

# Конфигурация прокси
PROXY_URL = os.getenv("PROXY_URL")
PROXY_ENABLED = (os.getenv("PROXY_ENABLED", "false").lower() == "true") and bool(PROXY_URL)

_client: Optional[genai.Client] = None
_client_lock = asyncio.Lock()

logger = logging.getLogger(__name__)


class GeminiClientError(RuntimeError):
    """Ошибки взаимодействия с Google Gemini."""


def _get_api_key() -> str:
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise GeminiClientError(
            "GEMINI_API_KEY не задан. Передайте ключ через переменные окружения."
        )
    return api_key


async def _get_client() -> genai.Client:
    global _client
    if _client is not None:
        return _client

    async with _client_lock:
        if _client is None:
            if PROXY_ENABLED:
                for var in ("ALL_PROXY", "HTTPS_PROXY", "HTTP_PROXY"):
                    os.environ[var] = PROXY_URL
                logger.info("Gemini client proxy enabled: %s", PROXY_URL)
            else:
                logger.info("Gemini client proxy disabled")

            _client = genai.Client(
                api_key=_get_api_key(),
            )
            logger.info("Gemini client initialized with model %s", GEMINI_MODEL)
    return _client


async def ask_llm(prompt: str) -> str:
    """Отправить запрос в Gemini и вернуть текстовый ответ."""

    client = await _get_client()

    def _invoke() -> str:
        try:
            logger.info(
                "Sending prompt to Gemini (model=%s, length=%d)",
                GEMINI_MODEL,
                len(prompt),
            )
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
            )
        except Exception as exc:
            proxy_info = " (через прокси)" if PROXY_ENABLED else ""
            logger.exception("Gemini call failed%s", proxy_info)
            raise GeminiClientError(f"Ошибка обращения к Gemini{proxy_info}: {exc}") from exc

        text = getattr(response, "text", None)
        if text:
            logger.info("Gemini response received (length=%d)", len(text))
            return text

        candidates = getattr(response, "candidates", None) or []
        for candidate in candidates:
            content = getattr(candidate, "content", None)
            parts = getattr(content, "parts", None) or []
            joined = "".join(getattr(part, "text", "") for part in parts if getattr(part, "text", None))
            if joined:
                logger.info("Gemini response composed from parts (length=%d)", len(joined))
                return joined
        logger.error("Gemini response contained no text parts")
        raise GeminiClientError("В ответе Gemini отсутствует текстовая часть")

    return await asyncio.to_thread(_invoke)
