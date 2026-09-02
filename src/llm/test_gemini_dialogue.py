"""Изолированные тесты парсинга и сборки истории (без вызова LLM API)."""

from __future__ import annotations

import pytest

from llm.gemini_dialogue import (
    extract_json_objects,
    history_to_messages,
    infer_phase_update,
    parse_action_metadata,
    strip_action_json_from_text,
)


def test_extract_json_from_fence() -> None:
    text = 'Привет\n```json\n{"action": "complete", "memories": []}\n```'
    objs = extract_json_objects(text)
    assert any(o.get("action") == "complete" for o in objs)


def test_parse_action_metadata_propose() -> None:
    text = '{"action": "propose_spreads", "spreads": []}'
    meta = parse_action_metadata(text)
    assert meta is not None
    assert meta["action"] == "propose_spreads"


def test_strip_action_json() -> None:
    text = "Пока!\n```json\n{\"action\": \"complete\", \"memories\": []}\n```"
    clean = strip_action_json_from_text(text)
    assert "complete" not in clean or clean == "Пока!"


def test_infer_phase() -> None:
    assert infer_phase_update({"action": "propose_spreads"}, "x") == "proposing_spread"
    assert infer_phase_update({"action": "complete"}, "x") == "summary"
    assert infer_phase_update(None, "x") is None


def test_parse_suggest_questions() -> None:
    text = '{"action": "suggest_questions", "questions": ["А?", "Б?"]}'
    meta = parse_action_metadata(text)
    assert meta is not None
    assert meta["action"] == "suggest_questions"
    assert len(meta["questions"]) == 2


def test_build_system_prompt_reading_subject() -> None:
    from llm.gemini_dialogue import build_system_prompt

    p = build_system_prompt("", reading_subject="Кирюша и Катя")
    assert "Кирюша и Катя" in p
    assert "третьем лице" in p


def test_history_to_messages_roundtrip() -> None:
    hist = [
        {"role": "user", "text": "Привет"},
        {"role": "model", "text": "Мяу", "function_calls": [{"name": "draw_card", "args": {"position_name": "Совет"}}]},
        {"role": "tool", "name": "draw_card", "response": {"card": "Звезда"}},
    ]
    messages = history_to_messages(hist)
    assert len(messages) == 3
    assert messages[0]["role"] == "user"
    assert messages[1]["role"] == "assistant"
    assert messages[1]["tool_calls"][0]["function"]["name"] == "draw_card"
    assert messages[2]["role"] == "tool"
    assert messages[2]["tool_call_id"] == messages[1]["tool_calls"][0]["id"]
