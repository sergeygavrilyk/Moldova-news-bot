# Інструкція з експлуатації (Runbook)

Що робити щодня, як перевірити, що бот живий, і як лагодити типові збої.

## 1. Де що знаходиться

| Сервіс | Що там | Посилання |
|---|---|---|
| GitHub | Код, workflow, секрети, логи запусків | github.com/sergeygavrilyk/Moldova-news-bot → **Actions** |
| cron-job.org | Розклад запусків | console.cron-job.org |
| Supabase | БД `sergeygavrilyk's Project` (`mguksatdxvssywdbbkii`, eu-west-1) | supabase.com/dashboard → SQL Editor / Table Editor |
| Google AI Studio | Ключ Gemini, ліміти та використання | aistudio.google.com |
| Telegram | Бот (@BotFather), канал, чат адміна | — |
| Локальна папка | `C:\Users\Сергей\Desktop\Moldova news bot` (клон репозиторію) | — |

## 2. Щоденний контроль (2 хвилини)

1. **Звіт у чаті адміна** приходить після кожного запуску: час, помилки, токени, воронка, беклог, опубліковане. Якщо звітів нема кілька годин — бот не запускається (див. §6.1).
2. **Канал**: є свіжі пости, немає дублів, текст українською.
3. За потреби — SQL-перевірка в Supabase (§4).

Норма (дані 16–22.10.2026): ~10–11 запусків на добу, 14–33 публікації на добу, 60–110 тис. токенів на добу, запуск триває 1,5–4,5 хв, помилок 0.

## 3. Розклад (cron-job.org)

Workflow має тригер лише `workflow_dispatch`, тому cron-job.org запускає його через GitHub REST API.

| Поле | Значення |
|---|---|
| URL | `https://api.github.com/repos/sergeygavrilyk/Moldova-news-bot/actions/workflows/news_bot_run.yml/dispatches` |
| Метод | `POST` |
| Заголовки | `Authorization: Bearer <GitHub PAT>`<br>`Accept: application/vnd.github+json`<br>`X-GitHub-Api-Version: 2022-11-28` |
| Тіло | `{"ref":"main"}` |
| Розклад (з 30.10.2026) | `0 9-18,21 * * *` — щогодини з 9:00 до 18:00 і о 21:00 (11 запусків/добу) |
| Очікувана відповідь | **204 No Content** |

**GitHub PAT**: fine-grained token лише на цей репозиторій з правом **Actions: Read and write**. У токена є **термін дії** — запишіть дату закінчення в календар. Коли токен прострочиться, GitHub відповідатиме `401`, а cron-job.org після серії невдач може **автоматично вимкнути задачу**.

Перевірити вручну (PowerShell):
```powershell
$h = @{ Authorization = "Bearer <PAT>"; Accept = "application/vnd.github+json"; "X-GitHub-Api-Version" = "2022-11-28" }
Invoke-WebRequest -Method POST -Headers $h -Body '{"ref":"main"}' `
  -Uri "https://api.github.com/repos/sergeygavrilyk/Moldova-news-bot/actions/workflows/news_bot_run.yml/dispatches"
# StatusCode 204 = запуск поставлено в чергу
```

## 4. SQL для контролю (Supabase → SQL Editor)

```sql
-- Останні запуски
SELECT timestamp, execution_time, tokens_used, scraped_total, published_total, errors_count
FROM run_analytics ORDER BY id DESC LIMIT 15;

-- Скільки годин минуло з останнього запуску
SELECT NOW() - MAX(timestamp) AS since_last_run FROM run_analytics;

-- Зведення по днях
SELECT timestamp::date AS day, COUNT(*) runs, SUM(published_total) published,
       SUM(tokens_used) tokens, SUM(errors_count) errors
FROM run_analytics WHERE timestamp > NOW() - INTERVAL '14 days'
GROUP BY 1 ORDER BY 1 DESC;

-- Статуси статей
SELECT status, COUNT(*), MIN(created_at), MAX(created_at) FROM articles GROUP BY status;

-- Стан джерел
SELECT id, name, status, error_count, last_checked, last_error_log FROM sources ORDER BY id;

