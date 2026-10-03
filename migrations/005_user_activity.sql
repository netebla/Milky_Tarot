-- Только реальные входящие сообщения и нажатия в основном боте.
-- Старое last_activity_date не переносим: его перезаписывали автоматические пуши.
CREATE TABLE IF NOT EXISTS user_activity (
    user_id BIGINT NOT NULL,
    activity_date DATE NOT NULL,
    PRIMARY KEY (user_id, activity_date)
);
CREATE INDEX IF NOT EXISTS ix_user_activity_activity_date ON user_activity (activity_date);
