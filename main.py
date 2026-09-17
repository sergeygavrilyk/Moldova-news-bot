import datetime
import feedparser
import logging
import requests
import os
import time
import calendar
import json
import re
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
    """Допоміжна функція для підключення до БД"""
    return psycopg2.connect(DATABASE_URL)

def generate_hashtags(title, description):
    tags = []
    text_context = (title + " " + description).lower()
    if any(kw in text_context for kw in ['chisinau', 'chisinăului', 'кишинев']): tags.append('#Кишинів')
    if any(kw in text_context for kw in ['protectie', 'temporara', 'refugiati', 'azil', 'супорт', 'беженц', 'защит', 'убежищ']): tags.append('#ТимчасовийЗахист')
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
        
        message = (
            f"📊 <b>Звіт бота (Supabase)</b>\n\n"
            f"✅ Опубліковано: {stats.get('published_total', 0)}\n"
            f"🔍 Знайдено в RSS: {stats.get('scraped_total', 0)}\n"
            f"🗑 Відсіяно ШІ (дублі): {stats.get('duplicates_removed', 0)}\n"
            f"⚠️ Помилок: {stats.get('errors_count', 0)}\n\n"
            f"🪙 <b>Токени:</b>\n"
            f"▪️ Сьогодні: {tokens_day:,}\n"
            f"▪️ Тиждень: {tokens_week:,}\n"
            f"▪️ Місяць: {tokens_month:,}"
        )
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", 
                      data={'chat_id': ADMIN_CHAT_ID, 'text': message, 'parse_mode': 'HTML'})
    except Exception as e:
        logging.error(f"Помилка відправки статистики в Telegram: {e}")

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

def scrape_feeds(stats):
    conn = get_db_connection()
    cursor = conn.cursor()
    scraper = cloudscraper.create_scraper(browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False})
    
    cursor.execute("SELECT id, name, url FROM sources WHERE status = 'ok'")
    sources = cursor.fetchall()
    now = datetime.datetime.now()

    for source_id, source_name, feed_url in sources:
        try: 
            response = scraper.get(feed_url, headers={'Accept': 'application/rss+xml, application/xml, text/xml, */*'}, timeout=15)
            if response.status_code not in [200, 301, 302]: continue
            feed = feedparser.parse(response.text)
            if not feed.entries: continue
            
            cursor.execute("UPDATE sources SET error_count = 0, last_error_log = NULL, last_checked = %s WHERE id = %s", (now, source_id))
        except Exception as e: 
            cursor.execute("UPDATE sources SET error_count = error_count + 1, last_error_log = %s, last_checked = %s WHERE id = %s", (str(e), now, source_id))
            continue 
            
        for entry in feed.entries[:15]: 
            stats["scraped_total"] += 1
            url = entry.link
            title = entry.title
            guid = getattr(entry, 'id', url)
            description = entry.get('summary', title)
            
            if not is_quality_article(title, description, url): continue
            
            article_time = time.time()
            if hasattr(entry, 'published_parsed') and entry.published_parsed:
                article_time = calendar.timegm(entry.published_parsed)
                if time.time() - article_time > MAX_AGE_SECONDS: continue
                
            images = extract_image_urls(entry)
            
            # Якщо новина вже є в базі (неважливо, pending чи sent), вона просто ігнорується
            cursor.execute('''
                INSERT INTO articles (guid, source_id, title, description, url, image_urls, status)
                VALUES (%s, %s, %s, %s, %s, %s, 'pending')
                ON CONFLICT (guid) DO NOTHING
            ''', (guid, source_id, title, description, url, json.dumps(images)))
            
    conn.commit()
    cursor.close()        
    conn.close()

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

    try: 
        return execute_telegram_request(api_url, payload)
    except Exception as e: 
        logging.error(f"❌ Telegram API: {e}")
        return False

