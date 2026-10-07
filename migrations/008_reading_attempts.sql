CREATE TABLE IF NOT EXISTS reading_attempts (
    id VARCHAR(32) PRIMARY KEY,
    pending_reading_id VARCHAR(32) REFERENCES pending_readings(id) ON DELETE SET NULL,
    user_id INTEGER NOT NULL REFERENCES users(id),
    status VARCHAR NOT NULL DEFAULT 'generating',
    question TEXT NOT NULL,
    context TEXT NOT NULL DEFAULT '',
    card_titles TEXT NOT NULL,
    interpretation TEXT,
    price_fish INTEGER NOT NULL DEFAULT 0,
    reading_date DATE NOT NULL,
    lease_token VARCHAR(32),
    lease_expires_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT (NOW() AT TIME ZONE 'UTC')
);
CREATE INDEX IF NOT EXISTS ix_reading_attempts_user_id ON reading_attempts(user_id);
CREATE INDEX IF NOT EXISTS ix_reading_attempts_pending_reading_id ON reading_attempts(pending_reading_id);
CREATE INDEX IF NOT EXISTS ix_reading_attempts_status ON reading_attempts(status);
CREATE INDEX IF NOT EXISTS ix_reading_attempts_lease_expires_at ON reading_attempts(lease_expires_at);
