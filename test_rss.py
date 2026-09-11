import requests

feeds = [
  {"name": "Jutarnji List", "url": "https://www.jutarnji.hr/rss", "category": "Національні"},
  {"name": "Večernji List", "url": "https://www.vecernji.hr/rss/vijesti-hrvatska", "category": "Національні"},
  {"name": "Dnevnik.hr", "url": "https://dnevnik.hr/rss/feeds/vijesti.xml", "category": "Національні"},
  {"name": "Nacional", "url": "https://www.nacional.hr/feed/", "category": "Національні"},
  {"name": "HRT Vijesti", "url": "https://vijesti.hrt.hr/feed", "category": "Національні"},
  {"name": "Net.hr", "url": "https://net.hr/feed/", "category": "Національні"},
  {"name": "Tportal.hr", "url": "https://www.tportal.hr/rss/vijesti.xml", "category": "Національні"},
  {"name": "Poslovni - Ekonomija", "url": "https://www.poslovni.hr/feed/gospodarstvo", "category": "Економіка"},
  {"name": "ZADAR News", "url": "https://www.zadarnews.hr/rss/", "category": "Регіональні"},
  {"name": "Zadarski List", "url": "https://www.novilist.hr/feed/?post_type=zadarski-list", "category": "Регіональні"},
  {"name": "057info", "url": "https://www.057info.hr/feed/", "category": "Регіональні"},
  {"name": "Slobodna Dalmacija", "url": "https://slobodnadalmacija.hr/feed", "category": "Регіональні"},
  {"name": "Večernji List - Кримінал", "url": "https://www.vecernji.hr/rss/vijesti-crna-kronika", "category": "Надзвичайні події"}
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