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
from google.genai import types  # ДОДАНО ДЛЯ SAFETY SETTINGS
import cloudscraper
from tenacity import retry, stop_after_attempt, wait_exponential
from pydantic import BaseModel, Field

class RejectedItem(BaseModel):
    id: int
    entities_found: list[str] = Field(description="3-5 ключових фігурантів, компаній, цифр або локацій")
    reason: str

class NewsFilterResponse(BaseModel):
    keep: list[int]
    rejected: list[RejectedItem]

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


MAX_AGE_SECONDS = 43200 # 12 годин
BATCH_SIZE = 3
client = genai.Client(api_key=GEMINI_API_KEY)
# ===================================================

def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

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
    
    unwanted_keywords = [
        # --- Реклама та промо (Румунські + Англійські) ---
        'publicitate', 'sponsorizat', 'promo', 'native', 'anunturi', 'advertisement', 'sponsored',
        
        # --- Гороскопи та розіграші ---
        'horoscop', 'horoscope', 'loterie', 'lottery', 'castiga', 'win', 'loto', 'anunturi',
        
        # --- Медіа та галереї ---
        'foto', 'video', 'photo', 'gallery', 'galerie',
        
        # --- Спорт (Румунські + Англійські дисципліни та терміни) ---
        'sport', 'fotbal', 'football', 'soccer', 
        'meci', 'match', 'game', 
        'tenis', 'tennis', 
        'campionat', 'championship', 
        'liga', 'league', 
        'turneu', 'tournament', 
        'judo', 'box', 'boxing', 
        'fotbalist', 'sportiv', 'athlete', 'campion'
    ]
    
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
    # Пріоритет: спочатку розумна, потім швидка
    models_to_try = ['gemini-3.6-flash', 'gemini-3.5-flash-lite']
    last_error = None
    
    for current_model in models_to_try:
        try:
            response = client.models.generate_content(
                model=current_model, 
                contents=prompt, 
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=NewsFilterResponse, # <--- ДОДАНО ОСЬ ЦЕ
                    temperature=0.0,
                    safety_settings=[
                        types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE"),
                        types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_NONE"),
                        types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_NONE"),
                        types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_NONE"),
                    ]
                )
            )
            
            # ЗАХИСТ: Перевірка на порожню відповідь
            if not response or not getattr(response, 'text', None):
                raise ValueError(f"Порожня відповідь (None) з моделі {current_model}")
                
            # Якщо успіх — одразу повертаємо результат
            return response
            
        except Exception as e:
            logging.warning(f"⚠️ Дедуплікація: Модель {current_model} недоступна ({e}). Перемикаємось...")
            last_error = e
            time.sleep(5)  # Коротка пауза перед запасною моделлю
            
    # Якщо обидві моделі впали, викликаємо фінальну помилку.
    # Її підхопить @retry (tenacity) і відправить скрипт на тривалий сон (від 5 до 60 сек).
    raise last_error

@retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=2, min=15, max=60))
def generate_news_texts(client, prompt, expected_count):
    # Пріоритет для перекладу: спочатку швидка, потім розумна
    models_to_try = ['gemini-3.5-flash-lite', 'gemini-3.6-flash']
    last_error = None
    
    for current_model in models_to_try:
        try:
            response = client.models.generate_content(
                model=current_model, 
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.0,
                    safety_settings=[
                        types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE"),
                        types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_NONE"),
                        types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_NONE"),
                        types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_NONE"),
                    ]
                )
            )
            
            if not response or not getattr(response, 'text', None):
                raise ValueError(f"Порожня відповідь від {current_model}")
                
            logging.info(f"Текст від Gemini ({current_model}):\n{response.text}")
                
            # --- ЖОРСТКЕ ВИДАЛЕННЯ ГАЛЮЦИНАЦІЙ ШІ ---
            HOMOGLYPHS = {
                'ά': 'а', 'α': 'а', 'ό': 'о', 'ο': 'о', 
                'έ': 'е', 'ε': 'е', 'ί': 'і', 'ι': 'і', 
                'ή': 'и', 'η': 'и', 'ν': 'н', 'Ν': 'Н',
                'ב': 'б', 'ب': 'б',
                'є́': 'є', 'і́': 'і'
            }
            
            clean_text = response.text
            for bad_char, good_char in HOMOGLYPHS.items():
                clean_text = clean_text.replace(bad_char, good_char)
                
            texts = [t.strip() for t in clean_text.strip().split('|||') if t.strip()]
            
            if len(texts) != expected_count: 
                raise ValueError(f"Невірна кількість текстів від {current_model}: очікували {expected_count}, отримали {len(texts)}")
                
            return response, texts
            
        except Exception as e:
            logging.warning(f"⚠️ Переклад: Модель {current_model} недоступна ({e}). Перемикаємось...")
            last_error = e
            time.sleep(5)
            
    # Якщо обидві моделі впали, віддаємо помилку декоратору @retry
    raise last_error

@retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=2, min=3, max=30))
def execute_telegram_request(api_url, payload):
    resp = requests.post(api_url, data=payload)
    
    # ЗАХИСТ ВІД ЗЛАМАНИХ ТЕГІВ: Якщо Telegram лається на розмітку
    if resp.status_code == 400 and "parse entities" in resp.text.lower():
        logging.warning("⚠️ Помилка HTML-розмітки (ймовірно через обрізку тексту). Відправляємо без HTML...")
        if 'media' in payload:
            media_objs = json.loads(payload['media'])
            if 'parse_mode' in media_objs[0]: del media_objs[0]['parse_mode']
            payload['media'] = json.dumps(media_objs)
        elif 'parse_mode' in payload: 
            del payload['parse_mode']
        resp = requests.post(api_url, data=payload) 
        
    if resp.status_code != 200: raise Exception(f"Telegram Error: {resp.text}")
    return True

def deduplicate_news(client, articles, recent_history=None, stats=None):
    if not articles: return articles
    logging.info("Аналіз на дублікати та збір причин відхилення через Gemini...")
    
    history = recent_history[-75:] if recent_history else []
    prompt_data = [{"id": i, "title": a["title"], "description": a.get("description", "")} for i, a in enumerate(articles)]
# ДОДАНО ЛОГУВАННЯ ЗАГОЛОВКІВ ("Прожектор")
    titles_log = "\n".join([f"[{i}] {a['title']}" for i, a in enumerate(articles)])
    logging.info(f"📋 Кандидати на фільтрацію (Молдова):\n{titles_log}")
    
    prompt = f"""Ти — суворий і безжальний головний редактор Telegram-каналу для української діаспори, яка проживає в Молдові. Твоя мета — дати читачам максимально повну та корисну картину дня, відсікаючи інформаційне сміття. Твоє завдання — відфільтрувати список новин-кандидатів.

    АЛГОРИТМ ВІДБОРУ (Оцінюй кожну новину суворо за цими кроками у заданому порядку):

    КРОК 1. ІДЕНТИФІКАЦІЯ "КОРЕНЕВОЇ ПОДІЇ" (ОСНОВА):
    Читаючи кожну новину, визначай її "корінь" (головну фізичну подію: ХТО, ЩО і ДЕ). 
    Наприклад: реакція міністерства, перевірка соцслужб чи коментарі свідків ДТП у Кишиневі — це НЕ різні події. Це "гілки", що ростуть з одного "кореня". 

    КРОК 2. КЛАСТЕРИЗАЦІЯ ДУБЛІВ (МІЖ ДЖЕРЕЛАМИ): 
    Переглянь список <CANDIDATES>. Якщо ти бачиш кілька новин, які стосуються ОДНІЄЇ кореневої події, збери їх у кластер. 
    З цього кластера ЗАЛИШ ТІЛЬКИ ОДИН ID (ту статтю, яка є найбільш загальною, об'ємною і підсумовує ситуацію найкраще). Всі інші ID з цього кластера — відхиляй.

    КРОК 3. ФІЛЬТР ІСТОРІЇ (АНТИ-СПАМ): 
    Порівняй залишених кандидатів зі списком <HISTORY>. Якщо "коренева подія" кандидата ВЖЕ згадувалася в історії — БЕЗЖАЛЬНО ВІДХИЛЯЙ кандидата. 
    Ми НЕ публікуємо "нові подробиці", "реакції влади" чи "продовження розслідування" вчорашніх подій. Це інформаційний шум. 
    (ЗАЛИШАЙ кандидатів з історією ТІЛЬКИ у випадку кардинальної розв'язки: наприклад, злочинця спіймали, суд виніс остаточний вирок, потерпілий помер). 

    КРОК 4. СТОП-ТЕМИ (ЩО БЕЗЖАЛЬНО ВІДХИЛЯТИ): 
    1. СПОРТ: Будь-які матчі, турніри, результати, трансфери, спортсмени.
    2. РЕКЛАМА: Спонсорські матеріали, промо-акції, розіграші.
    3. СВІТОВИЙ ШУМ ТА ЛАЙФСТАЙЛ: Абстрактна наука (як-от дослідження Гарварда), поради дієтологів, рецепти, психологія, шоу-бізнес та події в третіх країнах, ЯКЩО вони не стосуються безпосередньо Хорватії чи України.

    КРОК 5. ТЕМАТИКА (ЩО ЗАЛИШИТИ): 
    Залишай ТІЛЬКИ ті новини, які пройшли Кроки 1-4 і належать до однієї з чотирьох категорій:
    1. ВАЖЛИВІ МОЛДОВСЬКІ НОВИНИ: Будь-які резонансні події всередині Молдови (кримінал, надзвичайні ситуації, політика, економіка, суспільство, а також культура та цікаві технологічні новини) — НАВІТЬ ЯКЩО там не згадуються українці. Це основа каналу.
    2. УКРАЇНЦІ В МОЛДОВІ: Події всередині країни, пов'язані з діаспорою, статусом захисту, кордоном, виплатами чи допомогою.
    3. ЄВРОПЕЙСЬКІ ТА СВІТОВІ РІШЕННЯ: Документи, закони та допомога, що безпосередньо впливають на ситуацію в Молдові чи на українців у ній. Оновлення по рішенню ЄС (Директива 2001/55). Виплати українцям та правила перетину кордону.
    4. ВАЖЛИВЕ В УКРАЇНІ: Головні події на Батьківщині (заяви перших осіб, стан енергосистеми та зв'язку, хід бойових дій), які хвилюють діаспору.

    <CANDIDATES>
    {json.dumps(prompt_data, ensure_ascii=False, indent=2)}
    </CANDIDATES>

    <HISTORY>
    {json.dumps(history, ensure_ascii=False, indent=2)}
    </HISTORY>
    
    Поверни результат СУВОРО у форматі валідного JSON-об'єкта з двома ключами:
    1. "keep": масив ID новин, які треба ЗАЛИШИТИ (наприклад, [0, 2, 5]).
    2. "rejected": масив об'єктів для всіх інших відхилених новин, де кожен об'єкт містить: 
       - "id": номер новини,
       - "entities_found": масив з 3-5 ключових сутностей (назви компаній, суми, імена, точні локації),
       - "reason": коротка причина відхилення українською (наприклад: "Спорт", "Дублікат", "Світовий шум").
       
    Більше ніякого тексту, тільки чистий JSON.
    """
    
    try:  
        response = call_gemini_json(client, prompt)  
        if stats is not None and getattr(response, 'usage_metadata', None): 
            stats["tokens_used"] += response.usage_metadata.total_token_count
            
        text_response = response.text.strip()
        logging.info(f"Відповідь Gemini (фільтрація): {text_response}")
        
        if "```json" in text_response:
            text_response = text_response.split("```json")[1].split("```")[0].strip()
        elif "```" in text_response:
            text_response = text_response.split("```")[1].split("```")[0].strip()

        data = json.loads(text_response)  
        
        keep_ids = [int(item) for item in data.get("keep", []) if str(item).isdigit()]
        rejected_items = data.get("rejected", []) # Список об'єктів [{"id": X, "reason": "..."}, ...]
        
        filtered_articles = [articles[i] for i in keep_ids if i < len(articles)]  
        
        # Зберігаємо відхилені товари у вигляді словника {index: reason} для подальшого запису в БД
        reasons_map = {}
        for item in rejected_items:
            if isinstance(item, dict) and 'id' in item:
                reasons_map[int(item['id'])] = item.get('reason', 'Не вказано')
                
        removed_count = len(articles) - len(filtered_articles)
        if removed_count > 0: 
            logging.info(f"🗑️ Відсіяно ШІ (дублі/спам/історія): {removed_count}")  
            
        return filtered_articles, reasons_map  
        
    except Exception as e:  
        logging.error(f"❌ Помилка Gemini API у deduplicate_news: {e}. Пропускаємо фільтрацію без втрати новин.")  
        return articles, {}

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

    # ОПТИМІЗАЦІЯ: кешуємо в пам'ять лише недавні URL та заголовки (за останні 3 дні), 
    # щоб уникнути витоку пам'яті та сповільнення при зростанні бази.
    cursor.execute("SELECT url FROM articles WHERE created_at >= NOW() - INTERVAL '3 days'")
    db_urls = {row[0] for row in cursor.fetchall()}
      
    cursor.execute("SELECT title FROM articles WHERE created_at >= NOW() - INTERVAL '3 days'")
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

            # --- 1. ГЕНЕРАЦИЯ ВЕКТОРА (EMBEDDING) ---
            try:
                embed_response = client.models.embed_content(
                    model="text-embedding-004",
                    contents=f"{title}. {description}"
                )
                vector = embed_response.embeddings[0].values
                # Форматируем список в строку вектора для PostgreSQL: "[0.1, 0.2, ...]"
                vector_str = f"[{','.join(map(str, vector))}]"
            except Exception as e:
                logging.error(f"Ошибка генерации вектора для {url}: {e}")
                vector_str = None

            # --- 2. ПРОВЕРКА НА СЕМАНТИЧЕСКИЙ ДУБЛИКАТ В БД (> 90%) ---
            is_semantic_duplicate = False
            if vector_str:
                try:
                    # Ищем косинусное сходство (1 - distance). 
                    cursor.execute("""
                        SELECT title, 1 - (embedding <=> %s::vector) AS similarity 
                        FROM articles 
                        WHERE created_at > NOW() - INTERVAL '3 days' 
                          AND embedding IS NOT NULL
                        ORDER BY embedding <=> %s::vector
                        LIMIT 1
                    """, (vector_str, vector_str))
                    
                    match = cursor.fetchone()
                    if match and match[1] > 0.90:  # Порог 90% сходства
                        logging.info(f"🛑 Векторный фильтр отсек дубликат (Сходство {match[1]:.2f}): {title[:40]}... -> Похоже на: {match[0][:40]}...")
                        is_semantic_duplicate = True
                except Exception as e:
                    logging.error(f"Ошибка векторного поиска: {e}")
                    conn.rollback() # Сброс транзакции при ошибке, чтобы скрипт не упал

            if is_semantic_duplicate:
                stats["funnel"]["already_seen"] += 1
                continue

            # --- 3. ЗАПИСЬ НОВОЙ СТАТЬИ И ЕЕ ВЕКТОРА ---
            images = extract_image_urls(entry)
            try:
                cursor.execute('''
                    INSERT INTO articles (guid, source_id, title, description, url, image_urls, status, embedding)
                    VALUES (%s, %s, %s, %s, %s, %s, 'pending', %s)
                    ON CONFLICT (guid) DO NOTHING
                ''', (guid, source_id, title, description, url, json.dumps(images), vector_str))
            except Exception as e:
                logging.error(f"Ошибка записи в БД: {e}")
                conn.rollback()
              
            db_urls.add(url)
            db_titles.add(title)
              
        stats["funnel"]["scraped_by_source"][source_name] = source_count
              
    conn.commit()
    cursor.close()          
    conn.close()

