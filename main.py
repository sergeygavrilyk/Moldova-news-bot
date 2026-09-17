import sqlite3
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
from urllib.parse import urlparse
from dotenv import load_dotenv
from google import genai
import cloudscraper
from tenacity import retry, stop_after_attempt, wait_exponential

# Додаємо маскування під звичайний браузер
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

if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID, GEMINI_API_KEY]):
    logging.error("Критична помилка: не знайдено базові токени у файлі .env!")
    exit(1)

DB_FILE = 'bot_database.db'
MAX_ERRORS_ALLOWED = 3
HISTORY_FILE = 'posted_news.txt'
TITLES_FILE = 'posted_titles.txt'
BACKLOG_FILE = 'news_backlog.json'
ANALYTICS_FILE = 'analytics.json'

MAX_HISTORY_SIZE = 1000 
MAX_AGE_SECONDS = 43200 # 12 годин
BATCH_SIZE = 3 # 3 новини за один запуск
# ===================================================

client = genai.Client(api_key=GEMINI_API_KEY)


def trim_history(filename, max_lines=1000):
    try:
        if os.path.exists(filename):
            with open(filename, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            if len(lines) > max_lines:
                with open(filename, 'w', encoding='utf-8') as f:
                    f.writelines(lines[-max_lines:])
    except Exception as e:
        logging.error(f"Помилка обрізки файлу {filename}: {e}")

def generate_hashtags(item):
    tags = []
    text_context = (item.get('title', '') + " " + item.get('description', '') + " " + item.get('source', '')).lower()
    
    if any(kw in text_context for kw in ['chisinau', 'chisinăului', 'кишинев']):
        tags.append('#Кишинів')
        
    if any(kw in text_context for kw in ['protectie', 'temporara', 'refugiati', 'azil', 'suport', 'беженц', 'защит', 'убежищ']):
        tags.append('#ТимчасовийЗахист')
        
    if any(kw in text_context for kw in ['ucraina', 'ukraine', 'kiev', 'zelenski', 'razboi', 'украин', 'киев', 'зеленск', 'войн']):
        tags.append('#Україна')
        
    return "\n\n" + " ".join(tags) if tags else ""

def send_alert_to_admin(message):
    if not ADMIN_CHAT_ID: return
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(api_url, data={'chat_id': ADMIN_CHAT_ID, 'text': f"⚠️ ALARM: {message}"})
    except: pass

def load_history(filename):
    if not os.path.exists(filename): return []
    with open(filename, 'r', encoding='utf-8') as f: return [line.strip() for line in f if line.strip()]

def save_to_history(history_list, new_item, filename):
    history_list.append(new_item)
    history_list = history_list[-MAX_HISTORY_SIZE:]
    with open(filename, 'w', encoding='utf-8') as f:
        for item in history_list: f.write(item + '\n')
    return history_list

def load_backlog():
    if not os.path.exists(BACKLOG_FILE): return []
    try:
        with open(BACKLOG_FILE, 'r', encoding='utf-8') as f: return json.load(f)
    except: return []

def save_backlog(backlog):
    with open(BACKLOG_FILE, 'w', encoding='utf-8') as f:
        json.dump(backlog, f, ensure_ascii=False, indent=4)

def load_analytics():
    if not os.path.exists(ANALYTICS_FILE): return []
    try:
        with open(ANALYTICS_FILE, 'r', encoding='utf-8') as f: return json.load(f)
    except: return []

def save_analytics(stats):
    history = load_analytics()
    history.append(stats)
    with open(ANALYTICS_FILE, 'w', encoding='utf-8') as f:
        json.dump(history[-1000:], f, ensure_ascii=False, indent=4)

def send_analytics_to_admin(stats):
    if not ADMIN_CHAT_ID: 
        logging.warning("ADMIN_CHAT_ID не налаштовано")
        return
        
    history = load_analytics()
    now = datetime.datetime.now()
    tokens_day = tokens_week = tokens_month = 0
    
    for run in history:
        try:
            run_date = datetime.datetime.strptime(run["timestamp"], '%Y-%m-%d %H:%M:%S')
            delta = now - run_date
            tokens = run.get("tokens_used", 0)
            
            if delta.days < 1: tokens_day += tokens
            if delta.days < 7: tokens_week += tokens
            if delta.days < 30: tokens_month += tokens
        except Exception:
            pass
        
    scraped_src_str = ", ".join([f"{k}: {v}" for k, v in stats['funnel'].get('scraped_by_source', {}).items()]) if stats['funnel'].get('scraped_by_source') else "Немає"
    backlog_src_str = ", ".join([f"{k}: {v}" for k, v in stats['backlog']['sources'].items()]) if stats['backlog']['sources'] else "Порожньо"
    pub_src_str = ", ".join([f"{k}: {v}" for k, v in stats['published']['sources'].items()]) if stats['published']['sources'] else "Немає"

    message = (
        f"📊 <b>Розширений звіт бота (Молдова)</b>\n\n"
        f"⏱ Час: {stats['execution_time']} сек | ⚠️ Помилок: {stats['errors_count']} | 🔄 Ретрай: {stats['gemini_retries']}\n"
        f"🪙 <b>Токени (сьогодні/тиждень/місяць):</b>\n"
        f"   {tokens_day:,} / {tokens_week:,} / {tokens_month:,}\n\n"
        f"🔍 <b>Воронка фільтрації:</b>\n"
        f"▪️ Знайдено в RSS: {stats['funnel']['scraped_total']}\n"
        f"▪️ По джерелах: {scraped_src_str}\n"
        f"▪️ Пропущено (старі/вже в історії): {stats['funnel']['already_seen']}\n"
        f"▪️ Відсіяно фільтрами (код): {stats['funnel']['rejected_by_keywords']}\n"
        f"▪️ Відсіяно Gemini (дублі/історія): {stats['funnel']['duplicates_removed_by_ai']}\n\n"
        f"📥 <b>Беклог (очікують ШІ-валідації: {stats['backlog']['total_size']}):</b>\n"
        f"▪️ {backlog_src_str}\n\n"
        f"✅ <b>Опубліковано: {stats['published']['total']}</b>\n"
        f"▪️ Формат: З фото ({stats['published']['with_images']}) | Текст ({stats['published']['text_only']})\n"
        f"▪️ Пріоритет 🇺🇦: {stats['published']['ukraine_priority']}\n"
        f"▪️ Джерела: {pub_src_str}"
    )
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        response = requests.post(api_url, data={'chat_id': ADMIN_CHAT_ID, 'text': message, 'parse_mode': 'HTML'})
        if response.status_code != 200:
            logging.error(f"Помилка відправки в Telegram: {response.text}")
    except Exception as e: 
        logging.error(f"Збій з'єднання з Telegram API: {e}")

def get_active_sources():
    if not os.path.exists(DB_FILE): 
        logging.error(f"БД {DB_FILE} не знайдена! Запустіть setup_db.py")
        return []
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT id, name, url FROM sources WHERE status = 'ok'")
        sources = cursor.fetchall()
        conn.close()
        return sources
    except Exception as e:
        logging.error(f"Помилка читання БД: {e}")
        return []

def extract_image_urls(entry):
    urls = []
    
    if 'media_content' in entry:
        for media in entry.media_content:
            url = media.get('url')
            if url and url not in urls:
                urls.append(url)
                
    if 'enclosures' in entry:
        for enc in entry.enclosures:
            if enc.get('type', '').startswith('image/'):
                url = enc.get('href')
                if url and url not in urls:
                    urls.append(url)
                    
    return urls[:10]

def get_source_name(url):
    try:
        parsed_uri = urlparse(url)
        domain = parsed_uri.netloc
        if domain.startswith('www.'): domain = domain[4:]
        return domain.capitalize()
    except: return "Джерело"

def is_quality_article(title, description, url):
    unwanted_url_categories = [
        '/lume/', '/sport/', '/divertisment/',  
        '/lifestyle/', '/magazin/', '/showbiz/', '/horoscop/', '/fun/'
    ]
    url_lower = url.lower()
    if any(category in url_lower for category in unwanted_url_categories):
        return False

    unwanted_keywords = [
        'publicitate', 'sponsorizat', 'promo', 'horoscop', 'anunturi', 
        'loterie', 'castiga', 'native', 'foto', 'video',
        'sport', 'fotbal', 'meci', 'tenis', 'campionat', 'liga'
    ]
    
    text_to_check = f"{title} {description} {url}".lower()
    
    for word in unwanted_keywords:
        if re.search(rf'\b{word}\b', text_to_check):
            return False
            
    if len(title.strip()) < 15 or len(text_to_check.strip()) < 20: return False
    if "!!!" in title or "???" in title: return False
    
    letters = [c for c in title if c.isalpha()]
    if letters and (sum(1 for c in letters if c.isupper()) / len(letters)) > 0.4: return False
    
    return True

# ================= TENACITY ОБГОРТКИ =================
@retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=2, min=5, max=60))
def call_gemini_json(client, prompt):
    return client.models.generate_content(
        model='gemini-3.5-flash-lite', 
        contents=prompt,
        config={"response_mime_type": "application/json"}
    )

@retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=2, min=15, max=60))
def generate_news_texts(client, prompt, expected_count):
    response = client.models.generate_content(model='gemini-3.5-flash-lite', contents=prompt)
    clean_text = response.text.replace('ב', 'б').replace('є́', 'є').replace('і́', 'і')
    texts = [t.strip() for t in clean_text.strip().split('|||') if t.strip()]
    
    if len(texts) != expected_count:
        logging.warning(f"Gemini повернув {len(texts)} текстів замість {expected_count}. Повтор...")
        raise ValueError("Невірна кількість згенерованих текстів")
        
    return response, texts

@retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=2, min=3, max=30))
def execute_telegram_request(api_url, payload):
    resp = requests.post(api_url, data=payload)
    
    if resp.status_code == 400 and "parse entities" in resp.text.lower():
        logging.warning("⚠️ Помилка HTML-розмітки. Відправляємо без HTML...")
        if 'media' in payload:
            media_objs = json.loads(payload['media'])
            if 'parse_mode' in media_objs[0]: del media_objs[0]['parse_mode']
            payload['media'] = json.dumps(media_objs)
        elif 'parse_mode' in payload:
            del payload['parse_mode']
        resp = requests.post(api_url, data=payload) 
        
    if resp.status_code != 200:
        raise Exception(f"Telegram API Error {resp.status_code}: {resp.text}")
        
    return True
