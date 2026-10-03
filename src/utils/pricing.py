from __future__ import annotations

"""Единый источник цен для основного, платёжного и административного ботов."""

from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from utils.db import ProductPrice, SessionLocal


@dataclass(frozen=True)
class PriceDefinition:
    code: str
    title: str
    kind: str
    amount_rub: int | None
    fish_amount: int
    bonus_fish: int = 0


DEFAULT_PRICES = (
    PriceDefinition("tariff_1", "Тариф 1", "tariff", 150, 350, 0),
    PriceDefinition("tariff_2", "Тариф 2", "tariff", 250, 1050, 150),
    PriceDefinition("tariff_3", "Тариф 3", "tariff", 450, 2100, 400),
    PriceDefinition("tariff_4", "Тариф 4", "tariff", 950, 4550, 1000),
    PriceDefinition("three_keys", "Задать свой вопрос", "service", None, 69, 0),
    PriceDefinition("live_dialogue", "Живой диалог", "service", None, 150, 0),
)


def ensure_default_prices() -> None:
    """Идемпотентно добавить отсутствующие цены, не перезаписывая изменения админа."""
    with SessionLocal() as db:
        existing = {row[0] for row in db.query(ProductPrice.code).all()}
        for item in DEFAULT_PRICES:
            if item.code not in existing:
                db.add(
                    ProductPrice(
                        code=item.code,
                        title=item.title,
                        kind=item.kind,
                        amount_rub=item.amount_rub,
                        fish_amount=item.fish_amount,
                        bonus_fish=item.bonus_fish,
                        active=True,
                    )
                )
        try:
            db.commit()
        except IntegrityError:
            # Несколько контейнеров могут одновременно выполнить первичное заполнение.
            db.rollback()


def get_tariffs(db: Session | None = None) -> list[ProductPrice]:
    owns_session = db is None
    session = db or SessionLocal()
    try:
        rows = (
            session.query(ProductPrice)
            .filter(ProductPrice.kind == "tariff", ProductPrice.active.is_(True))
            .order_by(ProductPrice.amount_rub, ProductPrice.code)
            .all()
        )
        if owns_session:
            for row in rows:
                session.expunge(row)
        return rows
    finally:
        if owns_session:
            session.close()


def get_service_price(code: str, default: int) -> int:
    with SessionLocal() as db:
        row = (
            db.query(ProductPrice)
            .filter(ProductPrice.code == code, ProductPrice.active.is_(True))
            .first()
        )
        return int(row.fish_amount) if row else default


def tariff_for_rubles(amount_rub: int) -> ProductPrice | None:
    with SessionLocal() as db:
        row = (
            db.query(ProductPrice)
            .filter(
                ProductPrice.kind == "tariff",
                ProductPrice.active.is_(True),
                ProductPrice.amount_rub == amount_rub,
            )
            .first()
        )
        if row:
            db.expunge(row)
        return row
