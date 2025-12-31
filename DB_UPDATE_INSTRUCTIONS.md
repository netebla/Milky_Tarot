# Инструкция по обновлению БД для расклада "Энергия года"

## Изменения в БД

Добавлено новое поле в таблицу `users`:
- `year_energy_card` (String, nullable=True) - сохраняет название карты, выбранной для расклада "Энергия года"

## Как обновить БД

### Вариант 1: Автоматическое обновление (рекомендуется)

Если используется SQLAlchemy с автоматической миграцией, просто запустите бота - поле будет создано автоматически при первом обращении к модели.

### Вариант 2: Ручное обновление через SQL

Выполните следующий SQL запрос в вашей БД:

```sql
ALTER TABLE users ADD COLUMN year_energy_card VARCHAR;
```

### Вариант 3: Через Python (если нужно)

```python
from utils.db import engine, Base
from sqlalchemy import text

# Создать поле через SQL
with engine.connect() as conn:
    conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS year_energy_card VARCHAR"))
    conn.commit()
```

## Проверка

После обновления БД проверьте, что поле создано:

```sql
SELECT column_name, data_type, is_nullable 
FROM information_schema.columns 
WHERE table_name = 'users' AND column_name = 'year_energy_card';
```

## Что изменилось в коде

1. **Модель User** (`src/utils/db.py`):
   - Добавлено поле `year_energy_card`

2. **Обработчики расклада** (`src/bot/handlers.py`):
   - Добавлена функция `_choose_year_energy_card()` - выбирает карту или возвращает сохраненную
   - Добавлена функция `_send_card_image()` - универсальная отправка изображения карты
   - Обновлены обработчики `btn_year_energy()` и `cb_admin_push_year_energy()` - теперь проверяют сохраненную карту

## Поведение

- При первом запросе расклада "Энергия года" пользователю выбирается случайная карта и сохраняется в БД
- При повторных запросах того же расклада пользователь всегда получает ту же самую карту
- Каждый пользователь имеет свою уникальную карту для расклада "Энергия года"

