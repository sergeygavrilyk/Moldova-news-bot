import datetime
import feedparser
import logging
import requests
import os
import time
import calendar
import json
import re
import random
import psycopg2
from urllib.parse import urlparse
from dotenv import load_dotenv
from google import genai
import cloudscraper
from tenacity import retry, stop_after_attempt, wait_exponential

feedparser.USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'

# ================= 1. НАЛАШТУВАННЯ =================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHANNEL_ID = os.getenv('TELEGRAM_CHANNEL_ID')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
ADMIN_CHAT_ID = os.getenv('ADMIN_CHAT_ID') 
DATABASE_URL = os.getenv('DATABASE_URL')

if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID, GEMINI_API_KEY, DATABASE_URL]):
    logging.error("Критична помилка: не знайдено базові токени або DATABASE_URL у файлі .env!")
    exit(1)

MAX_AGE_SECONDS = 43200 # 12 годин
BATCH_SIZE = 3
client = genai.Client(api_key=GEMINI_API_KEY)
# ===================================================

def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

def generate_hashtags(item):
    tags = []
    text_context = (item.get('title', '') + " " + item.get('description', '') + " " + item.get('source', '')).lower()
    if any(kw in text_context for kw in ['chisinau', 'chisinăului', 'кишинев']): tags.append('#Кишинів')
    if any(kw in text_context for kw in ['protectie', 'temporara', 'refugiati', 'azil', 'suport', 'беженц', 'защит', 'убежищ']): tags.append('#ТимчасовийЗахист')
    if any(kw in text_context for kw in ['ucraina', 'ukraine', 'kiev', 'zelenski', 'razboi', 'украин', 'киев', 'зеленск', 'войн']): tags.append('#Україна')
    return "\n\n" + " ".join(tags) if tags else ""

