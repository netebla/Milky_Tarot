"""Утилиты планировщика на базе APScheduler для ежедневных уведомлений."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

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

    @staticmethod
    def convert_user_time_to_moscow(time_str: str, tz_offset_hours: int) -> str:
        """
        Конвертировать локальное время пользователя (HH:MM) в московское время (HH:MM),
        учитывая смещение пользователя относительно МСК в часах.

        Пример: если пользователь в МСК+3 и хочет 10:00 локально, нужно запланировать в 07:00 МСК.
        То есть msk_time = user_time - offset.
        """
        try:
            user_hour, minute = map(int, time_str.split(":"))
        except ValueError:
            return time_str
        msk_hour = (user_hour - (tz_offset_hours or 0)) % 24
        return f"{msk_hour:02d}:{minute:02d}"

    def schedule_daily_with_offset(
        self,
        user_id: int,
        user_time_str: str,
        tz_offset_hours: int,
        callback: Callable[..., Any],
    ) -> None:
        """
        Запланировать ежедневный пуш, переводя пользовательское время в московское по смещению.
        """
        msk_time = self.convert_user_time_to_moscow(user_time_str, tz_offset_hours)
        self.schedule_daily(user_id, msk_time, callback)

    def schedule_every_n_days(self, user_id: int, time_str: str, n_days: int, callback: Callable[..., Any]) -> None:
        """
        Запланировать задачу с интервалом в n_days: каждый n-й день в заданное время HH:MM.

        Реализация использует IntervalTrigger с началом в ближайшее будущее в указанное время.
        """
        if n_days <= 0:
            logger.warning("Неверный интервал n_days=%s для пользователя %s", n_days, user_id)
            return

        try:
            hour, minute = map(int, time_str.split(":"))
        except ValueError as exc:
            logger.warning("Некорректное время '%s' для пользователя %s: %s", time_str, user_id, exc)
            return

        # Удаляем старую задачу, если есть
        job_id = self._job_id(user_id)
        self.remove(user_id)

        # Вычисляем ближайшую дату/время старта для запуска в нужный час:мин
        from datetime import datetime, time, timedelta

        now = datetime.now(self.timezone)
        target_time = time(hour=hour, minute=minute, tzinfo=self.timezone)
        today_target = datetime.combine(now.date(), target_time)
        if today_target <= now:
            # если время уже прошло сегодня — старт на завтра
            today_target = today_target + timedelta(days=1)

        trigger = IntervalTrigger(days=n_days, start_date=today_target, timezone=self.timezone)
        wrapped = self._wrap_callback(callback)
        self.scheduler.add_job(
            wrapped,
            trigger,
            id=job_id,
            kwargs={"user_id": user_id},
            replace_existing=True,
        )
        logger.info("Запланирован пуш для пользователя %s каждые %s дня(й) в %02d:%02d", user_id, n_days, hour, minute)
    def remove(self, user_id: int) -> None:
        job_id = self._job_id(user_id)
        job = self.scheduler.get_job(job_id)
        if job:
            self.scheduler.remove_job(job_id)
            logger.info("Удалено расписание пуша для пользователя %s", user_id)

    def has_job(self, user_id: int) -> bool:
        return self.scheduler.get_job(self._job_id(user_id)) is not None
