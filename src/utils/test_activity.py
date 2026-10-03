"""Проверки уникальности дневной активности и входящих действий."""

import asyncio
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, Mock

import pytest

from aiogram.types import CallbackQuery, Chat, Message, User
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bot.activity_middleware import UserActivityMiddleware
from utils import activity
from utils.db import UserActivity


def test_unique_users_and_new_day(monkeypatch):
    engine = create_engine("sqlite://")
    UserActivity.__table__.create(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(activity, "SessionLocal", sessions)
    monkeypatch.setattr(activity, "moscow_today", lambda: date(2026, 10, 3))
    activity.record_user_activity(1)
    activity.record_user_activity(1)
    activity.record_user_activity(2)
    with sessions() as db:
        assert activity.count_active_today(db) == 2
    monkeypatch.setattr(activity, "moscow_today", lambda: date(2026, 10, 4))
    with sessions() as db:
        assert activity.count_active_today(db) == 0
    activity.record_user_activity(1)
    with sessions() as db:
        assert activity.count_active_today(db) == 1
        assert db.query(UserActivity).count() == 3


def test_moscow_day_after_utc_midnight_boundary(monkeypatch):
    clock = Mock()
    clock.now.side_effect = lambda tz: datetime(2026, 10, 3, 22, tzinfo=timezone.utc).astimezone(tz)
    monkeypatch.setattr(activity, "datetime", clock)
    assert activity.moscow_today() == date(2026, 10, 4)


def _message(chat_type="private", is_bot=False):
    return Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=1, type=chat_type),
        from_user=User(id=1, is_bot=is_bot, first_name="Test"),
        text="/start",
    )


def test_messages_and_callbacks_are_actions(monkeypatch):
    record = Mock()
    monkeypatch.setattr("bot.activity_middleware.record_user_activity", record)
    message = _message()
    callback = CallbackQuery(
        id="test", from_user=message.from_user, chat_instance="test",
        message=message, data="push_draw_card",
    )
    handler = AsyncMock(return_value="handled")
    middleware = UserActivityMiddleware()
    assert asyncio.run(middleware(handler, message, {})) == "handled"
    assert asyncio.run(middleware(handler, callback, {})) == "handled"
    assert record.call_count == 2
    assert handler.await_count == 2


def test_group_messages_and_bots_are_not_user_activity(monkeypatch):
    record = Mock()
    monkeypatch.setattr("bot.activity_middleware.record_user_activity", record)
    middleware = UserActivityMiddleware()
    handler = AsyncMock()
    asyncio.run(middleware(handler, _message(chat_type="group"), {}))
    asyncio.run(middleware(handler, _message(is_bot=True), {}))
    record.assert_not_called()
    assert handler.await_count == 2


def test_analytics_failure_does_not_break_handler(monkeypatch):
    monkeypatch.setattr(
        "bot.activity_middleware.record_user_activity", Mock(side_effect=RuntimeError("DB offline"))
    )
    handler = AsyncMock(return_value="handled")
    assert asyncio.run(UserActivityMiddleware()(handler, _message(), {})) == "handled"


@pytest.mark.parametrize("delivery_error", [None, RuntimeError("blocked")])
def test_outgoing_push_does_not_update_activity(monkeypatch, delivery_error):
    from utils import push

    user = Mock(push_enabled=True, last_activity_date=date(2026, 10, 2))
    db = Mock()
    db.query.return_value.filter.return_value.first.return_value = user
    monkeypatch.setattr(push, "SessionLocal", lambda: db)
    bot = Mock(send_message=AsyncMock(side_effect=delivery_error))
    asyncio.run(push.send_push_card(bot, 1))
    assert user.last_activity_date == date(2026, 10, 2)
    db.commit.assert_not_called()
    bot.send_message.assert_awaited_once()
