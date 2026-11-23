"""Клиент Gemini API (бесплатный уровень): повторы при перегрузке и мягкая остановка по лимитам."""
import json
import os
import re
import time
import urllib.error
import urllib.request

API_KEY = os.environ.get("GEMINI_API_KEY", "")
MODEL = os.environ.get("GEMINI_MODEL") or "gemini-flash-latest"
REQUEST_DELAY = float(os.environ.get("REQUEST_DELAY") or 7)  # пауза между запросами, сек


class SoftStop(Exception):
    """Лимит или временный сбой API: сохраняем прогресс и спокойно завершаемся."""


class Fatal(Exception):
    """Ошибка настройки (неверный ключ, имя модели): запуск помечается как неудачный."""


def gemini(prompt, temperature):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
    body = json.dumps({
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": temperature, "responseMimeType": "application/json"},
    }).encode("utf-8")
    headers = {"Content-Type": "application/json", "x-goog-api-key": API_KEY}
    waits = [20, 45, 90]
    data = None
    for attempt in range(len(waits) + 1):
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                data = json.load(r)
            break
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            if e.code in (429, 500, 502, 503, 504):
                if attempt < len(waits):
                    print(f"  Gemini вернул {e.code}, жду {waits[attempt]} с и повторяю...")
                    time.sleep(waits[attempt])
                    continue
                raise SoftStop(f"Gemini {e.code}: {detail}")
            raise Fatal(f"Gemini API {e.code}: {detail}")
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < len(waits):
                time.sleep(waits[attempt])
                continue
            raise SoftStop(f"сеть недоступна: {e}")
    try:
        parts = data["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts if not p.get("thought"))
    except (KeyError, IndexError, TypeError):
        return None  # например, ответ заблокирован фильтром


def parse_json(text):
    if not text:
        return None
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        return None
