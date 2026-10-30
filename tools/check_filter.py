"""Smoke-тест фільтра Gemini: справжній виклик deduplicate_news на тестових новинах.

Перевіряє, що поточна схема відповіді (NewsFilterResponse) приймається моделлю і що
причини відхилення приходять з фіксованого переліку (D-04).
Не публікує в Telegram і не звертається до БД. Витрачає 1 запит до моделі фільтра.
Запуск: GitHub Actions -> «Check Gemini filter» (потрібен секрет GEMINI_API_KEY).
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import main  # noqa: E402

CANDIDATES = [
    {"title": "Parlamentul a adoptat legea privind prelungirea protecției temporare pentru refugiații din Ucraina",
     "description": "Deputații au votat prelungirea statutului de protecție temporară pentru cetățenii ucraineni până în martie 2027."},
    {"title": "Депутаты продлили временную защиту для граждан Украины до марта 2027 года",
     "description": "Парламент Молдовы проголосовал за продление статуса временной защиты для украинских беженцев."},
    {"title": "Zimbru Chișinău a câștigat meciul cu Sheriff Tiraspol în campionatul național",
     "description": "Scor final 2:1, golurile au fost marcate în repriza a doua."},
    {"title": "Учёные Гарварда выяснили, сколько кофе в день полезно пить",
     "description": "Американские исследователи опубликовали результаты исследования о пользе кофе."},
    {"title": "В Кишиневе из-за ремонта перекроют улицу Измаил на три дня",
     "description": "Мэрия сообщила, что движение по улице Измаил будет закрыто с 1 по 3 ноября."},
]
HISTORY = ["В Кишиневе из-за ремонта перекроют улицу Измаил с 1 по 3 ноября"]


def run():
    stats = {"tokens_used": 0, "errors_count": 0}
    articles = [dict(c, guid=f"t{i}") for i, c in enumerate(CANDIDATES)]
    kept, reasons = main.deduplicate_news(main.client, articles, HISTORY, stats)
    if kept is None:
        print("::error title=filter::Фільтр Gemini повернув помилку (див. лог вище)")
        sys.exit(1)
    result = {
        "model": main.MODEL_USAGE.get("filter"),
        "kept": [a["guid"] for a in kept],
        "updates": [a["guid"] for a in kept if a.get("is_update")],
        "rejected": {f"t{i}": r for i, r in reasons.items()},
        "tokens": stats["tokens_used"],
    }
    bad = [r for r in reasons.values() if r not in main.REJECTION_CATEGORIES]
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"::notice title=filter-check::{json.dumps(result, ensure_ascii=False)}")
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write("## Перевірка фільтра Gemini\n\n```json\n" + json.dumps(result, ensure_ascii=False, indent=2) + "\n```\n")
    if bad:
        print(f"::error title=filter::Причини не з переліку: {bad}")
        sys.exit(1)


if __name__ == "__main__":
    run()
