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

CREATE INDEX IF NOT EXISTS ix_product_prices_kind ON product_prices (kind);

INSERT INTO product_prices (code, title, kind, amount_rub, fish_amount, bonus_fish)
VALUES
    ('tariff_1', 'Тариф 1', 'tariff', 150, 350, 0),
    ('tariff_2', 'Тариф 2', 'tariff', 250, 1050, 150),
    ('tariff_3', 'Тариф 3', 'tariff', 450, 2100, 400),
    ('tariff_4', 'Тариф 4', 'tariff', 950, 4550, 1000),
    ('three_keys', 'Задать свой вопрос', 'service', NULL, 69, 0),
    ('live_dialogue', 'Живой диалог', 'service', NULL, 150, 0)
ON CONFLICT (code) DO NOTHING;
