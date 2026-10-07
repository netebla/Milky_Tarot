# Milky Tarot Bot

Telegram-система с раскладами Таро, оплатой через ЮKassa, отдельными ботами поддержки и администрирования, а также LLM-интерпретациями через OpenRouter.

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
- `src/bot/support_main.py` — точка входа support-бота.
- `src/bot/admin_main.py` — точка входа закрытого админ-бота.
- `src/bot/admin_handlers.py` — команды и маршруты админ-панели.
- `src/bot/support_handlers.py` — обращения пользователей и ответы администраторов.
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
- Отдельный support-бот с общей админской очередью обращений.
- Отдельный закрытый admin-бот: цены, рассылки, продуктовая и финансовая статистика.
- Админ-рассылки (`/admin_push`) с выбором типа.
- Статистика (`/admin_stats`).
- LLM-интерпретации с дополнительным RAG-контекстом из `src/data/rag_cards.csv`.

Разбор пути оплаты, подготовленные исправления и предложения по развитию:
[CJM пополнения рыбок](docs/payment-cjm.md). При пополнении основной бот отправляет свежий
прайс от имени payment-бота и открывает его чат. Для первого запуска, блокировки или ошибки
доставки используется ссылка с параметром `start`; Telegram может показать кнопку «Начать».
Прайс также доступен по `/topup` и кнопке «Пополнить ещё».

При нехватке рыбок для «Задать свой вопрос» вопрос, история и карты сохраняются в
`pending_readings`; продолжить можно в течение 24 часов, в том числе после перезапуска.
После начисления основной бот сообщает, что Милки готова отвечать, и предлагает
«Продолжить вопрос»; в платёжном боте фото, благодарность и кнопки объединены в одно сообщение.
Возврат открывает подтверждение с актуальным балансом и ценой. Рыбки списываются однократно,
только после успешной отправки всей трактовки в Telegram. При сбое LLM или доставки вопрос
доступен для повторения; готовый ответ сохраняется в `reading_attempts` и не генерируется заново.
Для доставки прайса `PAYMENT_BOT_TOKEN` передаётся и в основной сервис `bot` в обоих compose-файлах.

Payment-сервис использует `BOT_TOKEN` для уведомлений в Milky. Он постоянно сверяет
незавершённые операции из БД с API ЮKassa, с паузой 10 секунд между проходами; трёхминутного
ограничения нет. Успех проверяется по ID, сумме, валюте и `succeeded + paid`. Начисление и записи
в `payment_notifications` фиксируются одной транзакцией. При ошибке доставки уведомление
повторяется через 5 минут; после рестарта незавершённые операции и уведомления восстанавливаются.
Webhook-сервер не требуется для текущей реализации: публичный HTTPS endpoint в проекте не настроен.

## Переменные окружения (минимум)

Обязательные:

- `BOT_TOKEN`
- `ADMIN_ID` (может быть списком через запятую)
- `PAYMENT_BOT_TOKEN`
- `SUPPORT_BOT_TOKEN`
- `ADMIN_BOT_TOKEN`
- `YOOKASSA_SHOP_ID`
- `YOOKASSA_SECRET_KEY`

Опциональные:

- `TZ` (по умолчанию `Europe/Moscow`)
- `YOOKASSA_RETURN_URL` (по умолчанию `https://t.me/Milky_Tarot_Bot`)
- `OPENROUTER_API_KEY`
- `OPENROUTER_MODEL` (по умолчанию `deepseek/deepseek-v4-flash`)
- `SUPPORT_BOT_USERNAME` — необязательный username support-бота без `@`; если не задан,
  основной бот автоматически получает его через `SUPPORT_BOT_TOKEN`.

## Бот поддержки

Кнопка «Помощь» в основном боте открывает отдельного support-бота. Пользователь может отправлять
текст, фото, документы и голосовые сообщения. Все администраторы из `ADMIN_ID` получают карточку
обращения и копию сообщения. Ответ отправляется пользователю через reply на карточку/сообщение
или кнопку «Ответить»; личные аккаунты администраторов пользователю не показываются.