def send_analytics_to_admin(stats):
    if not ADMIN_CHAT_ID: return
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT 
                COALESCE(SUM(tokens_used) FILTER (WHERE timestamp >= NOW() - INTERVAL '1 day'), 0),
                COALESCE(SUM(tokens_used) FILTER (WHERE timestamp >= NOW() - INTERVAL '7 days'), 0),
                COALESCE(SUM(tokens_used) FILTER (WHERE timestamp >= NOW() - INTERVAL '30 days'), 0)
            FROM run_analytics
        """)
        tokens_day, tokens_week, tokens_month = cursor.fetchone()
        cursor.close()
        conn.close()
        
        scraped_src_str = ", ".join([f"{k}: {v}" for k, v in stats['funnel'].get('scraped_by_source', {}).items()]) if stats['funnel'].get('scraped_by_source') else "Немає"
        backlog_src_str = ", ".join([f"{k}: {v}" for k, v in stats['backlog']['sources'].items()]) if stats['backlog']['sources'] else "Порожньо"
        pub_src_str = ", ".join([f"{k}: {v}" for k, v in stats['published']['sources'].items()]) if stats['published']['sources'] else "Немає"

        message = (
            f"📊 <b>Розширений звіт бота (Молдова)</b>\n\n"
            f"⏱ Час: {stats['execution_time']} сек | ⚠️ Помилок: {stats['errors_count']} | 🔄 Ретрай: {stats['gemini_retries']}\n"
            f"🪙 <b>Токени (сьогодні/тиждень/місяць):</b>\n   {tokens_day:,} / {tokens_week:,} / {tokens_month:,}\n\n"
            f"🔍 <b>Воронка фільтрації:</b>\n▪️ Знайдено в RSS: {stats['funnel']['scraped_total']}\n▪️ По джерелах: {scraped_src_str}\n"
            f"▪️ Пропущено (старі/вже в історії): {stats['funnel']['already_seen']}\n▪️ Відсіяно фільтрами: {stats['funnel']['rejected_by_keywords']}\n"
            f"▪️ Відсіяно Gemini (дублі/історія): {stats['funnel']['duplicates_removed_by_ai']}\n\n"
            f"📥 <b>Беклог (очікують ШІ-валідації: {stats['backlog']['total_size']}):</b>\n▪️ {backlog_src_str}\n\n"
            f"✅ <b>Опубліковано: {stats['published']['total']}</b>\n"
            f"▪️ Формат: З фото ({stats['published']['with_images']}) | Текст ({stats['published']['text_only']})\n"
            f"▪️ Пріоритет 🇺🇦: {stats['published']['ukraine_priority']}\n▪️ Джерела: {pub_src_str}"
        )
        
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", 
                      data={'chat_id': ADMIN_CHAT_ID, 'text': message, 'parse_mode': 'HTML'})
    except Exception as e:
        logging.error(f"Помилка відправки статистики в Telegram: {e}")

def get_active_sources():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT id, name, url FROM sources WHERE status = 'ok'")
        sources = cursor.fetchall()
        cursor.close()
        conn.close()
        return sources
    except Exception as e:
        logging.error(f"Помилка читання PostgreSQL: {e}")
        return []

def extract_image_urls(entry):
    urls = []
    if 'media_content' in entry:
        for media in entry.media_content:
            url = media.get('url')
            if url and url not in urls: urls.append(url)
    if 'enclosures' in entry:
        for enc in entry.enclosures:
            if enc.get('type', '').startswith('image/'):
                url = enc.get('href')
                if url and url not in urls: urls.append(url)
    return urls[:10]

def is_quality_article(title, description, url):
    unwanted_url_categories = ['/lume/', '/sport/', '/divertisment/', '/lifestyle/', '/magazin/', '/showbiz/', '/horoscop/', '/fun/']
    if any(category in url.lower() for category in unwanted_url_categories): return False
    unwanted_keywords = ['publicitate', 'sponsorizat', 'promo', 'horoscop', 'anunturi', 'loterie', 'castiga', 'native', 'foto', 'video', 'sport', 'fotbal', 'meci', 'tenis', 'campionat', 'liga']
    text_to_check = f"{title} {description} {url}".lower()
    for word in unwanted_keywords:
        if re.search(rf'\b{word}\b', text_to_check): return False
    if len(title.strip()) < 15 or len(text_to_check.strip()) < 20: return False
    if "!!!" in title or "???" in title: return False
    letters = [c for c in title if c.isalpha()]
    if letters and (sum(1 for c in letters if c.isupper()) / len(letters)) > 0.4: return False
    return True

@retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=2, min=5, max=60))
def call_gemini_json(client, prompt):
    return client.models.generate_content(model='gemini-3.5-flash-lite', contents=prompt, config={"response_mime_type": "application/json"})

@retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=2, min=15, max=60))
def generate_news_texts(client, prompt, expected_count):
    response = client.models.generate_content(model='gemini-3.5-flash-lite', contents=prompt)
    clean_text = response.text.replace('ב', 'б').replace('є́', 'є').replace('і́', 'і')
    texts = [t.strip() for t in clean_text.strip().split('|||') if t.strip()]
    if len(texts) != expected_count: raise ValueError("Невірна кількість згенерованих текстів")
    return response, texts

@retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=2, min=3, max=30))
def execute_telegram_request(api_url, payload):
    resp = requests.post(api_url, data=payload)
    if resp.status_code == 400 and "parse entities" in resp.text.lower():
        if 'media' in payload:
            media_objs = json.loads(payload['media'])
            if 'parse_mode' in media_objs[0]: del media_objs[0]['parse_mode']
            payload['media'] = json.dumps(media_objs)
        elif 'parse_mode' in payload: del payload['parse_mode']
        resp = requests.post(api_url, data=payload) 
    if resp.status_code != 200: raise Exception(f"Telegram Error: {resp.text}")
    return True

def deduplicate_news(client, articles, recent_history=None, stats=None):
    if not articles: return articles
    logging.info("Аналіз на дублікати через Gemini...")
    history = recent_history[-75:] if recent_history else []
    prompt_data = [{"id": i, "title": a["title"], "description": a.get("description", "")} for i, a in enumerate(articles)]
    prompt = f"""Ти головний редактор. Відфільтруй дублікати.
    <CANDIDATES>{json.dumps(prompt_data, ensure_ascii=False, indent=2)}</CANDIDATES>
    <HISTORY>{json.dumps(history, ensure_ascii=False, indent=2)}</HISTORY>
    Поверни результат СУВОРО як масив JSON з ID новин, які треба ЗАЛИШИТИ. Більше ніякого тексту.
    """
    try:
        response = call_gemini_json(client, prompt)
        if stats is not None and getattr(response, 'usage_metadata', None): stats["tokens_used"] += response.usage_metadata.total_token_count
        raw_ids = json.loads(response.text)
        keep_ids = list(set([int(item['id']) if isinstance(item, dict) and 'id' in item else int(item) for item in raw_ids if str(item if not isinstance(item, dict) else item.get('id', '')).isdigit()]))
        filtered_articles = [articles[i] for i in keep_ids if i < len(articles)]
        if len(articles) - len(filtered_articles) > 0: logging.info(f"🗑️ Відсіяно ШІ: {len(articles) - len(filtered_articles)}")
        return filtered_articles
    except Exception as e:
        logging.error(f"❌ Помилка Gemini API: {e}")
        return []

def prioritize_ukraine_news(backlog_items):
    ukraine_keywords = [r'\bucrain', r'\bzelenski', r'\bkiev', r'\bkiiv', r'\bodesa', r'\bodessa', r'\brefugiat', r'\bprotectie temporara', r'\bprotecție temporară', r'\bprivremen\w*\s+protectie', r'\bукраин', r'\bзеленск', r'\bкиев', r'\bодесс', r'\bбеженц']
    ukraine_news = []
    other_news = []
    for item in backlog_items:
        if any(re.search(kw, (item.get('title', '') + " " + item.get('description', '')).lower()) for kw in ukraine_keywords): ukraine_news.append(item)
        else: other_news.append(item)
    return ukraine_news + other_news

def scrape_feeds(stats):
    conn = get_db_connection()
    cursor = conn.cursor()
    scraper = cloudscraper.create_scraper(browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False})
    
    active_sources = get_active_sources()
    if not active_sources:
        conn.close()
        return
        
    now = datetime.datetime.now()
    current_time = time.time()

    # Отримуємо наявні URL та заголовки з бази для перевірки дублікатів
    cursor.execute("SELECT url FROM articles")
    db_urls = {row[0] for row in cursor.fetchall()}
    
    cursor.execute("SELECT title FROM articles")
    db_titles = {row[0] for row in cursor.fetchall()}

    for source_id, source_name, feed_url in active_sources:
        if "scraped_by_source" not in stats["funnel"]: stats["funnel"]["scraped_by_source"] = {}
        source_count = 0  
        try: 
            response = scraper.get(feed_url, headers={'Accept': 'application/rss+xml, application/xml, text/xml, */*'}, timeout=15)
            if response.status_code not in [200, 301, 302]: raise Exception(f"HTTP {response.status_code}")
            feed = feedparser.parse(response.text)
            if not feed.entries: continue
            cursor.execute("UPDATE sources SET status = 'ok', error_count = 0, last_error_log = NULL, last_checked = %s WHERE id = %s", (now, source_id))
        except Exception as e: 
            cursor.execute("UPDATE sources SET error_count = error_count + 1, last_error_log = %s, last_checked = %s WHERE id = %s", (str(e), now, source_id))
            cursor.execute("SELECT error_count FROM sources WHERE id = %s", (source_id,))
            if cursor.fetchone()[0] >= 3: cursor.execute("UPDATE sources SET status = 'error' WHERE id = %s", (source_id,))
            conn.commit()
            continue 
            
        for entry in feed.entries[:15]: 
            stats["funnel"]["scraped_total"] += 1
            source_count += 1
            url = entry.link
            title = entry.title
            guid = getattr(entry, 'id', url)
            
            if url in db_urls or title in db_titles:
                stats["funnel"]["already_seen"] += 1
                continue
            
            article_time = time.time()
            if hasattr(entry, 'published_parsed') and entry.published_parsed:
                article_time = calendar.timegm(entry.published_parsed)
                if current_time - article_time > MAX_AGE_SECONDS:
                    stats["funnel"]["already_seen"] += 1
                    continue
                
            description = entry.get('summary', title)
            if not is_quality_article(title, description, url): 
                stats["funnel"]["rejected_by_keywords"] += 1
                continue

            images = extract_image_urls(entry)
            
            cursor.execute('''
                INSERT INTO articles (guid, source_id, title, description, url, image_urls, status)
                VALUES (%s, %s, %s, %s, %s, %s, 'pending')
                ON CONFLICT (guid) DO NOTHING
            ''', (guid, source_id, title, description, url, json.dumps(images)))
            
            db_urls.add(url)
            db_titles.add(title)
            
        stats["funnel"]["scraped_by_source"][source_name] = source_count
            
    conn.commit()
    cursor.close()        
    conn.close()

def process_backlog(stats):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT a.guid, a.title, a.description, a.url, a.image_urls, s.name, EXTRACT(EPOCH FROM a.created_at) as timestamp
        FROM articles a JOIN sources s ON a.source_id = s.id 
        WHERE a.status = 'pending' ORDER BY a.created_at DESC
    """)
    rows = cursor.fetchall()
    backlog = []
    for r in rows:
        backlog.append({
            'guid': r[0], 'title': r[1], 'description': r[2], 'url': r[3], 
            'image_urls': r[4] if r[4] else [], 'source': r[5], 'timestamp': r[6] or time.time()
        })
        
    stats["backlog"]["total_size"] = len(backlog)
    source_counts = {}
    for item in backlog: source_counts[item.get('source', 'Невідомо')] = source_counts.get(item.get('source', 'Невідомо'), 0) + 1
    stats["backlog"]["sources"] = source_counts

    if not backlog:
        conn.close()
        return

    ukraine_keywords = [r'\bucrain', r'\bzelenski', r'\bkiev', r'\bkiiv', r'\bodesa', r'\bodessa', r'\brefugiat', r'\bprotectie temporara', r'\bprotecție temporară', r'\bprivremen\w*\s+protectie', r'\bукраин', r'\bзеленск', r'\bкиев', r'\bодесс', r'\bбеженц']
    backlog.sort(key=lambda x: x.get('timestamp', 0), reverse=True)
    
    all_sources = list(dict.fromkeys(item['source'] for item in backlog))
    all_sources.sort()
    if not all_sources:
        conn.close()
        return
        
    ordered_sources = all_sources.copy()
    random.shuffle(ordered_sources) 
    
    candidate_batch = []
    used_urls = set()
    for item in prioritize_ukraine_news(backlog):
        if any(re.search(kw, (item.get('title', '') + " " + item.get('description', '')).lower()) for kw in ukraine_keywords):
            if item['url'] not in used_urls:
                candidate_batch.append(item)
                used_urls.add(item['url'])
                
    while len(candidate_batch) < 25:
        added_this_round = False
        for src in ordered_sources:
            for item in backlog:
                if item.get('source') == src and item['url'] not in used_urls:
                    candidate_batch.append(item)
                    used_urls.add(item['url'])
                    added_this_round = True
                    break 
            if len(candidate_batch) >= 25: break
        if not added_this_round: break 
            
    candidate_batch = candidate_batch[:25]
    if not candidate_batch:
        conn.close()
        return

    cursor.execute("SELECT title FROM articles WHERE status = 'sent' ORDER BY created_at DESC LIMIT 75")
    recent_history_titles = [row[0] for row in cursor.fetchall()]

    evaluated_urls = {item['url'] for item in candidate_batch}
    len_before = len(candidate_batch)
    candidate_batch = deduplicate_news(client, candidate_batch, recent_history_titles, stats)
    stats["funnel"]["duplicates_removed_by_ai"] += (len_before - len(candidate_batch))

    batch = candidate_batch[:BATCH_SIZE]
    if not batch:
        conn.close()
        return
        
    prompt = f"Переклади {len(batch)} новин українською. ПРАВИЛА: 1. Всі події в Молдові/світі. 2. Якщо про біженців - детально. 3. Пріоритет Україна - емодзі 🇺🇦. 4. ТІЛЬКИ українська. 5. HTML теги <b>, <i>, <u>, <s>. Розділяй `|||`.\n\n"
    for idx, item in enumerate(batch):
        has_image = "Так" if item.get('image_urls') else "Ні"
        prompt += f"=== НОВИНА {idx+1} ===\nКартинка: {has_image}\nЗаголовок: {item['title']}\nОпис: {item['description']}\n\n"
    
    time.sleep(15)
    try:
        response, generated_texts = generate_news_texts(client, prompt, len(batch))
        if getattr(response, 'usage_metadata', None): stats["tokens_used"] += response.usage_metadata.total_token_count
    except Exception as e:
        logging.error(f"❌ Помилка Gemini: {e}")
        stats["errors_count"] += 1 
        conn.close()
        return

    for idx, item in enumerate(batch):
        if idx >= len(generated_texts): break 
        tags = generate_hashtags(item)
        images = item.get('image_urls', [])
        
        if send_to_telegram(generated_texts[idx], item['url'], item.get('source', 'Джерело'), images, tags):
            try:
                cursor.execute("UPDATE articles SET status = 'sent' WHERE guid = %s", (item['guid'],))
                conn.commit()
                stats["published"]["total"] += 1
                if any(re.search(kw, (item.get('title', '') + " " + item.get('description', '')).lower()) for kw in ukraine_keywords): 
                    stats["published"]["ukraine_priority"] += 1
                if images: stats["published"]["with_images"] += 1
                else: stats["published"]["text_only"] += 1
                stats["published"]["sources"][item.get('source', 'Джерело')] = stats["published"]["sources"].get(item.get('source', 'Джерело'), 0) + 1
                
                time.sleep(45)
            except Exception as db_err: 
                logging.error(f"Помилка БД: {db_err}")
        else:
            stats["errors_count"] += 1

    conn.close()

