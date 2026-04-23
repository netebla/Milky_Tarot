"""Расклад «Живой диалог» — тест: только админы, многоходовый чат с Gemini и draw_card."""

from __future__ import annotations

import html
import json
import logging
import re
import time
from typing import Any

import httpx
from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

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
    in_dialogue = State()


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


def _system_prompt_for_session(user_id: int, db) -> str:
    mem = sm.load_user_memory(db, user_id)
    return build_system_prompt(mem)


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
        r"```(?:json)?\s*\{\s*\"action\"\s*:\s*\"(?:propose_spreads|draw_cards|complete)\"[\s\S]*?\}\s*```",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    # Удаляем однострочные action JSON.
    cleaned = re.sub(
        r"\{\s*\"action\"\s*:\s*\"(?:propose_spreads|draw_cards|complete)\"[^\n]*\}",
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
        if not is_propose_ui:
            await _send_drawn_cards_live(message, drawn_this_turn)
            await _send_drawn_cards_summary(message, drawn_this_turn)

        if action == "propose_spreads":
            db.refresh(session)
            spreads = session.pending_spreads or []
            if spreads:
                if len(spreads) == 1 and auto_spread_depth < _AUTO_SPREAD_CHAIN_MAX:
                    sp = spreads[0]
                    name = (sp.get("name") or "Расклад").strip()
                    positions = sp.get("positions") or {}
                    if not isinstance(positions, dict):
                        positions = {}
                    sm.set_session_spread(db, session, name, positions)
                    choice = (
                        f"Я выбираю расклад «{name}». Позиции: {json.dumps(positions, ensure_ascii=False)}"
                    )
                    sm.save_message(db, session_id, "user", choice)

                    raw_intro = _strip_action_artifacts_for_user(display_text or "")
                    if raw_intro:
                        await message.answer(format_model_reply_for_telegram_html(raw_intro))
                    else:
                        await message.answer(
                            f"Договорились — расклад «{html.escape(name)}», открываем карты по позициям."
                        )

                    system_prompt = _system_prompt_for_session(user_id, db)
                    system_prompt += (
                        f"\n\nТекущая фаза сессии в базе: {session.phase}. "
                        "Пользователь согласился на единственный предложенный расклад (автовыбор в боте). "
                        "Вызови draw_card по каждой позиции из расклада подряд (все позиции), "
                        "затем дай связную интерпретацию; не останавливайся на одной карте, если позиций несколько."
                    )
                    try:
                        display_text2, meta2, drawn2 = await _gemini_multi_round(
                            db, session_id, system_prompt
                        )
                    except GeminiClientError:
                        logger.exception("Gemini error after auto-select spread")
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

                if len(spreads) == 1:
                    raw_intro = _strip_action_artifacts_for_user(display_text or "")
                    raw_intro = raw_intro or "Продолжим с этим раскладом:"
                    body = format_model_reply_for_telegram_html(raw_intro)
                    await message.answer(body)
                    return

                raw_intro = _strip_action_artifacts_for_user(display_text or "")
                raw_intro = raw_intro or "Выбери расклад:"
                body = format_model_reply_for_telegram_html(raw_intro)
                body += "\n\nВыбери вариант кнопкой под этим сообщением."
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

            spread_positions = {str(i): name for i, name in enumerate(positions, start=1)}
            spread_title = spread_name or f"Расклад на {count} карт"
            sm.set_session_spread(db, session, spread_title, spread_positions)

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
                drawn_batch.append(
                    {"card_name": title, "is_reversed": rev, "position_name": pos}
                )

            await _send_drawn_cards_live(message, drawn_batch)
            await _send_drawn_cards_summary(message, drawn_batch)
            clean_text = _strip_action_artifacts_for_user(display_text or "")
            if not drawn_batch and positions:
                await message.answer("Я уже открыла эти позиции и продолжаю трактовку.")
            if clean_text:
                await message.answer(format_model_reply_for_telegram_html(clean_text))
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
                system_prompt = _system_prompt_for_session(user_id, db)
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
    with SessionLocal() as db:
        session = db.get(DialogueSession, session_id)
        if not session or session.user_id != user_id or session.completed_at is not None:
            await message.answer("Сессия недействительна. Нажми /live_dialogue.")
            await state.clear()
            return

        if sm.count_user_messages(db, session_id) >= sm.MAX_USER_MESSAGES_PER_SESSION:
            await message.answer(
                "В этом диалоге уже максимум сообщений. Заверши мысль или начни новую сессию позже "
                "(/cancel_dialogue, затем /live_dialogue)."
            )
            return

        sm.save_message(db, session_id, "user", user_text)

        system_prompt = _system_prompt_for_session(user_id, db)
        system_prompt += f"\n\nТекущая фаза сессии в базе: {session.phase}. Следуй логике этой фазы."

        try:
            gemini_t0 = time.perf_counter()
            display_text, meta, drawn = await _gemini_multi_round(db, session_id, system_prompt)
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
        session = sm.get_or_create_session(db, uid)
        sid = session.id

    await state.set_state(LiveDialogueStates.in_dialogue)
    await state.update_data(live_session_id=sid)
    await message.answer(
        "Мяу, это режим «Живой диалог» — поговорим по-настоящему, без заранее заданного расклада. "
        "Расскажи, что у тебя на душе, или как хочешь назвать тему.\n\n"
        "Чтобы выйти без завершения: /cancel_dialogue"
    )


@router.message(F.text == LIVE_BUTTON_TEXT)
async def btn_live_dialogue(message: Message, state: FSMContext) -> None:
    if not message.from_user or not _is_admin(message.from_user.id):
        return
    await cmd_live_dialogue(message, state)


@router.message(Command("cancel_dialogue"), StateFilter(LiveDialogueStates.in_dialogue))
async def cmd_cancel_dialogue(message: Message, state: FSMContext) -> None:
    from bot.keyboards import main_menu_kb

    data = await state.get_data()
    session_id = data.get("live_session_id")
    uid = message.from_user.id if message.from_user else 0
    if session_id:
        with SessionLocal() as db:
            s = db.get(DialogueSession, session_id)
            if s and s.completed_at is None:
                sm.abandon_session_no_charge(db, s)
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
        sm.set_session_spread(db, session, name, positions)
        choice = f"Я выбираю расклад «{name}». Позиции: {json.dumps(positions, ensure_ascii=False)}"
        sm.save_message(db, session_id, "user", choice)

        system_prompt = _system_prompt_for_session(uid, db)
        system_prompt += f"\n\nТекущая фаза сессии в базе: {session.phase}. Пользователь выбрал расклад."

        try:
            display_text, meta, drawn = await _gemini_multi_round(db, session_id, system_prompt)
        except GeminiClientError:
            logger.exception("Gemini error in live_dialogue callback")
            await cb.answer()
            await cb.message.answer("Не удалось связаться с Милки. Попробуй чуть позже.")
            return

    await cb.answer()
    await _handle_model_result(cb.message, state, uid, session_id, display_text, meta, drawn)
