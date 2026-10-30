"""Оцінка RSS-джерел тими самими правилами, що й у бота (main.py).

Нічого не публікує, не пише в БД і не викликає Gemini. Для кожної стрічки:
доступність через cloudscraper, обсяг, свіжість (вікно 12 год, як MAX_AGE_SECONDS),
скільки новин проходить фільтр якості is_quality_article, фото в RSS,
фото зі сторінки статті (og:image) для новин без фото, мова, «українські» теми.

Запуск: GitHub Actions -> «Check RSS feeds» або локально: python tools/check_feeds.py [feeds.json]
"""
import calendar
import json
import os
import re
import sys
import time

os.environ.setdefault("GEMINI_API_KEY", "not-used")  # main.py створює клієнт Gemini при імпорті; запитів не буде
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import main  # noqa: E402
import cloudscraper  # noqa: E402
import feedparser  # noqa: E402

UKRAINE_RE = re.compile(r"\bucrain|\bzelenski|\bkiev|\bkiiv|\bodes+a|\brefugiat|protec[tț]ie temporar|\bукраин|\bзеленск|\bкиев|\bодесс|\bбеженц|\bukrain", re.I)
OG_SAMPLE = int(os.getenv("OG_SAMPLE", "4"))


def lang_of(text):
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return "?"
    cyr = sum(1 for c in letters if "Ѐ" <= c <= "ӿ") / len(letters)
    if cyr > 0.5:
        return "ru"
    if re.search(r"[ăâîșşțţ]", text.lower()):
        return "ro"
    return "en/ro"


def check(feed, scraper):
    row = {"name": feed["name"], "url": feed["url"]}
    started = time.time()
    try:
        r = scraper.get(feed["url"], headers={"Accept": "application/rss+xml, application/xml, text/xml, */*"}, timeout=15)
        row["http"] = r.status_code
        if r.status_code not in (200, 301, 302):
            row["verdict"] = f"HTTP {r.status_code}"
            return row
        parsed = feedparser.parse(r.content)  # байти: кодування береться з XML-заголовка (як у main.py, D-20)
    except Exception as e:
        row["http"] = "-"
        row["verdict"] = type(e).__name__
        return row

    entries = parsed.entries
    row["entries"] = len(entries)
    if not entries:
        row["verdict"] = "порожня стрічка"
        return row

    now = time.time()
    ages = []
    fresh = []
    for e in entries:
        if getattr(e, "published_parsed", None):
            age_h = (now - calendar.timegm(e.published_parsed)) / 3600
            ages.append(age_h)
            if age_h <= main.MAX_AGE_SECONDS / 3600:
                fresh.append(e)
        else:
            fresh.append(e)  # бот теж вважає новину без дати свіжою
    row["newest_h"] = round(min(ages), 1) if ages else None
    row["fresh_12h"] = len(fresh)
    # частота публікацій: новин на добу за проміжок, який покриває стрічка
    if len(ages) >= 2 and max(ages) > 0:
        row["per_day"] = round(len(ages) / max(max(ages) / 24, 0.25), 1)

    # Бот бере перші 15 записів стрічки
    window = entries[:15]
    quality = [e for e in window if main.is_quality_article(e.get("title", ""), e.get("summary", e.get("title", "")), e.get("link", ""))]
    row["quality_of_15"] = len(quality)
    descr = [len(re.sub(r"<[^>]+>", "", e.get("summary", ""))) for e in window]
    row["avg_descr_chars"] = round(sum(descr) / len(descr)) if descr else 0
    row["lang"] = lang_of(" ".join(e.get("title", "") for e in window))
    row["ukraine_topics"] = sum(1 for e in window if UKRAINE_RE.search(e.get("title", "") + " " + e.get("summary", "")))

    with_rss_img = [e for e in window if main.extract_image_urls(e)]
    row["rss_images"] = f"{len(with_rss_img)}/{len(window)}"

    # og:image для новин без фото в RSS (вибірка — щоб не навантажувати сайти)
    no_img = [e for e in window if not main.extract_image_urls(e)][:OG_SAMPLE]
    og_found = [main.fetch_og_image(scraper, e.get("link", "")) for e in no_img]
    og_ok = [u for u in og_found if u]
    row["og_images"] = f"{len(og_ok)}/{len(no_img)}" if no_img else "—"
    if len(og_ok) >= 2 and len(set(og_ok)) == 1:
        row["og_images"] += " (однакове — схоже на логотип)"
    row["sec"] = round(time.time() - started, 1)
    row["verdict"] = "OK"
    return row


def main_cli():
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "feed_candidates.json")
    feeds = json.load(open(path, encoding="utf-8"))
    scraper = cloudscraper.create_scraper(browser={"browser": "chrome", "platform": "windows", "mobile": False})
    rows = []
    for f in feeds:
        row = check(f, scraper)
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False))

    cols = ["name", "verdict", "entries", "per_day", "newest_h", "fresh_12h", "quality_of_15", "avg_descr_chars", "lang", "ukraine_topics", "rss_images", "og_images"]
    table = "| " + " | ".join(cols) + " |\n|" + "---|" * len(cols) + "\n"
    table += "\n".join("| " + " | ".join(str(r.get(c, "")) for c in cols) + " |" for r in rows)
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write("## Перевірка RSS-джерел\n\n" + table + "\n")
    # Компактна анотація — читається через GitHub API (по рядку на джерело, ліміт довжини анотації)
    for i in range(0, len(rows), 7):
        chunk = [[r.get(c) for c in cols] for r in rows[i:i + 7]]
        print(f"::notice title=feeds-{i // 7}::{json.dumps(chunk, ensure_ascii=False)}")


if __name__ == "__main__":
    main_cli()
