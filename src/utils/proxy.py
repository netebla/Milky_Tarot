"""Единая настройка исходящего прокси для Telegram, OpenRouter, httpx и прочего HTTP."""

from __future__ import annotations

import logging
import os
from urllib.parse import urlsplit, urlunsplit

from aiogram.client.session.aiohttp import AiohttpSession

logger = logging.getLogger(__name__)

_PROXY_ENV_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")


def normalize_proxy_url(raw: str | None) -> str | None:
    """HTTP(S)_PROXY требуют схему; user:pass@host:port без http:// ломает httpx/aiohttp."""
    url = (raw or "").strip()
    if not url:
        return None
    if "://" not in url:
        url = f"http://{url}"
    return url


def get_proxy_url() -> str | None:
    """Вернуть URL прокси, если PROXY_ENABLED=true и PROXY_URL задан."""
    url = normalize_proxy_url(os.getenv("PROXY_URL"))
    enabled = os.getenv("PROXY_ENABLED", "false").lower() == "true"
    if enabled and url:
        return url
    return None


def mask_proxy_url(proxy_url: str | None) -> str:
    """Скрывает учётные данные прокси в логах."""
    if not proxy_url:
        return "<empty>"
    try:
        parts = urlsplit(proxy_url)
        host = parts.hostname or ""
        port = f":{parts.port}" if parts.port else ""
        auth = "***:***@" if parts.username or parts.password else ""
        netloc = f"{auth}{host}{port}"
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    except Exception:
        return "<invalid-proxy-url>"


def configure_process_proxy() -> str | None:
    """
    Выставить HTTP_PROXY/HTTPS_PROXY/ALL_PROXY для процесса.

    httpx и aiohttp (aiogram) подхватят их автоматически.
    NO_PROXY оставляет внутренние сервисы и api.yookassa.ru без прокси.
    """
    proxy_url = get_proxy_url()
    if proxy_url:
        for var in _PROXY_ENV_VARS:
            os.environ[var] = proxy_url
        logger.info("Исходящий прокси включён: %s", mask_proxy_url(proxy_url))
    else:
        logger.info("Исходящий прокси выключен (PROXY_ENABLED=false или PROXY_URL пуст)")
    return proxy_url


def create_aiogram_session() -> AiohttpSession:
    """Сессия aiogram с явным proxy (надёжнее, чем только env для long polling)."""
    proxy_url = get_proxy_url()
    if proxy_url:
        return AiohttpSession(proxy=proxy_url)
    return AiohttpSession()