def send_to_telegram(text, url, source_name, image_urls=None, tags=""):
    if not image_urls: image_urls = []
    elif isinstance(image_urls, str): image_urls = [image_urls]
    crop_msg = "...\n\n👉 <i>Деталі за посиланням нижче</i> 👇"
    safe_caption_limit = 920 - len(tags)
    
    if image_urls and len(text) > safe_caption_limit: text = text[:safe_caption_limit] + crop_msg
    elif not image_urls and len(text) > 3900: text = text[:3900] + crop_msg
    
    full_message = f"{text}{tags}\n\n🔗 Джерело: <a href='{url}'>{source_name}</a>"
    payload = {'chat_id': TELEGRAM_CHANNEL_ID}
    
    if len(image_urls) == 0:
        payload['parse_mode'] = 'HTML'; payload['text'] = full_message
        api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    elif len(image_urls) == 1:
        payload['parse_mode'] = 'HTML'; payload['caption'] = full_message; payload['photo'] = image_urls[0]
        api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    else:
        media = []
        for idx, img_url in enumerate(image_urls):
            media_item = {'type': 'photo', 'media': img_url}
            if idx == 0: media_item['caption'] = full_message; media_item['parse_mode'] = 'HTML'
            media.append(media_item)
        payload['media'] = json.dumps(media)
        api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMediaGroup"

    try: return execute_telegram_request(api_url, payload)
    except Exception as e: 
        logging.error(f"❌ Telegram API: {e}")
        return False

