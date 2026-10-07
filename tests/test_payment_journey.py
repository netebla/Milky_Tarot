"""Регрессии повторного пополнения без реальных Telegram/ЮKassa запросов.

Запуск: DATABASE_URL=sqlite:// PYTHONPATH=src python -m pytest tests/test_payment_journey.py
"""

import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError
from aiogram.methods import SendMessage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.base import StorageKey
import httpx
from aiogram.types import Chat, Message, MessageEntity, Update, User as TelegramUser
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bot import payment_handlers as payments
from bot import handlers as main_handlers
from bot import payment_navigation
from bot import payment_messages
from utils.db import Payment, User, PendingReading, PaymentNotification, ReadingAttempt
from utils import pending_readings
from utils import reading_charge


@pytest.fixture
def sessions(monkeypatch, tmp_path):
    engine = create_engine("sqlite://")
    User.__table__.create(engine)
    Payment.__table__.create(engine)
    PendingReading.__table__.create(engine)
    PaymentNotification.__table__.create(engine)
    ReadingAttempt.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(payments, "SessionLocal", factory)
    monkeypatch.setattr(pending_readings, "SessionLocal", factory)
    monkeypatch.setattr(main_handlers, "SessionLocal", factory)
    monkeypatch.setattr(reading_charge, "SessionLocal", factory)
    monkeypatch.setattr(payment_messages, "IMAGES_DIR", tmp_path)
    monkeypatch.setattr(payments, "get_tariffs", lambda: [
        SimpleNamespace(amount_rub=150, fish_amount=350),
    ])
    monkeypatch.setattr(payments, "tariff_to_amounts", lambda _: (350, 0))
    yield factory
    engine.dispose()


def callback(data, user_id=1):
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=user_id, username="test"),
        answer=AsyncMock(),
        message=SimpleNamespace(
            answer=AsyncMock(), answer_photo=AsyncMock(),
            edit_reply_markup=AsyncMock(), bot=SimpleNamespace(send_message=AsyncMock(), send_photo=AsyncMock()),
        ),
    )


def actions(call):
    return [b for row in call.kwargs["reply_markup"].inline_keyboard for b in row]


@pytest.mark.parametrize("entry,source,is_callback", [
    (main_handlers.msg_fish_topup, "topup_balance", False),
    (main_handlers.cb_fish_topup, "topup_balance", True),
    (main_handlers.cb_three_keys_buy_fish, "topup_three_keys", True),
])
def test_main_payment_entries_carry_start_parameter(entry, source, is_callback):
    cb = callback("unused")
    state = SimpleNamespace(clear=AsyncMock())
    cb.message.from_user = cb.from_user
    asyncio.run(entry(cb if is_callback else cb.message, state))
    buttons = actions(cb.message.answer.call_args)
    assert any(b.url == f"https://t.me/Milky_payment_bot?start={source}" for b in buttons)


def test_repeated_deep_links_and_topup_are_dispatched(sessions):
    async def scenario():
        session = AsyncMock()
        bot = Bot(token="123456:TEST", session=session)
        dp = Dispatcher()
        dp.include_router(payments.router)
        try:
            for idx, text in enumerate([
                "/start topup_balance", "/start topup_balance",
                "/start topup_three_keys", "/topup", "Тарифы",
            ], start=1):
                message = Message(
                    message_id=idx, date=datetime.now(timezone.utc),
                    chat=Chat(id=1, type="private"),
                    from_user=TelegramUser(id=1, is_bot=False, first_name="Test"),
                    text=text,
                    entities=[MessageEntity(type="bot_command", offset=0, length=len(text.split()[0]))]
                    if text.startswith("/") else [],
                )
                await dp.feed_update(bot, Update(update_id=idx, message=message))
            assert session.call_count == 5
            for call in session.call_args_list:
                method = call.args[1]
                assert any(b.callback_data == "pay_tariff:150"
                           for row in method.reply_markup.inline_keyboard for b in row)
        finally:
            await bot.session.close()
    asyncio.run(scenario())


