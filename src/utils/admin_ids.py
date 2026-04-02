"""Разбор ADMIN_ID из окружения (Docker, секрет CI). Несколько админов — id через запятую в одной переменной."""

from __future__ import annotations

import os


def get_admin_ids() -> frozenset[str]:
    raw = (os.getenv("ADMIN_ID") or "").strip()
    raw = raw.replace("\r", "").replace("\n", ",").replace(";", ",")
    return frozenset(p.strip() for p in raw.split(",") if p.strip())


def is_admin(user_id: int | None) -> bool:
    if user_id is None:
        return False
    return str(user_id) in get_admin_ids()
