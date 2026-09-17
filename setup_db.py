import sqlite3
import json

DB_FILE = 'bot_database.db'
FEEDS_FILE = 'feeds.json'

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    # Створюємо таблицю джерел
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS sources (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        url TEXT UNIQUE NOT NULL,
        status TEXT DEFAULT 'ok',        -- 'ok', 'error', 'disabled'
        error_count INTEGER DEFAULT 0,
        last_checked TIMESTAMP,
        last_error_log TEXT
    )
    ''')

    # Створюємо таблицю для новин (щоб потім зберігати GUID)
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS articles (
        guid TEXT PRIMARY KEY,
        source_id INTEGER,
        title TEXT,
        status TEXT DEFAULT 'pending', -- 'pending', 'sent'
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(source_id) REFERENCES sources(id)
    )
    ''')
    
    conn.commit()
    print("Таблиці 'sources' та 'articles' успішно створено.")
    return conn

def import_from_json(conn):
    """Переносить джерела з feeds.json у SQLite (якщо вони ще не там)"""
    try:
        with open(FEEDS_FILE, 'r', encoding='utf-8') as f:
            feeds = json.load(f)
    except FileNotFoundError:
        print(f"Файл {FEEDS_FILE} не знайдено. Пропускаємо імпорт.")
        return

    cursor = conn.cursor()
    for feed in feeds:
        try:
            # INSERT OR IGNORE захищає від дублів при повторному запуску
            cursor.execute('''
            INSERT OR IGNORE INTO sources (name, url, status, error_count)
            VALUES (?, ?, ?, ?)
            ''', (feed.get('name', feed['url']), feed['url'], feed.get('status', 'ok'), feed.get('error_count', 0)))
        except Exception as e:
            print(f"Помилка при додаванні {feed['url']}: {e}")
            
    conn.commit()
    print("Джерела імпортовано з JSON в базу даних SQLite.")

if __name__ == "__main__":
    connection = init_db()
    import_from_json(connection)
    connection.close()