Milky Tarot Bot
================

Запуск локально (Docker)
------------------------

1. Создайте файл `.env` со значениями:

```
BOT_TOKEN=...
ADMIN_ID=...
TZ=Europe/Moscow
```

2. Соберите и запустите:

```
docker compose up --build -d
```

Продакшн запуск (Compose)
-------------------------

В продакшне используется `docker-compose.prod.yml`, который тянет образ из GHCR. Постоянные данные: монтируется только файл `./data/users.json` на сервере в `/app/src/data/users.json` внутри контейнера. Это позволяет обновлять код (образ) без затрагивания базы пользователей.

CI/CD через GitHub Actions
--------------------------

Workflow `.github/workflows/cicd.yml` делает следующее:

- Сборка и публикация Docker-образа в GitHub Container Registry (`ghcr.io/<owner>/<repo>:latest` и тег SHA)
- SSH деплой на вашу ВМ: выкладывает `docker-compose.prod.yml`, пишет `.env`, выполняет `docker compose pull && up -d`

Необходимые GitHub Secrets:

- `SSH_HOST` — IP/домен ВМ
- `SSH_USER` — пользователь SSH
- `SSH_KEY` — приватный ключ (OpenSSH) для SSH
- `SSH_PORT` — порт SSH (если не 22)
- `BOT_TOKEN` — токен Telegram-бота
- `ADMIN_ID` — ID админа (можно список через запятую)

Примечания
----------

- Контейнер использует `python:3.10-slim`, точка входа `python -m bot.main`.
- Данные и изображения — в `src/data`. Файл `users.json` сохраняется между перезапусками.
- Часовой пояс по умолчанию `Europe/Moscow`.# Test CI/CD
