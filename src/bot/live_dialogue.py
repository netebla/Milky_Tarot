"""Расклад «Живой диалог» — тест: только админы, многоходовый чат с Gemini и draw_card."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from llm.client import GeminiClientError
from llm.gemini_dialogue import (
    assistant_payload_from_response,
    build_system_prompt,
    call_gemini,
    infer_phase_update,
    strip_action_json_from_text,
)
from llm.rag import RAG_CARD_MEANINGS
from utils.card_drawer import draw_random_card
from utils.cards_loader import Card, load_cards
from utils.admin_ids import is_admin as _is_admin
from utils.db import DialogueSession, SessionLocal, User
from utils import session_manager as sm

logger = logging.getLogger(__name__)

router = Router(name="live_dialogue")

LIVE_BUTTON_TEXT = "Живой диалог 🌙"
MAX_TOOL_ROUNDS = 10

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


def _system_prompt_for_session(user_id: int, db) -> str:
    mem = sm.load_user_memory(db, user_id)
    return build_system_prompt(mem)


async def _gemini_multi_round(db, session_id: int, system_prompt: str) -> tuple[str, dict[str, Any] | None]:
    """Повторные вызовы Gemini, пока есть draw_card; история перечитывается из БД."""
    last_meta: dict[str, Any] | None = None
    display_parts: list[str] = []

    for _ in range(MAX_TOOL_ROUNDS):
        history = sm.load_history(session_id, db)
        try:
            result = await call_gemini(history, system_prompt)
        except GeminiClientError:
            raise

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

        for c in calls:
            if c.get("name") != "draw_card":
                continue
            pos = (c.get("args") or {}).get("position_name") or "Позиция"
            if not CARDS:
                tool_payload = {"error": "Колода недоступна", "position_name": pos}
            else:
                title, rev = draw_random_card(CARDS)
                sm.save_drawn_card(db, session_id, pos, title, rev)
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
    return combined, last_meta


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


async def _handle_model_result(
    message: Message,
    state: FSMContext,
    user_id: int,
    session_id: int,
    display_text: str,
    meta: dict[str, Any] | None,
) -> None:
    from bot.keyboards import main_menu_kb

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
        if action == "propose_spreads":
            db.refresh(session)
            spreads = session.pending_spreads or []
            if spreads:
                await message.answer(
                    display_text or "Выбери расклад:",
                    reply_markup=_spreads_keyboard(session.id, spreads),
                )
                return

        if action == "complete":
            memories = (meta or {}).get("memories") or []
            if not isinstance(memories, list):
                memories = []
            db.refresh(session)
            ok, err = sm.try_complete_session(db, user_id, session, memories)
            if ok:
                await state.clear()
                await message.answer(
                    (display_text or "До встречи, солнце.") + "\n\nСессия завершена.",
                    reply_markup=main_menu_kb(_is_admin(user_id)),
                )
            else:
                await message.answer(
                    (display_text or "")
                    + ("\n\n" if display_text else "")
                    + (err or "Не удалось завершить сессию.")
                )
            return

        await message.answer(display_text or "…")


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
            display_text, meta = await _gemini_multi_round(db, session_id, system_prompt)
        except GeminiClientError:
            logger.exception("Gemini error in live_dialogue")
            await message.answer("Не удалось связаться с Милки. Попробуй чуть позже.")
            return

    await _handle_model_result(message, state, user_id, session_id, display_text, meta)


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
            display_text, meta = await _gemini_multi_round(db, session_id, system_prompt)
        except GeminiClientError:
            logger.exception("Gemini error in live_dialogue callback")
            await cb.answer()
            await cb.message.answer("Не удалось связаться с Милки. Попробуй чуть позже.")
            return

    await cb.answer()
    await _handle_model_result(cb.message, state, uid, session_id, display_text, meta)
