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


class _Unavailable(Exception):
    """Модель перегружена или недоступна (503, 429 и т. п.): можно подождать или взять запасную."""


class _NoModel(Exception):
    """Модели с таким именем нет (404)."""


# Запасные модели на случай перегрузки основной (через запятую; у каждой модели свои лимиты)
FALLBACK_MODELS = [m.strip() for m in (os.environ.get("GEMINI_FALLBACK_MODELS") or "gemini-flash-lite-latest").split(",")
                   if m.strip()]


def _short(detail):
    """Достаёт из ответа ошибки короткое сообщение вместо длинного JSON."""
    try:
        err = json.loads(detail).get("error", {})
        return f"{err.get('status', '')} {err.get('message', '')}".strip() or detail[:200]
    except (ValueError, AttributeError):
        return detail[:200]


def _request(model, body):
    """Один запрос к одной модели: при перегрузке подождать и повторить."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    headers = {"Content-Type": "application/json", "x-goog-api-key": API_KEY}
    waits = [20, 45]
    last = ""
    for attempt in range(len(waits) + 1):
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            detail = _short(e.read().decode("utf-8", "replace"))
            if e.code in (429, 500, 502, 503, 504):
                last = f"{e.code} {detail}"
                if attempt < len(waits):
                    print(f"  {model}: {e.code}, жду {waits[attempt]} с и повторяю...")
                    time.sleep(waits[attempt])
                    continue
                raise _Unavailable(last)
            if e.code == 404:
                raise _NoModel(detail)
            raise Fatal(f"Gemini API {e.code}: {detail}")
        except (urllib.error.URLError, TimeoutError) as e:
            last = f"сеть: {e}"
            if attempt < len(waits):
                time.sleep(waits[attempt])
                continue
            raise _Unavailable(last)


def gemini(prompt, temperature):
    body = json.dumps({
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": temperature, "responseMimeType": "application/json"},
    }).encode("utf-8")
    models = [MODEL] + [m for m in FALLBACK_MODELS if m != MODEL]
    errors, data = [], None
    for i, model in enumerate(models):
        try:
            data = _request(model, body)
            if i > 0:
                print(f"  (ответила запасная модель {model})")
            break
        except _NoModel as e:
            if i == 0:
                raise Fatal(f"Модель {model} не найдена, проверьте GEMINI_MODEL: {e}")
            print(f"  запасная модель {model} недоступна, пропускаю")
        except _Unavailable as e:
            errors.append(f"{model}: {e}")
            if i + 1 < len(models):
                print(f"  {model} перегружена, пробую запасную модель...")
    if data is None:
        raise SoftStop("; ".join(errors) or "все модели недоступны")
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
