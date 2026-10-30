import os
import json
import psycopg2
from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")

def setup_database():
    if not DATABASE_URL:
        print("❌ DATABASE_URL не знайдено.")
        return

    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()

        # pgvector: тип vector для ембедингів (семантичні дублі). Окремою транзакцією:
        # якщо роль не має прав на CREATE EXTENSION, решта схеми все одно створиться.
        try:
            cursor.execute("CREATE EXTENSION IF NOT EXISTS vector")
            conn.commit()
        except Exception as ext_err:
            conn.rollback()
            print(f"⚠️ Не вдалося увімкнути розширення vector: {ext_err}")

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS sources (
            id SERIAL PRIMARY KEY,
            name VARCHAR(255) NOT NULL,
            url TEXT UNIQUE NOT NULL,
            status VARCHAR(50) DEFAULT 'ok',
            error_count INTEGER DEFAULT 0,
            last_checked TIMESTAMP,
            last_error_log TEXT
        )
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS articles (
            guid TEXT PRIMARY KEY,
            source_id INTEGER REFERENCES sources(id),
            title TEXT,
            description TEXT,
            url TEXT,
            image_urls JSONB,
            status VARCHAR(50) DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            rejection_reason TEXT,
            embedding vector,
            published_at TIMESTAMP
        )
        """)

        # Для баз, створених старою версією скрипта (без цих колонок)
        cursor.execute("ALTER TABLE articles ADD COLUMN IF NOT EXISTS rejection_reason TEXT")
        cursor.execute("ALTER TABLE articles ADD COLUMN IF NOT EXISTS embedding vector")
        cursor.execute("ALTER TABLE articles ADD COLUMN IF NOT EXISTS published_at TIMESTAMP")

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS model_calls (
            id SERIAL PRIMARY KEY,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            task VARCHAR(20),
            model VARCHAR(64),
            outcome VARCHAR(20),
            http_code INTEGER,
            latency_ms INTEGER,
            error TEXT
        )
        """)
        cursor.execute("ALTER TABLE model_calls ENABLE ROW LEVEL SECURITY")

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS run_analytics (
            id SERIAL PRIMARY KEY,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            execution_time REAL,
            tokens_used INTEGER,
            scraped_total INTEGER,
            duplicates_removed INTEGER,
            published_total INTEGER,
            errors_count INTEGER
        )
        """)

        # Розумне завантаження джерел
        if os.path.exists('feeds.json'):
            with open('feeds.json', 'r', encoding='utf-8') as f:
                feeds = json.load(f)
            
            # Якщо це словник {"Назва": "URL"}
            if isinstance(feeds, dict):
                items = feeds.items()
            # Якщо це список
            elif isinstance(feeds, list):
                items = []
                for item in feeds:
                    if isinstance(item, str): 
                        items.append(("Джерело", item))
                    elif isinstance(item, dict):
                        items.append((item.get("name", "Джерело"), item.get("url", "")))
            else:
                items = []

            for source_name, feed_url in items:
                if not feed_url: continue
                cursor.execute("""
                    INSERT INTO sources (name, url) VALUES (%s, %s)
                    ON CONFLICT (url) DO NOTHING
                """, (source_name, feed_url))

        conn.commit()
        cursor.close()
        conn.close()
        print("✅ Хмарна БД PostgreSQL повністю налаштована!")
    except Exception as e:
        print(f"❌ Помилка БД: {e}")

if __name__ == "__main__":
    setup_database()