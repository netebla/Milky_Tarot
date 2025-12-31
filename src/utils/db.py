from sqlalchemy import create_engine, Column, Integer, String, Boolean, Date, DateTime
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