def process_backlog(stats):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # --- ДОДАНО: Відновлення завислих у статусі 'processing' ---
    try:
        cursor.execute("""
            UPDATE articles 
            SET status = 'pending' 
            WHERE status = 'processing' 
              AND created_at < NOW() - INTERVAL '30 minutes'
        """)
        conn.commit()
    except Exception as db_err:
        logging.error(f"Помилка відновлення завислих processing: {db_err}")
    # ----------------------------------------------------------

    # --- ДОДАНО: Автоматичне очищення старого беклогу ---
    try:
        cursor.execute("UPDATE articles SET status = 'expired' WHERE status = 'pending' AND created_at < NOW() - INTERVAL '12 hours'")
        conn.commit()
    except Exception as db_err:
        logging.error(f"Помилка очищення старого беклогу: {db_err}")
    # ----------------------------------------------------
      
    cursor.execute("""
        SELECT a.guid, a.title, a.description, a.url, a.image_urls, s.name, EXTRACT(EPOCH FROM a.created_at) as timestamp
        FROM articles a JOIN sources s ON a.source_id = s.id 
        WHERE a.status = 'pending' ORDER BY a.created_at DESC
    """)
    rows = cursor.fetchall()
    # ... далі код залишається без змін ...
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
        cursor.close()
        conn.close()
        return

    ukraine_keywords = [r'\bucrain', r'\bzelenski', r'\bkiev', r'\bkiiv', r'\bodesa', r'\bodessa', r'\brefugiat', r'\bprotectie temporara', r'\bprotecție temporară', r'\bprivremen\w*\s+protectie', r'\bукраин', r'\bзеленск', r'\bкиев', r'\bодесс', r'\bбеженц']
    backlog.sort(key=lambda x: x.get('timestamp', 0), reverse=True)
      
    all_sources = list(dict.fromkeys(item['source'] for item in backlog))
    all_sources.sort()
    if not all_sources:
        cursor.close()
        conn.close()
        return
          
    ordered_sources = all_sources.copy()
    random.shuffle(ordered_sources) 
      
    candidate_batch = []
    used_urls = set()
    
    # ОПТИМІЗАЦІЯ: оскільки prioritize_ukraine_news виносить українські новини на початок,
    # перевіряємо їх і робимо ранній вихід (break), коли вони закінчуються.
    sorted_backlog = prioritize_ukraine_news(backlog)
    for item in sorted_backlog:
        text_context = (item.get('title', '') + " " + item.get('description', '')).lower()
        if any(re.search(kw, text_context) for kw in ukraine_keywords):
            if item['url'] not in used_urls:
                candidate_batch.append(item)
                used_urls.add(item['url'])
        else:
            # Зустріли першу не-українську новину — далі йдуть інші, виходимо з циклу пріоритету
            break
                  
    while len(candidate_batch) < 40:
        added_this_round = False
        for src in ordered_sources:
            for item in backlog:
                if item.get('source') == src and item['url'] not in used_urls:
                    candidate_batch.append(item)
                    used_urls.add(item['url'])
                    added_this_round = True
                    break 
            if len(candidate_batch) >= 40: break
        if not added_this_round: break 
              
    candidate_batch = candidate_batch[:40]
    if not candidate_batch:
        cursor.close()
        conn.close()
        return

    cursor.execute("SELECT title FROM articles WHERE status = 'sent' ORDER BY created_at DESC LIMIT 75")
    recent_history_titles = [row[0] for row in cursor.fetchall()]

    # Зберігаємо ID всіх 40 новин, які ми відправили на перевірку
    evaluated_guids = {item['guid']: item for item in candidate_batch}
    
    len_before = len(candidate_batch)
    candidate_batch, reasons_map = deduplicate_news(client, candidate_batch, recent_history_titles, stats)
    stats["funnel"]["duplicates_removed_by_ai"] += (len_before - len(candidate_batch))

    # --- ВИПРАВЛЕННЯ БЕКЛОГУ: позначаємо відхилені новини як rejected з причиною ---
    approved_guids = {item['guid'] for item in candidate_batch}
    
    for idx, item in enumerate(candidate_batch):
        # Якщо стаття не увійшла в approved, вона відхилена
        pass
        
    # Проходимо по всіх кандидатах і оновлюємо статус в БД
    try:
        for idx, item in enumerate(evaluated_guids.values()):
            guid = item['guid']
            if guid not in approved_guids:
                reason = reasons_map.get(idx, 'ШІ фільтр (дублікат/спам)')
                cursor.execute(
                    "UPDATE articles SET status = 'rejected', rejection_reason = %s WHERE guid = %s",
                    (reason, guid)
                )
        conn.commit()
    except Exception as db_err:
        logging.error(f"Помилка оновлення статусу rejected та причин: {db_err}")
    # ---------------------------------------------------------------------

    batch = candidate_batch[:BATCH_SIZE]
    if not batch:
        cursor.close()
        conn.close()
        return
      
    # ДОДАЙТЕ ЦІ ДВА РЯДКИ:
    used_sources_log = list(dict.fromkeys(item.get('source') for item in batch))
    logging.info(f"🔄 Обрано {len(batch)} новин. Фактичні джерела: {', '.join(used_sources_log)}")
      
    # ЗАХИСТ ВІД ДУБЛЮВАННЯ: одразу переводимо обрану партію в статус 'processing'
    batch_guids = [item['guid'] for item in batch]
    try:
        cursor.execute("UPDATE articles SET status = 'processing' WHERE guid = ANY(%s)", (batch_guids,))
        conn.commit()
    except Exception as db_err:
        logging.error(f"Помилка встановлення статусу processing: {db_err}")
          
    prompt = f"Ось список із {len(batch)} новин. Ти — досвідчений головний редактор Telegram-каналу для українців, які живуть у Молдові (діаспора, біженці). Твоє завдання — перекласти та адаптувати ці новини українською мовою.\n\n"
    prompt += """СУВОРІ ПРАВИЛА ПЕРЕКЛАДУ ТА ФОРМАТУВАННЯ:
    1. ГЕОГРАФІЯ: Зберігай оригінальну країну події. Якщо подія сталася в Молдові — пиши про Молдову, якщо в іншій країні (світові новини) — так і залишай. "Молдовани" — це місцеві жителі. НІКОЛИ не замінюй слова "молдовани" або "Молдова" на "українці" чи "Україна", якщо цього немає в оригінальному тексті.
    2. ДЕТАЛІЗАЦІЯ ДОКУМЕНТІВ (ДИРЕКТИВА 2001/55/ЄС): Якщо новина стосується тимчасового захисту, прав біженців, виплат чи документів, ПЕРЕКЛАДАЙ ЇЇ МАКСИМАЛЬНО ДЕТАЛЬНО. Не скорочуй жодної цифри, дати чи юридичної назви. Додай на початок заголовка емодзі 📄 або 🇲🇩.
    3. ПРІОРИТЕТ "УКРАЇНА": Якщо в оригінальній новині згадується Україна, українці, війна або біженці — обов'язково постав емодзі 🇺🇦 на самий початок заголовка та винеси цю інформацію у перший абзац.
    4. ЧИСТОТА МОВИ: Перекладай живою та природною українською мовою. КАТЕГОРИЧНО ЗАБОРОНЕНО вигадувати неіснуючі слова (наприклад, замість кальки "бізшрний" чи "бізарний" використовуй існуючі аналоги: "дивний", "курйозний", "химерний"). Усі слова у твоєму тексті мають існувати в словнику української мови. Перекладай українською мовою АБСОЛЮТНО ВЕСЬ текст. Жодного румунського чи російського слова не повинно залишитись (крім власних назв).
    5. ТОПОНІМИ ТА ІМЕНА: Усі молдовські імена, прізвища, назви вулиць та міст (наприклад, Кишинів, Бєльці, Тирасполь) обов'язково транслітеруй українською. Жодних латинських літер у тексті (крім хештегів у кінці).
    6. СТИЛЬ ТА СТРУКТУРА: Пиши у стилі сучасного новинного Telegram-каналу. Уникай сухого поліцейського сленгу та дослівного перекладу юридичних термінів. Абзаци мають бути короткими (не більше 3-4 речень). Головну думку подавай у першому абзаці (принцип перевернутої піраміди).
    7. НІЯКОГО ШІ-КОНТЕКСТУ: Видавай СУВОРО лише готовий текст новини. Жодних вступних фраз, привітань, коментарів чи висновків від себе. Зберігай абсолютну журналістську об'єктивність.
    8. АКЦЕНТИ: Виділяй жирним шрифтом (в тегах <b>) ключові суми штрафів, дати, імена посадовців або назви законів.
    9. ХЕШТЕГИ: В самому кінці тексту обов'язково додай 2-3 хештеги з нового рядка. Перший хештег — це місто або регіон події оригінальною латиницею (наприклад, #Chisinau, #Balti, #Tiraspol, #Orhei). Якщо точне місто невідоме — пиши #Moldova. Якщо подія має загальнонаціональний масштаб (закони, міжнародні суди, статистика по країні) — пиши ТІЛЬКИ #Moldova. КАТЕГОРИЧНО ЗАБОРОНЕНО ставити хештег країни (наприклад, #Moldova), якщо ти вже поставив хештег конкретного міста!  Другі хештеги — тематичні (наприклад, #Україна, #ТимчасовийЗахист, #Кримінал, #ДТП, #Суспільство). Категорично ЗАБОРОНЕНО ставити хештеги #ТимчасовийЗахист або #Україна, якщо в самому тексті новини про це не йдеться. КАТЕГОРИЧНО ЗАБОРОНЕНО ставити хештег #Chisinau, якщо подія не сталася фізично на вулицях Кишинева (не ліпи хештег столиці на всі підряд новини!).
    10. ОНОВЛЕННЯ ПОДІЙ: Якщо ти бачиш, що новина є продовженням чи оновленням попередніх подій (наприклад, завершення слідства, нові деталі аварії), додай на самий початок заголовка префікс 🔄 [ОНОВЛЕНО]:
    """
    
    for idx, item in enumerate(batch):
        has_image = "Так" if item.get('image_urls') else "Ні"
        prompt += f"=== НОВИНА {idx+1} ===\nМає картинку: {has_image}\nЗаголовок: {item['title']}\nОпис: {item['description']}\n\n"
    
    prompt += f"""
    Для кожної новини виконай наступні вимоги:
    
    1. ЗАГОЛОВОК: Зроби привабливий, змістовний заголовок (обов'язково виділений тегами <b>).
    
    2. ОБ'ЄМ ТА КОНТЕКСТ:
       - Якщо "Має картинку: Так" -> напиши лаконічний текст ДО 900 символів. Якщо суть події повністю розкрита за 500-600 символів — зупинись, не "лий воду" і не розтягуй штучно.
       - Якщо "Має картинку: Ні" -> напиши розширений текст ДО 2500 символів.
       УВАГА: Розширюй текст ВИКЛЮЧНО за рахунок пояснення контексту події та її значення. КАТЕГОРИЧНО ЗАБОРОНЕНО вигадувати неіснуючі імена, цитати, точні цифри чи дати, яких не було в оригіналі.
       
    3. СТРУКТУРА: Дроби текст на короткі абзаци (по 3-4 речення). Для розширених текстів використовуй марковані списки (з символом • або -).
    
    4. TELEGRAM HTML: Використовуй ТІЛЬКИ ці теги: <b>, <i>, <u>, <s>. Ніяких тегів <p>, <br>, <h1>, <li> і ніякої Markdown-розмітки.
    
    5. ФОРМАТ ВИВОДУ: Розділяй готові тексти новин СУВОРО рядком `|||`.
    """
      
    time.sleep(15)
    try:
        response, generated_texts = generate_news_texts(client, prompt, len(batch))
        if getattr(response, 'usage_metadata', None): stats["tokens_used"] += response.usage_metadata.total_token_count
    except Exception as e:
        logging.error(f"❌ Помилка Gemini під час перекладу: {e}")
        stats["errors_count"] += 1 
        
        # --- ДОДАНО: Повертаємо завислі статті назад у чергу ---
        try:
            cursor.execute("UPDATE articles SET status = 'pending' WHERE guid = ANY(%s)", (batch_guids,))
            conn.commit()
            logging.info("🔄 Статуси статей повернуто на 'pending' для повторної спроби у наступному запуску.")
        except Exception as db_err:
            logging.error(f"Помилка повернення статусу в БД: {db_err}")
        # ---------------------------------------------------------
            
        cursor.close()
        conn.close()
        return

    for idx, item in enumerate(batch):
        if idx >= len(generated_texts): break 
        
        # Видалили виклик функції, передаємо порожні tags
        images = item.get('image_urls', [])
          
        if send_to_telegram(generated_texts[idx], item['url'], item.get('source', 'Джерело'), images, tags=""):
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
            try:
                cursor.execute("UPDATE articles SET status = 'failed' WHERE guid = %s", (item['guid'],))
                conn.commit()
            except Exception as db_err:
                logging.error(f"Помилка оновлення статусу failed в БД: {db_err}")

    cursor.close()
    conn.close()