# =====================================================

def deduplicate_news(client, articles, history_titles=None, stats=None):
    if not articles: 
        return articles
        
    logging.info("Аналіз на дублікати (включно з історією) через Gemini...")
    
    recent_history = history_titles[-75:] if history_titles else []
    prompt_data = [{"id": i, "title": a["title"], "description": a.get("description", "")} for i, a in enumerate(articles)]
    
    prompt = f"""
    Ти — суворий і безжальний головний редактор. Твоє завдання — відфільтрувати список новин-кандидатів.

    АЛГОРИТМ ВІДБОРУ (Оцінюй кожну новину суворо за цими кроками):
    КРОК 1. ІДЕНТИФІКАЦІЯ "КОРЕНЕВОЇ ПОДІЇ" (ОСНОВА): Читаючи кожну новину, визначай її "корінь" (головну фізичну подію: ХТО, ЩО і ДЕ).
    КРОК 2. КЛАСТЕРИЗАЦІЯ ДУБЛІВ (МІЖ ДЖЕРЕЛАМИ): Якщо кілька новин стосуються однієї кореневої події, збери їх у кластер і залишай ТІЛЬКИ ОДНУ.
    КРОК 3. ФІЛЬТР ІСТОРІЇ (АНТИ-СПАМ): Порівняй кандидатів зі списком <HISTORY>. Якщо було — відхиляй.
    КРОК 4. СТОП-ТЕМИ: Відхиляй спорт, шоу-бізнес, розваги, гороскопи, рекламу.
    КРОК 5. ТЕМАТИКА: Залишай новини про українців та тимчасовий захист у Молдові (Абсолютний пріоритет) та важливі молдовські новини.

    <CANDIDATES>
    {json.dumps(prompt_data, ensure_ascii=False, indent=2)}
    </CANDIDATES>

    <HISTORY>
    {json.dumps(recent_history, ensure_ascii=False, indent=2)}
    </HISTORY>
    
    Поверни результат СУВОРО як масив JSON з ID новин, які треба ЗАЛИШИТИ. Більше ніякого тексту.
    """
    
    try:
        response = call_gemini_json(client, prompt)
        
        if stats is not None and getattr(response, 'usage_metadata', None):
            stats["tokens_used"] += response.usage_metadata.total_token_count

        logging.info(f"Промпт до Gemini містив {len(articles)} новин.")
        logging.info(f"Відповідь Gemini: {response.text}")
        
        raw_ids = json.loads(response.text)
        keep_ids = list(set([
            int(item['id']) if isinstance(item, dict) and 'id' in item else int(item)
            for item in raw_ids if str(item if not isinstance(item, dict) else item.get('id', '')).isdigit()
        ]))
                
        filtered_articles = [articles[i] for i in keep_ids if i < len(articles)]
        removed_count = len(articles) - len(filtered_articles)
        
        if removed_count > 0:
            logging.info(f"🗑️ [Gemini] Відсіяно як дублікати/спам (або вже було): {removed_count}")
        return filtered_articles
        
    except Exception as e:
        logging.error(f"❌ Помилка дедуплікації після всіх спроб: {e}")
        send_alert_to_admin(f"Gemini API недоступний: {e}")
        return []