Обращения сохраняются в Postgres, назначаются первому ответившему администратору и не теряют
маршрутизацию reply после перезапуска. Команда `/tickets` показывает администратору открытые
обращения. Перед первым рабочим уведомлением каждый администратор должен один раз открыть
support-бота и нажать `/start` — это ограничение Telegram.

## Админ-бот

Админ-бот — отдельный сервис и отдельный Telegram-бот. Доступ разрешён только Telegram ID из
`ADMIN_ID`. После `/start` доступны:

- изменение рублёвых тарифов, количества рыбок и бонусов;
- изменение стоимости платных повторных раскладов;
- рассылка форматированного текста или фотографии с подписью от имени основного бота;
- статистика пользователей карты дня и сценария «Задать свой вопрос» за сегодня, 7, 30 дней
  или всё время;
- финансовая статистика ЮKassa: выручка, платежи, средний чек, плательщики и начисленные рыбки.

Токен создаётся через BotFather и хранится в GitHub Actions Secret `ADMIN_BOT_TOKEN`. Сам токен
нельзя добавлять в `.env.example`, compose-файлы или исходный код — там используется только имя
переменной окружения.

«Активны сегодня» в обоих ботах — уникальные пользователи, отправившие хотя бы одно сообщение
или нажавшие кнопку в личном чате основного бота за текущий день МСК. Пуши, рассылки и простое
открытие чата не учитываются. Данные сохраняются в `user_activity` начиная с установки этого
обновления: старое `last_activity_date` не переносится, поскольку его обновляли пуши.

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
- `SUPPORT_BOT_TOKEN`
- `ADMIN_BOT_TOKEN`
- `OPENROUTER_API_KEY`
- `PAYMENT_BOT_TOKEN`
- `YOOKASSA_SHOP_ID`, `YOOKASSA_SECRET_KEY`, `YOOKASSA_RETURN_URL`

`SUPPORT_BOT_USERNAME` можно дополнительно хранить в GitHub Actions Secrets как явный override,
но для работы ссылки достаточно `SUPPORT_BOT_TOKEN`.

Миграции `003_support_bot.sql`–`008_reading_attempts.sql` из `migrations/` применяются workflow
автоматически перед запуском сервисов.

Проверки сценария оплаты и списания без реальных Telegram/ЮKassa вызовов:

```bash
DATABASE_URL=sqlite:// PYTHONPATH=src python -m pytest -q tests
```

Для тестов на PostgreSQL задайте `MILKY_TEST_POSTGRES_URL` на отдельную тестовую БД.
Они проверяют SQL миграции и конкурентные операции в отдельных временных схемах.

После аварийного прерывания генерации сохранённый вопрос можно повторить, когда истечёт
блокировка попытки (до 10 минут). Telegram не обеспечивает атомарную транзакцию с нашей БД:
авария после отправки, но до её фиксации может повторить сообщение или ответ при восстановлении;
начисление оплаты и списание за попытку остаются однократными.

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

## Безопасность ответов LLM

Бот не отправляет пользователю ссылок, рекламных сигнатур и метаданных постов из ответа LLM.
Такой ответ блокируется на сервере до отправки, а в журнал попадают только причина блокировки,
идентификатор запроса и фактически разрешённая модель — без текста пользователя.

Параметры генерации можно задать в секретах/deployment environment:

```env
OPENROUTER_MODEL=deepseek/deepseek-v4-flash
OPENROUTER_TEMPERATURE=0.35
OPENROUTER_MAX_TOKENS=900
```

Для дополнительной защиты включите в OpenRouter Dashboard Guardrails: allowlist используемой
модели/провайдера, блокировку prompt injection и запрет сбора данных провайдером, если это
совместимо с выбранной моделью. Настройки в панели не заменяют серверный фильтр в этом проекте.

## Для разработчиков и AI-агентов

См. `AGENTS.md` — там сжатое описание архитектуры, точек входа, инвариантов и типового workflow изменений.
