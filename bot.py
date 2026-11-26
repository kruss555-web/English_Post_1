#!/usr/bin/env python3
"""Публикует ОДИН пост: цитата с подписью и опрос-викторина на смысл в одном сообщении.

Цитата (жирным) и источник (курсивом) помещаются в описание опроса, поэтому всё выглядит одним блоком.
Берётся следующий пост из очереди: записи перемешиваются и выдаются по одной, потом банк перемешивается заново.
"""
import datetime
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request

from common import (STATE_PATH, load_bank, load_json, poll_question, quote_html, save_json, validate_item)

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
FORCE = os.environ.get("FORCE") == "1"      # публиковать, даже если сегодня уже было
DRY_RUN = os.environ.get("DRY_RUN") == "1"  # только показать, ничего не отправлять
MAX_PER_DAY = int(os.environ.get("POSTS_PER_DAY") or 1)


def choose_next(bank, state, rng):
    """Выбирает следующую запись. Возвращает (запись, новое_состояние)."""
    by_id = {q["id"]: q for q in bank}
    queue = [i for i in state.get("queue", []) if i in by_id]
    done = [i for i in state.get("done", []) if i in by_id]
    cycle = state.get("cycle", 1)

    known = set(queue) | set(done)
    for qid in by_id:                     # новые записи встают в случайные места очереди
        if qid not in known:
            queue.insert(rng.randint(0, len(queue)), qid)

    if not queue:                         # очередь закончилась: новый круг с новой перемешкой
        queue = list(by_id)
        rng.shuffle(queue)
        if done:
            cycle += 1
        done = []
        if len(queue) > 1 and queue[0] == state.get("last_id"):
            j = rng.randint(1, len(queue) - 1)
            queue[0], queue[j] = queue[j], queue[0]

    qid = queue.pop(0)
    done.append(qid)
    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    posted_before = state.get("posts_today", 1) if state.get("last_date") == today else 0
    return by_id[qid], {"cycle": cycle, "queue": queue, "done": done, "last_id": qid,
                        "last_date": today, "posts_today": posted_before + 1}


def tg_call(method, payload):
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    data = json.dumps(payload).encode("utf-8")
    res = {}
    for attempt in range(3):
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                res = json.load(r)
        except urllib.error.HTTPError as e:
            try:
                res = json.load(e)
            except Exception:
                res = {"ok": False, "error_code": e.code, "description": "HTTP error"}
        if res.get("ok") or res.get("error_code") != 429 or attempt == 2:
            return res
        time.sleep(min(res.get("parameters", {}).get("retry_after", 10), 60))
    return res


def send_post(item):
    """Опрос-викторина, у которого в описании стоит цитата с подписью."""
    base = {
        "chat_id": CHAT_ID,
        "question": poll_question(item),
        "description": quote_html(item),
        "description_parse_mode": "HTML",
        "options": [{"text": o} for o in item["options"]],
        "type": "quiz",
        "is_anonymous": True,   # в каналах викторины всегда анонимные
        "explanation": item["explanation"],
    }
    last = None
    # Новый параметр (Bot API 9.6+) и старый как запасной вариант
    for extra in ({"correct_option_ids": [item["correct"]]}, {"correct_option_id": item["correct"]}):
        r = tg_call("sendPoll", {**base, **extra})
        if r.get("ok"):
            return r
        last = r
    raise RuntimeError(f"Telegram не принял пост: {last.get('error_code')}: {last.get('description')}")


def main():
    good = []
    for item in load_bank():
        err = validate_item(item)
        if err:
            print(f"Пропускаю {item.get('id', '?')}: {err}")
        else:
            good.append(item)
    if not good:
        print("::warning::Банк постов пока пуст. Запустите «пополнение банка» и дождитесь результата.")
        return 0

    state = load_json(STATE_PATH, {})
    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    posted_today = state.get("posts_today", 1) if state.get("last_date") == today else 0
    if not FORCE and not DRY_RUN and posted_today >= MAX_PER_DAY:
        print(f"Сегодня уже опубликовано {posted_today} из {MAX_PER_DAY}, пропускаю.")
        return 0

    item, new_state = choose_next(good, state, random.Random())
    print(f"Пост {item['id']} (круг {new_state['cycle']}, осталось в круге: {len(new_state['queue'])})")
    print(quote_html(item) + "\n" + poll_question(item) + "\n" + "\n".join(
        f" {'*' if i == item['correct'] else '-'} {o}" for i, o in enumerate(item["options"])))

    if DRY_RUN:
        print("DRY_RUN: ничего не отправлено.")
        return 0
    if not TOKEN or not CHAT_ID:
        print("Не заданы TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID.")
        return 1

    try:
        send_post(item)
    except RuntimeError as e:
        print(f"ОШИБКА: {e}\nОчередь не сдвинута: следующий запуск попробует этот же пост.")
        return 1
    save_json(STATE_PATH, new_state)      # состояние сохраняем только после успешной отправки
    print("Опубликовано.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
