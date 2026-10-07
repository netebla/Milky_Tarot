"""Уведомление о начислении ведёт к вопросу и остаётся последним сообщением."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import SendPhoto

from bot import payment_messages as messages


@pytest.fixture
def result():
    return SimpleNamespace(status="succeeded", user_id=7, fish_amount=350, balance=370)


def buttons(call):
    return [button for row in call.kwargs["reply_markup"].inline_keyboard for button in row]


def test_main_notification_continues_question_directly(monkeypatch, result):
    monkeypatch.setattr(messages, "get_pending_reading", lambda _: SimpleNamespace(id="saved"))
    bot = SimpleNamespace(send_message=AsyncMock())
    asyncio.run(messages.send_success_notification(bot, result, "main"))
    assert buttons(bot.send_message.call_args)[0].callback_data == "resume_reading:saved"
    assert "350 🐟" in bot.send_message.call_args.kwargs["text"]
    assert "370 🐟" in bot.send_message.call_args.kwargs["text"]
    assert "вопрос сохранён" in bot.send_message.call_args.kwargs["text"]
    bot.send_message.assert_awaited_once()


def test_main_notification_without_question(monkeypatch, result):
    monkeypatch.setattr(messages, "get_pending_reading", lambda _: None)
    bot = SimpleNamespace(send_message=AsyncMock())
    asyncio.run(messages.send_success_notification(bot, result, "main"))
    assert buttons(bot.send_message.call_args)[0].callback_data == "daily_card_ask_question"
    assert "вопрос сохранён" not in bot.send_message.call_args.kwargs["text"]


def test_payment_photo_combines_thanks_and_actions(monkeypatch, tmp_path, result):
    (tmp_path / "fed_milky.jpg").write_bytes(b"test image")
    monkeypatch.setattr(messages, "IMAGES_DIR", tmp_path)
    monkeypatch.setattr(messages, "get_pending_reading", lambda _: SimpleNamespace(id="saved"))
    bot = SimpleNamespace(send_message=AsyncMock(), send_photo=AsyncMock())
    asyncio.run(messages.send_success_notification(bot, result, "payment"))
    bot.send_photo.assert_awaited_once()
    bot.send_message.assert_not_awaited()
    assert buttons(bot.send_photo.call_args)[0].url.endswith("start=resume_saved")
    assert buttons(bot.send_photo.call_args)[1].callback_data == "show_tariffs"
    assert "350 🐟" in bot.send_photo.call_args.kwargs["caption"]
    assert "370 🐟" in bot.send_photo.call_args.kwargs["caption"]


def test_rejected_photo_keeps_actions_on_fallback(monkeypatch, tmp_path, result):
    (tmp_path / "fed_milky.jpg").write_bytes(b"test image")
    monkeypatch.setattr(messages, "IMAGES_DIR", tmp_path)
    monkeypatch.setattr(messages, "get_pending_reading", lambda _: None)
    error = TelegramBadRequest(method=SendPhoto(chat_id=7, photo="file"), message="invalid photo")
    bot = SimpleNamespace(send_message=AsyncMock(), send_photo=AsyncMock(side_effect=error))
    asyncio.run(messages.send_success_notification(bot, result, "payment"))
    assert buttons(bot.send_message.call_args)[0].text == "Вернуться в Милки"
    assert buttons(bot.send_message.call_args)[1].callback_data == "show_tariffs"


def test_transport_failure_propagates_for_retry(monkeypatch, tmp_path, result):
    (tmp_path / "fed_milky.jpg").write_bytes(b"test image")
    monkeypatch.setattr(messages, "IMAGES_DIR", tmp_path)
    monkeypatch.setattr(messages, "get_pending_reading", lambda _: None)
    error = TelegramNetworkError(method=SendPhoto(chat_id=7, photo="file"), message="timeout")
    bot = SimpleNamespace(send_message=AsyncMock(), send_photo=AsyncMock(side_effect=error))
    with pytest.raises(TelegramNetworkError):
        asyncio.run(messages.send_success_notification(bot, result, "payment"))
    bot.send_message.assert_not_awaited()


def test_redirect_without_confirmation_cannot_announce_success(result):
    result.status = "pending"
    bot = SimpleNamespace(send_message=AsyncMock(), send_photo=AsyncMock())
    with pytest.raises(ValueError):
        asyncio.run(messages.send_success_notification(bot, result, "main"))
    bot.send_message.assert_not_awaited()
