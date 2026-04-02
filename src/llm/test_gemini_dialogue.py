"""Изолированные тесты парсинга и сборки истории (без вызова Gemini API)."""

from __future__ import annotations

import pytest

from llm.gemini_dialogue import (
    extract_json_objects,
    history_to_contents,
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


def test_history_to_contents_roundtrip() -> None:
    hist = [
        {"role": "user", "text": "Привет"},
        {"role": "model", "text": "Мяу", "function_calls": [{"name": "draw_card", "args": {"position_name": "Совет"}}]},
        {"role": "tool", "name": "draw_card", "response": {"card": "Звезда"}},
    ]
    contents = history_to_contents(hist)
    assert len(contents) == 3
    assert contents[0].role == "user"
    assert contents[1].role == "model"
    assert contents[2].role == "tool"
