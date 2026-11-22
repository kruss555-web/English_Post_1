#!/usr/bin/env python3
"""Публикует ОДИН пост в Telegram-канал.

POST_KIND=quiz     — вопрос-викторина (questions.json, очередь в state.json)
POST_KIND=passage  — отрывок из классики с иллюстрацией (passages.json, очередь в state_passages.json)

Порядок: все записи перемешиваются и выдаются по одной. Когда очередь кончилась,
банк перемешивается заново (новый круг). Новые записи автоматически встают
в случайные места очереди.
"""
import datetime
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
import uuid

from common import (
    BASE, BANK_PATH, LEVEL_LINE, PASSAGES_PATH, PASSAGES_STATE_PATH, STATE_PATH,
    level_line, load_bank, load_json, passage_caption, poll_text, save_json,
    validate, validate_passage,
)

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
KIND = (os.environ.get("POST_KIND") or "quiz").strip().lower()
FORCE = os.environ.get("FORCE") == "1"      # публиковать, даже если сегодня уже было
DRY_RUN = os.environ.get("DRY_RUN") == "1"  # только показать, ничего не отправлять
MAX_PER_DAY = int(os.environ.get("POSTS_PER_DAY") or 1)  # лимит публикаций каждого вида за день


def choose_next(bank, state, rng):
    """Выбирает следующую запись. Возвращает (запись, новое_состояние)."""
    by_id = {q["id"]: q for q in bank}
    queue = [i for i in state.get("queue", []) if i in by_id]
    done = [i for i in state.get("done", []) if i in by_id]
    cycle = state.get("cycle", 1)

    # Новые записи вставляем в случайные места очереди
    known = set(queue) | set(done)
    for qid in by_id:
        if qid not in known:
            queue.insert(rng.randint(0, len(queue)), qid)

    # Очередь закончилась: начинаем новый круг с новой перемешкой
    if not queue:
        queue = list(by_id)
        rng.shuffle(queue)
        if done:
            cycle += 1
        done = []
        # чтобы последняя запись прошлого круга не стала первой в новом
        if len(queue) > 1 and queue[0] == state.get("last_id"):
            j = rng.randint(1, len(queue) - 1)
            queue[0], queue[j] = queue[j], queue[0]

    qid = queue.pop(0)
    done.append(qid)
    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    posted_before = state.get("posts_today", 1) if state.get("last_date") == today else 0
    new_state = {
        "cycle": cycle,
        "queue": queue,
        "done": done,
        "last_id": qid,
        "last_date": today,
        "posts_today": posted_before + 1,
    }
    return by_id[qid], new_state


# ---------- Telegram ----------

def _read_response(req):
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return json.load(e)
        except Exception:
            return {"ok": False, "error_code": e.code, "description": "HTTP error"}


def tg_call(method, payload):
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return _read_response(req)


def encode_multipart(fields, files):
    """Собирает multipart/form-data. files: {имя_поля: (имя_файла, байты, mime)}."""
    boundary = uuid.uuid4().hex
    body = bytearray()
    for name, value in fields.items():
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                 f'{value}\r\n').encode("utf-8")
    for name, (filename, data, mime) in files.items():
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
                 f'filename="{filename}"\r\nContent-Type: {mime}\r\n\r\n').encode("utf-8")
        body += data + b"\r\n"
    body += f"--{boundary}--\r\n".encode("utf-8")
    return bytes(body), f"multipart/form-data; boundary={boundary}"


def tg_upload(method, fields, files):
    body, ctype = encode_multipart(fields, files)
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    req = urllib.request.Request(url, data=body, headers={"Content-Type": ctype}, method="POST")
    return _read_response(req)


def send_quiz(q):
    base = {
        "chat_id": CHAT_ID,
        "options": [{"text": o} for o in q["options"]],
        "type": "quiz",
        "is_anonymous": True,  # в каналах викторины всегда анонимные
        "explanation": q["explanation"],
    }
    # Режимы показа строки уровня: сначала выбранный в common.py, запасной всегда "question"
    modes = ["description", "question"] if LEVEL_LINE == "description" else ["question"]
    # Новый параметр (Bot API 9.6+) и старый как запасной вариант
    variants = [{"correct_option_ids": [q["correct"]]}, {"correct_option_id": q["correct"]}]
    last = None
    for mode in modes:
        if mode == "description":
            payload = {**base, "question": q["question"], "description": level_line(q)}
        else:
            payload = {**base, "question": poll_text(q)}
        for extra in variants:
            for _ in range(2):
                r = tg_call("sendPoll", {**payload, **extra})
                if r.get("ok"):
                    print(f"Режим строки уровня: {mode}")
                    return r
                last = r
                if r.get("error_code") == 429:  # слишком часто — подождать и повторить
                    time.sleep(min(r.get("parameters", {}).get("retry_after", 10), 60))
                    continue
                break
        if mode == "description":
            print(f"Telegram не принял описание опроса ({last}); публикую с уровнем в вопросе.")
    raise RuntimeError(f"Telegram отклонил запрос: {last}")


def send_passage(p):
    with open(os.path.join(BASE, p["image"]), "rb") as f:
        data = f.read()
    fields = {"chat_id": CHAT_ID, "caption": passage_caption(p), "parse_mode": "HTML"}
    last = None
    for _ in range(2):
        r = tg_upload("sendPhoto", fields, {"photo": (os.path.basename(p["image"]), data, "image/jpeg")})
        if r.get("ok"):
            return r
        last = r
        if r.get("error_code") == 429:
            time.sleep(min(r.get("parameters", {}).get("retry_after", 10), 60))
            continue
        break
    raise RuntimeError(f"Telegram отклонил запрос: {last}")


# ---------- Основная логика ----------

def run(kind, limit):
    if kind == "passage":
        bank = load_bank(PASSAGES_PATH)
        check = lambda x: validate_passage(x, check_image=True)
        state_path, send, show = PASSAGES_STATE_PATH, send_passage, passage_caption
    else:
        bank = load_bank(BANK_PATH)
        check = validate
        state_path, send, show = STATE_PATH, send_quiz, poll_text

    good = []
    for item in bank:
        err = check(item)
        if err:
            print(f"Пропускаю {item.get('id', '?')}: {err}")
        else:
            good.append(item)

    if not good:
        if kind == "passage":
            print("Банк отрывков пока пуст: вместо отрывка публикую викторину.")
            return run("quiz", max(limit, 2))
        print("В questions.json нет корректных вопросов.")
        return 1

    state = load_json(state_path, {})
    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    posted_today = state.get("posts_today", 1) if state.get("last_date") == today else 0
    if not FORCE and not DRY_RUN and posted_today >= limit:
        print(f"Сегодня уже опубликовано {posted_today} из {limit} ({kind}), пропускаю.")
        return 0

    item, new_state = choose_next(good, state, random.Random())
    print(f"{kind} {item['id']} (круг {new_state['cycle']}, "
          f"осталось в круге: {len(new_state['queue'])})")
    print(show(item))

    if DRY_RUN:
        print("DRY_RUN: ничего не отправлено.")
        return 0
    if not TOKEN or not CHAT_ID:
        print("Не заданы TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID.")
        return 1

    send(item)
    save_json(state_path, new_state)  # состояние сохраняем только после успешной отправки
    print("Опубликовано.")
    return 0


def main():
    kind = "passage" if KIND == "passage" else "quiz"
    return run(kind, MAX_PER_DAY)


if __name__ == "__main__":
    sys.exit(main())
