"""Сессии живого диалога, история для Gemini, карты, память пользователя, биллинг."""

from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from typing import Any, List, Optional

from sqlalchemy.orm import Session

from utils.db import (
    DialogueMessage,
    DialogueSession,
    DrawnCard,
    User,
    UserMemory,
)

logger = logging.getLogger(__name__)

# Фазы сессии (совпадают с ТЗ + completed)
PHASE_COLLECTING = "collecting_context"
PHASE_PROPOSING = "proposing_spread"
PHASE_DIALOGUE = "dialogue_with_cards"
PHASE_SUMMARY = "summary"
PHASE_COMPLETED = "completed"

LIVE_DIALOGUE_FREE_PER_DAY = 1
LIVE_DIALOGUE_PRICE_FISH = 150
MAX_USER_MESSAGES_PER_SESSION = 20
SESSION_STALE_HOURS = 24

_session_locks: dict[int, asyncio.Lock] = {}

# Маркеры вопроса «про других людей», не про спрашивающего
_THIRD_PARTY_MARKERS = re.compile(
    r"(?:"
    r"что\s+(?:жд[её]т|будет|будет\s+с|ждут|жд[её]т\s+у)"
    r"|расклад\s+(?:на|про|для)"
    r"|про\s+(?:него|неё|них|тебя\s+и|моего|мою|моих)"
    r"|как\s+(?:у|с)\s+"
    r"|между\s+"
    r"|у\s+\w+"
    r")",
    re.IGNORECASE,
)


def get_active_session(db: Session, user_id: int) -> Optional[DialogueSession]:
    return (
        db.query(DialogueSession)
        .filter(DialogueSession.user_id == user_id, DialogueSession.completed_at.is_(None))
        .order_by(DialogueSession.created_at.desc())
        .first()
    )


def get_or_create_session(db: Session, user_id: int) -> DialogueSession:
    """Устаревший путь: предпочитай create_fresh_session / get_active_session."""
    active = get_active_session(db, user_id)
    if active:
        return active
    return create_fresh_session(db, user_id)


def create_fresh_session(db: Session, user_id: int) -> DialogueSession:
    """Новая изолированная сессия (история только внутри неё)."""
    sess = DialogueSession(user_id=user_id, phase=PHASE_COLLECTING)
    db.add(sess)
    db.commit()
    db.refresh(sess)
    return sess


def session_has_user_messages(db: Session, session_id: int) -> bool:
    return count_user_messages(db, session_id) > 0


def abandon_active_session_for_user(db: Session, user_id: int) -> int:
    """Закрыть все незавершённые сессии пользователя без списания. Возвращает число закрытых."""
    active_list = (
        db.query(DialogueSession)
        .filter(DialogueSession.user_id == user_id, DialogueSession.completed_at.is_(None))
        .all()
    )
    for s in active_list:
        abandon_session_no_charge(db, s)
    return len(active_list)


@asynccontextmanager
async def session_turn_lock(session_id: int):
    """Сериализация ходов одной сессии (защита от гонок при быстрых сообщениях)."""
    if session_id not in _session_locks:
        _session_locks[session_id] = asyncio.Lock()
    async with _session_locks[session_id]:
        yield


def set_reading_subject(db: Session, session: DialogueSession, subject: str | None) -> None:
    session.reading_subject = (subject or "").strip() or None
    db.add(session)
    db.commit()
    db.refresh(session)


def infer_reading_subject_from_text(text: str) -> str | None:
    """
    Эвристика: извлечь субъект расклада из реплики пользователя (имена / «X и Y»).
    Возвращает None, если вопрос явно про самого спрашивающего.
    """
    raw = (text or "").strip()
    if not raw or len(raw) < 8:
        return None
    lower = raw.lower()
    if not _THIRD_PARTY_MARKERS.search(lower):
        # «моя работа», «меня ждёт» — про пользователя
        if re.search(r"\b(?:меня|мне|мой|моя|моё|мои|у\s+меня|для\s+меня)\b", lower):
            return None
        return None

    # «что ждёт Кирюшу и Катю»
    m = re.search(
        r"(?:что\s+(?:жд[её]т|будет)|расклад\s+(?:на|про|для)|про)\s+(.+?)(?:\?|$)",
        raw,
        re.IGNORECASE,
    )
    if m:
        chunk = m.group(1).strip().rstrip("?.!")
        chunk = re.sub(r"\s+и\s+как\b.*", "", chunk, flags=re.IGNORECASE)
        if len(chunk) >= 2 and not re.match(r"^(меня|мне|мой|моя|моё|мои)\b", chunk, re.I):
            return chunk[:200]

    # «Кирюша и Катя» в середине фразы
    m2 = re.search(
        r"([А-ЯЁA-Z][а-яёa-z]+(?:\s+[А-ЯЁA-Z][а-яёa-z]+)?)\s+и\s+([А-ЯЁA-Z][а-яёa-z]+)",
        raw,
    )
    if m2:
        return f"{m2.group(1)} и {m2.group(2)}"[:200]

    return None


