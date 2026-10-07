CREATE TABLE IF NOT EXISTS pending_readings (
    id VARCHAR(32) PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    question TEXT NOT NULL,
    context TEXT NOT NULL DEFAULT '',
    card_titles TEXT NOT NULL,
    status VARCHAR NOT NULL DEFAULT 'pending',
    created_at TIMESTAMP NOT NULL DEFAULT (NOW() AT TIME ZONE 'UTC'),
    expires_at TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_pending_readings_user_id ON pending_readings(user_id);
CREATE INDEX IF NOT EXISTS ix_pending_readings_status ON pending_readings(status);
CREATE INDEX IF NOT EXISTS ix_pending_readings_expires_at ON pending_readings(expires_at);
