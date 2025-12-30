"""
Парсер для docx файла с данными расклада 'Энергия года'.

Этот модуль преобразует docx файл с архетипами года в CSV формат,
который затем используется для загрузки данных в бот.

Формат входного docx:
- Каждый архетип начинается со строки вида "N Архетип — Название_карты"
- За названием следует описание архетипа

Формат выходного CSV:
- card_name: название карты Таро (соответствует названиям в cards.csv)
- description: полное описание архетипа года для этой карты

Использование:
    python src/utils/parse_year_energy.py
"""

import csv
import re
from pathlib import Path
from docx import Document


# Маппинг сокращённых названий на полные названия карт
CARD_NAME_MAPPING = {
    "Маг": "Маг",
    "Жрица": "Верховная Жрица",
    "Императрица": "Императрица",
    "Император": "Император",
    "Жрец": "Иерофант",
    "Влюблённые": "Влюбленные",
    "Влюбленные": "Влюбленные",
    "Колесница": "Колесница",
    "Справедливость": "Справедливость",
    "Отшельник": "Отшельник",
    "Колесо фортуны": "Колесо Фортуны",
    "Колесо Фортуны": "Колесо Фортуны",
    "Сила": "Сила",
    "Повешенный": "Повешенный",
    "Смерть": "Смерть",
    "Умеренность": "Умеренность",
    "Дьявол": "Дьявол",
    "Башня": "Башня",
    "Звезда": "Звезда",
    "Луна": "Луна",
    "Солнце": "Солнце",
    "Суд": "Суд",
    "Мир": "Мир",
    "Шут": "Шут",
}


def extract_card_name(text: str) -> str | None:
    """Извлекает название карты из строки вида '1 Архетип — Маг 🧚‍♀️'."""
    # Сначала проверяем многословные названия
    multi_word_cards = [
        "Верховная Жрица",
        "Колесо Фортуны",
        "Повешенный",
    ]
    
    for card_name in multi_word_cards:
        if card_name in text:
            return CARD_NAME_MAPPING.get(card_name, card_name)
    
    # Убираем эмодзи и специальные символы, но сохраняем пробелы для многословных названий
    text_clean = re.sub(r'[^\w\s—\-]', '', text)
    
    # Ищем паттерн "Архетип — Название" (может быть многословным)
    match = re.search(r'Архетип\s*—\s*([А-Яа-яЁё\s]+)', text_clean)
    if match:
        card_name = match.group(1).strip()
        # Проверяем многословные варианты
        if "Колесо" in card_name and "Фортуны" in card_name:
            return "Колесо Фортуны"
        if "Верховная" in card_name and "Жрица" in card_name:
            return "Верховная Жрица"
        if "Повешенный" in card_name:
            return "Повешенный"
        # Берем первое слово для однозначных карт
        first_word = card_name.split()[0] if card_name.split() else card_name
        return CARD_NAME_MAPPING.get(first_word, first_word)
    
    # Альтернативный паттерн: номер и название
    match = re.search(r'\d+\s+Архетип\s*—\s*([А-Яа-яЁё\s]+)', text_clean)
    if match:
        card_name = match.group(1).strip()
        # Проверяем многословные варианты
        if "Колесо" in card_name and "Фортуны" in card_name:
            return "Колесо Фортуны"
        if "Верховная" in card_name and "Жрица" in card_name:
            return "Верховная Жрица"
        if "Повешенный" in card_name:
            return "Повешенный"
        first_word = card_name.split()[0] if card_name.split() else card_name
        return CARD_NAME_MAPPING.get(first_word, first_word)
    
    # Если просто название карты в тексте
    for key in sorted(CARD_NAME_MAPPING.keys(), key=len, reverse=True):  # Сначала длинные названия
        if key in text:
            return CARD_NAME_MAPPING[key]
    
    return None


def parse_year_energy_docx(docx_path: str | Path, output_csv_path: str | Path) -> None:
    """
    Парсит docx файл с данными расклада 'Энергия года' и сохраняет в CSV.
    
    Ожидаемый формат docx:
    - Название архетипа (название карты)
    - Описание/трактовка
    """
    doc = Document(docx_path)
    
    archetypes = []
    current_archetype = None
    current_text = []
    
    for para in doc.paragraphs:
        text = para.text.strip()
        if not text:
            continue
        
        # Пытаемся извлечь название карты
        card_name = extract_card_name(text)
        
        if card_name:
            # Сохраняем предыдущий архетип, если есть
            if current_archetype and current_text:
                archetypes.append({
                    'card_name': current_archetype,
                    'description': '\n'.join(current_text).strip()
                })
            
            # Начинаем новый архетип
            current_archetype = card_name
            current_text = []
            # Убираем строку с названием из описания
            continue
        else:
            # Это часть описания
            if current_archetype:
                current_text.append(text)
            else:
                # Если нет текущего архетипа, возможно это первый архетип без явного названия
                # Пытаемся найти название в тексте
                card_name = extract_card_name(text)
                if card_name:
                    current_archetype = card_name
                    # Убираем название из текста
                    text_without_name = re.sub(r'\d+\s+Архетип\s*—\s*\w+[^\w\s]*', '', text).strip()
                    if text_without_name:
                        current_text = [text_without_name]
                    else:
                        current_text = []
                elif not archetypes:
                    # Первый параграф без названия - пропускаем или используем как заголовок
                    continue
    
    # Сохраняем последний архетип
    if current_archetype and current_text:
        archetypes.append({
            'card_name': current_archetype,
            'description': '\n'.join(current_text).strip()
        })
    
    # Убираем дубликаты (оставляем первый)
    seen = set()
    unique_archetypes = []
    for arch in archetypes:
        if arch['card_name'] not in seen:
            seen.add(arch['card_name'])
            unique_archetypes.append(arch)
        else:
            # Если дубликат, объединяем описания
            for existing in unique_archetypes:
                if existing['card_name'] == arch['card_name']:
                    existing['description'] += '\n\n' + arch['description']
                    break
    
    # Сохраняем в CSV
    with open(output_csv_path, 'w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=['card_name', 'description'])
        writer.writeheader()
        writer.writerows(unique_archetypes)
    
    print(f"Парсинг завершён. Найдено {len(unique_archetypes)} уникальных архетипов.")
    print(f"Результат сохранён в {output_csv_path}")
    
    # Выводим список найденных карт для проверки
    print("\nНайденные карты:")
    for arch in unique_archetypes:
        print(f"  - {arch['card_name']}")


if __name__ == "__main__":
    # Пути к файлам
    data_dir = Path(__file__).parent.parent / "data"
    docx_path = data_dir / "архетипы года с советами.docx"
    output_path = data_dir / "year_energy_archetypes.csv"
    
    if not docx_path.exists():
        print(f"Файл {docx_path} не найден!")
        exit(1)
    
    parse_year_energy_docx(docx_path, output_path)

