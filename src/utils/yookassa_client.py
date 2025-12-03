from __future__ import annotations

"""
Утилиты для работы с API ЮKassa.

Используем только серверный REST API:
 - создание платежа (POST /v3/payments)
 - получение статуса платежа (GET /v3/payments/{id})
"""

import logging
import os
import uuid
from typing import Any, Dict

import httpx

logger = logging.getLogger(__name__)


YOOKASSA_API_BASE = "https://api.yookassa.ru/v3"

YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY")
# Куда вернётся пользователь после оплаты
YOOKASSA_RETURN_URL = os.getenv("YOOKASSA_RETURN_URL", "https://t.me/Milky_Tarot_Bot")
YOOKASSA_RECEIPT_PHONE = os.getenv("YOOKASSA_RECEIPT_PHONE", "79000000000")
YOOKASSA_TAX_SYSTEM_CODE = os.getenv("YOOKASSA_TAX_SYSTEM_CODE")
# Тип предмета расчёта и способ расчёта для чека (по умолчанию — услуга, полная предоплата)
YOOKASSA_PAYMENT_SUBJECT = os.getenv("YOOKASSA_PAYMENT_SUBJECT", "service")
YOOKASSA_PAYMENT_MODE = os.getenv("YOOKASSA_PAYMENT_MODE", "full_prepayment")


class YooKassaError(Exception):
    """Базовое исключение для ошибок при работе с API ЮKassa."""


def _get_auth() -> tuple[str, str]:
    """
    Вернуть пару (shop_id, secret_key) для HTTP Basic Auth.

    Поднимает YooKassaError, если переменные окружения не заданы.
    """
    if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
        raise YooKassaError(
            "YOOKASSA_SHOP_ID / YOOKASSA_SECRET_KEY не заданы в переменных окружения"
        )
    return YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY


async def create_payment(
    amount_rub: int,
    description: str,
    metadata: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """
    Создать платёж в ЮKassa и вернуть JSON-ответ.

    :param amount_rub: сумма в рублях (целое число)
    :param description: описание платежа (отображается в ЛК и у пользователя)
    :param metadata: произвольные метаданные, которые пригодятся при разборе платежа
    """
    shop_id, secret_key = _get_auth()

    payload: Dict[str, Any] = {
        "amount": {
            "value": f"{amount_rub:.2f}",
            "currency": "RUB",
        },
        "capture": True,
        "confirmation": {
            "type": "redirect",
            "return_url": YOOKASSA_RETURN_URL,
        },
        "description": description[:128],
        # Для прод-ключа ЮKassa может требовать обязательный чек.
        # Добавляем минимальный чек с одним товаром.
        "receipt": {
            "customer": {
                "phone": YOOKASSA_RECEIPT_PHONE,
            },
            "items": [
                {
                    "description": description[:128],
                    "quantity": "1.00",
                    "amount": {
                        "value": f"{amount_rub:.2f}",
                        "currency": "RUB",
                    },
                    # Базовый код НДС, при необходимости можно переопределить через настройки магазина
                    "vat_code": 1,
                    # Обязательные поля для предмета и способа расчёта
                    "payment_subject": YOOKASSA_PAYMENT_SUBJECT,
                    "payment_mode": YOOKASSA_PAYMENT_MODE,
                }
            ],
        },
    }
    if YOOKASSA_TAX_SYSTEM_CODE:
        payload["receipt"]["tax_system_code"] = int(YOOKASSA_TAX_SYSTEM_CODE)
    if metadata:
        payload["metadata"] = metadata

    headers = {
        # Любая уникальная строка, чтобы избежать дублей при повторах запроса
        "Idempotence-Key": str(uuid.uuid4()),
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=20) as client:
        try:
            response = await client.post(
                f"{YOOKASSA_API_BASE}/payments",
                json=payload,
                auth=(shop_id, secret_key),
                headers=headers,
            )
        except httpx.HTTPError as e:
            logger.exception("Ошибка сети при создании платежа в ЮKassa: %s", e)
            raise YooKassaError("Не удалось создать платёж в ЮKassa (сетевая ошибка)") from e

    if response.status_code >= 400:
        logger.error(
            "Ошибка ЮKassa при создании платежа: %s %s",
            response.status_code,
            response.text,
        )
        raise YooKassaError(
            f"ЮKassa вернула ошибку при создании платежа: {response.status_code}"
        )

    data = response.json()
    logger.info("Создан платёж в ЮKassa: %s", data.get("id"))
    return data


async def get_payment(payment_id: str) -> Dict[str, Any]:
    """
    Получить информацию о платеже по идентификатору ЮKassa.

    :param payment_id: значение поля id из ответа ЮKassa
    """
    shop_id, secret_key = _get_auth()

    async with httpx.AsyncClient(timeout=20) as client:
        try:
            response = await client.get(
                f"{YOOKASSA_API_BASE}/payments/{payment_id}",
                auth=(shop_id, secret_key),
            )
        except httpx.HTTPError as e:
            logger.exception("Ошибка сети при получении платежа в ЮKassa: %s", e)
            raise YooKassaError("Не удалось получить платёж в ЮKassa (сетевая ошибка)") from e

    if response.status_code >= 400:
        logger.error(
            "Ошибка ЮKassa при получении платежа %s: %s %s",
            payment_id,
            response.status_code,
            response.text,
        )
        raise YooKassaError(
            f"ЮKassa вернула ошибку при получении платежа: {response.status_code}"
        )

    return response.json()
