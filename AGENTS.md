# AGENTS.md

This file is a compact operational guide for AI agents and new contributors.

## Project Purpose

`Milky Tarot Bot` is a Telegram bot system with:

- card readings;
- premium flows paid with internal currency (`fish_balance`);
- payment integration via YooKassa;
- LLM-based interpretations (Gemini).

## Runtime Topology

- Main bot entrypoint: `python -m bot.main`
- Payment bot entrypoint: `python -m bot.payment_main`
- Container base image: `python:3.10-slim` (see `Dockerfile`)

## Key Directories

- `src/bot/` — Telegram handlers and bot wiring
- `src/llm/` — LLM clients and prompt logic
- `src/utils/` — DB models, scheduler, payments, helpers
- `src/data/` — cards datasets, images, CSV/docx artifacts
- `migrations/` — migration-related resources

## Primary Files to Read First

1. `src/bot/handlers.py`
2. `src/bot/payment_handlers.py`
3. `src/llm/client.py`
4. `src/llm/three_cards.py`
5. `src/utils/db.py`
6. `README.md`

## Business-Critical Invariants

- Payment crediting must be idempotent.
- Daily limits for premium readings rely on DB fields, not memory state.
- LLM failures must not crash update processing; handlers should return user-safe fallback text.
- Admin-only flows must always check admin permissions.

## LLM/Gemini Notes

- LLM calls are centralized in `src/llm/client.py`.
- Geography restrictions can produce:
  - `400 FAILED_PRECONDITION`
  - `"User location is not supported for the API use."`
- If this happens in production, verify outbound proxy env (`HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, `NO_PROXY`) for the bot container.

## Database Notes

Current logic expects at least:

- `users.fish_balance`
- `users.three_keys_last_date`
- `users.three_keys_daily_count`
- `payments` table with `yookassa_payment_id` unique

When changing payment or premium logic, validate SQL schema compatibility.

## Safe Change Workflow (for agents)

1. Read affected handler + utility + DB model files.
2. Search for callback/data key reuse before adding new callbacks.
3. Keep user-facing Russian text consistent with existing style.
4. Add/adjust logs near external integrations (Gemini, YooKassa).
5. Run quick smoke checks:
   - imports and startup
   - payment status check path
   - premium reading path

## Deployment Notes

- CI/CD workflow deploys via SSH and docker compose.
- Keep `.env.example` and README in sync with required runtime env vars.
- Avoid introducing hidden runtime assumptions; document all new env vars.

