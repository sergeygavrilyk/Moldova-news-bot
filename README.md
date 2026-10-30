# 🇲🇩 Moldova News Bot

Telegram-бот, який кілька разів на день збирає новини молдовських медіа з RSS, відсіює зайве й дублікати за допомогою Gemini, перекладає відібране українською та публікує в Telegram-канал для української діаспори в Молдові.

Репозиторій: <https://github.com/sergeygavrilyk/Moldova-news-bot> (публічний з 30.10.2026, гілка `main`)

---

## Як це працює (коротко)

```
cron-job.org ──(HTTP POST, ~щогодини вдень)──▶ GitHub Actions: news_bot_run.yml
                                                 │
                                                 ├─ python setup_db.py   (CREATE TABLE IF NOT EXISTS + джерела з feeds.json)
                                                 └─ python main.py
                                                      │
   RSS: Newsmaker · Radio Moldova ·                   │
        Moldovalive · Point MD ───────────────────────┤ 1. scrape_feeds()   → нові статті в Supabase (status = pending)
                                                      │    фільтр віку 12 год, стоп-слова, вектор-дублікати (pgvector > 0.90)
   Google Gemini (AI Studio) ◀────────────────────────┤ 2. process_backlog() → до 40 кандидатів → Gemini-редактор (JSON)
                                                      │    → 3 найкращі → переклад українською → Telegram
   Telegram Bot API ◀─────────────────────────────────┤ 3. finally: запис у run_analytics, видалення статей > 30 днів,
                                                      │    звіт адміну в Telegram
   Supabase PostgreSQL + pgvector ◀───────────────────┘
```

Детально — у [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Стек

| Компонент | Що робить | Де налаштовується |
|---|---|---|
| **Python 3.11** (CI) / 3.14 (локально) | Уся логіка в `main.py` | `requirements.txt` |
| **GitHub Actions** | Виконує скрипт у хмарі | `.github/workflows/news_bot_run.yml`, Secrets репозиторію |
| **cron-job.org** | Розклад: смикає `workflow_dispatch` через GitHub API | console.cron-job.org |
| **Supabase (PostgreSQL 17 + pgvector)** | Джерела, статті, статуси, вектори, аналітика | Проєкт `mguksatdxvssywdbbkii` (eu-west-1) |
| **Google Gemini (AI Studio)** | Фільтрація/дедуплікація, переклад, ембединги | aistudio.google.com → API key |
| **Telegram Bot API** | Публікація в канал + звіти адміну | @BotFather, ID каналу/чату |

## Структура папки

| Файл / папка | Призначення |
|---|---|
| `main.py` | Основний скрипт: збір RSS → фільтри → Gemini → Telegram → аналітика |
| `setup_db.py` | Створює таблиці (якщо їх нема) і заносить джерела з `feeds.json`. Запускається перед кожним `main.py` у CI. ⚠️ Схема в ньому застаріла — див. [db/schema.sql](db/schema.sql) |
| `feeds.json` | Початковий список RSS-джерел (читається лише `setup_db.py`; робочий список — таблиця `sources`) |
| `requirements.txt` | Python-залежності |
| `.github/workflows/news_bot_run.yml` | GitHub Actions workflow (тригер `workflow_dispatch`) |
| `.env` | Секрети для локального запуску. **Не комітиться** (є в `.gitignore`). Шаблон — `.env.example` |
| `tools/check_feeds.py` | Оцінка RSS-джерел правилами бота (workflow «Check RSS feeds»), список кандидатів — `tools/feed_candidates.json` |
| `check_models.py` | Утиліта: показує, які моделі Gemini доступні для вашого ключа |
| `test_rss.py` | Утиліта: перевіряє, що RSS-посилання відповідають (список джерел продубльовано всередині файлу) |
| `analytics.json` | **Застарілий** файл аналітики з часів до Supabase (до 17.09.2026). Зараз аналітика — у таблиці `run_analytics` |
| `__pycache__/` | Кеш Python, випадково потрапив у git |
| `db/schema.sql` | Фактична схема БД у Supabase (знята 30.10.2026) |
| `docs/` | Документація: архітектура, інструкція з експлуатації, аудит |
| `project_state.md` | Живий журнал проєкту: стан, задачі, тех. борг, ідеї, рішення, історія змін |
| `CLAUDE.md` | Правила та контекст для роботи з папкою через Claude / інших AI-асистентів |

## Змінні середовища

| Змінна | Обовʼязкова | Опис |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | так | Токен бота від @BotFather |
| `TELEGRAM_CHANNEL_ID` | так | ID або `@username` каналу, куди публікуються новини (бот має бути адміном) |
| `GEMINI_API_KEY` | так | Ключ Google AI Studio |
| `DATABASE_URL` | так | Рядок підключення до Supabase Postgres (`postgresql://...`) |
| `ADMIN_CHAT_ID` | ні | Чат, куди приходять звіти після кожного запуску та алерти про падіння |

Локально — у файлі `.env`; у хмарі — у **GitHub → Settings → Secrets and variables → Actions** з тими самими назвами.

## Швидкий старт (локально, Windows)

```powershell
cd "C:\Users\Сергей\Desktop\Moldova news bot"
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env      # і заповніть значення
python check_models.py      # чи бачить ключ потрібні моделі Gemini
python test_rss.py          # чи живі RSS-джерела
python main.py              # ⚠️ ПУБЛІКУЄ В РЕАЛЬНИЙ КАНАЛ і пише в робочу БД
```

> Для експериментів вкажіть у `.env` тестовий канал у `TELEGRAM_CHANNEL_ID`. База при цьому все одно робоча: статті, які ви «опублікуєте» в тестовий канал, отримають статус `sent` і не підуть у основний.

## Запуск у хмарі

Вручну: GitHub → **Actions → Run Moldova News Bot → Run workflow**.
За розкладом: cron-job.org надсилає `POST` на GitHub API (налаштування — у [docs/RUNBOOK.md](docs/RUNBOOK.md#3-розклад-cron-joborg)).

## Документація

- **[project_state.md](project_state.md) — живий журнал: поточний стан, задачі, тех. борг, ідеї, рішення, історія змін. Оновлюється при кожній зміні.**
- [docs/AI_RULES.md](docs/AI_RULES.md) — правила роботи AI-агента з проєктом
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — конвеєр обробки, статуси статей, промпти, ліміти, схема БД
- [docs/RUNBOOK.md](docs/RUNBOOK.md) — щоденна експлуатація, SQL-запити для контролю, типові проблеми та їх вирішення
- [docs/AUDIT.md](docs/AUDIT.md) — стан проєкту на 30.10.2026, знайдені ризики та рекомендації
- [db/schema.sql](db/schema.sql) — схема бази даних
- [CLAUDE.md](CLAUDE.md) — інструкція для AI-асистента

## Повʼязані проєкти

Код синхронізується з «хорватським» ботом (новини Хорватії) — частина комітів має позначку «синхронізовано з хорватським ботом». Зміни в промптах і захистах зазвичай переносяться між ботами.
