from __future__ import annotations

"""
Утилиты, связанные с внутренней валютой ("рыбки").
"""

from typing import Tuple

from utils.pricing import tariff_for_rubles


def tariff_to_amounts(amount_rub: int) -> Tuple[int, int]:
    """
    Вернуть (total_fish, bonus_fish) по сумме в рублях.

    total_fish — сколько рыбок начисляем всего,
    bonus_fish — из них сколько являются бонусом (для отображения).
    """
    tariff = tariff_for_rubles(amount_rub)
    if not tariff:
        return 0, 0
    return int(tariff.fish_amount), int(tariff.bonus_fish or 0)
