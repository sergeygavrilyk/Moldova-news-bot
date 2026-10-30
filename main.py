import datetime
import feedparser
import logging
import requests
import os
import time
import calendar
import json
import re
import html as html_lib
import random
import psycopg2
from urllib.parse import urlparse, urljoin
from dotenv import load_dotenv
from google import genai
from google.genai import types  # ДОДАНО ДЛЯ SAFETY SETTINGS
import cloudscraper
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_not_exception_type, retry_if_exception_type
from typing import Literal
from pydantic import BaseModel, Field

# D-04: фіксований перелік причин відхилення (відповідає крокам промпту фільтра)
REJECTION_CATEGORIES = ("Дублікат", "Вже публікувалось", "Спорт", "Реклама", "Світовий шум", "Не за тематикою")
NO_REASON = "Без причини"  # модель не повернула новину ні в keep, ні в rejected

class RejectedItem(BaseModel):
    id: int
    entities_found: list[str] = Field(description="3-5 ключових фігурантів, компаній, цифр або локацій")
    reason: Literal["Дублікат", "Вже публікувалось", "Спорт", "Реклама", "Світовий шум", "Не за тематикою"]
    detail: str = Field(description="коротке пояснення українською, до 12 слів")

class NewsFilterResponse(BaseModel):
    keep: list[int]
    rejected: list[RejectedItem]
    updates: list[int] = Field(description="ID з keep, які є кардинальною розв'язкою події з HISTORY; [] якщо таких немає")

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
# T-11: тестовий запуск — увесь цикл, але БД лише на читання, а пости йдуть в адмін-чат з позначкою «ТЕСТ»
DRY_RUN = os.getenv('DRY_RUN', '').strip().lower() in ('1', 'true', 'yes')
DRY_RUN_NEW_ARTICLES = []  # нові статті з RSS у тестовому запуску (у БД не записуються)
DATABASE_URL = os.getenv('DATABASE_URL')


MAX_AGE_SECONDS = 43200 # 12 годин
BATCH_SIZE = 3
# Таймаут 180 с на запит: модель, що «зависла», не блокує запуск — каскад перейде до наступної
client = genai.Client(api_key=GEMINI_API_KEY, http_options=types.HttpOptions(timeout=180_000))

# --- МОДЕЛІ GEMINI (каскад: перша доступна модель зі списку) ---
# На безкоштовному тарифі у кожної моделі своя денна квота
# (flash-моделі ~20 запитів/добу, lite-моделі ~500), тому використовуємо їх по черзі.
# Змінити без правки коду: GitHub -> Settings -> Secrets and variables -> Actions -> Variables
# (GEMINI_FILTER_MODELS / GEMINI_TRANSLATE_MODELS, назви через кому).
# Перевірено 30.10.2026 (workflow "Check Gemini models"): gemini-2.5-flash / 2.5-flash-lite — 404 для нових ключів,
# gemini-3.8-flash — періодично 503 (перевантаження).
# Знято з обігу 08.10.2026 (docs/deprecations): gemini-3.5-flash -> переадресація на 3.6-flash,
# gemini-3.7-flash -> на 3.8-flash — НЕ використовувати (фактично витрачають квоту інших моделей).
# Фільтр: gemini-3-flash-preview (окрема flash-модель, не перетинається з моделями перекладу) -> lite як резерв.
# gemini-3.1-flash-lite: відключення не раніше 07.05.2027.
DEFAULT_FILTER_MODELS = "gemini-3-flash-preview,gemini-3.5-flash-lite,gemini-3.1-flash-lite"
DEFAULT_TRANSLATE_MODELS = "gemini-3.8-flash,gemini-3.6-flash,gemini-3.5-flash-lite"

def parse_models(env_name, default):
    raw = os.getenv(env_name) or default
    return [m.strip() for m in raw.split(',') if m.strip()]

FILTER_MODELS = parse_models('GEMINI_FILTER_MODELS', DEFAULT_FILTER_MODELS)
TRANSLATE_MODELS = parse_models('GEMINI_TRANSLATE_MODELS', DEFAULT_TRANSLATE_MODELS)

EXHAUSTED_MODELS = set()  # моделі, чия квота вичерпана в цьому запуску — більше не смикаємо
MODEL_USAGE = {"filter": None, "translate": None, "failed_attempts": 0}
# ===================================================

class GeminiQuotaExhausted(Exception):
    """Усі моделі каскаду вичерпали квоту — повторювати в цьому запуску марно."""

def is_quota_error(error):
    return getattr(error, 'code', None) == 429 or 'RESOURCE_EXHAUSTED' in str(error)

# Статистика викликів моделей (T-22): кожна спроба -> рядок у таблиці model_calls наприкінці запуску
MODEL_CALLS = []

def classify_model_error(error):
    """Тип збою моделі для статистики: quota | unavailable | timeout | bad_response | error."""
    code = getattr(error, 'code', None)
    text = str(error)
    if code == 429 or 'RESOURCE_EXHAUSTED' in text:
        return 'quota', code or 429
    if code in (500, 502, 503, 504) or 'UNAVAILABLE' in text or 'overloaded' in text.lower() or 'high demand' in text.lower():
        return 'unavailable', code or 503
    if 'timeout' in type(error).__name__.lower() or 'timed out' in text.lower() or 'timeout' in text.lower():
        return 'timeout', None
    if isinstance(error, ValueError):  # порожня відповідь, неправильна кількість текстів тощо
        return 'bad_response', None
    return 'error', code if isinstance(code, int) else None

def record_model_call(task, model, outcome, started, http_code=None, error=None):
    MODEL_CALLS.append({
        'task': task, 'model': model, 'outcome': outcome, 'http_code': http_code,
        'latency_ms': int((time.time() - started) * 1000),
        'error': _mask_token(error)[:300] if error is not None else None,
    })

def model_calls_summary():
    """Коротко для звіту адміну: «3.8-flash ✅ | 3.6-flash 503×2»."""
    marks = {'ok': '✅', 'quota': '⛽', 'unavailable': '503', 'timeout': '⏱', 'bad_response': '⚠️', 'error': '❌'}
    counts = {}
    for c in MODEL_CALLS:
        key = (c['task'], c['model'].replace('gemini-', ''), c['outcome'])
        counts[key] = counts.get(key, 0) + 1
    parts = []
    for (task, model, outcome), n in counts.items():
        mark = marks.get(outcome, outcome)
        parts.append(f"{model} {mark}" + (f"×{n}" if n > 1 else ""))
    return ' | '.join(parts)

