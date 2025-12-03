from __future__ import annotations

"""
Утилиты, связанные с внутренней валютой ("рыбки").
"""

from typing import Tuple


def tariff_to_amounts(amount_rub: int) -> Tuple[int, int]:
    """
    Вернуть (total_fish, bonus_fish) по сумме в рублях.

    total_fish — сколько рыбок начисляем всего,
    bonus_fish — из них сколько являются бонусом (для отображения).
    """
    if amount_rub == 50:
        return 350, 0
    if amount_rub == 150:
        return 1050, 150
    if amount_rub == 300:
        return 2100, 400
    if amount_rub == 650:
        return 4550, 1000
    return 0, 0

