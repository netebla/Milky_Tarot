"""Клиент для взаимодействия с Google Gemini через пакет google-genai."""

from __future__ import annotations

import asyncio
import os
from typing import Optional

from google import genai

GEMINI_MODEL = os.getenv("GEMINI_MODEL") or "gemini-2.5-flash"

# Конфигурация прокси
PROXY_ENABLED = os.getenv("PROXY_ENABLED", "true").lower() == "true"
PROXY_URL = os.getenv("PROXY_URL") or "socks5://user327180:ixcln2@185.113.137.117:12989"

_client: Optional[genai.Client] = None
_client_lock = asyncio.Lock()


class GeminiClientError(RuntimeError):
    """Ошибки взаимодействия с Google Gemini."""


def _get_api_key() -> str:
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise GeminiClientError(
            "GEMINI_API_KEY не задан. Передайте ключ через переменные окружения."
        )
    return api_key


def _setup_proxy_environment():
    """Настраивает переменные окружения для работы с прокси."""
    if not PROXY_ENABLED:
        return
        
    proxy_url = PROXY_URL
    
    # Для библиотек, которые используют стандартные переменные окружения
    os.environ['HTTP_PROXY'] = proxy_url
    os.environ['HTTPS_PROXY'] = proxy_url
    os.environ['ALL_PROXY'] = proxy_url
    
    # Для Python requests/urllib3
    os.environ['http_proxy'] = proxy_url
    os.environ['https_proxy'] = proxy_url
    os.environ['all_proxy'] = proxy_url


async def _get_client() -> genai.Client:
    global _client
    if _client is not None:
        return _client

    async with _client_lock:
        if _client is None:
            # Настраиваем прокси перед созданием клиента
            if PROXY_ENABLED:
                _setup_proxy_environment()
            
            _client = genai.Client(api_key=_get_api_key())
    return _client


async def ask_llm(prompt: str) -> str:
    """Отправить запрос в Gemini и вернуть текстовый ответ."""

    client = await _get_client()

    def _invoke() -> str:
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
            )
        except Exception as exc:  # noqa: BLE001 - want полный stack
            proxy_info = " (через прокси)" if PROXY_ENABLED else ""
            raise GeminiClientError(f"Ошибка обращения к Gemini{proxy_info}: {exc}") from exc

        text = getattr(response, "text", None)
        if text:
            return text

        # В редких случаях text может отсутствовать, собираем части вручную
        candidates = getattr(response, "candidates", None) or []
        for candidate in candidates:
            content = getattr(candidate, "content", None)
            parts = getattr(content, "parts", None) or []
            joined = "".join(getattr(part, "text", "") for part in parts if getattr(part, "text", None))
            if joined:
                return joined
        raise GeminiClientError("В ответе Gemini отсутствует текстовая часть")

    return await asyncio.to_thread(_invoke)