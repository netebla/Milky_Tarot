# Milky Tarot Bot

Telegram-бот с раскладами Таро, оплатой через ЮKassa и LLM-интерпретациями через OpenRouter.

## Быстрый старт (локально, Docker)

1. Создайте `.env` на основе `.env.example`.
2. Запустите:

```bash
docker compose up --build -d
```

3. Логи:

```bash
docker logs -f tarot_bot
```

## Что внутри проекта

- `src/bot/main.py` — точка входа основного бота.
- `src/bot/payment_main.py` — точка входа payment-бота.
- `src/bot/handlers.py` — основные пользовательские сценарии и расклады.
- `src/bot/payment_handlers.py` — сценарии оплат и проверка статуса платежа.
- `src/llm/client.py` — клиент LLM (OpenRouter), включая обработку ошибок.
- `src/llm/three_cards.py` — генерация трактовки для расклада из 3 карт.
- `src/llm/rag.py` — сборка дополнительного контекста по картам.
- `src/utils/db.py` — модели БД (`User`, `Payment` и др.).
- `src/utils/yookassa_client.py` — интеграция с ЮKassa.
- `src/utils/scheduler.py` и `src/utils/push.py` — рассылки/планировщик.

## Основные возможности

- Расклады с картами и изображениями.
- Премиальные сценарии за внутреннюю валюту `fish_balance`.
- Отдельный payment-бот для пополнения баланса.
- Админ-рассылки (`/admin_push`) с выбором типа.
- Статистика (`/admin_stats`).
- LLM-интерпретации с дополнительным RAG-контекстом из `src/data/rag_cards.csv`.

## Переменные окружения (минимум)

Обязательные:

- `BOT_TOKEN`
- `ADMIN_ID` (может быть списком через запятую)
- `PAYMENT_BOT_TOKEN`
- `YOOKASSA_SHOP_ID`
- `YOOKASSA_SECRET_KEY`

Опциональные:

- `TZ` (по умолчанию `Europe/Moscow`)
- `YOOKASSA_RETURN_URL` (по умолчанию `https://t.me/Milky_Tarot_Bot`)
- `OPENROUTER_API_KEY`
- `OPENROUTER_MODEL` (по умолчанию `deepseek/deepseek-v4-flash`)

## Прокси для OpenRouter и внешних HTTP-запросов

Если серверу нужен прокси для доступа к внешним сервисам, задайте `PROXY_ENABLED=true` и `PROXY_URL`:

В `docker-compose*.yml` у сервиса бота:

```yaml
environment:
  HTTP_PROXY: ${PROXY_URL}
  HTTPS_PROXY: ${PROXY_URL}
  ALL_PROXY: ${PROXY_URL}
  NO_PROXY: ${NO_PROXY:-localhost,127.0.0.1,redis,postgres,db}
```

В `.env`:

```env
PROXY_URL=socks5://user:pass@host:port
NO_PROXY=localhost,127.0.0.1,redis,postgres,db
```

Примечание: в `requirements.txt` уже есть `httpx[socks]`, это важно для SOCKS-прокси.

## База данных и миграции

### Поля для баланса рыбок

```sql
ALTER TABLE users ADD COLUMN IF NOT EXISTS fish_balance integer DEFAULT 0;
```

### Поля для дневного лимита "Три ключа"

```sql
ALTER TABLE users ADD COLUMN IF NOT EXISTS three_keys_last_date date;
ALTER TABLE users ADD COLUMN IF NOT EXISTS three_keys_daily_count integer DEFAULT 0;
```

### Таблица платежей

```sql
CREATE TABLE IF NOT EXISTS payments (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL,
    yookassa_payment_id VARCHAR NOT NULL UNIQUE,
    amount_rub INTEGER NOT NULL,
    fish_amount INTEGER NOT NULL,
    status VARCHAR NOT NULL DEFAULT 'pending',
    method VARCHAR,
    description VARCHAR,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_payments_user_id ON payments(user_id);
CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status);
```

## CI/CD

Workflow `.github/workflows/cicd.yml`:

- собирает и публикует Docker-образ в GHCR;
- по SSH обновляет конфигурацию на VM и выполняет `docker compose pull && docker compose up -d`.

Secrets (базово):

- `SSH_HOST`, `SSH_USER`, `SSH_KEY`, `SSH_PORT`
- `BOT_TOKEN`, `ADMIN_ID`
- `OPENROUTER_API_KEY`
- `PAYMENT_BOT_TOKEN`
- `YOOKASSA_SHOP_ID`, `YOOKASSA_SECRET_KEY`, `YOOKASSA_RETURN_URL`

## Быстрая диагностика OpenRouter

1. Проверить, что в контейнере заданы прокси-переменные:

```bash
docker exec -it tarot_bot sh -c 'env | sort | grep -i proxy'
```

2. Проверить, не пустой ли `NO_PROXY`:

```bash
docker exec -it tarot_bot sh -c 'echo "$NO_PROXY"'
```

3. Проверить логи:

```bash
docker logs --tail 200 tarot_bot
```

## Для разработчиков и AI-агентов

См. `AGENTS.md` — там сжатое описание архитектуры, точек входа, инвариантов и типового workflow изменений.