def run_model_cascade(task, models, call):
    """Пробує моделі по черзі. Квота вичерпана (429) -> модель пропускається до кінця запуску
    і одразу береться наступна; інша помилка -> пауза 5 с і наступна модель."""
    available = [m for m in models if m not in EXHAUSTED_MODELS]
    if not available:
        raise GeminiQuotaExhausted(f"{task}: квоту вичерпано для всіх моделей ({', '.join(models)})")

    last_error = None
    for model in available:
        started = time.time()
        try:
            result = call(model)
            record_model_call(task, model, 'ok', started)
            MODEL_USAGE[task] = model
            logging.info(f"🤖 {task}: відповіла модель {model}")
            return result
        except Exception as e:
            last_error = e
            outcome, http_code = classify_model_error(e)
            record_model_call(task, model, outcome, started, http_code, e)
            MODEL_USAGE["failed_attempts"] += 1
            if is_quota_error(e):
                EXHAUSTED_MODELS.add(model)
                logging.warning(f"⛽ {task}: квоту моделі {model} вичерпано — переходимо до наступної")
            else:
                logging.warning(f"⚠️ {task}: модель {model} недоступна ({e}). Перемикаємось...")
                time.sleep(5)

    if all(m in EXHAUSTED_MODELS for m in available):
        raise GeminiQuotaExhausted(f"{task}: квоту вичерпано для всіх моделей ({', '.join(models)})")
    # Інша помилка — її підхопить @retry (tenacity) і повторить каскад після паузи
    raise last_error

class _ReadOnlyCursor:
    """DRY_RUN: SELECT виконується по-справжньому, INSERT/UPDATE/DELETE/DDL — лише пишуться в лог."""
    _WRITE = ('INSERT', 'UPDATE', 'DELETE', 'ALTER', 'CREATE', 'DROP', 'TRUNCATE', 'VACUUM')

    def __init__(self, cursor):
        self._cursor, self._skipped = cursor, False

    def execute(self, sql, params=None):
        first = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ''
        if first in self._WRITE:
            self._skipped = True
            logging.debug(f"🧪 DRY_RUN: пропущено запис у БД: {' '.join(sql.split())[:80]}")
            return None
        self._skipped = False
        return self._cursor.execute(sql, params)

    def fetchall(self):
        return [] if self._skipped else self._cursor.fetchall()

    def fetchone(self):
        return None if self._skipped else self._cursor.fetchone()

    def close(self):
        self._cursor.close()

class _ReadOnlyConnection:
    def __init__(self, conn):
        self._conn = conn

    def cursor(self):
        return _ReadOnlyCursor(self._conn.cursor())

    def commit(self):
        pass  # у тестовому запуску нічого не зберігаємо

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.rollback()
        self._conn.close()

def get_db_connection():
    conn = psycopg2.connect(DATABASE_URL)
    return _ReadOnlyConnection(conn) if DRY_RUN else conn

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
        models_str = f"фільтр — {MODEL_USAGE['filter'] or '—'} | переклад — {MODEL_USAGE['translate'] or '—'}"
        exhausted_str = f"\n⛽ Квоту вичерпано: {', '.join(sorted(EXHAUSTED_MODELS))}" if EXHAUSTED_MODELS else ""

        message = (
            f"{'🧪 <b>ТЕСТОВИЙ ЗАПУСК (DRY_RUN)</b> — нічого не опубліковано й не записано в БД' + chr(10) + chr(10) if DRY_RUN else ''}"
            f"📊 <b>Розширений звіт бота (Молдова)</b>\n\n"
            f"⏱ Час: {stats['execution_time']} сек | ⚠️ Помилок: {stats['errors_count']} | 🔄 Невдалих спроб моделей: {stats['gemini_retries']}\n"
            f"🤖 Моделі: {models_str}{exhausted_str}\n"
            f"{('🧾 Спроби: ' + model_calls_summary() + chr(10)) if MODEL_CALLS else ''}"
            f"🪙 <b>Токени (сьогодні/тиждень/місяць):</b>\n   {tokens_day:,} / {tokens_week:,} / {tokens_month:,}\n\n"
            f"🔍 <b>Воронка фільтрації:</b>\n▪️ Знайдено в RSS: {stats['funnel']['scraped_total']}\n▪️ По джерелах: {scraped_src_str}\n"
            f"▪️ Пропущено (старі/вже в історії): {stats['funnel']['already_seen']}\n▪️ Відсіяно фільтрами: {stats['funnel']['rejected_by_keywords']}\n"
            f"▪️ Відсіяно Gemini: {stats['funnel']['duplicates_removed_by_ai']}"
            f"{(' — ' + ', '.join(f'{k} {v}' for k, v in sorted(stats['funnel'].get('rejected_reasons', {}).items(), key=lambda kv: -kv[1]))) if stats['funnel'].get('rejected_reasons') else ''}\n\n"
            f"📥 <b>Беклог (очікують ШІ-валідації: {stats['backlog']['total_size']}):</b>\n▪️ {backlog_src_str}\n\n"
            f"✅ <b>{'Підготовлено (тест, не опубліковано)' if DRY_RUN else 'Опубліковано'}: {stats['published']['total']}</b>\n"
            f"▪️ Формат: З фото ({stats['published']['with_images']}; фото знайдено на сторінках статей: {stats['published'].get('og_images_found', 0)}) | Текст ({stats['published']['text_only']})\n"
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

def clean_description(raw, max_len=1500):
    """Опис з RSS без HTML-розмітки й зайвих пробілів (D-15): менше токенів, чистіші ембединги."""
    text = re.sub(r'<[^>]+>', ' ', raw or '')
    text = html_lib.unescape(text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text[:max_len]

UNSUPPORTED_IMAGE_FORMATS = ('.webp', '.avif', '.svg')  # Telegram їх не приймає як фото

def extract_image_urls(entry):
    unsupported_formats = UNSUPPORTED_IMAGE_FORMATS
    urls = []
    if 'media_content' in entry:
        for media in entry.media_content:
            url = media.get('url')
            if url and not url.lower().endswith(unsupported_formats) and url not in urls: 
                urls.append(url)
    if 'enclosures' in entry:
        for enc in entry.enclosures:
            if enc.get('type', '').startswith('image/'):
                url = enc.get('href')
                if url and not url.lower().endswith(unsupported_formats) and url not in urls: 
                    urls.append(url)
    return urls[:10]

# --- Фото зі сторінки статті (og:image), якщо в RSS картинки немає ---
_OG_META_RE = re.compile(r'<meta\b[^>]*?\b(?:property|name)\s*=\s*["\'](?:og:image|og:image:url|twitter:image)["\'][^>]*>', re.I)
_CONTENT_RE = re.compile(r'\bcontent\s*=\s*["\']([^"\']+)["\']', re.I)

def find_og_image(page_html, page_url):
    """Головне зображення статті з meta og:image / twitter:image.
    Пропускає непідтримувані Telegram формати і явні логотипи сайту."""
    for tag in _OG_META_RE.findall(page_html[:300000]):
        m = _CONTENT_RE.search(tag)
        if not m:
            continue
        url = urljoin(page_url, html_lib.unescape(m.group(1).strip()))
        if not url.lower().startswith(('http://', 'https://')):
            continue
        path = urlparse(url).path.lower()
        if path.endswith(UNSUPPORTED_IMAGE_FORMATS) or 'logo' in path:
            continue
        return url
    return None

def fetch_og_image(scraper, page_url, timeout=10):
    try:
        response = scraper.get(page_url, timeout=timeout)
        if response.status_code != 200:
            return None
        return find_og_image(response.text, page_url)
    except Exception as e:
        logging.warning(f"og:image не отримано для {page_url}: {e}")
        return None

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

SAFETY_OFF = [
    types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE"),
    types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_NONE"),
    types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_NONE"),
    types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_NONE"),
]

# Повтори tenacity: лише для тимчасових збоїв. Якщо квоту вичерпано для всіх моделей —
# одразу здаємося (повтори в цьому запуску не допоможуть і лише марнують час).
@retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=2, min=5, max=60),
       retry=retry_if_not_exception_type(GeminiQuotaExhausted), reraise=True)