def test_second_purchase_and_recheck_do_not_recredit(sessions, monkeypatch):
    create = AsyncMock(side_effect=[
        {"id": "first", "status": "pending", "confirmation": {"confirmation_url": "https://example.org/first"}},
        {"id": "second", "status": "pending", "confirmation": {"confirmation_url": "https://example.org/second"}},
    ])
    get = AsyncMock(side_effect=[
        {"id": provider_id, "status": "succeeded", "paid": True, "amount": {"value": "150.00", "currency": "RUB"}}
        for provider_id in ("first", "second")
    ])
    monkeypatch.setattr(payments, "create_payment", create)
    monkeypatch.setattr(payments, "get_payment", get)

    async def scenario():
        await payments.cb_pay_tariff(callback("pay_tariff:150"))
        await payments.cb_check_payment(callback("check_payment:1"))
        again = callback("show_tariffs")
        await payments.cb_show_tariffs(again)
        assert any(b.callback_data == "pay_tariff:150" for b in actions(again.message.answer.call_args))
        await payments.cb_pay_tariff(callback("pay_tariff:150"))
        await payments.cb_check_payment(callback("check_payment:2"))
        await payments.cb_check_payment(callback("check_payment:1"))
        await payments.cb_check_payment(callback("check_payment:2"))
    asyncio.run(scenario())
    with sessions() as db:
        assert db.get(User, 1).fish_balance == 700
        assert [p.yookassa_payment_id for p in db.query(Payment).order_by(Payment.id)] == ["first", "second"]
    assert create.await_count == 2
    assert get.await_count == 2


@pytest.mark.parametrize("status", ["succeeded", "canceled"])
def test_auto_result_has_return_and_new_purchase(sessions, monkeypatch, status):
    with sessions() as db:
        db.add(User(id=1, fish_balance=0))
        db.add(Payment(user_id=1, yookassa_payment_id="auto", amount_rub=150, fish_amount=350))
        db.commit()
    monkeypatch.setattr(payments, "get_payment", AsyncMock(return_value={"id": "auto", "status": status, "paid": status == "succeeded", "amount": {"value": "150.00", "currency": "RUB"}}))
    bot = SimpleNamespace(send_message=AsyncMock(), send_photo=AsyncMock())
    asyncio.run(payments._auto_check_payment(bot, 1, 1))
    buttons = actions(bot.send_message.call_args_list[0])
    assert any(b.callback_data == "show_tariffs" for b in buttons)
    assert any(b.text == "Вернуться в Милки" for b in buttons)


def test_foreign_payment_cannot_be_checked(sessions, monkeypatch):
    with sessions() as db:
        db.add(Payment(user_id=2, yookassa_payment_id="foreign", amount_rub=150, fish_amount=350))
        db.commit()
    get = AsyncMock()
    monkeypatch.setattr(payments, "get_payment", get)
    cb = callback("check_payment:1")
    asyncio.run(payments.cb_check_payment(cb))
    get.assert_not_awaited()
    assert "другому пользователю" in cb.answer.call_args.args[0]


@pytest.mark.parametrize("error,expected_url", [
    (None, "https://t.me/Milky_payment_bot"),
    (TelegramForbiddenError(method=SendMessage(chat_id=1, text="test"), message="Forbidden"),
     "https://t.me/Milky_payment_bot?start=topup_balance"),
    (TelegramNetworkError(method=SendMessage(chat_id=1, text="test"), message="Timeout"),
     "https://t.me/Milky_payment_bot?start=topup_balance"),
])
def test_payment_chat_delivers_tariffs_or_offers_start(monkeypatch, error, expected_url):
    monkeypatch.setenv("PAYMENT_BOT_TOKEN", "123456:TEST")
    bot = SimpleNamespace()
    manager = AsyncMock()
    manager.__aenter__.return_value = bot
    monkeypatch.setattr(payment_navigation, "Bot", lambda **_: manager)
    monkeypatch.setattr(payment_navigation, "create_aiogram_session", lambda: None)
    send = AsyncMock(side_effect=error)
    monkeypatch.setattr(payment_navigation, "send_tariffs", send)
    message = callback("unused").message
    asyncio.run(payment_navigation.open_payment_chat(message, 1, "topup_balance"))
    send.assert_awaited_once_with(bot, 1)
    assert actions(message.answer.call_args)[0].url == expected_url
    manager.__aexit__.assert_awaited_once()


def reading_state():
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=10, chat_id=1, user_id=1))


