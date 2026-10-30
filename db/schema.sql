-- =====================================================================
-- Moldova News Bot — фактична схема БД (Supabase, PostgreSQL 17)
-- Знята з проєкту mguksatdxvssywdbbkii 30.10.2026.
--
-- УВАГА: setup_db.py створює стару версію таблиці articles —
-- без колонок rejection_reason та embedding і без розширення pgvector.
-- Для розгортання з нуля виконайте цей файл у Supabase -> SQL Editor
-- (або перенесіть ці зміни в setup_db.py). Скрипт ідемпотентний.
-- =====================================================================

-- pgvector: тип vector та оператор косинусної відстані <=>
CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------------
-- RSS-джерела. main.py читає лише рядки зі status = 'ok'.
-- Після 3 помилок поспіль статус стає 'error' і джерело
-- більше не опитується, доки його не скинуть вручну (див. RUNBOOK).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sources (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(255) NOT NULL,
    url             TEXT UNIQUE NOT NULL,
    status          VARCHAR(50) DEFAULT 'ok',      -- 'ok' | 'error'
    error_count     INTEGER DEFAULT 0,
    last_checked    TIMESTAMP,
    last_error_log  TEXT
);

-- ---------------------------------------------------------------------
-- Статті. Життєвий цикл status:
--   pending    -> зібрано з RSS, чекає на Gemini-редактора
--   processing -> обрано в партію на переклад/публікацію
--   sent       -> опубліковано в канал
--   failed     -> Telegram не прийняв повідомлення
--   rejected   -> відхилено Gemini (причина в rejection_reason)
--   expired    -> новині понад 12 годин від публікації на сайті
--                 (COALESCE(published_at, created_at), з 30.10.2026 — D-14)
-- Статті старші за 30 днів видаляються наприкінці кожного запуску.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS articles (
    guid              TEXT PRIMARY KEY,               -- entry.id з RSS або URL
    source_id         INTEGER REFERENCES sources(id),
    title             TEXT,
    description       TEXT,
    url               TEXT,
    image_urls        JSONB,                          -- до 10 URL картинок (без webp/avif/svg)
    status            VARCHAR(50) DEFAULT 'pending',
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,  -- час UTC
    rejection_reason  TEXT,
    embedding         vector,                         -- gemini-embedding-001, 3072 виміри
    published_at      TIMESTAMP                       -- час публікації на сайті (UTC), з RSS; NULL — дати немає (D-14)
);

ALTER TABLE articles ADD COLUMN IF NOT EXISTS rejection_reason TEXT;
ALTER TABLE articles ADD COLUMN IF NOT EXISTS embedding vector;
ALTER TABLE articles ADD COLUMN IF NOT EXISTS published_at TIMESTAMP;

-- ---------------------------------------------------------------------
-- Аналітика запусків (по рядку на кожен запуск main.py).
-- Звідси бот рахує токени за день/тиждень/місяць для звіту адміну.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS run_analytics (
    id                  SERIAL PRIMARY KEY,
    timestamp           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    execution_time      REAL,
    tokens_used         INTEGER,
    scraped_total       INTEGER,
    duplicates_removed  INTEGER,
    published_total     INTEGER,
    errors_count        INTEGER
);

-- ---------------------------------------------------------------------
-- Статистика викликів моделей Gemini (T-22, з 30.10.2026): рядок на кожну спробу.
--   task:    filter | translate
--   outcome: ok | quota (429) | unavailable (5xx/перевантаження) | timeout |
--            bad_response (порожня / неправильна кількість текстів) | error
-- Тестові запуски (DRY_RUN) сюди не пишуть. Рядки старші за 90 днів видаляються.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS model_calls (
    id          SERIAL PRIMARY KEY,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,   -- UTC
    task        VARCHAR(20),
    model       VARCHAR(64),
    outcome     VARCHAR(20),
    http_code   INTEGER,
    latency_ms  INTEGER,
    error       TEXT                                    -- перші 300 символів помилки
);

-- ---------------------------------------------------------------------
-- RLS увімкнено на всіх трьох таблицях без жодної політики:
-- через публічний REST API (anon/authenticated ключі) даних не видно.
-- Бот ходить напряму через DATABASE_URL (роль postgres) і RLS обходить.
-- ---------------------------------------------------------------------
ALTER TABLE sources       ENABLE ROW LEVEL SECURITY;
ALTER TABLE articles      ENABLE ROW LEVEL SECURITY;
ALTER TABLE run_analytics ENABLE ROW LEVEL SECURITY;
ALTER TABLE model_calls   ENABLE ROW LEVEL SECURITY;

-- ---------------------------------------------------------------------
-- Рекомендовані індекси (ЗАРАЗ ВІДСУТНІ; при ~3 тис. рядків не критично).
-- Розкоментуйте, якщо таблиця articles суттєво виросте.
-- ---------------------------------------------------------------------
-- CREATE INDEX IF NOT EXISTS articles_status_created_idx ON articles (status, created_at);
-- CREATE INDEX IF NOT EXISTS articles_created_at_idx     ON articles (created_at);
-- Примітка: HNSW/IVFFlat-індекс pgvector для типу vector підтримує до 2000 вимірів.
-- Для 3072 потрібен halfvec або зменшення розмірності ембединга (output_dimensionality).