def call_gemini_json(client, prompt):
    # Фільтр/дедуплікація: потрібен лише JSON за схемою -> lite-моделі з великою квотою (FILTER_MODELS)
    def call(model):
        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=NewsFilterResponse,
                temperature=0.0,
                safety_settings=SAFETY_OFF
            )
        )
        # ЗАХИСТ: Перевірка на порожню відповідь
        if not response or not getattr(response, 'text', None):
            raise ValueError(f"Порожня відповідь (None) з моделі {model}")
        return response

    return run_model_cascade("filter", FILTER_MODELS, call)

@retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=2, min=15, max=60),
       retry=retry_if_not_exception_type(GeminiQuotaExhausted), reraise=True)
def generate_news_texts(client, prompt, expected_count):
    # Переклад: flash-моделі, lite — лише останній резерв (TRANSLATE_MODELS)
    def call(model):
        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.0,
                safety_settings=SAFETY_OFF
            )
        )

        if not response or not getattr(response, 'text', None):
            raise ValueError(f"Порожня відповідь від {model}")

        logging.info(f"Текст від Gemini ({model}):\n{response.text}")

        # --- ЖОРСТКЕ ВИДАЛЕННЯ ГАЛЮЦИНАЦІЙ ШІ ---
        HOMOGLYPHS = {
            'ά': 'а', 'α': 'а', 'ό': 'о', 'ο': 'о',
            'έ': 'е', 'ε': 'е', 'ί': 'і', 'ι': 'і',
            'ή': 'и', 'η': 'и', 'ν': 'н', 'Ν': 'Н',
            'ב': 'б', 'ب': 'б',
            'є́': 'є', 'і́': 'і'
        }

        clean_text = response.text
        clean_text = clean_text.replace('\u0301', '') # Видалення невидимого наголосу

        for bad_char, good_char in HOMOGLYPHS.items():
            clean_text = clean_text.replace(bad_char, good_char)

        texts = [t.strip() for t in clean_text.strip().split('|||') if t.strip()]

        if len(texts) != expected_count:
            raise ValueError(f"Невірна кількість текстів від {model}: очікували {expected_count}, отримали {len(texts)}")

        return response, texts

    return run_model_cascade("translate", TRANSLATE_MODELS, call)

# ================= TELEGRAM HTML (D-12, D-19) =================
# Теги, які розуміє Telegram parse_mode=HTML
_TG_TAG_RE = re.compile(r'<(/?)(b|strong|i|em|u|ins|s|strike|del|a|code|pre)\b([^<>]*)>', re.I)
# & що не є дозволеною сутністю (Telegram знає лише &lt; &gt; &amp; &quot; і числові)
_TG_BAD_AMP_RE = re.compile(r'&(?!(?:lt|gt|amp|quot|#\d+|#x[0-9a-fA-F]+);)')

def sanitize_telegram_html(text):
    """Робить текст безпечним для parse_mode=HTML: екранує & < > поза дозволеними тегами,
    відкидає закриваючі теги без пари, закриває незакриті. Повторний виклик нічого не змінює."""
    text = _TG_BAD_AMP_RE.sub('&amp;', text)
    out, stack, pos = [], [], 0
    for m in _TG_TAG_RE.finditer(text):
        out.append(text[pos:m.start()].replace('<', '&lt;').replace('>', '&gt;'))
        closing, name = m.group(1) == '/', m.group(2).lower()
        if not closing:
            stack.append(name)
            out.append(m.group(0))
        elif name in stack:
            while stack:  # закриваємо і все, що відкрите всередині (правильна вкладеність)
                top = stack.pop()
                out.append(f'</{top}>')
                if top == name:
                    break
        pos = m.end()
    out.append(text[pos:].replace('<', '&lt;').replace('>', '&gt;'))
    out.extend(f'</{tag}>' for tag in reversed(stack))
    return ''.join(out)