def prioritize_ukraine_news(backlog_items):
    ukraine_keywords = [
        r'\bucrain', r'\bzelenski', r'\bkiev', r'\bkiiv', r'\bodesa', r'\bodessa',
        r'\brefugiat', r'\bprotectie temporara', r'\bprotecție temporară',
        r'\bprivremen\w*\s+protectie',
        r'\bукраин', r'\bзеленск', r'\bкиев', r'\bодесс', r'\bбеженц'
    ]
    
    ukraine_news = []
    other_news = []
    
    for item in backlog_items:
        text_context = (item.get('title', '') + " " + item.get('description', '')).lower()
        if any(re.search(kw, text_context) for kw in ukraine_keywords):
            ukraine_news.append(item)
        else:
            other_news.append(item)
            
    return ukraine_news + other_news

def scrape_feeds(stats=None):
    history_urls = load_history(HISTORY_FILE)
    history_titles = load_history(TITLES_FILE)
    backlog = load_backlog()
    current_time = time.time()
    
    backlog = [item for item in backlog if current_time - item.get('timestamp', current_time) <= MAX_AGE_SECONDS]
    backlog_urls = [item['url'] for item in backlog]

    scraper = cloudscraper.create_scraper(browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False})
    
    active_sources = get_active_sources()
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    now = datetime.datetime.now().isoformat()

    for source_id, source_name, feed_url in active_sources:
        if stats is not None and "scraped_by_source" not in stats["funnel"]:
            stats["funnel"]["scraped_by_source"] = {}
            
        source_count = 0  
        
        try: 
            headers = {'Accept': 'application/rss+xml, application/xml, text/xml, */*'}
            response = scraper.get(feed_url, headers=headers, timeout=15)
            
            if response.status_code not in [200, 301, 302]:
                raise Exception(f"HTTP статус {response.status_code}")
                
            feed = feedparser.parse(response.text)
            
            if getattr(feed, 'bozo', False) and not feed.entries:
                raise Exception(f"Помилка структури RSS: {getattr(feed, 'bozo_exception', 'Unknown')}")
            
            if not feed.entries:
                logging.warning(f"⚠️ Фід порожній (немає новин): {feed_url}")
                continue
            
            cursor.execute("""
                UPDATE sources 
                SET status = 'ok', error_count = 0, last_error_log = NULL, last_checked = ?
                WHERE id = ?
            """, (now, source_id))
            conn.commit()
            
        except Exception as e: 
            error_text = str(e)
            logging.error(f"❌ Збій парсингу {feed_url}: {error_text}")
            
            cursor.execute("""
                UPDATE sources 
                SET error_count = error_count + 1, last_error_log = ?, last_checked = ?
                WHERE id = ?
            """, (error_text, now, source_id))
            
            cursor.execute("SELECT error_count FROM sources WHERE id = ?", (source_id,))
            if cursor.fetchone()[0] >= MAX_ERRORS_ALLOWED:
                logging.error(f"🚨 Джерело {source_name} ВІДКЛЮЧЕНО ({MAX_ERRORS_ALLOWED} помилок поспіль)")
                cursor.execute("UPDATE sources SET status = 'error' WHERE id = ?", (source_id,))
            
            conn.commit()
            continue 
            
        for entry in feed.entries[:15]: 
            if stats is not None: stats["funnel"]["scraped_total"] += 1
            source_count += 1
            
            url = entry.link
            title = entry.title
            guid = getattr(entry, 'id', url)
            
            cursor.execute('''
                INSERT OR IGNORE INTO articles (guid, source_id, title, status)
                VALUES (?, ?, ?, 'pending')
            ''', (guid, source_id, title))
            
            is_duplicate_guid = (cursor.rowcount == 0)
            
            if is_duplicate_guid or url in history_urls or title in history_titles or url in backlog_urls: 
                if stats is not None and "already_seen" in stats["funnel"]:
                    stats["funnel"]["already_seen"] += 1
                continue
            
            article_time = time.time()
            if hasattr(entry, 'published_parsed') and entry.published_parsed:
                article_time = calendar.timegm(entry.published_parsed)
                if current_time - article_time > MAX_AGE_SECONDS: continue
            
            description = entry.get('summary', title)
            if not is_quality_article(title, description, url): 
                if stats is not None: stats["funnel"]["rejected_by_keywords"] += 1
                continue

            backlog.append({
                'guid': guid, 
                'title': title, 'description': description, 'url': url,
                'image_urls': extract_image_urls(entry), 'source': source_name, 'timestamp': article_time
            })
            backlog_urls.append(url)
            
        if stats is not None:
            stats["funnel"]["scraped_by_source"][source_name] = source_count
            
    conn.close()
    save_backlog(backlog)

