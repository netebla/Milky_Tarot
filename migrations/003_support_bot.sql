-- Отдельный бот поддержки: обращения и маршрутизация ответов администраторов.
-- Миграция идемпотентна и автоматически применяется при деплое.

CREATE TABLE IF NOT EXISTS support_tickets (
    id SERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    username VARCHAR,
    display_name VARCHAR,
    status VARCHAR NOT NULL DEFAULT 'open',
    assigned_admin_id BIGINT,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
    closed_at TIMESTAMP WITHOUT TIME ZONE
);

CREATE INDEX IF NOT EXISTS ix_support_tickets_user_id ON support_tickets (user_id);
CREATE INDEX IF NOT EXISTS ix_support_tickets_status ON support_tickets (status);
CREATE INDEX IF NOT EXISTS ix_support_tickets_assigned_admin_id ON support_tickets (assigned_admin_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_support_tickets_one_open_per_user
    ON support_tickets (user_id) WHERE status = 'open';

CREATE TABLE IF NOT EXISTS support_messages (
    id SERIAL PRIMARY KEY,
    ticket_id INTEGER NOT NULL REFERENCES support_tickets(id) ON DELETE CASCADE,
    sender_role VARCHAR NOT NULL,
    sender_id BIGINT NOT NULL,
    telegram_message_id BIGINT,
    content_type VARCHAR NOT NULL DEFAULT 'unknown',
    preview TEXT,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_support_messages_ticket_id ON support_messages (ticket_id);

CREATE TABLE IF NOT EXISTS support_relays (
    id SERIAL PRIMARY KEY,
    ticket_id INTEGER NOT NULL REFERENCES support_tickets(id) ON DELETE CASCADE,
    admin_id BIGINT NOT NULL,
    admin_message_id BIGINT NOT NULL,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_support_relays_ticket_id ON support_relays (ticket_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_support_relays_admin_message
    ON support_relays (admin_id, admin_message_id);