def crop_html(text, limit):
    """Обрізає HTML-текст до ~limit символів по межі абзацу / речення / слова,
    не розриваючи тегів і сутностей; незакриті теги закриваються."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    lt, gt = cut.rfind('<'), cut.rfind('>')
    if lt > gt:                      # обірваний тег у кінці
        cut = cut[:lt]
    amp = cut.rfind('&')
    if amp != -1 and ';' not in cut[amp:]:  # обірвана сутність
        cut = cut[:amp]
    floor = int(len(cut) * 0.6)      # не відкидаємо більше 40 % тексту заради красивої межі
    for sep in ('\n\n', '. ', '! ', '? ', '.\n', '\n', ' '):
        i = cut.rfind(sep)
        if i >= floor:
            cut = cut[:i + 1] if sep[0] in '.!?' else cut[:i]
            break
    cut = cut.rstrip()
    if not cut.endswith(('.', '!', '?', '…')):
        cut += '…'
    return sanitize_telegram_html(cut)

def html_to_plain(text):
    """Запасний варіант, якщо Telegram усе ж не прийняв розмітку: чистий текст без сирих тегів,
    посилання <a href=X>Y</a> -> «Y: X»."""
    text = re.sub(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', r'\2: \1', text, flags=re.I | re.S)
    text = re.sub(r'<[^>]+>', '', text)
    return html_lib.unescape(text)

def _mask_token(s):
    return str(s).replace(TELEGRAM_BOT_TOKEN, '***') if TELEGRAM_BOT_TOKEN else str(s)

class TelegramRetryableError(Exception):
    """Тимчасова помилка Telegram (мережа, 429, 5xx) — є сенс повторити."""

def _post_telegram(api_url, payload):
    try:
        return requests.post(api_url, data=payload, timeout=60)
    except requests.RequestException as e:
        raise TelegramRetryableError(f"Мережева помилка Telegram: {_mask_token(e)}")

# D-16: повторюємо лише тимчасові помилки; 4xx (погане фото тощо) — одразу у фолбек
@retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=2, min=3, max=30),
       retry=retry_if_exception_type(TelegramRetryableError), reraise=True)
def execute_telegram_request(api_url, payload):
    resp = _post_telegram(api_url, payload)

    # ЗАХИСТ ВІД ЗЛАМАНИХ ТЕГІВ: відправляємо чистим текстом, а не сирими тегами
    if resp.status_code == 400 and "parse entities" in resp.text.lower():
        logging.warning(f"⚠️ Telegram не прийняв HTML-розмітку ({resp.text[:200]}). Відправляємо чистим текстом...")
        if 'media' in payload:
            media_objs = json.loads(payload['media'])
            media_objs[0].pop('parse_mode', None)
            if 'caption' in media_objs[0]:
                media_objs[0]['caption'] = html_to_plain(media_objs[0]['caption'])
            payload['media'] = json.dumps(media_objs)
        payload.pop('parse_mode', None)
        for key in ('text', 'caption'):
            if key in payload:
                payload[key] = html_to_plain(payload[key])
        resp = _post_telegram(api_url, payload)

    if resp.status_code == 200:
        return True
    if resp.status_code == 429:
        try:
            retry_after = int(resp.json().get('parameters', {}).get('retry_after', 5))
        except Exception:
            retry_after = 5
        retry_after = min(max(retry_after, 1), 60)
        logging.warning(f"⏳ Telegram 429: забагато запитів, чекаємо {retry_after} с")
        time.sleep(retry_after)
        raise TelegramRetryableError(f"Telegram 429: {resp.text[:200]}")
    if resp.status_code >= 500:
        raise TelegramRetryableError(f"Telegram {resp.status_code}: {resp.text[:200]}")
    raise Exception(f"Telegram Error {resp.status_code}: {resp.text[:300]}")

def normalize_reason(raw):
    """Зводить будь-яку причину від моделі до однієї з REJECTION_CATEGORIES (D-04).
    Страховка на випадок, якщо модель відповість не за переліком."""
    if raw in REJECTION_CATEGORIES:
        return raw
    text = (raw or '').lower()
    if any(k in text for k in ('істор', 'вже ', 'висвітлюва', 'згадува', 'публікува')):
        return "Вже публікувалось"
    if 'дубл' in text:
        return "Дублікат"
    if 'спорт' in text:
        return "Спорт"
    if 'реклам' in text or 'промо' in text:
        return "Реклама"
    if any(k in text for k in ('шум', 'лайфстайл', 'світськ', 'третіх країн')):
        return "Світовий шум"
    return "Не за тематикою" if text.strip() else NO_REASON

def deduplicate_news(client, articles, recent_history=None, stats=None):
    if not articles: return articles, {}
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
    (ЗАЛИШАЙ кандидатів з історією ТІЛЬКИ у випадку кардинальної розв'язки: наприклад, злочинця спіймали, суд виніс остаточний вирок, потерпілий помер). Кожного такого кандидата додай також у масив "updates" — система позначить його як оновлення.

    КРОК 4. СТОП-ТЕМИ (ЩО БЕЗЖАЛЬНО ВІДХИЛЯТИ): 
    1. СПОРТ: Будь-які матчі, турніри, результати, трансфери, спортсмени.
    2. РЕКЛАМА: Спонсорські матеріали, промо-акції, розіграші.
    3. СВІТОВИЙ ШУМ ТА ЛАЙФСТАЙЛ: Абстрактна наука (як-от дослідження Гарварда), поради дієтологів, рецепти, психологія та події в третіх країнах, ЯКЩО вони не стосуються безпосередньо Молдови чи України.

    КРОК 5. ТЕМАТИКА (ЩО ЗАЛИШИТИ): 
    Залишай ТІЛЬКИ ті новини, які пройшли Кроки 1-4 і належать до однієї з чотирьох категорій:
    1. ВАЖЛИВІ МОЛДОВСЬКІ НОВИНИ: Будь-які резонансні події всередині Молдови (кримінал, надзвичайні ситуації, політика, економіка, суспільство, а також культура та цікаві технологічні новини) — НАВІТЬ ЯКЩО там не згадуються українці. Це основа каналу.
    2. УКРАЇНЦІ В МОЛДОВІ: Події всередині країни, пов'язані з діаспорою, статусом захисту, кордоном, виплатами чи допомогою.
    3. ЄВРОПЕЙСЬКІ ТА СВІТОВІ РІШЕННЯ: Документи, закони та допомога, що безпосередньо впливають на ситуацію в Молдові чи на українців у ній. Оновлення по рішенню ЄС (Директива 2001/55). Виплати українцям та правила перетину кордону.
    4. ВАЖЛИВЕ В УКРАЇНІ: Головні події на Батьківщині (заяви перших осіб, стан енергосистеми та зв'язку, хід бойових дій, війна, енергетика, економіка, життя, політика, технології, оборона), які хвилюють діаспору.

    <CANDIDATES>
    {json.dumps(prompt_data, ensure_ascii=False, indent=2)}
    </CANDIDATES>

    <HISTORY>
    {json.dumps(history, ensure_ascii=False, indent=2)}
    </HISTORY>
    
    Поверни результат СУВОРО у форматі валідного JSON-об'єкта з трьома ключами:
    1. "keep": масив ID новин, які треба ЗАЛИШИТИ (наприклад, [0, 2, 5]).
    2. "rejected": масив об'єктів для всіх інших відхилених новин, де кожен об'єкт містить: 
       - "id": номер новини,
       - "entities_found": масив з 3-5 ключових сутностей (назви компаній, суми, імена, точні локації),
       - "reason": ОДНА категорія СУВОРО з переліку (дослівно):
           "Дублікат" — та сама коренева подія, що й в іншого кандидата (КРОК 2);
           "Вже публікувалось" — коренева подія вже є в <HISTORY> (КРОК 3);
           "Спорт", "Реклама", "Світовий шум" — стоп-теми (КРОК 4; «Світовий шум» включає лайфстайл і події в третіх країнах);
           "Не за тематикою" — не належить до жодної з чотирьох категорій КРОКУ 5 (дрібна місцева подія, анонс тощо),
       - "detail": коротке пояснення українською, до 12 слів.
    3. "updates": масив ID з "keep", які є кардинальною розв'язкою події, що вже є в <HISTORY> (наприклад, [2]). Якщо таких немає — порожній масив [].
       
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
        
        # dict.fromkeys — прибираємо повтори ID, зберігаючи порядок (D-13: інакше новина вийде двічі)
        keep_ids = list(dict.fromkeys(int(item) for item in data.get("keep", []) if str(item).isdigit()))
        rejected_items = data.get("rejected", []) # Список об'єктів [{"id": X, "reason": "..."}, ...]
        
        filtered_articles = [articles[i] for i in keep_ids if i < len(articles)]  

        # Q-05 п.2: позначку «оновлено» визначає фільтр (він бачить HISTORY), а не перекладач
        update_ids = {int(x) for x in data.get("updates", []) if str(x).isdigit()} & set(keep_ids)
        for i in keep_ids:
            if i < len(articles):
                articles[i]['is_update'] = i in update_ids
        
        # Зберігаємо відхилені товари у вигляді словника {index: reason} для подальшого запису в БД
        reasons_map = {}
        for item in rejected_items:
            if isinstance(item, dict) and str(item.get('id', '')).isdigit():
                idx = int(item['id'])
                category = normalize_reason(item.get('reason'))
                reasons_map[idx] = category
                if idx < len(articles):
                    logging.info(f"   ✖ [{idx}] {category}: {item.get('detail', '')} | {articles[idx]['title'][:70]}")
                
        removed_count = len(articles) - len(filtered_articles)
        if removed_count > 0: 
            logging.info(f"🗑️ Відсіяно ШІ (дублі/спам/історія): {removed_count}")  
            
        return filtered_articles, reasons_map  
        
    except Exception as e:
        # Фільтр не спрацював -> НЕ публікуємо неперевірене (AUDIT A4 / T-06).
        # Повертаємо None: статті лишаються 'pending' і підуть у фільтр наступного запуску.
        logging.error(f"❌ Помилка Gemini API у deduplicate_news: {e}. Публікацію в цьому запуску пропущено, статті лишаються в черзі.")
        if stats is not None:
            stats["errors_count"] += 1
        return None, {}

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
            # D-20: байти, а не текст — feedparser сам візьме кодування з XML-заголовка
            feed = feedparser.parse(response.content)
            # D-18: порожня стрічка (або HTML-сторінка замість RSS) — це помилка джерела, а не «нічого нового»
            if not feed.entries:
                raise Exception("Порожня стрічка: 0 записів (можливо, сайт віддав сторінку замість RSS)")
            cursor.execute("UPDATE sources SET status = 'ok', error_count = 0, last_error_log = NULL, last_checked = %s WHERE id = %s", (now, source_id))
            conn.commit()
        except Exception as e:
            conn.rollback()  # на випадок, якщо впав сам UPDATE — інакше наступні запити не виконаються
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
            published_at = None  # D-14: час публікації на сайті (UTC); None — у стрічці дати немає
            if hasattr(entry, 'published_parsed') and entry.published_parsed:
                article_time = calendar.timegm(entry.published_parsed)
                if current_time - article_time > MAX_AGE_SECONDS:
                    stats["funnel"]["already_seen"] += 1
                    continue
                # Дата «з майбутнього» (неправильний часовий пояс у стрічці) -> вважаємо, що опубліковано зараз
                published_at = datetime.datetime.fromtimestamp(min(article_time, current_time), datetime.timezone.utc).replace(tzinfo=None)
                  
            description = clean_description(entry.get('summary', '')) or title
            if not is_quality_article(title, description, url): 
                stats["funnel"]["rejected_by_keywords"] += 1
                continue

            # --- 1. ГЕНЕРАЦИЯ ВЕКТОРА (EMBEDDING) ---
            try:
                embed_response = client.models.embed_content(
                    model="gemini-embedding-001",
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
                    INSERT INTO articles (guid, source_id, title, description, url, image_urls, status, embedding, published_at)
                    VALUES (%s, %s, %s, %s, %s, %s, 'pending', %s, %s)
                    ON CONFLICT (guid) DO NOTHING
                ''', (guid, source_id, title, description, url, json.dumps(images), vector_str, published_at))
                # Комітимо КОЖНУ статтю окремо: помилка наступної вставки (rollback)
                # більше не скасовує все, що вже зібрано в цьому запуску (AUDIT A3 / T-05)
                conn.commit()
            except Exception as e:
                logging.error(f"Ошибка записи в БД: {e}")
                conn.rollback()

            if DRY_RUN:  # у БД нічого не записано — передаємо статтю у відбір напряму
                DRY_RUN_NEW_ARTICLES.append({
                    'guid': guid, 'title': title, 'description': description, 'url': url,
                    'image_urls': images, 'source': source_name, 'timestamp': article_time,
                })
              
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
        # D-14: вік рахуємо від публікації на сайті (якщо відома), а не від моменту збору
        cursor.execute(
            "UPDATE articles SET status = 'expired' WHERE status = 'pending' "
            "AND COALESCE(published_at, created_at) < NOW() - make_interval(secs => %s)",
            (MAX_AGE_SECONDS,)
        )
        conn.commit()
    except Exception as db_err:
        logging.error(f"Помилка очищення старого беклогу: {db_err}")
    # ----------------------------------------------------
      
    cursor.execute("""
        SELECT a.guid, a.title, a.description, a.url, a.image_urls, s.name,
               EXTRACT(EPOCH FROM COALESCE(a.published_at, a.created_at)) as timestamp
        FROM articles a JOIN sources s ON a.source_id = s.id 
        WHERE a.status = 'pending' ORDER BY COALESCE(a.published_at, a.created_at) DESC
    """)
    rows = cursor.fetchall()
    # ... далі код залишається без змін ...
    backlog = []
    for r in rows:
        backlog.append({
            'guid': r[0], 'title': r[1], 'description': clean_description(r[2]) or r[1], 'url': r[3], 
            'image_urls': r[4] if r[4] else [], 'source': r[5], 'timestamp': r[6] or time.time()
        })
          
    if DRY_RUN and DRY_RUN_NEW_ARTICLES:
        known = {item['guid'] for item in backlog}
        backlog.extend(a for a in DRY_RUN_NEW_ARTICLES if a['guid'] not in known)
        logging.info(f"🧪 DRY_RUN: до черги додано {len(DRY_RUN_NEW_ARTICLES)} нових статей з RSS (без запису в БД)")

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
    if candidate_batch is None:
        # Фільтр Gemini недоступний: нічого не відхиляємо і не публікуємо (статті лишаються 'pending')
        cursor.close()
        conn.close()
        return
    stats["funnel"]["duplicates_removed_by_ai"] += (len_before - len(candidate_batch))

    # --- ВИПРАВЛЕННЯ БЕКЛОГУ: позначаємо відхилені новини як rejected з причиною ---
    approved_guids = {item['guid'] for item in candidate_batch}

    if DRY_RUN:
        send_dry_run_filter_report(list(evaluated_guids.values()), approved_guids, reasons_map, candidate_batch)
    
    for idx, item in enumerate(candidate_batch):
        # Якщо стаття не увійшла в approved, вона відхилена
        pass
        
    # Проходимо по всіх кандидатах і оновлюємо статус в БД
    try:
        for idx, item in enumerate(evaluated_guids.values()):
            guid = item['guid']
            if guid not in approved_guids:
                reason = reasons_map.get(idx, NO_REASON)
                stats["funnel"].setdefault("rejected_reasons", {})
                stats["funnel"]["rejected_reasons"][reason] = stats["funnel"]["rejected_reasons"].get(reason, 0) + 1
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

    # --- ФОТО ЗІ СТОРІНКИ СТАТТІ: до перекладу, бо від наявності фото залежить ліміт тексту (900/2500) ---
    page_scraper = None
    for item in batch:
        if item.get('image_urls'):
            continue
        page_scraper = page_scraper or cloudscraper.create_scraper(browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False})
        og_image = fetch_og_image(page_scraper, item['url'])
        if not og_image:
            continue
        item['image_urls'] = [og_image]
        stats["published"]["og_images_found"] = stats["published"].get("og_images_found", 0) + 1
        logging.info(f"🖼 Фото зі сторінки статті: {item['url']} -> {og_image}")
        try:
            cursor.execute("UPDATE articles SET image_urls = %s WHERE guid = %s", (json.dumps([og_image]), item['guid']))
            conn.commit()
        except Exception as db_err:
            logging.error(f"Помилка збереження og:image: {db_err}")
            conn.rollback()
          
    prompt = f"Ось список із {len(batch)} новин. Ти — досвідчений головний редактор Telegram-каналу для українців, які живуть у Молдові (діаспора, біженці). Твоє завдання — перекласти та адаптувати ці новини українською мовою.\n\n"
    prompt += """СУВОРІ ПРАВИЛА ПЕРЕКЛАДУ ТА ФОРМАТУВАННЯ:
    1. ГЕОГРАФІЯ: Зберігай оригінальну країну події. Якщо подія сталася в Молдові — пиши про Молдову, якщо в іншій країні (світові новини) — так і залишай. "Молдовани" — це місцеві жителі. НІКОЛИ не замінюй слова "молдовани" або "Молдова" на "українці" чи "Україна", якщо цього немає в оригінальному тексті.
    2. ДЕТАЛІЗАЦІЯ ДОКУМЕНТІВ (ДИРЕКТИВА 2001/55/ЄС): Якщо новина стосується тимчасового захисту, прав біженців, виплат чи документів, ПЕРЕКЛАДАЙ ЇЇ МАКСИМАЛЬНО ДЕТАЛЬНО. Не скорочуй жодної цифри, дати чи юридичної назви. Додай на початок заголовка емодзі 📄 (порядок значків — правило 11).
    3. ПРІОРИТЕТ "УКРАЇНА": Якщо в оригінальній новині згадується Україна, українці, війна або біженці — обов'язково постав емодзі 🇺🇦 на початок заголовка (порядок значків — правило 11) та винеси цю інформацію у перший абзац.
    4. ЧИСТОТА МОВИ: Перекладай живою та природною українською мовою. КАТЕГОРИЧНО ЗАБОРОНЕНО вигадувати неіснуючі слова (наприклад, замість кальки "бізшрний" чи "бізарний" використовуй існуючі аналоги: "дивний", "курйозний", "химерний"). Усі слова у твоєму тексті мають існувати в словнику української мови. Перекладай українською мовою АБСОЛЮТНО ВЕСЬ текст. Жодного румунського чи російського слова не повинно залишитись (крім власних назв).
    5. ТОПОНІМИ, ІМЕНА ТА НАЗВИ: Усі молдовські імена, прізвища, назви вулиць та міст (наприклад, Кишинів, Бєльці, Тирасполь) обов'язково транслітеруй українською. Абревіатури, назви партій, компаній і брендів залишай в оригінальному написанні латиницею (наприклад, PAS, BNM, Binance, Energocom). Інших латинських слів у тексті бути не повинно (крім хештегів у кінці).
    6. СТИЛЬ ТА СТРУКТУРА: Пиши у стилі сучасного новинного Telegram-каналу. Уникай сухого поліцейського сленгу та дослівного перекладу юридичних термінів. Абзаци мають бути короткими (не більше 3-4 речень). Головну думку подавай у першому абзаці (принцип перевернутої піраміди).
    7. НІЯКОГО ШІ-КОНТЕКСТУ: Видавай СУВОРО лише готовий текст новини. Жодних вступних фраз, привітань, коментарів чи висновків від себе. Зберігай абсолютну журналістську об'єктивність.
    8. АКЦЕНТИ: Виділяй жирним шрифтом (в тегах <b>) ключові суми штрафів, дати, імена посадовців або назви законів.
    9. ХЕШТЕГИ: В самому кінці тексту обов'язково додай 2-3 хештеги з нового рядка. Перший хештег — місце події латиницею:
       - подія в Молдові: місто або регіон (наприклад, #Chisinau, #Balti, #Tiraspol, #Orhei); якщо точне місто невідоме або подія загальнонаціональна (закони, міжнародні суди, статистика по країні) — ТІЛЬКИ #Moldova;
       - подія за межами Молдови, яка стосується Молдови (рішення ЄС щодо Молдови, кордон, допомога) — #Moldova;
       - подія за межами Молдови, яка Молдови не стосується — країна або регіон латиницею (наприклад, #Ukraine, #Romania, #EU).
       КАТЕГОРИЧНО ЗАБОРОНЕНО ставити хештег країни (наприклад, #Moldova), якщо ти вже поставив хештег конкретного міста! Другі хештеги — тематичні (наприклад, #Україна, #ТимчасовийЗахист, #Кримінал, #ДТП, #Суспільство). Категорично ЗАБОРОНЕНО ставити хештеги #ТимчасовийЗахист або #Україна, якщо в самому тексті новини про це не йдеться. КАТЕГОРИЧНО ЗАБОРОНЕНО ставити хештег #Chisinau, якщо подія не сталася фізично на вулицях Кишинева (не ліпи хештег столиці на всі підряд новини!).
    10. ПОЗНАЧКА «ОНОВЛЕНО»: НЕ додавай «🔄 [ОНОВЛЕНО]» самостійно — система ставить її автоматично.
    11. ПОРЯДОК ЗНАЧКІВ У ЗАГОЛОВКУ: якщо підходить кілька значків, став їх на самому початку заголовка в такому порядку: спершу 🇺🇦, потім 📄, потім текст заголовка. Наприклад: <b>🇺🇦 📄 Уряд продовжив тимчасовий захист для українців</b>
    """
    
    for idx, item in enumerate(batch):
        has_image = "Так" if item.get('image_urls') else "Ні"
        prompt += f"=== НОВИНА {idx+1} ===\nМає картинку: {has_image}\nЗаголовок: {item['title']}\nОпис: {item['description']}\n\n"
    
    prompt += f"""
    Для кожної новини виконай наступні вимоги:
    
    1. ЗАГОЛОВОК: Переклади оригінальний заголовок максимально точно та близько до тексту. КАТЕГОРИЧНО ЗАБОРОНЕНО вигадувати від себе "креатив", метафори чи клікбейт. Заголовок має бути обов'язково виділений тегами <b>.
    
    2. ОБ'ЄМ ТА ПРЯМИЙ ПЕРЕКЛАД (АНТИ-ВОДА):
       - Твоє головне завдання — ПРЯМИЙ ПЕРЕКЛАД наявного тексту. Якщо в оригіналі лише 300 символів — переклади ці 300 символів і зупинись. КАТЕГОРИЧНО ЗАБОРОНЕНО штучно розтягувати текст, додавати "воду", висновки чи контекст, якого немає в оригіналі.
       - ЛІМІТИ ТЕЛЕГРАМА: 
         Якщо "Має картинку: Так" -> суворий ліміт 800 символів. Якщо оригінал довший, стисни головну суть, щоб влізти в цей ліміт.
         Якщо "Має картинку: Ні" -> суворий ліміт 2500 символів. Якщо оригінал довший, передай головну суть до цього ліміту.
       - ОБРІЗАНИЙ ТЕКСТ В RSS: Іноді текст обривається на півслові або закінчується трьома крапками (...) через ліміти сайтів. Якщо бачиш обірване речення в кінці — просто видали його або заверши логічно. КАТЕГОРИЧНО ЗАБОРОНЕНО друкувати фрази з трьома крапками (наприклад, "Вулиця ..."). КАТЕГОРИЧНО ЗАБОРОНЕНО вигадувати неіснуючі імена, цитати, точні цифри чи дати, яких не було в оригіналі.
       
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
          
        post_text = apply_update_prefix(generated_texts[idx], item.get('is_update', False))
        if send_to_telegram(post_text, item['url'], item.get('source', 'Джерело'), images, tags=""):
            try:
                cursor.execute("UPDATE articles SET status = 'sent' WHERE guid = %s", (item['guid'],))
                conn.commit()
                stats["published"]["total"] += 1
                if any(re.search(kw, (item.get('title', '') + " " + item.get('description', '')).lower()) for kw in ukraine_keywords): 
                    stats["published"]["ukraine_priority"] += 1
                if images: stats["published"]["with_images"] += 1
                else: stats["published"]["text_only"] += 1
                stats["published"]["sources"][item.get('source', 'Джерело')] = stats["published"]["sources"].get(item.get('source', 'Джерело'), 0) + 1
                  
                # Пауза між постами (ліміти Telegram, R-09); після останнього поста запуску чекати нема чого (D-17)
                if idx < len(batch) - 1:
                    time.sleep(10 if DRY_RUN else 45)
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

UPDATE_PREFIX = "🔄 [ОНОВЛЕНО]: "
_MODEL_UPDATE_PREFIX_RE = re.compile(r'🔄\s*\[ОНОВЛЕНО\]\s*:?\s*')

def apply_update_prefix(text, is_update):
    """Q-05 п.2: прибирає позначку, якщо модель поставила її сама, і ставить префікс,
    лише коли фільтр визначив новину як оновлення події з історії.
    Підсумковий порядок: <b>🔄 [ОНОВЛЕНО]: 🇺🇦 📄 Заголовок</b>"""
    text = _MODEL_UPDATE_PREFIX_RE.sub('', text).lstrip()
    if not is_update:
        return text
    if text.lower().startswith('<b>'):
        return text[:3] + UPDATE_PREFIX + text[3:]
    return UPDATE_PREFIX + text

DRY_RUN_HEADER = "🧪 <b>ТЕСТ — не опубліковано в каналі</b>\n\n"

def target_chat_id():
    """Куди надсилати пости: у тестовому запуску — в адмін-чат, інакше — в канал."""
    return ADMIN_CHAT_ID if DRY_RUN else TELEGRAM_CHANNEL_ID

def send_admin_html(html_text):
    """Службове повідомлення адміну (HTML), без падіння бота при помилці."""
    if not ADMIN_CHAT_ID:
        logging.info(html_to_plain(html_text))
        return False
    try:
        return execute_telegram_request(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                                        {'chat_id': ADMIN_CHAT_ID, 'text': html_text[:4000], 'parse_mode': 'HTML'})
    except Exception as e:
        logging.error(f"Не вдалося надіслати повідомлення адміну: {_mask_token(e)}")
        return False

def _short(text, limit):
    """Скорочує рядок по межі слова (для звітів)."""
    text = str(text or '')
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(' ', 1)[0]
    return (cut if len(cut) > limit * 0.6 else text[:limit]).rstrip(' ,.;:') + '…'

def send_dry_run_filter_report(evaluated, approved_guids, reasons_map, kept_ordered):
    """DRY_RUN: що фільтр залишив і що відкинув (з категоріями) — щоб оцінити зміни промпту."""
    esc = lambda s: html_lib.escape(str(s or ''), quote=False)
    lines = [f"🧪 <b>ТЕСТ: фільтр Gemini</b> ({MODEL_USAGE.get('filter') or '—'})", "",
             f"✅ <b>Залишено {len(kept_ordered)}</b> (у пости підуть перші {BATCH_SIZE}):"]
    for i, item in enumerate(kept_ordered):
        mark = "🔄 " if item.get('is_update') else ""
        lines.append(f"{i + 1}. {mark}{esc(_short(item['title'], 110))} <i>({esc(item.get('source'))})</i>")
    lines += ["", "✖ <b>Відкинуто:</b>"]
    for idx, item in enumerate(evaluated):
        if item['guid'] not in approved_guids:
            lines.append(f"• [{esc(reasons_map.get(idx, NO_REASON))}] {esc(_short(item['title'], 100))}")
    send_admin_html("\n".join(lines))

_TRAILING_HASHTAGS_RE = re.compile(r'(?:\s*#[^\s#<>]+)+\s*$')

def split_trailing_hashtags(text):
    """Відокремлює хештеги в самому кінці тексту (хоч в один рядок, хоч кожен з нового).
    Повертає (текст без хештегів, «#тег1 #тег2» одним рядком). Якщо «хештегів» підозріло
    багато (> 200 символів) — нічого не відокремлює."""
    match = _TRAILING_HASHTAGS_RE.search(text or '')
    if not match or len(match.group(0)) > 200:
        return (text or '').rstrip(), ''
    hashtags = ' '.join(re.findall(r'#[^\s#<>]+', match.group(0)))
    return text[:match.start()].rstrip(), hashtags

def visible_len(html_text):
    """Довжина тексту так, як її рахує Telegram (без HTML-тегів, сутності — один символ)."""
    return len(html_lib.unescape(re.sub(r'<[^>]+>', '', html_text or '')))

def send_to_telegram(text, url, source_name, image_urls=None, tags=""):
   
    
    # 2. ХЕШТЕГИ В КІНЦІ ТЕКСТУ (D-22): беремо лише самі хештеги, а не весь останній абзац —
    # модель інколи ставить їх з нового рядка без порожнього рядка, і тоді «абзацом» ставав увесь текст
    text, hashtags = split_trailing_hashtags(text)
    extracted_tags = f"\n\n{hashtags}" if hashtags else ""

    # Обробка списку зображень (з Молдовського)
    if not image_urls: image_urls = []
    elif isinstance(image_urls, str): image_urls = [image_urls]
    
    crop_msg = "\n\n👉 <i>Деталі за посиланням нижче</i> 👇"

    # D-12: текст від Gemini — у безпечний HTML (екранування & < >, закриті теги)
    text = sanitize_telegram_html(text)
    extracted_tags = sanitize_telegram_html(extracted_tags)

    # D-19: URL і назва джерела — з екрануванням
    safe_url = html_lib.escape(url or '', quote=True)
    safe_source = html_lib.escape(source_name or 'Джерело', quote=False)
    footer = f"{extracted_tags}{tags}\n\n🔗 Джерело: <a href=\"{safe_url}\">{safe_source}</a>"
    header = DRY_RUN_HEADER if DRY_RUN else ""

    # 3. ЛІМІТ ТЕКСТУ (D-22): Telegram рахує видимі символи — підпис до фото ≤ 1024, повідомлення ≤ 4096.
    # Бюджет тексту = ліміт (із запасом) − шапка − хештеги − «Джерело»; не менше 300 символів.
    total_limit = 1000 if image_urls else 4000
    budget = max(total_limit - visible_len(header) - visible_len(footer), 300)
    if visible_len(text) > budget:
        # crop_html рахує символи разом із тегами, тож видимий текст гарантовано вкладеться
        text = crop_html(text, budget - visible_len(crop_msg)) + crop_msg

    full_message = f"{header}{text}{footer}"

    if DRY_RUN and not ADMIN_CHAT_ID:
        logging.info(f"🧪 DRY_RUN (ADMIN_CHAT_ID не задано) — пост лише в лозі:\n{full_message}")
        return True

    payload = {'chat_id': target_chat_id()}
    
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

    # 4. ВІДПРАВКА ТА ЗАХИСТ ВІД ЗЛАМАНОГО HTML
    try: 
        return execute_telegram_request(api_url, payload)
    except Exception as e: 
        # ФОЛБЕК: Якщо відхилено фото/медіагрупу, відправляємо як голий текст
        if len(image_urls) > 0:
            logging.warning(f"⚠️ Telegram відхилив медіа ({e}). Відправляємо як текст...")
            payload_text = {'chat_id': target_chat_id(), 'text': full_message, 'parse_mode': 'HTML'}
            api_url_text = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            try:
                return execute_telegram_request(api_url_text, payload_text)
            except Exception as text_e:
                logging.error(f"❌ Критична помилка відправки тексту: {text_e}")
                return False
        else:
            logging.error(f"❌ Критична помилка Telegram API: {e}")
            return False

def main():
    # Перевіряємо токени тут, коли скрипт безпосередньо запускається
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID, GEMINI_API_KEY, DATABASE_URL]):
        logging.error("Критична помилка: не знайдено базові токени або DATABASE_URL у файлі .env!")
        exit(1)
        
    start_time = time.time()
    if DRY_RUN:
        logging.warning("🧪 DRY_RUN: тестовий запуск — БД лише на читання, пости йдуть в адмін-чат, а не в канал")
    logging.info(f"🤖 Моделі фільтра: {', '.join(FILTER_MODELS)}")
    logging.info(f"🤖 Моделі перекладу: {', '.join(TRANSLATE_MODELS)}")
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
                          data={'chat_id': ADMIN_CHAT_ID, 'text': f"{'🧪 DRY_RUN — ' if DRY_RUN else ''}⚠️ ALARM: Скрипт впав з помилкою: {_mask_token(e)}"}, timeout=60)
    finally:
        stats["execution_time"] = round(time.time() - start_time, 1)
        stats["gemini_retries"] = MODEL_USAGE["failed_attempts"]
        
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

        # T-22: статистика викликів моделей — окремо, щоб її збій не зачепив основну аналітику
        if MODEL_CALLS:
            try:
                conn = get_db_connection()
                cursor = conn.cursor()
                for c in MODEL_CALLS:
                    cursor.execute(
                        "INSERT INTO model_calls (task, model, outcome, http_code, latency_ms, error) VALUES (%s, %s, %s, %s, %s, %s)",
                        (c['task'], c['model'], c['outcome'], c['http_code'], c['latency_ms'], c['error'])
                    )
                cursor.execute("DELETE FROM model_calls WHERE created_at <= NOW() - INTERVAL '90 days'")
                conn.commit()
                cursor.close()
                conn.close()
            except Exception as e:
                logging.error(f"Помилка збереження статистики моделей: {e}")
            
        send_analytics_to_admin(stats)

if __name__ == "__main__":
    main()