#!/usr/bin/env python3
"""Публикует ОДИН пост (картинка + отрывок с пропусками) в Telegram-канал.

Все отрывки перемешиваются и выдаются по одному. Когда очередь кончилась,
банк перемешивается заново (новый круг). Новые отрывки, появившиеся в банке,
автоматически встают в случайные места очереди.
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

from common import (BASE, PASSAGES_PATH, STATE_PATH, load_bank, load_json,
                    passage_caption, save_json, validate_passage)

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
FORCE = os.environ.get("FORCE") == "1"      # публиковать, даже если сегодня уже было
DRY_RUN = os.environ.get("DRY_RUN") == "1"  # только показать, ничего не отправлять
MAX_PER_DAY = int(os.environ.get("POSTS_PER_DAY") or 1)  # лимит публикаций за день


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


def _read_response(req):
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return json.load(e)
        except Exception:
            return {"ok": False, "error_code": e.code, "description": "HTTP error"}


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
        if r.get("error_code") == 429:  # слишком часто: подождать и повторить
            time.sleep(min(r.get("parameters", {}).get("retry_after", 10), 60))
            continue
        break
    raise RuntimeError(f"Telegram отклонил запрос: {last}")


def main():
    good = []
    for item in load_bank(PASSAGES_PATH):
        err = validate_passage(item, check_image=True)
        if err:
            print(f"Пропускаю {item.get('id', '?')}: {err}")
        else:
            good.append(item)
    if not good:
        print("Банк отрывков пуст. Сначала запустите «Пополнение банка» (build) и дождитесь результата.")
        return 1

    state = load_json(STATE_PATH, {})
    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    posted_today = state.get("posts_today", 1) if state.get("last_date") == today else 0
    if not FORCE and not DRY_RUN and posted_today >= MAX_PER_DAY:
        print(f"Сегодня уже опубликовано {posted_today} из {MAX_PER_DAY}, пропускаю.")
        return 0

    item, new_state = choose_next(good, state, random.Random())
    print(f"Отрывок {item['id']} (круг {new_state['cycle']}, осталось в круге: {len(new_state['queue'])})")
    print(passage_caption(item))

    if DRY_RUN:
        print("DRY_RUN: ничего не отправлено.")
        return 0
    if not TOKEN or not CHAT_ID:
        print("Не заданы TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID.")
        return 1

    send_passage(item)
    save_json(STATE_PATH, new_state)  # состояние сохраняем только после успешной отправки
    print("Опубликовано.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