def process_backlog(stats=None):
    backlog = load_backlog()
    if not backlog: return

    ukraine_keywords = [
        r'\bucrain', r'\bzelenski', r'\bkiev', r'\bkiiv', r'\bodesa', r'\bodessa',
        r'\brefugiat', r'\bprotectie temporara', r'\bprotecție temporară',
        r'\bprivremen\w*\s+protectie',
        r'\bукраин', r'\bзеленск', r'\bкиев', r'\bодесс', r'\bбеженц'
    ]

    backlog.sort(key=lambda x: x.get('timestamp', 0), reverse=True)
    all_feeds = get_active_sources()
    all_sources = list(dict.fromkeys(name for _, name, _ in all_feeds))
    all_sources.sort()
    if not all_sources: return
    
    ordered_sources = all_sources.copy()
    random.shuffle(ordered_sources) 
    target_source = ordered_sources[0]
    
    logging.info(f"🎲 Рандомний порядок джерел на цей запуск: {ordered_sources}")

    candidate_batch = []
    used_urls = set()
    
    ukraine_items = prioritize_ukraine_news(backlog) 
    
    for item in ukraine_items:
        text_context = (item.get('title', '') + " " + item.get('description', '')).lower()
        if any(re.search(kw, text_context) for kw in ukraine_keywords):
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
    if not candidate_batch: return

    evaluated_urls = {item['url'] for item in candidate_batch}
    history_titles = load_history(TITLES_FILE)
    len_before = len(candidate_batch)
    candidate_batch = deduplicate_news(client, candidate_batch, history_titles, stats)
    
    if stats is not None:
        stats["funnel"]["duplicates_removed_by_ai"] += (len_before - len(candidate_batch))

    batch = candidate_batch[:BATCH_SIZE]
    if not batch: return
    
    used_sources_log = list(dict.fromkeys(item.get('source') for item in batch))
    logging.info(f"🔄 Обрано {len(batch)} новин. Головне джерело: {target_source}. Фактичні джерела: {', '.join(used_sources_log)}")

    prompt = f"Ось список із {len(batch)} новин. Ти — досвідчений головний редактор Telegram-каналу для українців, які живуть у Молдові (діаспора, біженці). Твоє завдання — перекласти та адаптувати ці новини українською мовою.\n\n"
    prompt += """СУВОРІ ПРАВИЛА ПЕРЕКЛАДУ ТА ФОРМАТУВАННЯ:
    1. ГЕОГРАФІЯ: Всі події відбуваються в Молдові або світі. "Молдавани" — це місцеві жителі. НІКОЛИ не замінюй слова "молдавани" або "Молдова" на "українці" чи "Україна", якщо цього немає в оригінальному тексті.
    2. ДЕТАЛІЗАЦІЯ ЗАКОНОДАВСТВА (ПРАВА БІЖЕНЦІВ): Якщо новина стосується тимчасового захисту, прав біженців, виплат чи документів у Молдові, перекладай її максимально детально, не скорочуючи цифри чи дати. Додай емодзі 📄 або 🇪🇺.
    3. ПРІОРИТЕТ "УКРАЇНА": Якщо в оригінальній новині згадується Україна, війна або біженці — обов'язково постав емодзі 🇺🇦 на самий початок заголовка та винеси цю інформацію у перший абзац.
    4. ЧИСТОТА МОВИ: Перекладай українською мовою АБСОЛЮТНО ВЕСЬ текст, включаючи пряму мову та цитати спікерів. Використовуй ВИКЛЮЧНО стандартний український алфавіт. Жодного російського чи румунського слова не повинно залишитись.
    5. ТОПОНІМИ ТА ІМЕНА: Усі молдавські імена, прізвища, назви вулиць та міст (наприклад, Кишинів) обов'язково транслітеруй українською мовою.
    6. СТИЛЬ ТА СТРУКТУРА: Пиши у стилі сучасного новинного Telegram-каналу. Абзаци мають бути короткими (не більше 3-4 речень). Головну думку подавай у першому абзаці.
    7. НІЯКОГО ШІ-КОНТЕКСТУ: Видавай СУВОРО лише готовий текст новини. Жодних вступних фраз чи коментарів від себе.
    8. АКЦЕНТИ: Виділяй жирним шрифтом (в тегах <b>) ключові суми штрафів, дати, імена посадовців або назви законів.
    """
    
    for idx, item in enumerate(batch):
        images = item.get('image_urls') or ([item['image_url']] if item.get('image_url') else [])
        has_image = "Так" if images else "Ні"
        prompt += f"=== НОВИНА {idx+1} ===\nМає картинку: {has_image}\nЗаголовок: {item['title']}\nОпис: {item['description']}\n\n"
    
    prompt += f"""
    Для кожної новини виконай наступні вимоги:
    1. ЗАГОЛОВОК: Зроби привабливий, змістовний заголовок (обов'язково виділений тегами <b>).
    2. ОБ'ЄМ ТА КОНТЕКСТ:
       - Якщо "Має картинку: Так" -> напиши лаконічний текст ДО 900 символів.
       - Якщо "Має картинку: Ні" -> напиши розширений текст ДО 2500 символів.
       КАТЕГОРИЧНО ЗАБОРОНЕНО вигадувати імена, цитати чи цифри, яких не було в оригіналі.
    3. СТРУКТУРА: Дроби текст на короткі абзаци (по 3-4 речення). Використовуй марковані списки (• або -) для розширених текстів.
    4. TELEGRAM HTML: Використовуй ТІЛЬКИ ці теги: <b>, <i>, <u>, <s>. Ніяких тегів <p>, <br>, <h1>, <li> і ніякої Markdown-розмітки.
    5. ФОРМАТ ВИВОДУ: Розділяй готові тексти новин СУВОРО рядком `|||`.
    """

    logging.info("Очікування 15с перед викликом Gemini API для дотримання RPM...")
    time.sleep(15)
    
    try:
        response, generated_texts = generate_news_texts(client, prompt, len(batch))
        if stats is not None and getattr(response, 'usage_metadata', None):
            stats["tokens_used"] += response.usage_metadata.total_token_count
    except Exception as e:
        logging.error(f"❌ Не вдалося згенерувати тексти після всіх спроб: {e}")
        if stats is not None: stats["errors_count"] += 1  
        return

    history_urls = load_history(HISTORY_FILE)
    history_titles = load_history(TITLES_FILE) 
    successful_urls = []

    for idx, item in enumerate(batch):
        if idx >= len(generated_texts): break 
        
        tags = generate_hashtags(item)
        images = item.get('image_urls') or ([item['image_url']] if item.get('image_url') else [])
        
        if send_to_telegram(generated_texts[idx], item['url'], item.get('source', 'Джерело'), images, tags):
            history_urls = save_to_history(history_urls, item['url'], HISTORY_FILE)
            history_titles = save_to_history(history_titles, item['title'], TITLES_FILE)
            successful_urls.append(item['url'])
            
            try:
                db_conn = sqlite3.connect(DB_FILE)
                db_conn.execute("UPDATE articles SET status = 'sent' WHERE guid = ?", (item.get('guid', item['url']),))
                db_conn.commit()
                db_conn.close()
            except Exception as db_err:
                logging.error(f"Помилка оновлення статусу статті в БД: {db_err}")
            
            if stats is not None:
                stats["published"]["total"] += 1
                text_context = (item.get('title', '') + " " + item.get('description', '')).lower()
                if any(re.search(kw, text_context) for kw in ukraine_keywords):
                    stats["published"]["ukraine_priority"] += 1
                
                if images: stats["published"]["with_images"] += 1
                else: stats["published"]["text_only"] += 1
                
                src = item.get('source', 'Джерело')
                stats["published"]["sources"][src] = stats["published"]["sources"].get(src, 0) + 1
                
            time.sleep(45)
        else:
            if stats is not None: stats["errors_count"] += 1

    remaining_backlog = [item for item in backlog if item['url'] not in evaluated_urls]
    remaining_backlog.sort(key=lambda x: x.get('timestamp', 0), reverse=True)
    save_backlog(remaining_backlog[:30])