-- Чому Gemini відхиляє (за 7 днів)
SELECT rejection_reason, COUNT(*) FROM articles
WHERE status = 'rejected' AND created_at > NOW() - INTERVAL '7 days'
GROUP BY 1 ORDER BY 2 DESC;

-- Публікації по джерелах (за 7 днів)
SELECT s.name, COUNT(*) FROM articles a JOIN sources s ON s.id = a.source_id
WHERE a.status = 'sent' AND a.created_at > NOW() - INTERVAL '7 days'
GROUP BY 1 ORDER BY 2 DESC;
```

### 4.1 Статистика моделей Gemini (таблиця `model_calls`, з 30.10.2026)
Кожна спроба виклику моделі (фільтр / переклад) — окремий рядок: `ok`, `unavailable` (503, перевантаження), `quota` (429), `timeout`, `bad_response`, `error`. Тестові запуски сюди не пишуть.

```sql
-- Надійність моделей за 7 днів
SELECT task, model, COUNT(*) AS calls,
       ROUND(100.0 * COUNT(*) FILTER (WHERE outcome = 'ok') / COUNT(*)) AS ok_pct,
       COUNT(*) FILTER (WHERE outcome = 'unavailable') AS unavailable,
       COUNT(*) FILTER (WHERE outcome = 'quota')       AS quota,
       COUNT(*) FILTER (WHERE outcome = 'timeout')     AS timeout,
       ROUND(AVG(latency_ms) FILTER (WHERE outcome = 'ok') / 1000.0, 1) AS avg_ok_sec
FROM model_calls WHERE created_at > NOW() - INTERVAL '7 days'
GROUP BY task, model ORDER BY task, calls DESC;

-- Коли модель найчастіше недоступна (година UTC)
SELECT model, EXTRACT(HOUR FROM created_at)::int AS hour_utc,
       COUNT(*) FILTER (WHERE outcome <> 'ok') AS failures, COUNT(*) AS calls
FROM model_calls WHERE created_at > NOW() - INTERVAL '7 days'
GROUP BY 1, 2 HAVING COUNT(*) FILTER (WHERE outcome <> 'ok') > 0 ORDER BY 1, 2;

-- Хто реально перекладав пости (перша успішна модель у кожному запуску)
SELECT model, COUNT(*) FROM model_calls
WHERE task = 'translate' AND outcome = 'ok' AND created_at > NOW() - INTERVAL '7 days'
GROUP BY 1 ORDER BY 2 DESC;
```
У звіті адміну після кожного запуску — рядок `🧾 Спроби: 3.8-flash 503 | 3.6-flash ✅`.

## 5. Типові операції

### 5.1 Додати RSS-джерело
1. Перевірте стрічку правилами бота: додайте її в `tools/feed_candidates.json` у гілці й запустіть workflow **«Check RSS feeds»** (або `python tools/check_feeds.py` локально). Звіт (Job Summary) покаже: доступність, новин на добу, скільки свіжих за 12 год, скільки проходять фільтр якості, середню довжину опису (Gemini потрібен текст, не лише заголовок), фото в RSS і на сторінках (`og:image`), мову, «українські» теми. Нічого не публікується і не пишеться в БД.
2. Додайте в базу (одне з двох):
   - у `feeds.json` → закомітити → наступний запуск `setup_db.py` додасть його сам; або
   - одразу SQL: `INSERT INTO sources (name, url) VALUES ('Назва', 'https://.../feed/');`
3. Якщо сайт має розділи зі спортом/лайфстайлом з іншими шляхами в URL — допишіть їх в `unwanted_url_categories` (`main.py`, `is_quality_article`).

### 5.2 Вимкнути джерело
```sql
UPDATE sources SET status = 'disabled' WHERE name = 'Назва';  -- будь-який статус, крім 'ok'
```
> Видалити рядок з `sources` не вийде, поки на нього посилаються статті (зовнішній ключ).

### 5.3 Повернути джерело після помилок
Після 3 невдалих завантажень поспіль джерело отримує `status = 'error'`, а адміну приходить «🔴 Джерело вимкнено» з текстом помилки.
Далі бот **сам** пробує його раз на 6 год (`SOURCE_RETRY_HOURS` у `main.py`); щойно стрічка відповіла — статус `ok` і повідомлення «✅ Джерело знову працює». Повторні невдалі спроби сповіщень не надсилають.
Повернути одразу, не чекаючи 6 год:
```sql
UPDATE sources SET status = 'ok', error_count = 0, last_error_log = NULL WHERE name = 'Назва';
```
Джерела зі статусом `disabled` (вимкнені вручну, §5.2) бот не чіпає.

### 5.4 Розблокувати «завислі» статті
```sql
UPDATE articles SET status = 'pending' WHERE status IN ('processing', 'failed')
  AND created_at > NOW() - INTERVAL '12 hours';