@pytest.fixture
def reading_setup(sessions, monkeypatch):
    with sessions() as db:
        db.add(User(id=1, fish_balance=20, three_keys_last_date=date.today(), three_keys_daily_count=1))
        db.commit()
    cards = [SimpleNamespace(title=f"Card {i}", image_url=lambda: "https://example.org/card") for i in range(3)]
    monkeypatch.setattr(main_handlers, "CARDS", cards)
    monkeypatch.setattr(main_handlers, "get_service_price", lambda *args: 69)
    monkeypatch.setattr(main_handlers, "_fetch_image_bytes", AsyncMock(side_effect=httpx.HTTPError("test")))
    interpretation = AsyncMock(return_value="Трактовка")
    monkeypatch.setattr(main_handlers, "generate_three_card_reading", interpretation)
    return sessions, cards, interpretation


def save_question(sessions):
    with sessions() as db:
        return pending_readings.save_pending_reading(db, 1, "Мой вопрос?", "Моя история", [f"Card {i}" for i in range(3)])


def test_insufficient_balance_saved_question_resumes_after_memory_reset(reading_setup):
    sessions, cards, interpretation = reading_setup

    async def scenario():
        state = reading_state()
        await state.update_data(three_cards=[c.title for c in cards], three_keys_context="Моя история")
        message = callback("unused").message
        message.from_user = SimpleNamespace(id=1, username="test")
        message.text, message.caption = "Мой вопрос?", None
        await main_handlers.handle_three_cards_question(message, state)
        assert await state.get_data() == {}
        reading = pending_readings.get_pending_reading(1)
        assert reading.question == "Мой вопрос?"
        assert reading.context == "Моя история"
        with sessions() as db:
            assert db.get(User, 1).fish_balance == 20
            assert db.get(User, 1).three_keys_daily_count == 1
            db.get(User, 1).fish_balance = 350
            db.commit()
        # Новый FSM имитирует перезапуск основного бота.
        fresh_state = reading_state()
        await main_handlers._offer_pending_reading(message, 1, reading.id)
        with sessions() as db:
            assert db.get(User, 1).fish_balance == 350
        cb = callback(f"confirm_reading:{reading.id}:69")
        await main_handlers.cb_confirm_reading(cb, fresh_state)
        await main_handlers.cb_confirm_reading(cb, fresh_state)
        with sessions() as db:
            assert db.get(User, 1).fish_balance == 281
            assert db.get(User, 1).three_keys_daily_count == 2
            assert db.get(PendingReading, reading.id).status == "consumed"
        interpretation.assert_awaited_once_with(cards, "Мой вопрос?", context="Моя история")
    asyncio.run(scenario())


@pytest.mark.parametrize("case", ["foreign", "expired", "insufficient", "changed_price"])
def test_saved_reading_checks_owner_expiry_balance_and_price(reading_setup, case):
    sessions, _, interpretation = reading_setup
    reading_id = save_question(sessions)
    if case == "expired":
        with sessions() as db:
            db.get(PendingReading, reading_id).expires_at = datetime.utcnow() - timedelta(seconds=1)
            db.commit()
    cb = callback(f"confirm_reading:{reading_id}:{70 if case == 'changed_price' else 69}", user_id=2 if case == "foreign" else 1)
    asyncio.run(main_handlers.cb_confirm_reading(cb, reading_state()))
    interpretation.assert_not_awaited()
    with sessions() as db:
        assert db.get(User, 1).fish_balance == 20
        assert db.get(User, 1).three_keys_daily_count == 1
        assert db.get(PendingReading, reading_id).status == "pending"
        assert db.query(PendingReading).count() == 1
    if case == "changed_price":
        assert "Стоимость расклада изменилась" in cb.message.answer.call_args_list[0].args[0]


def test_next_day_saved_question_can_be_free(reading_setup):
    sessions, _, interpretation = reading_setup
    reading_id = save_question(sessions)
    with sessions() as db:
        db.get(User, 1).three_keys_last_date = date.today() - timedelta(days=1)
        db.commit()
    cb = callback(f"confirm_reading:{reading_id}:0")
    asyncio.run(main_handlers.cb_confirm_reading(cb, reading_state()))
    with sessions() as db:
        assert db.get(User, 1).fish_balance == 20
        assert db.get(User, 1).three_keys_daily_count == 1
    interpretation.assert_awaited_once()


def test_success_offers_saved_question_return(reading_setup):
    sessions, _, _ = reading_setup
    reading_id = save_question(sessions)
    buttons = [b for row in payments._new_payment_kb(1).inline_keyboard for b in row]
    assert any(b.url == f"https://t.me/Milky_Tarot_Bot?start=resume_{reading_id}" for b in buttons)


