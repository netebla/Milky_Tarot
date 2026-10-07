"""Delivery-based charging, lease fencing and durable retries; no external IO."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from bot import handlers
from utils import reading_charge, pending_readings
from utils.db import User, PendingReading, ReadingAttempt


@pytest.fixture
def sessions(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'readings.db'}", connect_args={"check_same_thread": False})
    for model in (User, PendingReading, ReadingAttempt):
        model.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    for module in (reading_charge, pending_readings, handlers):
        monkeypatch.setattr(module, "SessionLocal", factory)
    with factory() as session:
        session.add(User(id=1, fish_balance=200, three_keys_last_date=date.today(), three_keys_daily_count=1))
        session.commit()
    yield factory
    engine.dispose()


def claim(**kwargs):
    return reading_charge.claim_reading(1, "test", "Вопрос", "История", ["A", "B", "C"], 69, **kwargs)


def test_success_charges_only_after_delivery_and_once(sessions):
    attempt = claim()
    assert reading_charge.cache_interpretation(attempt['id'], attempt['token'], 'Ответ')
    with sessions() as session:
        assert session.get(User, 1).fish_balance == 200
        assert session.get(User, 1).three_keys_daily_count == 1
    assert reading_charge.complete_reading(attempt['id'], attempt['token'])
    assert not reading_charge.complete_reading(attempt['id'], attempt['token'])
    with sessions() as session:
        user = session.get(User, 1)
        assert (user.fish_balance, user.three_keys_daily_count, user.draw_count) == (131, 2, 3)
        assert session.get(PendingReading, attempt['pending_id']).status == 'consumed'


def test_llm_failure_retains_question_and_balance(sessions):
    attempt = claim()
    reading_charge.release_reading(attempt['id'], attempt['token'])
    retried = claim(pending_id=attempt['pending_id'], confirmed_price=69)
    assert retried['id'] == attempt['id']
    assert retried['token'] != attempt['token']
    with sessions() as session:
        assert session.get(User, 1).fish_balance == 200
        assert session.get(User, 1).three_keys_daily_count == 1
        assert session.get(PendingReading, attempt['pending_id']).question == 'Вопрос'


def test_restart_preserves_cached_result_and_fences_old_worker(sessions):
    attempt = claim()
    reading_charge.cache_interpretation(attempt['id'], attempt['token'], 'Готовый ответ')
    with sessions() as session:
        session.get(ReadingAttempt, attempt['id']).lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
        session.commit()
    resumed = claim(pending_id=attempt['pending_id'], confirmed_price=69)
    assert resumed['interpretation'] == 'Готовый ответ'
    assert not reading_charge.complete_reading(attempt['id'], attempt['token'])
    assert not reading_charge.cache_interpretation(attempt['id'], attempt['token'], 'Stale')
    assert reading_charge.complete_reading(resumed['id'], resumed['token'])


def test_concurrent_confirmation_has_single_owner(sessions):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: claim(), range(2)))
    assert sorted(item['status'] for item in results) == ['busy', 'claimed']
    with sessions() as session:
        assert session.query(ReadingAttempt).count() == 1
        assert session.get(User, 1).fish_balance == 200


def test_other_product_spending_does_not_create_negative_balance(sessions):
    attempt = claim()
    reading_charge.cache_interpretation(attempt['id'], attempt['token'], 'Ответ')
    with sessions() as session:
        session.get(User, 1).fish_balance = 5
        session.commit()
    assert reading_charge.complete_reading(attempt['id'], attempt['token'])
    with sessions() as session:
        assert session.get(User, 1).fish_balance == 5


def test_new_question_must_finish_existing_attempt(sessions):
    attempt = claim()
    reading_charge.release_reading(attempt['id'], attempt['token'])
    other = reading_charge.claim_reading(1, 'test', 'Другой вопрос', '', ['A', 'B', 'C'], 69)
    assert other == {'status': 'unfinished', 'pending_id': attempt['pending_id']}


def test_empty_llm_response_is_not_chargeable(sessions):
    attempt = claim()
    with pytest.raises(ValueError):
        reading_charge.cache_interpretation(attempt['id'], attempt['token'], ' ')
    assert not reading_charge.complete_reading(attempt['id'], attempt['token'])


def test_price_change_requires_new_consent(sessions):
    with sessions() as session:
        session.get(User, 1).fish_balance = 0
        session.commit()
    insufficient = claim()
    with sessions() as session:
        session.get(User, 1).fish_balance = 200
        session.commit()
    result = reading_charge.claim_reading(1, 'test', 'Вопрос', '', ['A', 'B', 'C'], 80,
                                         insufficient['pending_id'], 69)
    assert result['status'] == 'price_changed'
    with sessions() as session:
        assert session.query(ReadingAttempt).count() == 0
        assert session.get(User, 1).fish_balance == 200


def test_free_daily_reading_is_consumed_after_success_only(sessions):
    with sessions() as session:
        session.get(User, 1).three_keys_last_date = date.today() - timedelta(days=1)
        session.commit()
    attempt = claim()
    assert attempt['price'] == 0
    reading_charge.cache_interpretation(attempt['id'], attempt['token'], 'Ответ')
    reading_charge.complete_reading(attempt['id'], attempt['token'])
    with sessions() as session:
        assert session.get(User, 1).fish_balance == 200
        assert session.get(User, 1).three_keys_daily_count == 1


def test_delivery_failure_retries_cached_response_without_llm(sessions, monkeypatch):
    cards = [SimpleNamespace(title=title, image_path=lambda: SimpleNamespace(exists=lambda: False),
                             image_url=lambda: 'unused') for title in ['A', 'B', 'C']]
    monkeypatch.setattr(handlers, 'CARDS', cards)
    monkeypatch.setattr(handlers, 'get_service_price', lambda *args: 69)
    fetch = AsyncMock(side_effect=handlers.httpx.HTTPError('offline'))
    monkeypatch.setattr(handlers, '_fetch_image_bytes', fetch)
    llm = AsyncMock(return_value='Готовый ответ')
    monkeypatch.setattr(handlers, 'generate_three_card_reading', llm)
    async def send(text, **kwargs):
        if 'Готовый ответ' in text:
            raise RuntimeError('Telegram offline')
    message = SimpleNamespace(text='Вопрос', from_user=SimpleNamespace(id=1, username='test'),
                              answer=AsyncMock(side_effect=send), answer_photo=AsyncMock())
    state = SimpleNamespace(get_data=AsyncMock(return_value={'three_cards': ['A', 'B', 'C']}), clear=AsyncMock())
    asyncio.run(handlers.handle_three_cards_question(message, state))
    with sessions() as session:
        assert session.get(User, 1).fish_balance == 200
        assert session.get(User, 1).three_keys_daily_count == 1
        attempt = session.query(ReadingAttempt).one()
        assert attempt.interpretation == 'Готовый ответ'
        pending_id = attempt.pending_reading_id
    message.answer = AsyncMock()
    asyncio.run(handlers.handle_three_cards_question(message, state, pending_reading_id=pending_id, confirmed_price=69))
    assert llm.await_count == 1
    with sessions() as session:
        assert session.get(User, 1).fish_balance == 131
        assert session.get(User, 1).three_keys_daily_count == 2


def test_expired_failed_attempt_does_not_block_new_question(sessions):
    attempt = claim()
    reading_charge.release_reading(attempt['id'], attempt['token'])
    with sessions() as session:
        session.get(PendingReading, attempt['pending_id']).expires_at = datetime.utcnow() - timedelta(seconds=1)
        session.commit()
    assert claim(pending_id=attempt['pending_id'], confirmed_price=69)['status'] == 'unavailable'
    fresh = reading_charge.claim_reading(1, 'test', 'Новый вопрос', '', ['A', 'B', 'C'], 69)
    assert fresh['status'] == 'claimed'
    with sessions() as session:
        assert session.get(ReadingAttempt, attempt['id']).status == 'expired'
        assert session.get(User, 1).fish_balance == 200


def test_midnight_delivery_uses_new_free_daily_entitlement(sessions):
    attempt = claim()
    assert attempt['price'] == 69
    reading_charge.cache_interpretation(attempt['id'], attempt['token'], 'Ответ')
    with sessions() as session:
        session.get(User, 1).three_keys_last_date = date.today() - timedelta(days=1)
        session.commit()
    reading_charge.complete_reading(attempt['id'], attempt['token'])
    with sessions() as session:
        assert session.get(User, 1).fish_balance == 200
        assert session.get(User, 1).three_keys_daily_count == 1


def test_cached_special_characters_sent_as_plain_text(sessions, monkeypatch):
    attempt = claim()
    reading_charge.cache_interpretation(attempt['id'], attempt['token'], 'A < B & C > D')
    reading_charge.release_reading(attempt['id'], attempt['token'])
    cards = [SimpleNamespace(title=title, image_path=lambda: SimpleNamespace(exists=lambda: False),
                             image_url=lambda: 'unused') for title in ['A', 'B', 'C']]
    monkeypatch.setattr(handlers, 'CARDS', cards)
    monkeypatch.setattr(handlers, '_fetch_image_bytes', AsyncMock(side_effect=handlers.httpx.HTTPError('offline')))
    monkeypatch.setattr(handlers, 'get_service_price', lambda *args: 69)
    llm = AsyncMock(side_effect=AssertionError('cached result must bypass LLM'))
    monkeypatch.setattr(handlers, 'generate_three_card_reading', llm)
    message = SimpleNamespace(text='Вопрос', from_user=SimpleNamespace(id=1, username='test'),
                              answer=AsyncMock(), answer_photo=AsyncMock())
    state = SimpleNamespace(get_data=AsyncMock(return_value={'three_cards': ['A', 'B', 'C']}), clear=AsyncMock())
    asyncio.run(handlers.handle_three_cards_question(message, state, pending_reading_id=attempt['pending_id'], confirmed_price=69))
    result_calls = [call for call in message.answer.call_args_list if 'A < B & C > D' in call.args[0]]
    assert len(result_calls) == 1
    assert result_calls[0].kwargs['parse_mode'] is None
    assert llm.await_count == 0


def test_llm_failure_and_unavailable_telegram_preserve_retry(sessions, monkeypatch):
    monkeypatch.setattr(handlers, 'CARDS', [SimpleNamespace(title=title) for title in ['A', 'B', 'C']])
    monkeypatch.setattr(handlers, 'get_service_price', lambda *args: 69)
    monkeypatch.setattr(handlers, 'generate_three_card_reading', AsyncMock(side_effect=RuntimeError('LLM offline')))
    async def send(text, **kwargs):
        if 'Карты пока' in text:
            raise RuntimeError('Telegram offline')
    message = SimpleNamespace(text='Вопрос', from_user=SimpleNamespace(id=1, username='test'), answer=AsyncMock(side_effect=send))
    state = SimpleNamespace(get_data=AsyncMock(return_value={'three_cards': ['A', 'B', 'C']}), clear=AsyncMock())
    asyncio.run(handlers.handle_three_cards_question(message, state))
    with sessions() as session:
        attempt = session.query(ReadingAttempt).one()
        assert attempt.lease_token is None
        assert session.get(User, 1).fish_balance == 200
        assert session.get(User, 1).three_keys_daily_count == 1
        assert session.get(PendingReading, attempt.pending_reading_id).question == 'Вопрос'
