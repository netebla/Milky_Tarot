from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    String,
    Boolean,
    Date,
    DateTime,
    Text,
    ForeignKey,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import os
from datetime import datetime, date

DATABASE_URL = os.getenv("DATABASE_URL")

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, nullable=True)
    # Отображаемое имя (как обращаться)
    display_name = Column(String, nullable=True)
    registered_at = Column(DateTime, default=datetime.utcnow)
    push_time = Column(String, default="10:00")
    push_enabled = Column(Boolean, default=True)
    last_card = Column(String, nullable=True)
    last_card_date = Column(Date, nullable=True)
    last_activity_date = Column(Date, default=date.today)
    draw_count = Column(Integer, default=0)
    daily_advice_count = Column(Integer, default=0)
    advice_last_date = Column(Date, nullable=True)
    # Дата рождения пользователя
    birth_date = Column(Date, nullable=True)
    # Смещение относительно МСК в часах (например, +3 => 3, -2 => -2)
    tz_offset_hours = Column(Integer, default=0)
    is_subscribed = Column(Boolean, default=False)
    subscription_plan = Column(String, nullable=True)
    subscription_started_at = Column(DateTime, nullable=True)
    subscription_expires_at = Column(DateTime, nullable=True)
    # Баланс внутренней валюты ("рыбки") для платных раскладов
    fish_balance = Column(Integer, default=0)
    # Использование премиального расклада "Три ключа"
    three_keys_last_date = Column(Date, nullable=True)
    three_keys_daily_count = Column(Integer, default=0)
    # Карта для расклада "Энергия года" (сохраняется один раз на год)
    year_energy_card = Column(String, nullable=True)
    # Расклад «Живой диалог»: учёт бесплатной сессии в день
    live_dialogue_last_date = Column(Date, nullable=True)
    live_dialogue_daily_count = Column(Integer, default=0)


class DialogueSession(Base):
    """Сессия многоходового диалога с Milky."""

    __tablename__ = "dialogue_sessions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    phase = Column(String, nullable=False, default="collecting_context")
    spread_type = Column(String, nullable=True)
    spread_positions = Column(JSONB, nullable=True)
    pending_spreads = Column(JSONB, nullable=True)
    fish_cost = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)


class DialogueMessage(Base):
    """Сообщение в истории диалога (для Gemini и аудита)."""

    __tablename__ = "dialogue_messages"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("dialogue_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    role = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    tool_name = Column(String, nullable=True)
    tool_result = Column(JSONB, nullable=True)
    model_function_calls = Column(JSONB, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class DrawnCard(Base):
    """Карта, вытянутая в ходе живого диалога."""

    __tablename__ = "drawn_cards"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("dialogue_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    position_name = Column(String, nullable=False)
    card_name = Column(String, nullable=False)
    is_reversed = Column(Boolean, nullable=False, default=False)
    drawn_at = Column(DateTime, default=datetime.utcnow)


class UserMemory(Base):
    """Долгосрочные заметки о пользователе для будущих раскладов."""

    __tablename__ = "user_memory"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    session_id = Column(Integer, ForeignKey("dialogue_sessions.id", ondelete="SET NULL"), nullable=True)
    memory_type = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    is_resolved = Column(Boolean, nullable=False, default=False)
    session_date = Column(Date, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class Payment(Base):
    """
    Платёж через ЮKassa.

    Храним только данные, необходимые для проверки статуса
    и начисления внутренней валюты пользователю.
    """

    __tablename__ = "payments"

    id = Column(Integer, primary_key=True, index=True)
    # Telegram ID пользователя, который платит
    user_id = Column(Integer, index=True, nullable=False)
    # Идентификатор платежа в ЮKassa (поле id)
    yookassa_payment_id = Column(String, unique=True, index=True, nullable=False)
    # Сумма к оплате в рублях
    amount_rub = Column(Integer, nullable=False)
    # Сколько "рыбок" будет начислено после успешной оплаты
    fish_amount = Column(Integer, nullable=False)
    # Статус: pending / succeeded / canceled / error
    status = Column(String, default="pending", index=True, nullable=False)
    # Человекочитаемый способ оплаты (например, "bank_card", "sbp")
    method = Column(String, nullable=True)
    description = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


def init_db():
    Base.metadata.create_all(bind=engine)
