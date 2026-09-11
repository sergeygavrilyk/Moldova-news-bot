import requests

feeds = [
  {
    "name": "Newsmaker",
    "url": "https://newsmaker.md/feed/",
    "category": "Регіональні"
  },
  {
    "name": "IPN",
    "url": "https://ipn.md/en/rss",
    "category": "Регіональні"
  },
{
    "name": "Point MD",
    "url": "https://point.md/ru/rss/novosti/",
    "category": "Регіональні"
  }

]

# Імітуємо запит від звичайного браузера
headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'application/rss+xml, application/xml, text/xml, */*'
}

print("Починаємо перевірку RSS-посилань...\n" + "-"*50)

working_feeds = []

for feed in feeds:
    try:
        response = requests.get(feed['url'], headers=headers, timeout=10)
        if response.status_code == 200:
            print(f"✅ [200 OK] Працює: {feed['name']}")
            working_feeds.append(feed)
        elif response.status_code == 403:
            print(f"🛡️ [403 Forbidden] Блокує ботів: {feed['name']}")
        elif response.status_code == 404:
            print(f"❌ [404 Not Found] Не існує: {feed['name']} (перевірте URL)")
        else:
            print(f"⚠️ [{response.status_code}] Інша помилка: {feed['name']}")
    except Exception as e:
        print(f"❌ Збій підключення: {feed['name']} - {type(e).__name__}")

print("-"*50)
print(f"Робочих посилань: {len(working_feeds)} з {len(feeds)}")