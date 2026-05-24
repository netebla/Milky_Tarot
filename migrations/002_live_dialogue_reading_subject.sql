-- Субъект расклада (на кого читаем карты) для живого диалога
ALTER TABLE dialogue_sessions ADD COLUMN IF NOT EXISTS reading_subject VARCHAR;
