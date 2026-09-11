import cloudscraper
import feedparser
from config import RSS_FEEDS, PYTHON_STOP_WORDS

def fetch_rss_feed(feed_url):
    """Стійкий парсинг RSS із захистом від Cloudflare через cloudscraper."""
    scraper = cloudscraper.create_scraper(
        browser={'browser': 'firefox', 'platform': 'windows', 'desktop': True}
    )
    try:
        response = scraper.get(feed_url, timeout=15)
        if response.status_code == 200:
            feed = feedparser.parse(response.text)
            return feed.entries
        else:
            print(f"Помилка завантаження {feed_url}: статус {response.status_code}")
            return []
    except Exception as e:
        print(f"Виняток при запиті до {feed_url}: {e}")
        return []

def filter_by_url(entries):
    """Дворівнева фільтрація: Рівень Python (відсікання за стоп-словами в URL)."""
    # Ізоляція змінних області видимості (Scope)
    filtered_entries = []
    
    for entry in entries:
        link = getattr(entry, 'link', '')
        if any(stop_word in link for stop_word in PYTHON_STOP_WORDS):
            continue
        filtered_entries.append(entry)
        
    return filtered_entries