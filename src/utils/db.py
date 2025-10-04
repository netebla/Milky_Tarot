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
    registered_at = Column(DateTime, default=datetime.utcnow)
    push_time = Column(String, default="10:00")
    push_enabled = Column(Boolean, default=True)
    last_card = Column(String, nullable=True)
    last_card_date = Column(Date, nullable=True)
    last_activity_date = Column(Date, default=date.today)
    draw_count = Column(Integer, default=0)
    daily_advice_count = Column(Integer, default=0)
    advice_last_date = Column(Date, nullable=True)
    is_subscribed = Column(Boolean, default=False)
    subscription_plan = Column(String, nullable=True)
    subscription_started_at = Column(DateTime, nullable=True)
    subscription_expires_at = Column(DateTime, nullable=True)

def init_db():
    Base.metadata.create_all(bind=engine)
