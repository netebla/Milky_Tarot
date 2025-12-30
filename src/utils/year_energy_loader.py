"""
Загрузчик данных для расклада 'Энергия года'.

Этот модуль загружает архетипы года из CSV файла и предоставляет
удобный интерфейс для получения трактовок по названиям карт.

Данные кэшируются в памяти после первой загрузки для быстрого доступа.
"""

import csv
import logging
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# Путь к CSV файлу с архетипами
DATA_DIR = Path(__file__).parent.parent / "data"
ARCHETYPES_CSV = DATA_DIR / "year_energy_archetypes.csv"

# Кэш загруженных данных
_ARCHETYPES_CACHE: Optional[Dict[str, str]] = None


def load_year_energy_archetypes() -> Dict[str, str]:
    """
    Загружает архетипы года из CSV файла.
    
    Returns:
        Словарь {название_карты: описание_архетипа}
    """
    global _ARCHETYPES_CACHE
    
    if _ARCHETYPES_CACHE is not None:
        return _ARCHETYPES_CACHE
    
    archetypes = {}
    
    if not ARCHETYPES_CSV.exists():
        logger.warning(f"Файл {ARCHETYPES_CSV} не найден!")
        return archetypes
    
    try:
        with open(ARCHETYPES_CSV, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                card_name = row['card_name'].strip()
                description = row['description'].strip()
                if card_name and description:
                    archetypes[card_name] = description
        
        logger.info(f"Загружено {len(archetypes)} архетипов года")
        _ARCHETYPES_CACHE = archetypes
        return archetypes
    except Exception as e:
        logger.error(f"Ошибка при загрузке архетипов года: {e}")
        return {}


def get_archetype_by_card(card_name: str) -> Optional[str]:
    """
    Получает описание архетипа по названию карты.
    
    Args:
        card_name: Название карты Таро
        
    Returns:
        Описание архетипа или None, если не найдено
    """
    archetypes = load_year_energy_archetypes()
    return archetypes.get(card_name)

