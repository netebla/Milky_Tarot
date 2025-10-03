"""Клиент для взаимодействия с Google Gemini через пакет google-genai."""

from __future__ import annotations

import asyncio
import os
from typing import Optional

from google import genai
import httpx

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


def _create_http_client_with_proxy():
    """Создает HTTP клиент с поддержкой SOCKS5 прокси."""
    if not PROXY_ENABLED:
        return None
    
    # ПРАВИЛЬНЫЙ СИНТАКСИС ДЛЯ HTTXP
    transport = httpx.HTTPTransport(proxy=PROXY_URL)
    
    return httpx.Client(
        transport=transport,
        timeout=30.0,
        follow_redirects=True
    )


async def _get_client() -> genai.Client:
    global _client
    if _client is not None:
        return _client

    async with _client_lock:
        if _client is None:
            client_options = {}
            
            if PROXY_ENABLED:
                http_client = _create_http_client_with_proxy()
                if http_client:
                    client_options["http_client"] = http_client
                    print(f"✅ Прокси настроен: {PROXY_URL}")
            
            _client = genai.Client(
                api_key=_get_api_key(),
                **client_options
            )
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
        except Exception as exc:
            proxy_info = " (через прокси)" if PROXY_ENABLED else ""
            raise GeminiClientError(f"Ошибка обращения к Gemini{proxy_info}: {exc}") from exc

        text = getattr(response, "text", None)
        if text:
            return text

        candidates = getattr(response, "candidates", None) or []
        for candidate in candidates:
            content = getattr(candidate, "content", None)
            parts = getattr(content, "parts", None) or []
            joined = "".join(getattr(part, "text", "") for part in parts if getattr(part, "text", None))
            if joined:
                return joined
        raise GeminiClientError("В ответе Gemini отсутствует текстовая часть")

    return await asyncio.to_thread(_invoke)