def send_to_telegram(text, url, source_name, image_urls=None, tags=""):
    text = text.replace('ב', 'б').replace('є́', 'є').replace('і́', 'і')
    
    if not image_urls: image_urls = []
    elif isinstance(image_urls, str): image_urls = [image_urls]
    
    crop_msg = "...\n\n👉 <i>Повні деталі читайте за посиланням нижче</i> 👇"
    safe_caption_limit = 920 - len(tags)
    
    if image_urls and len(text) > safe_caption_limit: 
        text = text[:safe_caption_limit] + crop_msg
    elif not image_urls and len(text) > 3900: 
        text = text[:3900] + crop_msg
    
    full_message = f"{text}{tags}\n\n🔗 Джерело: <a href='{url}'>{source_name}</a>"
    payload = {'chat_id': TELEGRAM_CHANNEL_ID}
    
    if len(image_urls) == 0:
        payload['parse_mode'] = 'HTML'
        payload['text'] = full_message
        api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        
    elif len(image_urls) == 1:
        payload['parse_mode'] = 'HTML'
        payload['caption'] = full_message
        payload['photo'] = image_urls[0]
        api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
        
    else:
        media = []
        for idx, img_url in enumerate(image_urls):
            media_item = {'type': 'photo', 'media': img_url}
            if idx == 0:
                media_item['caption'] = full_message
                media_item['parse_mode'] = 'HTML'
            media.append(media_item)
            
        payload['media'] = json.dumps(media)
        api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMediaGroup"

    try:
        return execute_telegram_request(api_url, payload)
    except Exception as e: 
        logging.error(f"❌ Критична помилка відправки в Telegram після всіх спроб: {e}")
        return False

