import os
import json
import psycopg2
from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")

def migrate_data():
    if not DATABASE_URL:
        print("❌ Помилка: DATABASE_URL не знайдено у файлі .env")
        return

    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()

        # 1. Міграція історії (опубліковані новини)
        history_urls = []
        history_titles = []
        
        if os.path.exists('posted_news.txt'):
            with open('posted_news.txt', 'r', encoding='utf-8') as f:
                history_urls = [line.strip() for line in f if line.strip()]
                
        if os.path.exists('posted_titles.txt'):
            with open('posted_titles.txt', 'r', encoding='utf-8') as f:
                history_titles = [line.strip() for line in f if line.strip()]

        print(f"🔄 Знайдено {len(history_urls)} посилань та {len(history_titles)} заголовків в історії.")
        
        history_count = 0
        for i in range(len(history_urls)):
            url = history_urls[i]
            title = history_titles[i] if i < len(history_titles) else "Без заголовка"
            guid = url # Для старих записів використовуємо URL як унікальний ідентифікатор
            
            cursor.execute("""
                INSERT INTO articles (guid, title, url, status)
                VALUES (%s, %s, %s, 'sent')
                ON CONFLICT (guid) DO NOTHING
            """, (guid, title, url))
            history_count += cursor.rowcount

        # 2. Міграція беклогу (новини в черзі)
        backlog_count = 0
        if os.path.exists('news_backlog.json'):
            with open('news_backlog.json', 'r', encoding='utf-8') as f:
                backlog = json.load(f)
                print(f"🔄 Знайдено {len(backlog)} новин у беклозі.")
                
                for item in backlog:
                    guid = item.get('guid', item.get('url'))
                    title = item.get('title', '')
                    description = item.get('description', '')
                    url = item.get('url', '')
                    image_urls = json.dumps(item.get('image_urls', []))
                    
                    cursor.execute("""
                        INSERT INTO articles (guid, title, description, url, image_urls, status)
                        VALUES (%s, %s, %s, %s, %s, 'pending')
                        ON CONFLICT (guid) DO NOTHING
                    """, (guid, title, description, url, image_urls))
                    backlog_count += cursor.rowcount

        conn.commit()
        print(f"✅ Міграція успішна! Записано в базу — Історія: {history_count} | Беклог: {backlog_count}")
        
    except Exception as e:
        print(f"❌ Помилка міграції: {e}")
    finally:
        if 'cursor' in locals(): cursor.close()
        if 'conn' in locals(): conn.close()

if __name__ == "__main__":
    migrate_data()