def send_to_telegram(text, url, source_name, image_urls=None, tags=""):
   
    
    # 2. РОЗУМНЕ ВИТЯГУВАННЯ ХЕШТЕГІВ (з Хорватського)
    extracted_tags = ""
    parts = text.split('\n\n')
    if parts and '#' in parts[-1]:
        extracted_tags = "\n\n" + parts[-1].strip()
        text = '\n\n'.join(parts[:-1]) # Залишаємо основний текст без хештегів

    # Обробка списку зображень (з Молдовського)
    if not image_urls: image_urls = []
    elif isinstance(image_urls, str): image_urls = [image_urls]
    
    crop_msg = "...\n\n👉 <i>Деталі за посиланням нижче</i> 👇"
    
    # 3. ДИНАМІЧНИЙ ЛІМІТ з урахуванням витягнутих хештегів
    safe_caption_limit = 920 - len(extracted_tags) - len(tags)
    
    if image_urls and len(text) > safe_caption_limit: 
        text = text[:safe_caption_limit] + crop_msg
    elif not image_urls and len(text) > 3900: 
        text = text[:3900] + crop_msg
    
    # Збираємо повідомлення назад: текст + згенеровані хештеги + статичні теги
    full_message = f"{text}{extracted_tags}{tags}\n\n🔗 Джерело: <a href='{url}'>{source_name}</a>"
    
    payload = {'chat_id': TELEGRAM_CHANNEL_ID}
    
    # Структура відправки (з Молдовського)
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

    # 4. ЗАХИСТ ВІД ЗЛАМАНОГО HTML (Адаптовано з Хорватського для execute_telegram_request)
    try: 
        # Якщо у вас є execute_telegram_request, залишаємо його виклик:
        # return execute_telegram_request(api_url, payload)
        
        # АБО якщо використовуємо requests напряму (що краще для перехоплення помилок HTML):
        resp = requests.post(api_url, data=payload)
        
        if resp.status_code == 400 and "parse entities" in resp.text.lower():
            logging.warning("⚠️ Помилка HTML-розмітки. Відправляємо без HTML...")
            if len(image_urls) > 1:
                media_data = json.loads(payload['media'])
                if 'parse_mode' in media_data[0]: del media_data[0]['parse_mode']
                payload['media'] = json.dumps(media_data)
            elif 'parse_mode' in payload:
                del payload['parse_mode']
            resp = requests.post(api_url, data=payload)
            
        if resp.status_code != 200:
            logging.error(f"❌ Telegram відхилив повідомлення: {resp.text}")
            
        return resp.status_code == 200
        
    except Exception as e: 
        logging.error(f"❌ Telegram API: {e}")
        return False

def main():
    # Перевіряємо токени тут, коли скрипт безпосередньо запускається
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID, GEMINI_API_KEY, DATABASE_URL]):
        logging.error("Критична помилка: не знайдено базові токени або DATABASE_URL у файлі .env!")
        exit(1)
        
    start_time = time.time()
    # ... далі йде словник stats ...
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