def cleanup_old_data(db_path, days_to_keep=30):
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        cursor.execute(f"DELETE FROM articles WHERE created_at <= datetime('now', '-{days_to_keep} days')")
        deleted_count = cursor.rowcount
        
        cursor.execute("VACUUM")
        conn.commit()
        conn.close()
        
        if deleted_count > 0:
            logging.info(f"🧹 Автоочищення: видалено {deleted_count} старих статей. Базу стиснуто.")
            
    except Exception as e:
        logging.error(f"Помилка при очищенні бази даних: {e}")

def main():
    start_time = time.time()
    
    stats = {
        "timestamp": time.strftime('%Y-%m-%d %H:%M:%S'),
        "execution_time": 0,
        "tokens_used": 0,
        "funnel": {
            "scraped_total": 0,
            "scraped_by_source": {},  
            "already_seen": 0,        
            "rejected_by_keywords": 0,
            "duplicates_removed_by_ai": 0
        },
        "backlog": {
            "total_size": 0,
            "sources": {}
        },
        "published": {
            "total": 0,
            "ukraine_priority": 0,
            "with_images": 0,
            "text_only": 0,
            "sources": {}
        },
        "errors_count": 0,
        "gemini_retries": 0
    }

    try:
        scrape_feeds(stats)
        
        backlog = load_backlog()
        stats["backlog"]["total_size"] = len(backlog)
        
        source_counts = {}
        for item in backlog:
            src = item.get('source', 'Невідомо')
            source_counts[src] = source_counts.get(src, 0) + 1
        stats["backlog"]["sources"] = source_counts
        
        process_backlog(stats)
        cleanup_old_data(DB_FILE, days_to_keep=30)
        
    except Exception as e:
        stats["errors_count"] += 1
        send_alert_to_admin(f"Скрипт впав з помилкою: {e}")

    finally:
        trim_history(HISTORY_FILE, MAX_HISTORY_SIZE)
        trim_history(TITLES_FILE, MAX_HISTORY_SIZE)
        
        stats["execution_time"] = round(time.time() - start_time, 1)
        save_analytics(stats)
        
        if stats["published"]["total"] > 0 or stats["errors_count"] > 0:
            send_analytics_to_admin(stats)

if __name__ == "__main__":
    main()