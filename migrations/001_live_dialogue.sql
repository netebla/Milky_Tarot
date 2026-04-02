-- Живой диалог: новые таблицы и поля users (PostgreSQL)
-- Применить вручную: psql $DATABASE_URL -f migrations/001_live_dialogue.sql

ALTER TABLE users ADD COLUMN IF NOT EXISTS live_dialogue_last_date DATE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS live_dialogue_daily_count INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS dialogue_sessions (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    phase VARCHAR NOT NULL DEFAULT 'collecting_context',
    spread_type VARCHAR,
    spread_positions JSONB,
    pending_spreads JSONB,
    fish_cost INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW(),
    completed_at TIMESTAMP WITHOUT TIME ZONE
);

CREATE INDEX IF NOT EXISTS ix_dialogue_sessions_user_completed
    ON dialogue_sessions (user_id, completed_at);

CREATE TABLE IF NOT EXISTS dialogue_messages (
    id SERIAL PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES dialogue_sessions(id) ON DELETE CASCADE,
    role VARCHAR NOT NULL,
    content TEXT NOT NULL,
    tool_name VARCHAR,
    tool_result JSONB,
    model_function_calls JSONB,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_dialogue_messages_session_created
    ON dialogue_messages (session_id, created_at);

CREATE TABLE IF NOT EXISTS drawn_cards (
    id SERIAL PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES dialogue_sessions(id) ON DELETE CASCADE,
    position_name VARCHAR NOT NULL,
    card_name VARCHAR NOT NULL,
    is_reversed BOOLEAN NOT NULL DEFAULT FALSE,
    drawn_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS user_memory (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    session_id INTEGER REFERENCES dialogue_sessions(id) ON DELETE SET NULL,
    memory_type VARCHAR NOT NULL,
    content TEXT NOT NULL,
    is_resolved BOOLEAN NOT NULL DEFAULT FALSE,
    session_date DATE,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_user_memory_user_created ON user_memory (user_id, created_at);

-- Если таблица уже создана без колонки (повторный запуск миграции):
ALTER TABLE dialogue_messages ADD COLUMN IF NOT EXISTS model_function_calls JSONB;
ALTER TABLE dialogue_sessions ADD COLUMN IF NOT EXISTS pending_spreads JSONB;