def process_backlog(stats):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Витягуємо новини в очікуванні
    cursor.execute("""
        SELECT a.guid, a.title, a.description, a.url, a.image_urls, s.name 
        FROM articles a JOIN sources s ON a.source_id = s.id 
        WHERE a.status = 'pending' ORDER BY a.created_at DESC LIMIT 25
    """)
    candidates = cursor.fetchall()
    
    if not candidates:
        conn.close()
        return

    # Беремо останні 75 відправлених новин для перевірки дублікатів контексту
    cursor.execute("SELECT title FROM articles WHERE status = 'sent' ORDER BY created_at DESC LIMIT 75")
    recent_history = [row[0] for row in cursor.fetchall()]
    
    prompt_data = [{"id": idx, "title": c[1]} for idx, c in enumerate(candidates)]
    prompt = f"Відфільтруй дублікати. Кандидати: {json.dumps(prompt_data, ensure_ascii=False)}. Історія: {json.dumps(recent_history, ensure_ascii=False)}. Поверни JSON масив з ID новин, які треба ЗАЛИШИТИ. Більше ніякого тексту."
    
    try:
        filter_resp = client.models.generate_content(model='gemini-3.5-flash-lite', contents=prompt, config={"response_mime_type": "application/json"})
        keep_ids = json.loads(filter_resp.text)
        if getattr(filter_resp, 'usage_metadata', None): stats["tokens_used"] += filter_resp.usage_metadata.total_token_count
        
        # Залишаємо тільки ті, що пройшли ШІ-фільтр, обмежуємо до BATCH_SIZE
        batch = [candidates[int(i)] for i in keep_ids if int(i) < len(candidates)][:BATCH_SIZE]
        stats["duplicates_removed"] += (len(candidates) - len(batch))
    except Exception as e:
        logging.error(f"Помилка фільтрації Gemini: {e}")
        conn.close()
        return

    if not batch:
        conn.close()
        return
        
    translate_prompt = f"Переклади {len(batch)} новин українською. ПРАВИЛА: 1. Всі події в Молдові/світі. 2. Якщо про біженців - детально. 3. Пріоритет Україна - емодзі 🇺🇦. 4. ТІЛЬКИ українська. 5. HTML теги <b>, <i>, <u>, <s>. Розділяй `|||`.\n\n"
    for idx, c in enumerate(batch):
        translate_prompt += f"=== НОВИНА {idx+1} ===\nЗаголовок: {c[1]}\nОпис: {c[2]}\n\n"
        
    time.sleep(15)
    try:
        gen_resp, generated_texts = generate_news_texts(client, translate_prompt, len(batch))
        if getattr(gen_resp, 'usage_metadata', None): stats["tokens_used"] += gen_resp.usage_metadata.total_token_count
    except Exception as e:
        logging.error(f"Помилка генерації Gemini: {e}")
        stats["errors_count"] += 1
        conn.close()
        return

    for idx, c in enumerate(batch):
        if idx >= len(generated_texts): break
        guid, title, description, url, images, source_name = c
        tags = generate_hashtags(title, description)
        
        if send_to_telegram(generated_texts[idx], url, source_name, images, tags):
            try:
                cursor.execute("UPDATE articles SET status = 'sent' WHERE guid = %s", (guid,))
                conn.commit()
                stats["published_total"] += 1
                time.sleep(45)
            except Exception as db_err:
                logging.error(f"Помилка оновлення статусу в БД: {db_err}")
        else:
            stats["errors_count"] += 1

    cursor.close()
    conn.close()

def main():
    start_time = time.time()
    stats = {
        "scraped_total": 0, 
        "duplicates_removed": 0, 
        "published_total": 0, 
        "errors_count": 0, 
        "tokens_used": 0
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
        exec_time = round(time.time() - start_time, 1)
        
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO run_analytics (execution_time, tokens_used, scraped_total, duplicates_removed, published_total, errors_count)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (exec_time, stats["tokens_used"], stats["scraped_total"], stats["duplicates_removed"], stats["published_total"], stats["errors_count"]))
            
            cursor.execute("DELETE FROM articles WHERE created_at <= NOW() - INTERVAL '30 days'")
            
            conn.commit()
            cursor.close()
            conn.close()
        except Exception as e:
            logging.error(f"Помилка збереження аналітики в БД: {e}")
            
        if stats["published_total"] > 0 or stats["errors_count"] > 0:
            send_analytics_to_admin(stats)

if __name__ == "__main__":
    main()