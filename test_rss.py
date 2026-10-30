"""Швидка перевірка: чи відповідають RSS-посилання з feeds.json.

Нічого не публікує й не пише в БД. Детальніша оцінка джерел (свіжість, фото, фільтр
якості) — tools/check_feeds.py або GitHub Actions -> «Check RSS feeds».
Запуск: python test_rss.py [feeds.json]
"""
import json
import os
import sys

import requests

FEEDS_PATH = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "feeds.json")

# Імітуємо запит від звичайного браузера
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'application/rss+xml, application/xml, text/xml, */*'
}


def main():
    with open(FEEDS_PATH, encoding="utf-8") as fh:
        feeds = json.load(fh)

    print("Починаємо перевірку RSS-посилань...\n" + "-" * 50)
    working = 0
    for feed in feeds:
        try:
            response = requests.get(feed['url'], headers=HEADERS, timeout=10)
            if response.status_code == 200:
                print(f"✅ [200 OK] Працює: {feed['name']}")
                working += 1
            elif response.status_code == 403:
                print(f"🛡️ [403 Forbidden] Блокує ботів: {feed['name']}")
            elif response.status_code == 404:
                print(f"❌ [404 Not Found] Не існує: {feed['name']} (перевірте URL)")
            else:
                print(f"⚠️ [{response.status_code}] Інша помилка: {feed['name']}")
        except Exception as e:
            print(f"❌ Збій підключення: {feed['name']} - {type(e).__name__}")

    print("-" * 50)
    print(f"Робочих посилань: {working} з {len(feeds)}")


if __name__ == "__main__":
    main()
