-- Редактируемые цены для отдельного админ-бота.
CREATE TABLE IF NOT EXISTS product_prices (
    code VARCHAR PRIMARY KEY,
    title VARCHAR NOT NULL,
    kind VARCHAR NOT NULL,
    amount_rub INTEGER,
    fish_amount INTEGER NOT NULL,
    bonus_fish INTEGER NOT NULL DEFAULT 0,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
);

-- init_db() may create this table first through SQLAlchemy, whose defaults are
-- client-side and therefore do not apply to inserts executed by this migration.
ALTER TABLE product_prices ALTER COLUMN bonus_fish SET DEFAULT 0;
ALTER TABLE product_prices ALTER COLUMN active SET DEFAULT TRUE;
ALTER TABLE product_prices ALTER COLUMN updated_at SET DEFAULT NOW();

CREATE INDEX IF NOT EXISTS ix_product_prices_kind ON product_prices (kind);

INSERT INTO product_prices (code, title, kind, amount_rub, fish_amount, bonus_fish, active, updated_at)
VALUES
    ('tariff_1', 'Тариф 1', 'tariff', 150, 350, 0, TRUE, NOW()),
    ('tariff_2', 'Тариф 2', 'tariff', 250, 1050, 150, TRUE, NOW()),
    ('tariff_3', 'Тариф 3', 'tariff', 450, 2100, 400, TRUE, NOW()),
    ('tariff_4', 'Тариф 4', 'tariff', 950, 4550, 1000, TRUE, NOW()),
    ('three_keys', 'Задать свой вопрос', 'service', NULL, 69, 0, TRUE, NOW()),
    ('live_dialogue', 'Живой диалог', 'service', NULL, 150, 0, TRUE, NOW())
ON CONFLICT (code) DO NOTHING;