def update_reading_subject_from_user_text(
    db: Session, session: DialogueSession, user_text: str
) -> None:
    inferred = infer_reading_subject_from_text(user_text)
    if inferred:
        set_reading_subject(db, session, inferred)
    elif re.search(
        r"\b(?:меня|мне|мой|моя|моё|мои|у\s+меня|для\s+меня|моей\s+жизни)\b",
        user_text or "",
        re.IGNORECASE,
    ):
        set_reading_subject(db, session, "сам пользователь (спрашивающий)")


def count_user_messages(db: Session, session_id: int) -> int:
    return (
        db.query(DialogueMessage)
        .filter(DialogueMessage.session_id == session_id, DialogueMessage.role == "user")
        .count()
    )


def save_message(
    db: Session,
    session_id: int,
    role: str,
    content: str,
    tool_name: str | None = None,
    tool_result: dict[str, Any] | None = None,
    model_function_calls: list[dict[str, Any]] | None = None,
) -> DialogueMessage:
    row = DialogueMessage(
        session_id=session_id,
        role=role,
        content=content,
        tool_name=tool_name,
        tool_result=tool_result,
        model_function_calls=model_function_calls,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def load_history(session_id: int, db: Session) -> list[dict[str, Any]]:
    """
    История в упрощённом виде для сборки запроса к Gemini.

    role: user | model | tool (tool → Part.from_function_response в llm-слое).
    """
    rows = (
        db.query(DialogueMessage)
        .filter(DialogueMessage.session_id == session_id)
        .order_by(DialogueMessage.created_at.asc(), DialogueMessage.id.asc())
        .all()
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        if r.role == "user":
            out.append({"role": "user", "text": r.content})
        elif r.role == "assistant":
            item: dict[str, Any] = {"role": "model", "text": r.content or ""}
            if r.model_function_calls:
                item["function_calls"] = list(r.model_function_calls)
            out.append(item)
        elif r.role == "tool":
            out.append(
                {
                    "role": "tool",
                    "name": r.tool_name or "",
                    "response": dict(r.tool_result) if r.tool_result else {},
                }
            )
    return out


def save_drawn_card(
    db: Session,
    session_id: int,
    position_name: str,
    card_name: str,
    is_reversed: bool,
) -> DrawnCard:
    row = DrawnCard(
        session_id=session_id,
        position_name=position_name,
        card_name=card_name,
        is_reversed=is_reversed,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def update_session_phase(db: Session, session: DialogueSession, phase: str) -> None:
    session.phase = phase
    db.add(session)
    db.commit()
    db.refresh(session)


def set_session_spread(
    db: Session,
    session: DialogueSession,
    spread_type: str,
    spread_positions: dict[str, Any],
) -> None:
    session.spread_type = spread_type
    session.spread_positions = spread_positions
    session.pending_spreads = None
    session.phase = PHASE_DIALOGUE
    db.add(session)
    db.commit()
    db.refresh(session)


def set_pending_spreads(db: Session, session: DialogueSession, spreads: list[dict[str, Any]]) -> None:
    session.pending_spreads = spreads
    db.add(session)
    db.commit()
    db.refresh(session)


def load_user_memory(db: Session, user_id: int, limit: int = 15) -> str:
    """
    Текстовый блок для системного промпта: последние записи, группировка по типам.
    """
    rows = (
        db.query(UserMemory)
        .filter(UserMemory.user_id == user_id)
        .order_by(UserMemory.created_at.desc())
        .limit(limit)
        .all()
    )
    if not rows:
        return ""

    themes: list[UserMemory] = []
    patterns: list[UserMemory] = []
    preferences: list[UserMemory] = []
    key_cards: list[UserMemory] = []
    open_q: list[UserMemory] = []

    for m in rows:
        t = (m.memory_type or "").strip()
        if t == "open_question" and not m.is_resolved:
            open_q.append(m)
        elif t == "key_card":
            key_cards.append(m)
        elif t == "theme":
            themes.append(m)
        elif t == "pattern":
            patterns.append(m)
        elif t == "preference":
            preferences.append(m)
        else:
            themes.append(m)

    def _fmt(m: UserMemory) -> str:
        when = ""
        if m.session_date:
            when = f" ({m.session_date.isoformat()})"
        return f"— {m.content}{when}"

    lines: list[str] = ["ЧТО ТЫ ЗНАЕШЬ ОБ ЭТОМ ЧЕЛОВЕКЕ:"]
    for label, group in (
        ("Темы и контекст", themes),
        ("Паттерны", patterns),
        ("Как удобнее общаться", preferences),
        ("Значимые карты", key_cards),
        ("Открытые вопросы с прошлых встреч", open_q),
    ):
        if not group:
            continue
        for m in reversed(group):
            lines.append(_fmt(m))
    if len(lines) == 1:
        return ""
    return "\n".join(lines)


def save_memories(
    db: Session,
    user_id: int,
    session_id: int,
    memories: list[dict[str, Any]],
    session_day: date | None = None,
    *,
    do_commit: bool = True,
) -> None:
    """Сохранить инсайты после завершения диалога."""
    day = session_day or date.today()
    for item in memories:
        mtype = (item.get("type") or "").strip()
        content = (item.get("content") or "").strip()
        if not mtype or not content:
            continue
        row = UserMemory(
            user_id=user_id,
            session_id=session_id,
            memory_type=mtype,
            content=content,
            is_resolved=False,
            session_date=day,
        )
        db.add(row)
    if do_commit:
        db.commit()


def try_complete_session(
    db: Session,
    user_id: int,
    session: DialogueSession,
    memories: list[dict[str, Any]],
) -> tuple[bool, str | None]:
    """
    Завершить сессию: списать рыбки при необходимости, обновить дневной счётчик, сохранить память.

    Возвращает (успех, сообщение об ошибке).
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return False, "Пользователь не найден"

    today = date.today()
    last = getattr(user, "live_dialogue_last_date", None)
    daily = getattr(user, "live_dialogue_daily_count", 0) or 0
    if last != today:
        daily = 0

    fish_cost = 0
    if daily >= LIVE_DIALOGUE_FREE_PER_DAY:
        balance = getattr(user, "fish_balance", 0) or 0
        if balance < LIVE_DIALOGUE_PRICE_FISH:
            return (
                False,
                "Чтобы завершить этот диалог и сохранить итог, нужно 150 рыбок. "
                "Пополни баланс или вернись завтра — первая сессия дня бесплатная.",
            )
        user.fish_balance = balance - LIVE_DIALOGUE_PRICE_FISH
        fish_cost = LIVE_DIALOGUE_PRICE_FISH

    session.completed_at = datetime.utcnow()
    session.phase = PHASE_COMPLETED
    session.fish_cost = fish_cost
    user.live_dialogue_last_date = today
    user.live_dialogue_daily_count = daily + 1
    db.add(session)
    db.add(user)
    if memories:
        save_memories(db, user_id, session.id, memories, session_day=today, do_commit=False)
    db.commit()

    return True, None


def abandon_session_no_charge(db: Session, session: DialogueSession) -> None:
    """Закрыть сессию без списания (отмена пользователем)."""
    session.completed_at = datetime.utcnow()
    session.phase = PHASE_COMPLETED
    session.fish_cost = 0
    db.add(session)
    db.commit()


def expire_stale_sessions(db: Session, hours: int = SESSION_STALE_HOURS) -> int:
    """
    Пометить незавершённые сессии старше hours как completed без списания.
    Возвращает число затронутых сессий.
    """
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    stale = (
        db.query(DialogueSession)
        .filter(DialogueSession.completed_at.is_(None), DialogueSession.created_at < cutoff)
        .all()
    )
    n = 0
    for s in stale:
        s.completed_at = datetime.utcnow()
        s.phase = PHASE_COMPLETED
        s.fish_cost = 0
        db.add(s)
        n += 1
    if n:
        db.commit()
    return n
