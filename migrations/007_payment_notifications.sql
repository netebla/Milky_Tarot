-- New payments enqueue both rows in the same transaction as their credit.
-- Do not backfill old succeeded payments: they were already announced.
CREATE TABLE IF NOT EXISTS payment_notifications (
    id SERIAL PRIMARY KEY,
    payment_id INTEGER NOT NULL REFERENCES payments(id) ON DELETE CASCADE,
    channel VARCHAR NOT NULL,
    event_type VARCHAR NOT NULL DEFAULT 'succeeded',
    sent_at TIMESTAMP NULL,
    claimed_until TIMESTAMP NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    CONSTRAINT uq_payment_notification_channel UNIQUE (payment_id, channel, event_type)
);
CREATE INDEX IF NOT EXISTS ix_payment_notifications_payment_id ON payment_notifications(payment_id);
CREATE INDEX IF NOT EXISTS ix_payment_notifications_claimed_until ON payment_notifications(claimed_until);
