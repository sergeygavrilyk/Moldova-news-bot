import feedparser
import requests
import os
import time
import calendar
import logging
import json
import re
from urllib.parse import urlparse
from dotenv import load_dotenv
from google import genai
import cloudscraper

# Додаємо маскування під звичайний браузер, щоб сайти не блокували бота
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

HISTORY_FILE = 'posted_news.txt'
TITLES_FILE = 'posted_titles.txt'
BACKLOG_FILE = 'news_backlog.json'
FEEDS_FILE = 'feeds.json'
ANALYTICS_FILE = 'analytics.json'

MAX_HISTORY_SIZE = 1000 
MAX_AGE_SECONDS = 43200 # 12 годин
BATCH_SIZE = 3 # 3 новини за один запуск
# ===================================================

client = genai.Client(api_key=GEMINI_API_KEY)


def trim_history(filename, max_lines=1000):
    """Очищує старі записи, залишаючи лише останні max_lines."""
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
    """Генерує хештеги на основі тексту новини (адаптовано під Молдову)."""
    tags = []
    text_context = (item.get('title', '') + " " + item.get('description', '') + " " + item.get('source', '')).lower()
    
    if any(kw in text_context for kw in ['chisinau', 'chisinăului', 'moldova', 'moldovei', 'stiri']):
        tags.append('#Молдова')
    if any(kw in text_context for kw in ['protectie', 'temporara', 'refugiati', 'azil', 'suport']):
        tags.append('#ТимчасовийЗахист')
    if any(kw in text_context for kw in ['ucraina', 'ukraine', 'kiev', 'zelenski', 'razboi']):
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
        json.dump(history[-100:], f, ensure_ascii=False, indent=4)