```

### 5.5 Змінити модель Gemini
Без правки коду:
1. `python check_models.py` показує лише, які моделі **існують** для ключа, а не чи вони працюють і мають безкоштовну квоту. Реальна перевірка — workflow **«Check Gemini models»** (гілка `tools/check-gemini-models`; запускається push-ем у цю гілку): по одному запиту на модель, результат у Job Summary.
2. GitHub → **Settings → Secrets and variables → Actions → Variables** → `GEMINI_FILTER_MODELS` або `GEMINI_TRANSLATE_MODELS` — назви через кому, в порядку пріоритету. Видалити змінну = повернути значення за замовчуванням з `main.py`.
3. Наступний звіт адміну покаже в рядку `🤖 Моделі`, яка модель відповіла.

Ознаки проблем з квотою: рядок `⛽ Квоту вичерпано: …` у звіті щодня, або «Опубліковано: 0» з помилкою перекладу — додайте в кінець списку ще моделі або зменшіть кількість запусків.
⚠️ Зміна моделі **ембедингів** (у коді, `scrape_feeds`) робить старі вектори несумісними: на 3 дні вектор-фільтр дублів працюватиме гірше (можна очистити `UPDATE articles SET embedding = NULL;`).

### 5.6 Замінити секрет (токен/ключ/пароль БД)
1. Згенерувати новий у відповідному сервісі.
2. GitHub → Settings → Secrets and variables → Actions → оновити значення.
3. Оновити локальний `.env`.
4. Ручний запуск workflow і перевірка звіту адміна.

### 5.7 Змінити поведінку бота
| Що хочу | Де міняти |
|---|---|
| Більше/менше постів за запуск | `BATCH_SIZE` (р. 48) — пам'ятайте про паузу 45 с на пост |
| Пропускати/дозволяти теми | промпт `deduplicate_news` (КРОК 4–5) + `unwanted_keywords` |
| Стиль, довжина, хештеги | промпт перекладу в `process_backlog` |
| Частота запусків | розклад у cron-job.org |

### 5.8 Тестовий запуск (DRY_RUN)
Перевірити зміну (промпт, модель, джерело) **без публікації в канал і без змін у БД**:
1. GitHub → **Actions → Run Moldova News Bot → Run workflow**.
2. Обрати гілку (зазвичай гілку з PR, який перевіряємо) і поставити галочку **«Тестовий запуск»** → Run.
3. В адмін-чат прийдуть:
   - звіт фільтра 🧪: що залишено (🔄 — позначено як оновлення) і що відкинуто з категоріями;
   - до 3 готових постів з позначкою «🧪 ТЕСТ — не опубліковано в каналі»;
   - звіт запуску з позначкою «ТЕСТОВИЙ ЗАПУСК».

Що відбувається: RSS, фільтр Gemini, фото, переклад — справжні; БД лише читається (статті, статуси, аналітика не змінюються; `setup_db.py` не запускається). Витрачає 1 запит до моделі фільтра і 1 до моделі перекладу — не варто запускати десятки разів на день.
Локально: `DRY_RUN=1 python main.py` (PowerShell: `$env:DRY_RUN=1; python main.py`).

### 5.9 Тижневий звіт
Приходить адміну **автоматично з першого запуску понеділка** (≈ 09:00 за Кишиневом, умова: понеділок UTC і ще не було запусків за цю добу). Окремого розкладу немає.
Отримати будь-коли: GitHub → **Actions → Run Moldova News Bot → Run workflow** → галочка **«weekly_report»** → Run. Новини при цьому не збираються й не публікуються, БД лише читається. Локально: `python main.py --weekly-report`.

Що в звіті (останні 7 днів, UTC):
- запуски, запуски з помилками, середній час, токени;
- опубліковано по джерелах: скільки постів, % з фото, скільки відхилено / не надіслано; підсумок статусів (зібрано, відхилено, застаріли, у черзі);
- причини відхилень за категоріями D-04 (старі довільні причини зводяться до категорій);
- моделі Gemini окремо для фільтра й перекладу: частка виконаної роботи, вдалі спроби, 503 / ⛽ квота / ⏱ таймаут / ⚠️ погана відповідь (дані з `model_calls`, з 30.10);
- вичерпані квоти: у скільки днів із 7 і скільки разів;
- джерела: 🔴 вимкнені через помилки, 🟡 з помилками поспіль, ⚪️ вимкнені вручну.

Примітка: пости рахуються за часом збору статті (`created_at`), тестові запуски у звіт не потрапляють.

## 6. Діагностика збоїв

### 6.1 Бот нічого не публікує / немає звітів адміну
Йдіть ланцюжком зліва направо:

1. **cron-job.org → History задачі.** Задача увімкнена? Яка відповідь GitHub?
   - `204` — запуски йдуть, дивіться п. 2.
   - `401`/`403` — прострочений або відкликаний PAT → новий токен (§3).
   - `404` — змінилася назва файлу workflow/репозиторію або токен не має доступу.
   - Задача вимкнена — cron-job.org сам вимикає задачі після серії помилок; виправити причину й увімкнути.
2. **GitHub → Actions.** Є запуски за останні години?
   - Немає жодного, хоча cron-job.org отримує 204 → перевірте **Settings → Billing → Actions minutes** (на безкоштовному плані для приватних репозиторіїв — 2000 хв/місяць на весь акаунт, разом з іншими ботами). Також банер про вимкнені Actions.
   - Запуски червоні → відкрийте лог кроку `Run News Bot`.
3. **Лог запуску**:
   - `Критична помилка: не знайдено базові токени` → немає секрету в GitHub.
   - `could not connect to server` / `Помилка читання PostgreSQL` → Supabase на паузі або змінився пароль. Безкоштовні проєкти Supabase ставляться на паузу після тижня неактивності — **Restore** у дашборді.
   - `Модель ... недоступна` на обох моделях → модель знята з обігу або вичерпано квоту ключа (див. AI Studio).
   - `Telegram Error: ... chat not found / bot is not a member` → бота прибрали з каналу або змінився ID.

### 6.2 Звіти приходять, але «Опубліковано: 0»
- `Беклог: 0` і `Знайдено в RSS: 0` → усі джерела в `error` (§5.3, рядок «🔴 Недоступні джерела» у звіті) або стрічки порожні.
- `Знайдено` > 0, але все «Пропущено» → новин за 12 годин немає або все вже бачили — нормально вночі.
- Беклог є, але все відсіяв Gemini → перегляньте `rejection_reason` (§4); можливо, промпт надто суворий.
- Помилка перекладу (`Невірна кількість текстів`) → модель повернула не той формат; бот сам поверне статті в `pending`.

### 6.3 У каналі дублі
- Подивіться, чи не впав фільтр Gemini (у лозі: `Помилка Gemini API у deduplicate_news ... Пропускаємо фільтрацію`) — тоді публікуються кандидати без перевірки.
- Перевірте, чи не було двох паралельних запусків (дві задачі в cron-job.org або ручний запуск під час планового).
- Поріг вектор-дубля 0.90 можна знизити до 0.85–0.88.

### 6.4 Текст з «дивними» літерами чи румунськими словами
Додайте символ у словник `HOMOGLYPHS` (`generate_news_texts`) або посильте правило 4 промпту перекладу.

## 7. Розгортання з нуля
1. Supabase: новий проєкт → SQL Editor → виконати `db/schema.sql`.
2. GitHub: секрети з розділу «Змінні середовища» в README.
3. Ручний запуск workflow → `setup_db.py` додасть джерела з `feeds.json`.
4. cron-job.org: задача за §3.
5. Додати бота адміністратором у канал з правом публікації.
