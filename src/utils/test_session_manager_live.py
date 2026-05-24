"""Тесты эвристики субъекта расклада."""

from utils.session_manager import infer_reading_subject_from_text


def test_infer_third_party_names() -> None:
    subj = infer_reading_subject_from_text("Что ждёт Кирюшу и Катю на работе?")
    assert subj and "Кат" in subj
    assert infer_reading_subject_from_text("Расклад про Машу и Петю") is not None


def test_infer_self_question() -> None:
    assert infer_reading_subject_from_text("Что меня ждёт на работе") is None
