"""Утилиты планировщика на базе APScheduler для ежедневных уведомлений."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)

DEFAULT_PUSH_TIME = "10:00"


class PushScheduler:
    """Управляет ежедневными заданиями для каждого пользователя."""

    def __init__(self, timezone: str = "Europe/Moscow") -> None:
        self.timezone = pytz.timezone(timezone)
        self.scheduler = BackgroundScheduler(timezone=self.timezone)
        self.started = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def configure(self, loop: asyncio.AbstractEventLoop) -> None:
        """Привязать планировщик к основному asyncio-циклу."""
        self._loop = loop

    def start(self) -> None:
        if not self.started:
            self.scheduler.start()
            self.started = True

    def shutdown(self) -> None:
        if self.started:
            self.scheduler.shutdown(wait=False)
            self.started = False

    def _job_id(self, user_id: int) -> str:
        return f"push-{user_id}"

    def _wrap_callback(self, callback: Callable[..., Any]) -> Callable[..., None]:
        def runner(*args: Any, **kwargs: Any) -> None:
            try:
                result = callback(*args, **kwargs)
                if asyncio.iscoroutine(result):
                    if not self._loop:
                        raise RuntimeError("Event loop is not configured for PushScheduler")
                    asyncio.run_coroutine_threadsafe(result, self._loop)
            except Exception:
                logger.exception("Ошибка при выполнении задания пуша")

        return runner

    def schedule_daily(self, user_id: int, time_str: str, callback: Callable[..., Any]) -> None:
        """Запланировать (или перепланировать) ежедневное задание на HH:MM для user_id."""
        try:
            hour, minute = map(int, time_str.split(":"))
        except ValueError as exc:
            logger.warning("Некорректное время '%s' для пользователя %s: %s", time_str, user_id, exc)
            return

        job_id = self._job_id(user_id)
        self.remove(user_id)
        trigger = CronTrigger(hour=hour, minute=minute)
        wrapped = self._wrap_callback(callback)
        self.scheduler.add_job(wrapped, trigger, id=job_id, kwargs={"user_id": user_id}, replace_existing=True)
        logger.info("Запланирован ежедневный пуш для пользователя %s на %02d:%02d", user_id, hour, minute)

    def remove(self, user_id: int) -> None:
        job_id = self._job_id(user_id)
        job = self.scheduler.get_job(job_id)
        if job:
            self.scheduler.remove_job(job_id)
            logger.info("Удалено расписание пуша для пользователя %s", user_id)

    def has_job(self, user_id: int) -> bool:
        return self.scheduler.get_job(self._job_id(user_id)) is not None
