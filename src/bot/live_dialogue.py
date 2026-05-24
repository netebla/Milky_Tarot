"""Расклад «Живой диалог» — тест: только админы, многоходовый чат с Gemini и draw_card."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import time
from typing import Any

import httpx
from aiogram import BaseMiddleware, F, Router
from aiogram.enums import ChatAction
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, TelegramObject

from llm.client import GeminiClientError
from llm.gemini_dialogue import (
    assistant_payload_from_response,
    build_system_prompt,
    call_gemini,
    format_model_reply_for_telegram_html,
    infer_phase_update,
    strip_action_json_from_text,
)
from llm.rag import RAG_CARD_MEANINGS
from utils.card_drawer import draw_random_card
from utils.cards_loader import Card, load_cards
from utils.admin_ids import is_admin as _is_admin
from utils.db import DialogueSession, DrawnCard, SessionLocal, User
from utils import session_manager as sm

logger = logging.getLogger(__name__)

router = Router(name="live_dialogue")

LIVE_BUTTON_TEXT = "Живой диалог 🌙"
# Достаточно для расклада ~9–10 карт (по одному draw_card на раунд) + финальный ответ.
MAX_TOOL_ROUNDS = 22
# Защита от бесконечной цепочки «один расклад → автопродолжение».
_AUTO_SPREAD_CHAIN_MAX = 4
IMAGE_FETCH_TIMEOUT_SEC = 4

# Не трактовать нажатия главного меню как реплики диалога (обработают другие роутеры после выхода).
_MAIN_MENU_TEXTS = frozenset(
    {
        "Вытянуть карту дня",
        "Узнать совет карт",
        "Задать свой вопрос",
        LIVE_BUTTON_TEXT,
        "Мои рыбки",
        "Мои настройки",
        "Помощь",
        "Пополнить баланс 🐟",
        "Энергия года",
    }
)

try:
    CARDS: list[Card] = load_cards()
except Exception as exc:
    logger.error("live_dialogue: не удалось загрузить карты: %s", exc)
    CARDS = []


class LiveDialogueStates(StatesGroup):
    choosing_session = State()
    in_dialogue = State()


_INTRO_TEXT = (
    "Мяу, это режим «Живой диалог» — поговорим по-настоящему, без заранее заданного расклада. "
    "Расскажи, что у тебя на душе, или как хочешь назвать тему.\n\n"
    "Чтобы выйти без завершения: /cancel_dialogue"
)


def _rag_hint(card_title: str) -> str | None:
    t = card_title.replace("\ufeff", "").strip()
    return RAG_CARD_MEANINGS.get(t)


async def _fetch_image_bytes(url: str, client: httpx.AsyncClient) -> bytes:
    response = await client.get(url)
    response.raise_for_status()
    return response.content


def _card_by_title(title: str) -> Card | None:
    t = title.replace("\ufeff", "").strip()
    for c in CARDS:
        if c.title.replace("\ufeff", "").strip() == t:
            return c
    return None


def _get_existing_drawn_for_position(db, session_id: int, position_name: str) -> dict[str, Any] | None:
    pos = (position_name or "").strip()
    if not pos:
        return None
    row = (
        db.query(DrawnCard)
        .filter(
            DrawnCard.session_id == session_id,
            DrawnCard.position_name == pos,
        )
        .order_by(DrawnCard.id.desc())
        .first()
    )
    if not row:
        return None
    return {
        "card_name": row.card_name,
        "is_reversed": bool(row.is_reversed),
        "position_name": row.position_name,
    }


async def _send_drawn_cards_live(message: Message, drawn: list[dict[str, Any]]) -> None:
    """Отправить изображения только что вытянутых в этом ходе карт (как в раскладе «три карты»)."""
    if not drawn:
        return
    t0 = time.perf_counter()
    sent_local = 0
    sent_remote = 0
    sent_text = 0
    async with httpx.AsyncClient(timeout=IMAGE_FETCH_TIMEOUT_SEC) as client:
        for item in drawn:
            title = (item.get("card_name") or "").replace("\ufeff", "").strip()
            pos = (item.get("position_name") or "").strip()
            rev = bool(item.get("is_reversed"))
            card = _card_by_title(title)
            rev_note = "\n(перевёрнутая)" if rev else ""
            if pos:
                caption = f"{html.escape(pos)}: {html.escape(title)}{rev_note}"
            else:
                caption = f"{html.escape(title)}{rev_note}"

            if not card:
                await message.answer(caption)
                sent_text += 1
                continue
            sent = False
            path = card.image_path()
            if path.exists():
                try:
                    await message.answer_photo(
                        photo=BufferedInputFile(path.read_bytes(), filename=path.name),
                        caption=caption,
                    )
                    sent = True
                    sent_local += 1
                except TelegramBadRequest:
                    sent = False
            if not sent:
                fetch_t0 = time.perf_counter()
                try:
                    image_bytes = await _fetch_image_bytes(card.image_url(), client)
                    await message.answer_photo(
                        photo=BufferedInputFile(image_bytes, filename=f"{card.title}.jpg"),
                        caption=caption,
                    )
                    sent = True
                    sent_remote += 1
                    logger.info(
                        "live_dialogue image fetched remote card=%s in %.0fms",
                        title,
                        (time.perf_counter() - fetch_t0) * 1000,
                    )
                except (httpx.HTTPError, TelegramBadRequest, TelegramNetworkError):
                    sent = False
            if not sent:
                await message.answer(caption)
                sent_text += 1
    logger.info(
        "live_dialogue send_cards done count=%s local=%s remote=%s text=%s elapsed_ms=%.0f",
        len(drawn),
        sent_local,
        sent_remote,
        sent_text,
        (time.perf_counter() - t0) * 1000,
    )


async def _send_drawn_cards_summary(message: Message, drawn: list[dict[str, Any]]) -> None:
    """Явно показать пользователю, какие карты выпали в этом ходе."""
    if not drawn:
        return
    if len(drawn) == 1:
        card = drawn[0]
        title = html.escape(str(card.get("card_name") or "").strip() or "Неизвестная карта")
        pos = html.escape(str(card.get("position_name") or "").strip())
        if pos:
            text = f"Выпавшая карта: <b>{title}</b>\nПозиция: {pos}"
        else:
            text = f"Выпавшая карта: <b>{title}</b>"
        await message.answer(text)
        return

    lines = ["Выпавшие карты:"]
    for idx, card in enumerate(drawn, start=1):
        title = html.escape(str(card.get("card_name") or "").strip() or "Неизвестная карта")
        pos = html.escape(str(card.get("position_name") or "").strip())
        if pos:
            lines.append(f"{idx}. <b>{title}</b> — {pos}")
        else:
            lines.append(f"{idx}. <b>{title}</b>")
    await message.answer("\n".join(lines))


def _system_prompt_for_session(
    user_id: int, db, session: DialogueSession | None = None
) -> str:
    mem = sm.load_user_memory(db, user_id)
    subject = getattr(session, "reading_subject", None) if session else None
    return build_system_prompt(mem, reading_subject=subject)


def _format_spreads_block(spreads: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for idx, sp in enumerate(spreads, start=1):
        name = html.escape(str(sp.get("name") or f"Вариант {idx}").strip())
        lines.append(f"<b>{idx}. {name}</b>")
        positions = sp.get("positions") or {}
        if isinstance(positions, dict):
            for key in sorted(positions.keys(), key=lambda x: str(x)):
                pos = str(positions.get(key) or "").strip()
                if pos:
                    lines.append(f"  • {html.escape(pos)}")
        why = sp.get("why")
        if why:
            lines.append(f"  <i>{html.escape(str(why).strip())}</i>")
    return "\n".join(lines)


def _positions_from_spread_dict(positions: dict[str, Any]) -> list[str]:
    names: list[str] = []
    if not isinstance(positions, dict):
        return names
    for key in sorted(positions.keys(), key=lambda x: str(x)):
        pos = str(positions.get(key) or "").strip()
        if pos:
            names.append(pos)
    return names


def _batch_draw_positions(
    db,
    session: DialogueSession,
    session_id: int,
    spread_name: str,
    positions: list[str],
) -> list[dict[str, Any]]:
    spread_positions = {str(i): name for i, name in enumerate(positions, start=1)}
    sm.set_session_spread(db, session, spread_name, spread_positions)
    drawn_batch: list[dict[str, Any]] = []
    for pos in positions:
        existing = _get_existing_drawn_for_position(db, session_id, pos)
        if existing:
            hint = _rag_hint(existing["card_name"])
            sm.save_message(
                db,
                session_id,
                "tool",
                "",
                tool_name="draw_card",
                tool_result={
                    "card_name": existing["card_name"],
                    "is_reversed": bool(existing["is_reversed"]),
                    "position_name": existing["position_name"],
                    "meaning_hint": hint,
                    "already_opened": True,
                },
            )
            continue
        if not CARDS:
            continue
        title, _rev = draw_random_card(CARDS)
        rev = False
        sm.save_drawn_card(db, session_id, pos, title, rev)
        hint = _rag_hint(title)
        sm.save_message(
            db,
            session_id,
            "tool",
            "",
            tool_name="draw_card",
            tool_result={
                "card_name": title,
                "is_reversed": rev,
                "position_name": pos,
                "meaning_hint": hint,
            },
        )
        drawn_batch.append({"card_name": title, "is_reversed": rev, "position_name": pos})
    return drawn_batch


def _followup_questions_keyboard(questions: list[str]) -> InlineKeyboardMarkup:
    rows = []
    for idx, q in enumerate(questions[:3]):
        label = (q or "").strip()[:60] or f"Вопрос {idx + 1}"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"ldq:{idx}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _continue_or_new_keyboard(session_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Продолжить прошлый диалог",
                    callback_data=f"lds:continue:{session_id}",
                )
            ],
            [InlineKeyboardButton(text="Начать заново", callback_data="lds:new")],
        ]
    )


async def _typing_while(coro, message: Message):
    """Показать «печатает…» на время долгой операции."""
    chat_id = message.chat.id

    async def _tick() -> None:
        try:
            while True:
                await message.bot.send_chat_action(chat_id, ChatAction.TYPING)
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            return

    tick = asyncio.create_task(_tick())
    try:
        return await coro
    finally:
        tick.cancel()
        try:
            await tick
        except asyncio.CancelledError:
            pass


async def _request_interpretation_after_batch(
    message: Message,
    state: FSMContext,
    user_id: int,
    session_id: int,
    db,
    session: DialogueSession,
) -> None:
    system_prompt = _system_prompt_for_session(user_id, db, session)
    system_prompt += (
        f"\n\nТекущая фаза сессии в базе: {session.phase}. "
        "Все карты расклада уже открыты (результаты draw_card в истории). "
        "Не вызывай draw_card повторно. Дай связную интерпретацию по всем позициям."
    )
    display_text, meta, drawn = await _gemini_multi_round(db, session_id, system_prompt)
    await _handle_model_result(message, state, user_id, session_id, display_text, meta, drawn)


def abandon_live_sessions_for_user(user_id: int) -> None:
    """Закрыть незавершённые живые диалоги пользователя (БД)."""
    with SessionLocal() as db:
        sm.abandon_active_session_for_user(db, user_id)


class LiveDialogueMenuExitMiddleware(BaseMiddleware):
    """При уходе в главное меню — закрыть сессию в БД и сбросить FSM."""

    async def __call__(self, handler, event: TelegramObject, data: dict[str, Any]):
        state: FSMContext | None = data.get("state")
        if state and isinstance(event, Message) and event.text in _MAIN_MENU_TEXTS:
            current = await state.get_state()
            if current in (
                LiveDialogueStates.in_dialogue.state,
                LiveDialogueStates.choosing_session.state,
            ):
                uid = event.from_user.id if event.from_user else 0
                if uid:
                    abandon_live_sessions_for_user(uid)
                await state.clear()
        return await handler(event, data)


async def _gemini_multi_round(
    db, session_id: int, system_prompt: str
) -> tuple[str, dict[str, Any] | None, list[dict[str, Any]]]:
    """Повторные вызовы Gemini, пока есть draw_card; история перечитывается из БД."""
    last_meta: dict[str, Any] | None = None
    display_parts: list[str] = []
    drawn_this_turn: list[dict[str, Any]] = []

    round_idx = 0
    t0 = time.perf_counter()
    for _ in range(MAX_TOOL_ROUNDS):
        round_idx += 1
        history = sm.load_history(session_id, db)
        call_t0 = time.perf_counter()
        try:
            result = await call_gemini(history, system_prompt)
        except GeminiClientError:
            raise
        logger.info(
            "live_dialogue gemini round=%s session_id=%s history=%s elapsed_ms=%.0f",
            round_idx,
            session_id,
            len(history),
            (time.perf_counter() - call_t0) * 1000,
        )

        raw = result["raw_response"]
        text = result["text"] or ""
        calls = result["tool_calls"]
        meta = result["metadata"]
        if meta:
            last_meta = meta

        ap = assistant_payload_from_response(raw, text, calls)
        sm.save_message(
            db,
            session_id,
            "assistant",
            ap["content"],
            model_function_calls=ap["model_function_calls"],
        )

        if not calls:
            if text.strip():
                display_parts.append(strip_action_json_from_text(text))
            break

        logger.info(
            "live_dialogue tool_calls round=%s session_id=%s count=%s",
            round_idx,
            session_id,
            len(calls),
        )
        for c in calls:
            if c.get("name") != "draw_card":
                continue
            pos = (c.get("args") or {}).get("position_name") or "Позиция"
            existing = _get_existing_drawn_for_position(db, session_id, pos)
            if existing:
                title = existing["card_name"]
                rev = bool(existing["is_reversed"])
                hint = _rag_hint(title)
                tool_payload = {
                    "card_name": title,
                    "is_reversed": rev,
                    "position_name": existing["position_name"],
                    "meaning_hint": hint,
                    "already_opened": True,
                }
                sm.save_message(
                    db,
                    session_id,
                    "tool",
                    "",
                    tool_name="draw_card",
                    tool_result=tool_payload,
                )
                continue
            if not CARDS:
                tool_payload = {"error": "Колода недоступна", "position_name": pos}
            else:
                title, _rev = draw_random_card(CARDS)
                rev = False
                sm.save_drawn_card(db, session_id, pos, title, rev)
                drawn_this_turn.append(
                    {"card_name": title, "is_reversed": rev, "position_name": pos}
                )
                hint = _rag_hint(title)
                tool_payload = {
                    "card_name": title,
                    "is_reversed": rev,
                    "position_name": pos,
                    "meaning_hint": hint,
                }
            sm.save_message(
                db,
                session_id,
                "tool",
                "",
                tool_name="draw_card",
                tool_result=tool_payload,
            )

    combined = "\n\n".join(p for p in display_parts if p.strip())
    logger.info(
        "live_dialogue multi_round done session_id=%s rounds=%s drawn=%s elapsed_ms=%.0f",
        session_id,
        round_idx,
        len(drawn_this_turn),
        (time.perf_counter() - t0) * 1000,
    )
    return combined, last_meta, drawn_this_turn


def _spreads_keyboard(session_id: int, spreads: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    rows = []
    for idx, sp in enumerate(spreads):
        name = (sp.get("name") or f"Вариант {idx + 1}")[:40]
        rows.append([InlineKeyboardButton(text=name, callback_data=f"ldp:{session_id}:{idx}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _apply_phase_metadata(db, session: DialogueSession, meta: dict[str, Any] | None) -> None:
    if not meta:
        return
    action = meta.get("action")
    new_phase = infer_phase_update(meta, session.phase)
    if new_phase:
        sm.update_session_phase(db, session, new_phase)
    if action == "propose_spreads":
        spreads = meta.get("spreads") or []
        if isinstance(spreads, list) and spreads:
            sm.set_pending_spreads(db, session, spreads)


def _spread_completion_stats(db, session: DialogueSession) -> tuple[int, int, list[str]]:
    """
    Вернуть (ожидаемо_позиций, уже_открыто_позиций, недостающие_позиции).

    Если spread_positions не задан или пустой, expected будет 0 (нечего валидировать).
    """
    positions = session.spread_positions or {}
    if not isinstance(positions, dict) or not positions:
        return 0, 0, []

    expected_names: list[str] = []
    for key in sorted(positions.keys(), key=lambda x: str(x)):
        value = positions.get(key)
        pos_name = str(value or "").strip()
        if not pos_name:
            continue
        expected_names.append(pos_name)
    if not expected_names:
        return 0, 0, []

    opened_rows = (
        db.query(DrawnCard.position_name)
        .filter(DrawnCard.session_id == session.id)
        .distinct()
        .all()
    )
    opened = {str((row[0] if row else "") or "").strip() for row in opened_rows}
    opened.discard("")

    missing = [p for p in expected_names if p not in opened]
    return len(expected_names), len(expected_names) - len(missing), missing


def _strip_action_artifacts_for_user(text: str) -> str:
    """
    Финальная серверная очистка текста перед отправкой пользователю.
    Удаляет артефакты action-json, даже если модель вернула их в нестандартном виде.
    """
    if not text:
        return ""

    cleaned = text
    # Удаляем fenced JSON-блоки c action.
    cleaned = re.sub(
        r"```(?:json)?\s*\{\s*\"action\"\s*:\s*\"(?:propose_spreads|draw_cards|complete|suggest_questions)\"[\s\S]*?\}\s*```",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    # Удаляем однострочные action JSON.
    cleaned = re.sub(
        r"\{\s*\"action\"\s*:\s*\"(?:propose_spreads|draw_cards|complete|suggest_questions)\"[^\n]*\}",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    # Удаляем строки, похожие на технический action-декоратор.
    cleaned_lines: list[str] = []
    for line in cleaned.splitlines():
        ln = line.strip()
        if ln.startswith("{") and "\"action\"" in ln:
            continue
        cleaned_lines.append(line)
    cleaned = "\n".join(cleaned_lines)
    # Нормализуем лишние пустые строки.
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


def _extract_batch_request(meta: dict[str, Any] | None) -> tuple[int | None, list[str], str]:
    """Разобрать action=draw_cards: (count, positions, spread_name)."""
    if not meta or meta.get("action") != "draw_cards":
        return None, [], ""
    raw_count = meta.get("count")
    try:
        count = int(raw_count)
    except (TypeError, ValueError):
        return None, [], ""
    if count < 1 or count > 15:
        return None, [], ""

    raw_positions = meta.get("positions")
    positions: list[str] = []
    if isinstance(raw_positions, list):
        for p in raw_positions:
            pos = str(p or "").strip()
            if pos:
                positions.append(pos)
    if not positions:
        positions = [f"Позиция {i}" for i in range(1, count + 1)]
    if len(positions) != count:
        return None, [], ""

    spread_name = str(meta.get("spread_name") or "").strip()
    return count, positions, spread_name


async def _handle_model_result(
    message: Message,
    state: FSMContext,
    user_id: int,
    session_id: int,
    display_text: str,
    meta: dict[str, Any] | None,
    drawn_this_turn: list[dict[str, Any]] | None = None,
    *,
    auto_spread_depth: int = 0,
) -> None:
    from bot.keyboards import main_menu_kb

    drawn_this_turn = drawn_this_turn or []

    with SessionLocal() as db:
        session = db.get(DialogueSession, session_id)
        if not session or session.user_id != user_id:
            await message.answer("Сессия недействительна.")
            await state.clear()
            return
        if session.completed_at is not None:
            await state.clear()
            return

        db.refresh(session)
        _apply_phase_metadata(db, session, meta)

        action = (meta or {}).get("action")
        db.refresh(session)
        pending_after = session.pending_spreads or []
        # Не показывать карты вместе с экраном выбора расклада (частая ошибка модели).
        is_propose_ui = action == "propose_spreads" and len(pending_after) > 0
        skip_cards = is_propose_ui or action == "suggest_questions"
        if drawn_this_turn and not skip_cards:
            await _send_drawn_cards_live(message, drawn_this_turn)

        if action == "propose_spreads":
            db.refresh(session)
            spreads = session.pending_spreads or []
            if not spreads:
                spreads = (meta or {}).get("spreads") or []
            if spreads:
                if len(spreads) == 1:
                    sp = spreads[0]
                    name = (sp.get("name") or "Расклад").strip()
                    pos_list = _positions_from_spread_dict(sp.get("positions") or {})
                    if not pos_list:
                        await message.answer(
                            "Не смогла разобрать позиции расклада. Опиши, пожалуйста, сколько карт нужно."
                        )
                        return
                    raw_intro = _strip_action_artifacts_for_user(display_text or "")
                    block = _format_spreads_block([sp])
                    body_parts = []
                    if raw_intro:
                        body_parts.append(format_model_reply_for_telegram_html(raw_intro))
                    body_parts.append(block)
                    await message.answer("\n\n".join(body_parts))
                    await message.answer("Открываю карты…")
                    drawn_batch = _batch_draw_positions(db, session, session_id, name, pos_list)
                    await _send_drawn_cards_live(message, drawn_batch)
                    try:
                        await _request_interpretation_after_batch(
                            message, state, user_id, session_id, db, session
                        )
                    except GeminiClientError:
                        logger.exception("Gemini error after single-spread batch")
                        await message.answer("Не удалось связаться с Милки. Попробуй чуть позже.")
                    return

                raw_intro = _strip_action_artifacts_for_user(display_text or "")
                raw_intro = raw_intro or "Вот варианты раскладов:"
                body = format_model_reply_for_telegram_html(raw_intro)
                body += "\n\n" + _format_spreads_block(spreads)
                body += "\n\nВыбери вариант кнопкой ниже."
                await message.answer(
                    body,
                    reply_markup=_spreads_keyboard(session.id, spreads),
                )
                return

        if action == "draw_cards":
            count, positions, spread_name = _extract_batch_request(meta)
            if count is None:
                await message.answer(
                    "Не смогла понять пакетный запрос карт. Уточни: сколько карт нужно вытянуть."
                )
                return
            if not CARDS:
                await message.answer("Колода недоступна.")
                return

            spread_title = spread_name or f"Расклад на {count} карт"
            await message.answer("Открываю карты…")
            drawn_batch = _batch_draw_positions(db, session, session_id, spread_title, positions)
            await _send_drawn_cards_live(message, drawn_batch)
            clean_text = _strip_action_artifacts_for_user(display_text or "")
            if not drawn_batch and positions:
                await message.answer("Я уже открыла эти позиции и продолжаю трактовку.")
            if clean_text and len(clean_text) >= 80:
                await message.answer(format_model_reply_for_telegram_html(clean_text))
                return
            try:
                await _request_interpretation_after_batch(
                    message, state, user_id, session_id, db, session
                )
            except GeminiClientError:
                logger.exception("Gemini error after draw_cards batch")
                await message.answer("Не удалось связаться с Милки. Попробуй чуть позже.")
            return

        if action == "suggest_questions":
            raw = (meta or {}).get("questions") or []
            questions = [str(q).strip() for q in raw if str(q).strip()][:3]
            clean_body = _strip_action_artifacts_for_user(display_text or "")
            if clean_body:
                await message.answer(format_model_reply_for_telegram_html(clean_body))
            if questions:
                await state.update_data(live_followup_questions=questions)
                await message.answer(
                    "Можешь ответить своими словами или нажать подсказку:",
                    reply_markup=_followup_questions_keyboard(questions),
                )
            return

        if action == "complete":
            expected_cnt, opened_cnt, missing_positions = _spread_completion_stats(db, session)
            if expected_cnt > 0 and opened_cnt < expected_cnt:
                if auto_spread_depth >= _AUTO_SPREAD_CHAIN_MAX:
                    await message.answer(
                        "Пока не могу корректно дотянуть оставшиеся позиции. Напиши «дотяни оставшиеся карты»."
                    )
                    return
                missing_json = json.dumps(missing_positions, ensure_ascii=False)
                await message.answer(
                    "Секунду — расклад ещё не полностью открыт. Сейчас дотяну оставшиеся позиции."
                )
                system_prompt = _system_prompt_for_session(user_id, db, session)
                system_prompt += (
                    f"\n\nТекущая фаза сессии в базе: {session.phase}. "
                    "Ты попыталась завершить сессию раньше времени. "
                    f"В этом раскладе ожидается {expected_cnt} позиций, открыто {opened_cnt}. "
                    f"Недостающие позиции: {missing_json}. "
                    "Сейчас не завершай сессию. "
                    "Сначала вызови draw_card для каждой недостающей позиции (ровно по одному разу), "
                    "потом дай краткую цельную интерпретацию всех позиций вместе. "
                    "После этого только при необходимости верни action=complete."
                )
                try:
                    display_text2, meta2, drawn2 = await _gemini_multi_round(db, session_id, system_prompt)
                except GeminiClientError:
                    logger.exception("Gemini error when forcing remaining spread positions")
                    await message.answer("Не удалось связаться с Милки. Попробуй чуть позже.")
                    return

                await _handle_model_result(
                    message,
                    state,
                    user_id,
                    session_id,
                    display_text2,
                    meta2,
                    drawn2,
                    auto_spread_depth=auto_spread_depth + 1,
                )
                return

            memories = (meta or {}).get("memories") or []
            if not isinstance(memories, list):
                memories = []
            db.refresh(session)
            ok, err = sm.try_complete_session(db, user_id, session, memories)
            if ok:
                await state.clear()
                goodbye_src = _strip_action_artifacts_for_user(display_text or "")
                goodbye = format_model_reply_for_telegram_html(
                    goodbye_src.strip() or "До встречи, солнце."
                )
                await message.answer(
                    goodbye + "\n\nСессия завершена.",
                    reply_markup=main_menu_kb(_is_admin(user_id)),
                )
            else:
                err_html = html.escape(err or "Не удалось завершить сессию.")
                main_src = _strip_action_artifacts_for_user(display_text or "")
                main_part = format_model_reply_for_telegram_html(main_src)
                await message.answer(
                    (main_part + "\n\n" if main_part else "") + err_html
                )
            return

        clean_body = _strip_action_artifacts_for_user(display_text or "")
        body = format_model_reply_for_telegram_html(clean_body.strip() or "…")
        await message.answer(body)


async def _process_turn(message: Message, state: FSMContext, user_text: str) -> None:
    user = message.from_user
    if not user:
        return
    user_id = user.id

    data = await state.get_data()
    session_id = data.get("live_session_id")
    if not session_id:
        await message.answer("Сессия потеряна. Нажми /live_dialogue снова.")
        await state.clear()
        return

    turn_t0 = time.perf_counter()
    logger.info("live_dialogue turn start user_id=%s text_len=%s", user_id, len(user_text))

    async with sm.session_turn_lock(session_id):
        with SessionLocal() as db:
            session = db.get(DialogueSession, session_id)
            if not session or session.user_id != user_id or session.completed_at is not None:
                await message.answer("Сессия недействительна. Нажми /live_dialogue.")
                await state.clear()
                return

            msg_count = sm.count_user_messages(db, session_id)
            if msg_count >= sm.MAX_USER_MESSAGES_PER_SESSION:
                await message.answer(
                    "В этом диалоге уже максимум сообщений. Заверши мысль или начни новую сессию "
                    "(/cancel_dialogue, затем /live_dialogue)."
                )
                return

            sm.save_message(db, session_id, "user", user_text)
            sm.update_reading_subject_from_user_text(db, session, user_text)
            db.refresh(session)

            system_prompt = _system_prompt_for_session(user_id, db, session)
            system_prompt += f"\n\nТекущая фаза сессии в базе: {session.phase}. Следуй логике этой фазы."

            async def _run_gemini():
                return await _gemini_multi_round(db, session_id, system_prompt)

            try:
                gemini_t0 = time.perf_counter()
                display_text, meta, drawn = await _typing_while(_run_gemini(), message)
                logger.info(
                    "live_dialogue turn gemini_done user_id=%s session_id=%s elapsed_ms=%.0f action=%s drawn=%s",
                    user_id,
                    session_id,
                    (time.perf_counter() - gemini_t0) * 1000,
                    (meta or {}).get("action"),
                    len(drawn),
                )
            except GeminiClientError:
                logger.exception("Gemini error in live_dialogue")
                await message.answer("Не удалось связаться с Милки. Попробуй чуть позже.")
                return

            handle_t0 = time.perf_counter()
            await _handle_model_result(message, state, user_id, session_id, display_text, meta, drawn)
            logger.info(
                "live_dialogue turn done user_id=%s session_id=%s handle_ms=%.0f total_ms=%.0f",
                user_id,
                session_id,
                (time.perf_counter() - handle_t0) * 1000,
                (time.perf_counter() - turn_t0) * 1000,
            )


def _ensure_user_row(db, user_id: int, username: str | None) -> None:
    row = db.query(User).filter(User.id == user_id).first()
    if not row:
        db.add(User(id=user_id, username=username))
        db.commit()


@router.message(Command("live_dialogue"))
async def cmd_live_dialogue(message: Message, state: FSMContext) -> None:
    if not message.from_user or not _is_admin(message.from_user.id):
        await message.answer("Команда доступна только администраторам.")
        return
    if not CARDS:
        await message.answer("Колода недоступна.")
        return

    await state.clear()
    uid = message.from_user.id
    uname = message.from_user.username

    with SessionLocal() as db:
        _ensure_user_row(db, uid, uname)
        active = sm.get_active_session(db, uid)
        if active and sm.session_has_user_messages(db, active.id):
            await state.set_state(LiveDialogueStates.choosing_session)
            await state.update_data(live_pending_session_id=active.id)
            await message.answer(
                "У тебя есть незавершённый диалог. Продолжить его или начать с чистого листа?",
                reply_markup=_continue_or_new_keyboard(active.id),
            )
            return
        if active:
            sm.abandon_session_no_charge(db, active)
        session = sm.create_fresh_session(db, uid)
        sid = session.id

    await state.set_state(LiveDialogueStates.in_dialogue)
    await state.update_data(live_session_id=sid, live_followup_questions=None)
    await message.answer(_INTRO_TEXT)


async def _enter_live_dialogue(
    message: Message, state: FSMContext, session_id: int, *, resumed: bool = False
) -> None:
    await state.set_state(LiveDialogueStates.in_dialogue)
    await state.update_data(live_session_id=session_id, live_followup_questions=None)
    if resumed:
        await message.answer(
            _INTRO_TEXT + "\n\n<i>Продолжаем прошлый разговор — пиши дальше.</i>"
        )
    else:
        await message.answer(_INTRO_TEXT)


@router.message(F.text == LIVE_BUTTON_TEXT)
async def btn_live_dialogue(message: Message, state: FSMContext) -> None:
    if not message.from_user or not _is_admin(message.from_user.id):
        return
    await cmd_live_dialogue(message, state)


@router.callback_query(F.data.startswith("lds:"))
async def cb_live_session_choice(cb: CallbackQuery, state: FSMContext) -> None:
    if not cb.from_user or not _is_admin(cb.from_user.id):
        await cb.answer()
        return
    if not cb.message:
        await cb.answer()
        return
    uid = cb.from_user.id
    data = cb.data or ""

    if data == "lds:new":
        await cb.answer()
        with SessionLocal() as db:
            sm.abandon_active_session_for_user(db, uid)
            session = sm.create_fresh_session(db, uid)
            sid = session.id
        await _enter_live_dialogue(cb.message, state, sid, resumed=False)
        return

    m = re.match(r"^lds:continue:(\d+)$", data)
    if not m:
        await cb.answer()
        return
    session_id = int(m.group(1))
    with SessionLocal() as db:
        session = db.get(DialogueSession, session_id)
        if not session or session.user_id != uid or session.completed_at is not None:
            await cb.answer("Сессия недоступна.", show_alert=True)
            return
    await cb.answer()
    await _enter_live_dialogue(cb.message, state, session_id, resumed=True)


@router.message(
    Command("cancel_dialogue"),
    StateFilter(LiveDialogueStates.in_dialogue, LiveDialogueStates.choosing_session),
)
async def cmd_cancel_dialogue(message: Message, state: FSMContext) -> None:
    from bot.keyboards import main_menu_kb

    uid = message.from_user.id if message.from_user else 0
    if uid:
        abandon_live_sessions_for_user(uid)
    await state.clear()
    await message.answer("Диалог отменён.", reply_markup=main_menu_kb(_is_admin(uid)))


@router.message(
    StateFilter(LiveDialogueStates.in_dialogue),
    F.text,
    ~F.text.startswith("/"),
    ~F.text.in_(_MAIN_MENU_TEXTS),
)
async def msg_live_dialogue_text(message: Message, state: FSMContext) -> None:
    if not message.from_user or not _is_admin(message.from_user.id):
        await state.clear()
        return
    text = (message.text or "").strip()
    if not text:
        return
    await _process_turn(message, state, text)


@router.callback_query(
    StateFilter(LiveDialogueStates.in_dialogue),
    F.data.startswith("ldp:"),
)
async def cb_live_pick_spread(cb: CallbackQuery, state: FSMContext) -> None:
    if not cb.from_user or not _is_admin(cb.from_user.id):
        await cb.answer()
        return
    data = await state.get_data()
    session_id_fsm = data.get("live_session_id")
    m = re.match(r"^ldp:(\d+):(\d+)$", cb.data or "")
    if not m or not cb.message:
        await cb.answer()
        return
    session_id = int(m.group(1))
    idx = int(m.group(2))
    if session_id != session_id_fsm:
        await cb.answer("Это меню устарело.", show_alert=True)
        return

    uid = cb.from_user.id

    with SessionLocal() as db:
        session = db.get(DialogueSession, session_id)
        if not session or session.user_id != uid or session.completed_at is not None:
            await cb.answer("Сессия недействительна.", show_alert=True)
            return
        spreads = session.pending_spreads or []
        if idx < 0 or idx >= len(spreads):
            await cb.answer("Нет такого варианта.", show_alert=True)
            return
        sp = spreads[idx]
        name = (sp.get("name") or "Расклад").strip()
        positions = sp.get("positions") or {}
        if not isinstance(positions, dict):
            positions = {}
        pos_list = _positions_from_spread_dict(positions)
        choice = f"Я выбираю расклад «{name}». Позиции: {json.dumps(positions, ensure_ascii=False)}"
        sm.save_message(db, session_id, "user", choice)

        if not pos_list:
            await cb.answer("В этом варианте нет позиций.", show_alert=True)
            return

        await cb.answer()
        await cb.message.answer(f"Открываю расклад «{html.escape(name)}»…")
        drawn_batch = _batch_draw_positions(db, session, session_id, name, pos_list)

    async with sm.session_turn_lock(session_id):
        await _send_drawn_cards_live(cb.message, drawn_batch)
        try:
            with SessionLocal() as db:
                session = db.get(DialogueSession, session_id)
                if session:
                    await _typing_while(
                        _request_interpretation_after_batch(
                            cb.message, state, uid, session_id, db, session
                        ),
                        cb.message,
                    )
        except GeminiClientError:
            logger.exception("Gemini error in live_dialogue callback")
            await cb.message.answer("Не удалось связаться с Милки. Попробуй чуть позже.")


@router.callback_query(
    StateFilter(LiveDialogueStates.in_dialogue),
    F.data.startswith("ldq:"),
)
async def cb_live_followup_question(cb: CallbackQuery, state: FSMContext) -> None:
    if not cb.from_user or not _is_admin(cb.from_user.id) or not cb.message:
        await cb.answer()
        return
    m = re.match(r"^ldq:(\d+)$", cb.data or "")
    if not m:
        await cb.answer()
        return
    idx = int(m.group(1))
    data = await state.get_data()
    questions: list[str] = data.get("live_followup_questions") or []
    if idx < 0 or idx >= len(questions):
        await cb.answer("Подсказка устарела.", show_alert=True)
        return
    await cb.answer()
    await _process_turn(cb.message, state, questions[idx])