def send_analytics_to_admin(stats):
    if not ADMIN_CHAT_ID: 
        logging.warning("ADMIN_CHAT_ID не налаштовано")
        return
        
    backlog_src_str = ", ".join([f"{k}: {v}" for k, v in stats['backlog']['sources'].items()]) if stats['backlog']['sources'] else "Порожньо"
    pub_src_str = ", ".join([f"{k}: {v}" for k, v in stats['published']['sources'].items()]) if stats['published']['sources'] else "Немає"

    message = (
        f"📊 <b>Розширений звіт бота (Молдова)</b>\n\n"
        f"⏱ Час виконання: {stats['execution_time']} сек\n"
        f"⚠️ Помилок: {stats['errors_count']} | Ретраїв Gemini: {stats['gemini_retries']}\n\n"
        f"🔍 <b>Воронка фільтрації:</b>\n"
        f"▪️ Знайдено в RSS: {stats['funnel']['scraped_total']}\n"
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

def get_rss_feeds():
    if not os.path.exists(FEEDS_FILE): return []
    try:
        with open(FEEDS_FILE, 'r', encoding='utf-8') as f: return json.load(f)
    except Exception as e:
        logging.error(f"Помилка читання feeds.json: {e}")
        return []

def extract_image_url(entry):
    if 'media_content' in entry and len(entry.media_content) > 0:
        return entry.media_content[0].get('url')
    if 'enclosures' in entry and len(entry.enclosures) > 0:
        for enc in entry.enclosures:
            if enc.get('type', '').startswith('image/'): return enc.get('href')
    return None

def get_source_name(url):
    try:
        parsed_uri = urlparse(url)
        domain = parsed_uri.netloc
        if domain.startswith('www.'): domain = domain[4:]
        return domain.capitalize()
    except: return "Джерело"

def is_quality_article(title, description, url):
    # Адаптовано під румунськомовні/російськомовні категорії молдовських сайтів
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

def deduplicate_news(client, articles, history_titles=None):
    if not articles: 
        return articles
        
    logging.info("Аналіз на дублікати (включно з історією) через Gemini...")
    
    recent_history = history_titles[-25:] if history_titles else []
    prompt_data = [{"id": i, "title": a["title"], "description": a.get("description", "")} for i, a in enumerate(articles)]
    
    prompt = f"""
    Ось список новин (ID, Заголовок), які претендують на публікацію:
    {json.dumps(prompt_data, ensure_ascii=False, indent=2)}
    
    Ось ІСТОРІЯ - список із {len(recent_history)} останніх опублікованих нами новин:
    {json.dumps(recent_history, ensure_ascii=False, indent=2)}
    
    Твоє завдання:
    1. ЩО ЗАЛИШАТИ (ТЕМАТИКА): Ти ПОВИНЕН залишати новини, якщо це: 
       - Важливі молдовські новини (місцеві події, суспільство, економіка, кримінал, надзвичайні ситуації).
       - Життя українців та українців-біженців у Молдові.
       - Молдовські новини про події в Україні.
    2. ЩО ВІДХИЛЯТИ (СТОП-ТЕМИ): БЕЗЖАЛЬНО ВІДХИЛЯЙ світові новини (якщо немає прив'язки до Молдови/України), а також будь-який спорт, шоу-бізнес, розваги, гороскопи та рекламу.
    3. АБСОЛЮТНИЙ ПРІОРИТЕТ (ПРАВА БІЖЕНЦІВ / ЗАКОНОДАВСТВО МОЛДОВИ): Новини про статус тимчасового захисту, документи, соціальні виплати чи допомогу біженцям ОБОВ'ЯЗКОВО ЗАЛИШАТИ.
    4. ЗНАЙТИ ВНУТРІШНІ ДУБЛІКАТИ: Якщо кілька новин-претендентів описують одну подію, залиши серед них лише ОДНУ.
    5. ПОРІВНЯТИ З ІСТОРІЄЮ: Якщо новина стосується події, про яку ми ВЖЕ писали в Історії, її треба ВІДХИЛИТИ повністю.
    
    Поверни результат СУВОРО як масив JSON з ID новин, які треба ЗАЛИШИТИ. Більше ніякого тексту.
    """
    
    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model='gemini-3.5-flash-lite', 
                contents=prompt,
                config={"response_mime_type": "application/json"}
            )
            
            logging.info(f"Промпт до Gemini містив {len(articles)} новин.")
            logging.info(f"Відповідь Gemini: {response.text}")
            
            raw_ids = json.loads(response.text)
            keep_ids = []
            
            for item in raw_ids:
                if isinstance(item, dict) and 'id' in item: keep_ids.append(int(item['id']))
                elif isinstance(item, (int, str)) and str(item).isdigit(): keep_ids.append(int(item))
                    
            keep_ids = list(set(keep_ids))
            filtered_articles = [articles[i] for i in keep_ids if i < len(articles)]
            
            removed_count = len(articles) - len(filtered_articles)
            if removed_count > 0:
                logging.info(f"🗑️ [Gemini] Відсіяно як дублікати (або вже було): {removed_count}")
            return filtered_articles
            
        except Exception as e:
            logging.error(f"Помилка дедуплікації (спроба {attempt+1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                time.sleep(10)
            else:
                send_alert_to_admin(f"Gemini API недоступний після {max_retries} спроб. Помилка: {e}")
                return articles

def prioritize_ukraine_news(backlog_items):
    # Адаптовано ключі (румунська + українська + російська згадки)
    keywords = [
        'ucraina', 'ucrainean', 'ucraineana', 'kiev', 'zelenski', 'odessa', 
        'refugiat', 'protectie temporara', 'ajutor umanitar', 'razboi', 'conflict', 
        'agresiune', 'ukrajin', 'zelensk', 'kijev', 'pomoć', 'izbjeglic'
    ]
    ukraine_news = []
    other_news = []
    
    for item in backlog_items:
        text_context = (item.get('title', '') + " " + item.get('description', '')).lower()
        if any(kw in text_context for kw in keywords):
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

    for feed_item in get_rss_feeds():
        feed_url = feed_item['url']
        source_name = feed_item.get('name', get_source_name(feed_url))
        
        try: 
            response = scraper.get(feed_url, timeout=15)
            
            if response.status_code not in [200, 301, 302]:
                logging.error(f"❌ Помилка доступу до {feed_url}: HTTP статус {response.status_code}")
                continue
                
            feed = feedparser.parse(response.text)
            
            if not feed.entries:
                logging.warning(f"⚠️ Фід порожній (немає новин): {feed_url}")
                continue
                
        except Exception as e: 
            logging.error(f"❌ Збій парсингу {feed_url}: {e}")
            continue
            
        for entry in feed.entries[:15]: 
            if stats is not None: stats["funnel"]["scraped_total"] += 1
                
            url = entry.link
            title = entry.title
            if url in history_urls or title in history_titles or url in backlog_urls: continue
            
            article_time = time.time()
            if hasattr(entry, 'published_parsed') and entry.published_parsed:
                article_time = calendar.timegm(entry.published_parsed)
                if current_time - article_time > MAX_AGE_SECONDS: continue
            
            description = entry.get('summary', title)
            if not is_quality_article(title, description, url): 
                if stats is not None: stats["funnel"]["rejected_by_keywords"] += 1
                continue

            backlog.append({
                'title': title, 'description': description, 'url': url,
                'image_url': extract_image_url(entry), 'source': source_name, 'timestamp': article_time
            })
            backlog_urls.append(url)
    save_backlog(backlog)

def process_backlog(stats=None):
    backlog = load_backlog()
    if not backlog: return

    # Очищений Regex-список для Молдови (румунські та російські маркери)
    ukraine_keywords = [
        # Румунською
        r'\bucrain', r'\bzelenski', r'\bkiev', r'\bkiiv', r'\bodesa', r'\bodessa',
        r'\brefugiat', r'\bprotectie temporara', r'\bprotecție temporară',
        # Російською (для Point.md, Newsmaker тощо)
        r'\bукраин', r'\bзеленск', r'\bкиев', r'\bодесс', r'\bбеженц'
    ]

    backlog.sort(key=lambda x: x.get('timestamp', 0), reverse=True)
    all_feeds = get_rss_feeds()
    all_sources = list(dict.fromkeys(f.get('name', get_source_name(f['url'])) for f in all_feeds))
    all_sources.sort()
    if not all_sources: return
    
    current_hour = time.gmtime().tm_hour
    ordered_sources = []
    for i in range(len(all_sources)):
        idx = (current_hour + i) % len(all_sources)
        ordered_sources.append(all_sources[idx])
    target_source = ordered_sources[0]

    candidate_batch = []
    used_urls = set()
    
    ukraine_items = prioritize_ukraine_news(backlog) 
    
    for item in ukraine_items:
        text_context = (item.get('title', '') + " " + item.get('description', '')).lower()
        # Нова перевірка з регулярними виразами:
        if any(re.search(kw, text_context) for kw in ukraine_keywords):
            if item['url'] not in used_urls:
                candidate_batch.append(item)
                used_urls.add(item['url'])
                
    for src in ordered_sources:
        for item in backlog:
            if item.get('source') == src and item['url'] not in used_urls:
                candidate_batch.append(item)
                used_urls.add(item['url'])
            if len(candidate_batch) >= 25: break
        if len(candidate_batch) >= 25: break
            
    candidate_batch = candidate_batch[:25]
    if not candidate_batch: return

    # 1. Запам'ятовуємо всі URL, які пішли на оцінку до Gemini
    evaluated_urls = {item['url'] for item in candidate_batch}

    history_titles = load_history(TITLES_FILE)
    len_before = len(candidate_batch)
    candidate_batch = deduplicate_news(client, candidate_batch, history_titles)
    
    if stats is not None:
        stats["funnel"]["duplicates_removed_by_ai"] += (len_before - len(candidate_batch))

    batch = candidate_batch[:BATCH_SIZE]
    if not batch: return
    
    used_sources_log = list(dict.fromkeys(item.get('source') for item in batch))
    logging.info(f"🔄 Обрано {len(batch)} новин. Головне джерело: {target_source}. Фактичні джерела: {', '.join(used_sources_log)}")

    prompt = f"Ось список із {len(batch)} новин. Ти — досвідчений головний редактор Telegram-каналу для українців, які живуть у Молдові (діаспора, біженці). Твоє завдання — перекласти та адаптувати ці новини українською мовою.\n\n"
    prompt += """СУВОРІ ПРАВИЛА КОНТЕКСТУ ТА ПРІОРИТЕТІВ:
    1. ГЕОГРАФІЯ: Всі події відбуваються в Молдові або світі. "Молдавани" — це місцеві жителі. НІКОЛИ не замінюй слова "молдавани" або "Молдова" на "українці" чи "Україна", якщо цього немає в оригінальному тексті.
    2. АБСОЛЮТНИЙ ПРІОРИТЕТ (ПРАВА БІЖЕНЦІВ / ЗАКОНОДАВСТВО): Будь-які новини, що стосуються тимчасового захисту, статусу переміщених осіб, продовження терміну дії документів, соціальних прав або допомоги біженцам у Молдові, ОБОВ'ЯЗКОВО ЗАЛИШАТИ.
    3. ПРІОРИТЕТ "УКРАЇНА": Якщо в оригінальній новині згадується Україна, війна або біженці — постав емодзі 🇺🇦 на самий початок заголовка та винеси цю інформацію у перший абзац.
    4. ЧИСТОТА МОВИ: Використовуй ВИКЛЮЧНО стандартний український алфавіт.
    5. ТОПОНІМИ ТА ІМЕНА: Усі молдавскі імена, прізвища, назви вулиць та міст (наприклад, Кишинів) обов'язково транслітеруй українською мовою. Не залишай латинські літери в тексті.
    6. СТИЛЬ ТА СТРУКТУРА: Пиши у стилі сучасного новинного Telegram-каналу. Абзаци мають бути короткими (не більше 3-4 речень). Головну думку подавай у першому абзаці (принцип перевернутої піраміди).
    7. НІЯКОГО ШІ-КОНТЕКСТУ: Видавай СУВОРО лише готовий текст новини. Жодних вступних фраз, привітань, коментарів чи висновків від себе. Зберігай абсолютну журналістську об'єктивність.
    8. АКЦЕНТИ: Виділяй жирним шрифтом (в тегах <b>) ключові суми штрафів, дати, імена посадовців або назви законів, щоб текст було зручно читати.
    """
    
    for idx, item in enumerate(batch):
        has_image = "Так" if item.get('image_url') else "Ні"
        prompt += f"=== НОВИНА {idx+1} ===\nМає картинку: {has_image}\nЗаголовок: {item['title']}\nОпис: {item['description']}\n\n"
    
    prompt += f"""
    Для кожної новини виконай наступні вимоги:
    
    1. ЗАГОЛОВОК: Зроби привабливий, змістовний заголовок (обов'язково виділений тегами <b>).
    
    2. ОБ'ЄМ ТА КОНТЕКСТ:
       - Якщо "Має картинку: Так" -> напиши лаконічний текст ДО 900 символів. Якщо суть події повністю розкрита за 500-600 символів — зупинись, не "лий воду" і не розтягуй штучно.
       - Якщо "Має картинку: Ні" -> напиши розширений текст ДО 2500 символів. Якщо тема повністю вичерпана за 1500 символів — не додавай зайвого, головне — інформативність, а не обсяг.
       УВАГА: Розширюй текст ВИКЛЮЧНО за рахунок пояснення контексту події та її значення. КАТЕГОРИЧНО ЗАБОРОНЕНО вигадувати неіснуючі імена, цитати, точні цифри чи дати, яких не було в оригіналі.
       
    3. СТРУКТУРА: Дроби текст на короткі абзаци (по 3-4 речення). Для розширених текстів використовуй марковані списки (з символом • або -), щоб інформацію було зручно читати.
    
    4. TELEGRAM HTML: Використовуй ТІЛЬКИ ці теги: <b>, <i>, <u>, <s>. Ніяких тегів <p>, <br>, <h1>, <li> і ніякої Markdown-розмітки (без зірочок ** чи решіток ##).
    
    5. ФОРМАТ ВИВОДУ: Розділяй готові тексти новин СУВОРО рядком `|||`. Не додавай жодних своїх коментарів на початку чи в кінці всієї відповіді.
    """

    generated_texts = []
    logging.info("Очікування перед викликом Gemini API...")
    time.sleep(15)

    for attempt in range(3):
        try:
            response = client.models.generate_content(model='gemini-3.5-flash-lite', contents=prompt)
            
            # ДОДАНО: Виводимо сиру відповідь ШІ в лог, щоб бачити проблему
            logging.info(f"Текст від Gemini:\n{response.text}") 
            
            generated_texts = [t.strip() for t in response.text.strip().split('|||') if t.strip()]
            
            # ДОДАНО: Перевірка, чи співпадає кількість текстів з кількістю новин
            if len(generated_texts) == len(batch):
                break
            else:
                logging.warning(f"Gemini повернув {len(generated_texts)} текстів, а треба {len(batch)}. Повторна спроба...")
                if stats is not None: stats["gemini_retries"] += 1
                time.sleep(15)
                
        except Exception as e:
            logging.warning(f"Помилка Gemini API: {e}. Очікування 60с (Спроба {attempt+1}/3)")
            if stats is not None: stats["gemini_retries"] += 1
            time.sleep(60)
            
    if not generated_texts: 
        logging.error("❌ Не вдалося згенерувати тексти після 3 спроб. Скасування публікації.")
        if stats is not None: stats["errors_count"] += 1  # ДОДАНО
        return

    history_urls = load_history(HISTORY_FILE)
    history_titles = load_history(TITLES_FILE) 
    successful_urls = []

    for idx, item in enumerate(batch):
        if idx >= len(generated_texts): break 
        
        tags = generate_hashtags(item)
        
        # Передаємо текст від ШІ та хештеги окремо
        if send_to_telegram(generated_texts[idx], item['url'], item.get('source', 'Джерело'), item['image_url'], tags):
            history_urls = save_to_history(history_urls, item['url'], HISTORY_FILE)
            history_titles = save_to_history(history_titles, item['title'], TITLES_FILE)
            successful_urls.append(item['url'])
            
            if stats is not None:
                stats["published"]["total"] += 1
                
                text_context = (item.get('title', '') + " " + item.get('description', '')).lower()
                # Нова перевірка з регулярними виразами:
                if any(re.search(kw, text_context) for kw in ukraine_keywords):
                    stats["published"]["ukraine_priority"] += 1
                
                if item.get('image_url'): stats["published"]["with_images"] += 1
                else: stats["published"]["text_only"] += 1
                
                src = item.get('source', 'Джерело')
                stats["published"]["sources"][src] = stats["published"]["sources"].get(src, 0) + 1
                
            time.sleep(45)
        else:
            # ДОДАНО: Фіксуємо помилку, якщо Telegram не прийняв текст
            if stats is not None: stats["errors_count"] += 1

    # 2. Залишаємо тільки ті новини, які ще взагалі НЕ розглядалися ШІ
    remaining_backlog = [item for item in backlog if item['url'] not in evaluated_urls]
    remaining_backlog.sort(key=lambda x: x.get('timestamp', 0), reverse=True)
    save_backlog(remaining_backlog[:50])

def send_to_telegram(text, url, source_name, image_url=None, tags=""):
    crop_msg = "...\n\n👉 <i>Повні деталі читайте за посиланням нижче</i> 👇"
    
    # Вираховуємо безпечний ліміт з урахуванням довжини хештегів
    safe_caption_limit = 920 - len(tags)
    
    if image_url and len(text) > safe_caption_limit: 
        text = text[:safe_caption_limit] + crop_msg
    elif not image_url and len(text) > 3900: 
        text = text[:3900] + crop_msg
    
    # Додаємо хештеги ВЖЕ ПІСЛЯ обрізки, перед посиланням на джерело
    full_message = f"{text}{tags}\n\n🔗 Джерело: <a href='{url}'>{source_name}</a>"
    
    payload = {'chat_id': TELEGRAM_CHANNEL_ID, 'parse_mode': 'HTML'}
    
    if image_url:
        payload['photo'] = image_url
        payload['caption'] = full_message
        api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    else:
        payload['text'] = full_message
        api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    
    try:
        resp = requests.post(api_url, data=payload)
        if resp.status_code != 200:
            logging.error(f"❌ Telegram відхилив повідомлення: {resp.text}")
        return resp.status_code == 200
    except Exception as e: 
        logging.error(f"❌ Критична помилка відправки в Telegram: {e}")
        return False

def main():
    start_time = time.time()
    
    stats = {
        "timestamp": time.strftime('%Y-%m-%d %H:%M:%S'),
        "execution_time": 0,
        "funnel": {
            "scraped_total": 0,
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