from __future__ import annotations

from typing import Optional
from aiogram import Bot
from .scheduler import PushScheduler

_bot: Optional[Bot] = None
_scheduler: Optional[PushScheduler] = None


def set_bot(bot: Bot) -> None:
    global _bot
    _bot = bot


def get_bot() -> Bot:
    assert _bot is not None, "Бот не инициализирован"
    return _bot


def set_scheduler(scheduler: PushScheduler) -> None:
    global _scheduler
    _scheduler = scheduler


def get_scheduler() -> PushScheduler:
    assert _scheduler is not None, "Планировщик не инициализирован"
    return _scheduler 