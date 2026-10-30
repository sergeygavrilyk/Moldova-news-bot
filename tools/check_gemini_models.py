"""Перевірка, які моделі Gemini реально відповідають для ключа GEMINI_API_KEY.

Для кожної моделі робиться ОДИН короткий запит generateContent (≈20 токенів) —
це витрачає 1 запит з денної квоти кожної моделі.
Результат: таблиця в лозі, у Job Summary і анотація ::notice (видно через GitHub API).
Ключ не виводиться: передається в заголовку, у тексті помилок маскується.
"""
import json
import os
import re
import sys
import time

import requests

KEY = os.getenv("GEMINI_API_KEY")
MODELS = [m.strip() for m in os.getenv("MODELS_TO_CHECK", "").split(",") if m.strip()]
URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def check(model):
    body = {
        "contents": [{"parts": [{"text": "Переклади українською одним словом: 'casa'"}]}],
        "generationConfig": {"temperature": 0, "maxOutputTokens": 256},
    }
    started = time.time()
    try:
        r = requests.post(URL.format(model=model), headers={"x-goog-api-key": KEY}, json=body, timeout=60)
    except Exception as e:
        return {"model": model, "result": "ERROR", "http": "-", "detail": type(e).__name__, "sec": round(time.time() - started, 1)}
    sec = round(time.time() - started, 1)
    text = r.text.replace(KEY, "***") if KEY else r.text

    if r.status_code == 200:
        try:
            parts = r.json()["candidates"][0]["content"].get("parts", [])
            answer = " ".join(p.get("text", "") for p in parts).strip()[:40]
        except Exception:
            answer = "(без тексту)"
        return {"model": model, "result": "OK", "http": 200, "detail": answer or "(порожньо)", "sec": sec}

    try:
        err = r.json().get("error", {})
        status, message = err.get("status", ""), err.get("message", "")
    except Exception:
        status, message = "", text[:200]
    quota = re.findall(r'"quotaValue":\s*"?(\d+)', text)
    quota_id = re.findall(r'"quotaId":\s*"([^"]+)"', text)
    detail = status
    if quota:
        detail += f" | ліміт: {', '.join(sorted(set(quota)))}"
    if quota_id:
        detail += f" | {', '.join(sorted(set(q.split('-')[0] for q in quota_id)))}"
    if not quota:
        detail += f" | {message[:120]}"
    result = {404: "NOT_FOUND", 429: "QUOTA", 503: "OVERLOADED", 403: "FORBIDDEN", 400: "BAD_REQUEST"}.get(r.status_code, "ERROR")
    return {"model": model, "result": result, "http": r.status_code, "detail": detail, "sec": sec}


def main():
    if not KEY:
        print("::error::GEMINI_API_KEY не задано")
        sys.exit(1)
    rows = []
    for m in MODELS:
        row = check(m)
        rows.append(row)
        print(f"{row['result']:<11} {row['http']!s:<4} {row['sec']:>5}s  {m:<28} {row['detail']}")
        time.sleep(2)

    table = "| Модель | Результат | HTTP | Сек | Деталі |\n|---|---|---|---|---|\n" + "\n".join(
        f"| `{r['model']}` | {r['result']} | {r['http']} | {r['sec']} | {str(r['detail']).replace('|', '/')} |" for r in rows)
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("## Перевірка моделей Gemini\n\n" + table + "\n")
    # Одна анотація з компактним JSON — її можна прочитати через GitHub API (check-runs annotations)
    compact = json.dumps([[r["model"], r["result"], r["http"], r["detail"]] for r in rows], ensure_ascii=False)
    print(f"::notice title=gemini-models::{compact}")


if __name__ == "__main__":
    main()
