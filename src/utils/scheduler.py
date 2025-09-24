"""
Утилиты планировщика на базе APScheduler для ежедневных уведомлений по пользователям.

- Использует BackgroundScheduler с часовым поясом Europe/Moscow
- Экспортирует функции для планирования/перепланирования/отмены задач по user_id
- Формат id задания: push-<user_id>
"""
from __future__ import annotations

import logging
from typing import Callable

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)


class PushScheduler:
    """Управляет ежедневными заданиями для каждого пользователя.

    Колбэк вызывается с именованным аргументом user_id=<int>.
    """

    def __init__(self, timezone: str = "Europe/Moscow") -> None:
        self.timezone = pytz.timezone(timezone)
        self.scheduler = BackgroundScheduler(timezone=self.timezone)
        self.started = False

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

    def schedule_daily(self, user_id: int, time_str: str, callback: Callable[..., None]) -> None:
        """Запланировать (или перепланировать) ежедневное задание на HH:MM для user_id."""
        hour, minute = map(int, time_str.split(":"))
        job_id = self._job_id(user_id)
        self.remove(user_id)
        trigger = CronTrigger(hour=hour, minute=minute)
        self.scheduler.add_job(callback, trigger, id=job_id, kwargs={"user_id": user_id}, replace_existing=True)
        logger.info("Запланирован ежедневный пуш для пользователя %s на %02d:%02d", user_id, hour, minute)

    def remove(self, user_id: int) -> None:
        job_id = self._job_id(user_id)
        job = self.scheduler.get_job(job_id)
        if job:
            self.scheduler.remove_job(job_id)
            logger.info("Удалено расписание пуша для пользователя %s", user_id)

    def has_job(self, user_id: int) -> bool:
        return self.scheduler.get_job(self._job_id(user_id)) is not None 