def test_return_link_opens_confirmation_without_charge(reading_setup):
    sessions, _, interpretation = reading_setup
    reading_id = save_question(sessions)
    message = callback("unused").message
    message.text = f"/start resume_{reading_id}"
    message.from_user = SimpleNamespace(id=1)
    asyncio.run(main_handlers.cmd_start(message, reading_state()))
    buttons = actions(message.answer.call_args)
    assert any(b.callback_data == f"confirm_reading:{reading_id}:69" for b in buttons)
    interpretation.assert_not_awaited()
    with sessions() as db:
        assert db.get(User, 1).fish_balance == 20


def test_resumed_reading_llm_failure_returns_fallback(reading_setup):
    sessions, _, interpretation = reading_setup
    reading_id = save_question(sessions)
    with sessions() as db:
        db.get(User, 1).fish_balance = 350
        db.commit()
    interpretation.side_effect = RuntimeError("LLM unavailable")
    cb = callback(f"confirm_reading:{reading_id}:69")
    asyncio.run(main_handlers.cb_confirm_reading(cb, reading_state()))
    assert any("Рыбки остались у тебя" in call.args[0] for call in cb.message.answer.call_args_list)
    with sessions() as db:
        assert db.get(User, 1).fish_balance == 350
        assert db.get(User, 1).three_keys_daily_count == 1
        assert db.get(PendingReading, reading_id).status == "pending"


def test_sent_tariffs_show_current_balance_and_return(reading_setup):
    sessions, _, _ = reading_setup
    reading_id = save_question(sessions)
    bot = SimpleNamespace(send_message=AsyncMock())
    asyncio.run(payments.send_tariffs(bot, 1))
    assert "Сейчас у тебя 20 🐟" in bot.send_message.call_args.kwargs["text"]
    assert any(b.url.endswith(f"resume_{reading_id}") for b in actions(bot.send_message.call_args) if b.url)


def test_confirmed_payment_delivery_failure_does_not_lose_credit(sessions, monkeypatch):
    with sessions() as db:
        db.add(User(id=1, fish_balance=0))
        db.add(Payment(user_id=1, yookassa_payment_id="delivery-fails", amount_rub=150, fish_amount=350))
        db.commit()
    monkeypatch.setattr(payments, "get_payment", AsyncMock(return_value={
        "id": "delivery-fails", "status": "succeeded", "paid": True,
        "amount": {"value": "150.00", "currency": "RUB"},
    }))
    error = TelegramNetworkError(method=SendMessage(chat_id=1, text="test"), message="Timeout")
    cb = callback("check_payment:1")
    cb.message.bot.send_message.side_effect = error
    asyncio.run(payments.cb_check_payment(cb))
    asyncio.run(payments.cb_check_payment(callback("check_payment:1")))
    with sessions() as db:
        assert db.get(User, 1).fish_balance == 350
        assert db.get(Payment, 1).status == "succeeded"
        notifications = db.query(PaymentNotification).order_by(PaymentNotification.channel).all()
        assert len(notifications) == 2
        assert all(notification.sent_at is None for notification in notifications)
        assert next(notification for notification in notifications if notification.channel == "payment").attempts == 1
    assert "не удалось" not in str(cb.message.answer.call_args_list).lower()


@pytest.mark.parametrize("confirmation", [{}, {"confirmation_url": "https://example.org/already-paid"}])
def test_immediate_success_is_credited_and_has_no_stale_checkout(sessions, monkeypatch, confirmation):
    provider = {
        "id": "immediate", "status": "succeeded", "paid": True,
        "amount": {"value": "150.00", "currency": "RUB"}, "confirmation": confirmation,
    }
    monkeypatch.setattr(payments, "create_payment", AsyncMock(return_value=provider))
    monkeypatch.setattr(payments, "get_payment", AsyncMock(return_value=provider))
    cb = callback("pay_tariff:150")
    asyncio.run(payments.cb_pay_tariff(cb))
    cb.message.answer.assert_not_awaited()
    cb.message.bot.send_message.assert_awaited_once()
    assert all(button.callback_data != "check_payment:1" for button in actions(cb.message.bot.send_message.call_args))
    with sessions() as db:
        assert db.get(User, 1).fish_balance == 350
        assert db.get(Payment, 1).status == "succeeded"
        assert db.query(PaymentNotification).count() == 2
