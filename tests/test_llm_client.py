"""Проверки обработки остановленного по лимиту ответа OpenRouter."""

import asyncio
from unittest.mock import AsyncMock

from llm import client


def test_ask_llm_continues_response_stopped_by_token_limit(monkeypatch):
    first_response = {
        "id": "first-response",
        "model": "test-model",
        "choices": [
            {
                "finish_reason": "length",
                "message": {"content": "Не торопись с выбором, но и"},
            }
        ],
    }
    second_response = {
        "id": "second-response",
        "model": "test-model",
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"content": "не откладывай важный разговор."},
            }
        ],
    }
    completion = AsyncMock(side_effect=[first_response, second_response])
    monkeypatch.setattr(client, "chat_completion", completion)

    result = asyncio.run(client.ask_llm("Сделай трактовку"))

    assert result == "Не торопись с выбором, но и\nне откладывай важный разговор."
    assert completion.await_count == 2
    continuation_messages = completion.await_args_list[1].args[0]
    assert continuation_messages[-2] == {
        "role": "assistant",
        "content": "Не торопись с выбором, но и",
    }
    assert "Продолжи его с того места" in continuation_messages[-1]["content"]


def test_ask_llm_does_not_request_continuation_for_complete_response(monkeypatch):
    completion = AsyncMock(
        return_value={
            "choices": [
                {"finish_reason": "stop", "message": {"content": "Готовый ответ."}}
            ]
        }
    )
    monkeypatch.setattr(client, "chat_completion", completion)

    result = asyncio.run(client.ask_llm("Сделай трактовку"))

    assert result == "Готовый ответ."
    completion.assert_awaited_once()
