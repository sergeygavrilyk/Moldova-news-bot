import os
import json
import psycopg2
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

def setup_database():
    if not DATABASE_URL:
        print("❌ Помилка: DATABASE_URL не знайдено у файлі .env")
        return

    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()

        # Створення таблиці sources
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

        # Створення таблиці articles
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS articles (
            guid TEXT PRIMARY KEY,
            source_id INTEGER REFERENCES sources(id),
            title TEXT,
            status VARCHAR(50) DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)

        # Базові джерела (якщо у вас є файл feeds.json)
        if os.path.exists('feeds.json'):
            with open('feeds.json', 'r', encoding='utf-8') as f:
                feeds = json.load(f)

            for source_name, feed_url in feeds.items():
                cursor.execute("""
                    INSERT INTO sources (name, url)
                    VALUES (%s, %s)
                    ON CONFLICT (url) DO NOTHING
                """, (source_name, feed_url))

        conn.commit()
        cursor.close()
        conn.close()
        print("✅ База даних PostgreSQL успішно налаштована на Supabase!")
        
    except Exception as e:
        print(f"❌ Помилка налаштування БД: {e}")

if __name__ == "__main__":
    setup_database()