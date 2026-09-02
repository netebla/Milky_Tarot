"""Клиент LLM через OpenRouter."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import httpx

from utils.proxy import configure_process_proxy, get_proxy_url

OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
# DeepSeek V4 Flash: сильная модель для текста и reasoning с низкой стоимостью.
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL") or "deepseek/deepseek-v4-flash"
OPENROUTER_TIMEOUT_SECONDS = float(os.getenv("OPENROUTER_TIMEOUT_SECONDS") or "90")

PROXY_URL = get_proxy_url()
PROXY_ENABLED = bool(PROXY_URL)

logger = logging.getLogger(__name__)


class OpenRouterClientError(RuntimeError):
    """Ошибки взаимодействия с OpenRouter."""


def _get_api_key() -> str:
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise OpenRouterClientError(
            "OPENROUTER_API_KEY не задан. Передайте ключ через переменные окружения (секрет CI/CD)."
        )
    return api_key


def _invoke_chat_completion(payload: dict[str, Any]) -> dict[str, Any]:
    """Синхронный HTTP-вызов, который запускается в отдельном потоке."""
    configure_process_proxy()
    proxy_info = " (через прокси)" if PROXY_ENABLED else ""
    try:
        with httpx.Client(timeout=OPENROUTER_TIMEOUT_SECONDS, trust_env=True) as client:
            response = client.post(
                OPENROUTER_API_URL,
                headers={
                    "Authorization": f"Bearer {_get_api_key()}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:500]
        logger.exception("OpenRouter returned HTTP %s%s", exc.response.status_code, proxy_info)
        raise OpenRouterClientError(
            f"Ошибка OpenRouter HTTP {exc.response.status_code}{proxy_info}: {detail}"
        ) from exc
    except httpx.HTTPError as exc:
        logger.exception("OpenRouter request failed%s", proxy_info)
        raise OpenRouterClientError(f"Ошибка обращения к OpenRouter{proxy_info}: {exc}") from exc

    try:
        data = response.json()
    except ValueError as exc:
        logger.exception("OpenRouter returned invalid JSON")
        raise OpenRouterClientError("OpenRouter вернул некорректный ответ") from exc

    if not isinstance(data, dict):
        raise OpenRouterClientError("OpenRouter вернул ответ неожиданного формата")
    return data


async def chat_completion(
    messages: list[dict[str, Any]],
    *,
    system_prompt: str | None = None,
    tools: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Выполнить совместимый с OpenAI Chat Completions запрос к OpenRouter."""
    request_messages: list[dict[str, Any]] = []
    if system_prompt:
        request_messages.append({"role": "system", "content": system_prompt})
    request_messages.extend(messages)

    payload: dict[str, Any] = {
        "model": OPENROUTER_MODEL,
        "messages": request_messages,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    logger.info(
        "Sending prompt to OpenRouter (model=%s, messages=%d, tools=%s)",
        OPENROUTER_MODEL,
        len(request_messages),
        bool(tools),
    )
    response = await asyncio.to_thread(_invoke_chat_completion, payload)
    logger.info("OpenRouter response received (model=%s)", OPENROUTER_MODEL)
    return response


async def ask_llm(prompt: str) -> str:
    """Отправить запрос в OpenRouter и вернуть текстовый ответ."""
    response = await chat_completion([{"role": "user", "content": prompt}])
    choices = response.get("choices") or []
    message = choices[0].get("message") if choices and isinstance(choices[0], dict) else None
    text = message.get("content") if isinstance(message, dict) else None
    if isinstance(text, str) and text.strip():
        return text.strip()

    logger.error("OpenRouter response contained no text content")
    raise OpenRouterClientError("В ответе OpenRouter отсутствует текстовая часть")