def main():
    start_time = time.time()
    stats = {
        "timestamp": time.strftime('%Y-%m-%d %H:%M:%S'), 
        "execution_time": 0, 
        "tokens_used": 0,
        "funnel": {"scraped_total": 0, "scraped_by_source": {}, "already_seen": 0, "rejected_by_keywords": 0, "duplicates_removed_by_ai": 0},
        "backlog": {"total_size": 0, "sources": {}}, 
        "published": {"total": 0, "ukraine_priority": 0, "with_images": 0, "text_only": 0, "sources": {}},
        "errors_count": 0, "gemini_retries": 0
    }
    
    try:
        scrape_feeds(stats)
        process_backlog(stats)
    except Exception as e:
        logging.error(f"Критична помилка: {e}")
        stats["errors_count"] += 1
        if ADMIN_CHAT_ID:
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", 
                          data={'chat_id': ADMIN_CHAT_ID, 'text': f"⚠️ ALARM: Скрипт впав з помилкою: {e}"})
    finally:
        stats["execution_time"] = round(time.time() - start_time, 1)
        
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO run_analytics (execution_time, tokens_used, scraped_total, duplicates_removed, published_total, errors_count)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (stats["execution_time"], stats["tokens_used"], stats["funnel"]["scraped_total"], stats["funnel"]["duplicates_removed_by_ai"], stats["published"]["total"], stats["errors_count"]))
            
            cursor.execute("DELETE FROM articles WHERE created_at <= NOW() - INTERVAL '30 days'")
            
            conn.commit()
            cursor.close()
            conn.close()
        except Exception as e:
            logging.error(f"Помилка збереження аналітики в БД: {e}")
            
        send_analytics_to_admin(stats)

if __name__ == "__main__":
    main()