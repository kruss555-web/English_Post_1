#!/usr/bin/env python3
"""Публикует ОДИН пост: цитата с подписью + опрос-викторина на смысл, оформленные как единый блок.

Четыре способа оформления (переменная POST_FORMAT):
  description — всё в одном сообщении: цитата и подпись в описании опроса (самый цельный вариант);
  article     — цитата расширенным сообщением (Rich Message), опрос ответом на него;
  card        — цитата картинкой-карточкой, опрос ответом на неё;
  classic     — цитата обычным сообщением (жирный текст, ниже курсив), опрос ответом на него;
  auto        — по умолчанию: description, затем article, затем classic.
Если выбранный способ Telegram не принимает, бот переходит к следующему.

FORMAT_TEST=1 публикует один образец всеми способами подряд, чтобы выбрать оформление глазами.
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

from common import (STATE_PATH, load_bank, load_json, poll_question, quote_html, rich_html,
                    save_json, source_line, validate_item)

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
FORMAT = (os.environ.get("POST_FORMAT") or "auto").strip().lower()
FORMAT_TEST = os.environ.get("FORMAT_TEST") == "1"
FORCE = os.environ.get("FORCE") == "1"      # публиковать, даже если сегодня уже было
DRY_RUN = os.environ.get("DRY_RUN") == "1"  # только показать, ничего не отправлять
MAX_PER_DAY = int(os.environ.get("POSTS_PER_DAY") or 1)
OPTION_ORDER = ("description", "article", "card", "classic")


class FormatRejected(Exception):
    """Telegram не принял этот способ оформления; ничего не опубликовано, можно пробовать следующий."""


class PartialPost(Exception):
    """Цитата уже опубликована, а опрос под ней не вышел. Повторять нельзя, иначе цитата задвоится."""


# ---------- выбор следующего поста ----------

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


# ---------- Telegram ----------

def _read(req):
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return json.load(e)
        except Exception:
            return {"ok": False, "error_code": e.code, "description": "HTTP error"}


def _retry(make_request):
    res = {}
    for attempt in range(3):
        res = _read(make_request())
        if res.get("ok") or res.get("error_code") != 429 or attempt == 2:
            return res
        time.sleep(min(res.get("parameters", {}).get("retry_after", 10), 60))
    return res


def tg_call(method, payload):
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    data = json.dumps(payload).encode("utf-8")
    return _retry(lambda: urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"))


def encode_multipart(fields, files):
    """Собирает multipart/form-data. files: {имя_поля: (имя_файла, байты, mime)}."""
    boundary = uuid.uuid4().hex
    body = bytearray()
    for name, value in fields.items():
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n').encode("utf-8")
    for name, (filename, data, mime) in files.items():
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
                 f'filename="{filename}"\r\nContent-Type: {mime}\r\n\r\n').encode("utf-8")
        body += data + b"\r\n"
    body += f"--{boundary}--\r\n".encode("utf-8")
    return bytes(body), f"multipart/form-data; boundary={boundary}"


def tg_upload(method, fields, files):
    body, ctype = encode_multipart(fields, files)
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    return _retry(lambda: urllib.request.Request(url, data=body, headers={"Content-Type": ctype}, method="POST"))


def send_poll(item, description=None, reply_to=None):
    """Опрос-викторина. Возвращает ответ Telegram или бросает RuntimeError."""
    base = {
        "chat_id": CHAT_ID,
        "question": poll_question(item),
        "options": [{"text": o} for o in item["options"]],
        "type": "quiz",
        "is_anonymous": True,   # в каналах викторины всегда анонимные
        "explanation": item["explanation"],
    }
    if description:
        base["description"] = description
        base["description_parse_mode"] = "HTML"
    variants = [base]
    if reply_to:
        variants = [{**base, "reply_parameters": {"message_id": reply_to}}, base]   # без ответа: запасной вариант
    last = None
    for payload in variants:
        # Новый параметр (Bot API 9.6+) и старый как запасной вариант
        for extra in ({"correct_option_ids": [item["correct"]]}, {"correct_option_id": item["correct"]}):
            r = tg_call("sendPoll", {**payload, **extra})
            if r.get("ok"):
                return r
            last = r
    raise RuntimeError(f"{last.get('error_code')}: {last.get('description')}")


# ---------- четыре способа оформления ----------

def _then_poll(item, message_id):
    try:
        send_poll(item, reply_to=message_id)
    except RuntimeError as e:
        raise PartialPost(f"цитата опубликована, а опрос нет: {e}")


def post_description(item):
    try:
        send_poll(item, description=quote_html(item))
    except RuntimeError as e:
        raise FormatRejected(f"описание опроса не принято: {e}")


def post_article(item, small=False):
    r = tg_call("sendRichMessage", {"chat_id": CHAT_ID, "rich_message": {"html": rich_html(item, small)}})
    if not r.get("ok"):
        raise FormatRejected(f"расширенное сообщение не принято: {r.get('error_code')}: {r.get('description')}")
    _then_poll(item, r["result"]["message_id"])


def post_classic(item):
    r = tg_call("sendMessage", {"chat_id": CHAT_ID, "text": quote_html(item), "parse_mode": "HTML",
                                "link_preview_options": {"is_disabled": True}})
    if not r.get("ok"):
        raise FormatRejected(f"обычное сообщение не принято: {r.get('error_code')}: {r.get('description')}")
    _then_poll(item, r["result"]["message_id"])


FONT_DIRS = ["/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu", "/usr/share/fonts/TTF"]


def _font(name, size):
    from PIL import ImageFont
    for d in FONT_DIRS:
        path = os.path.join(d, name)
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _wrap(draw, text, font, max_w):
    lines = []
    for raw in text.split("\n"):
        cur = ""
        for word in raw.split(" "):
            test = f"{cur} {word}".strip()
            if cur and draw.textlength(test, font=font) > max_w:
                lines.append(cur)
                cur = word
            else:
                cur = test
        lines.append(cur)
    return lines


def render_card(item):
    """Картинка-карточка: цитата жирным шрифтом, ниже курсивом источник. Возвращает PNG-байты."""
    import io
    from PIL import Image, ImageDraw
    W, pad = 1080, 84
    quote_font, src_font = _font("DejaVuSerif-Bold.ttf", 44), _font("DejaVuSerif-Italic.ttf", 30)
    probe = ImageDraw.Draw(Image.new("RGB", (W, 10)))
    q_lines = _wrap(probe, item["text"], quote_font, W - 2 * pad - 24)
    s_lines = _wrap(probe, "— " + source_line(item), src_font, W - 2 * pad - 24)
    q_h, s_h = 62, 44
    H = max(560, pad * 2 + len(q_lines) * q_h + 50 + len(s_lines) * s_h)
    img = Image.new("RGB", (W, H), (248, 243, 232))
    d = ImageDraw.Draw(img)
    y = pad
    d.rectangle((pad, y, pad + 8, y + len(q_lines) * q_h), fill=(190, 140, 60))     # акцентная полоса
    for line in q_lines:
        d.text((pad + 36, y), line, font=quote_font, fill=(32, 30, 28))
        y += q_h
    y += 50
    for line in s_lines:
        d.text((pad + 36, y), line, font=src_font, fill=(115, 104, 90))
        y += s_h
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def post_card(item):
    try:
        png = render_card(item)
    except Exception as e:
        raise FormatRejected(f"не удалось нарисовать карточку (нужны Pillow и шрифт): {e}")
    r = tg_upload("sendPhoto", {"chat_id": CHAT_ID}, {"photo": ("quote.png", png, "image/png")})
    if not r.get("ok"):
        raise FormatRejected(f"фото не принято: {r.get('error_code')}: {r.get('description')}")
    _then_poll(item, r["result"]["message_id"])


POSTERS = {"description": post_description, "article": post_article, "card": post_card, "classic": post_classic}


def post_item(item, fmt):
    """Публикует запись выбранным способом, при отказе Telegram переходит к следующему. Возвращает способ."""
    order = ["description", "article", "classic"] if fmt == "auto" else [fmt, "classic"]
    order = [f for f in dict.fromkeys(order) if f in POSTERS]
    for name in order:
        try:
            POSTERS[name](item)
            return name
        except FormatRejected as e:
            print(f"Способ «{name}» не сработал: {e}")
    raise RuntimeError("Ни один способ оформления не принят Telegram.")


# ---------- проверка оформления ----------

SAMPLE = {
    "id": "sample", "type": "original", "genre": "humor",
    "text": ("The cat looked at the fish, then at the cook, then at the fish again.\n"
             "\"I am not hungry,\" he told himself. \"I am only checking that it is safe.\"\n"
             "By the time the cook turned around, the plate was empty."),
    "question": "Why did the cat \"check\" the fish?",
    "options": ["He wanted an excuse to eat it", "He was afraid of the cook",
                "He wanted to guard it from other cats", "He was not interested in food"],
    "correct": 0,
    "explanation": "«I am only checking» — отговорка: к концу тарелка пуста, значит, кот съел рыбу.",
}


def run_format_test():
    err = validate_item(SAMPLE)
    if err:
        print(f"Образец не прошёл проверку: {err}")
        return 1
    if DRY_RUN:
        print(quote_html(SAMPLE) + "\n\n" + rich_html(SAMPLE, True) + "\n\n" + poll_question(SAMPLE))
        print("DRY_RUN: ничего не отправлено.")
        return 0
    results = []

    def label(text):
        tg_call("sendMessage", {"chat_id": CHAT_ID, "text": text})

    def attempt(title, fn):
        label(f"▼ {title}")
        try:
            fn()
            results.append((title, "сработал"))
        except FormatRejected as e:
            results.append((title, f"НЕ принят: {e}"))
        except PartialPost as e:
            results.append((title, f"частично: {e}"))

    attempt("Вариант A: цитата в описании опроса (всё в одном сообщении)", lambda: post_description(SAMPLE))

    def article():
        try:
            post_article(SAMPLE, small=True)
            results.append(("   подпись мелким шрифтом (<small>)", "принята"))
        except FormatRejected as e:
            results.append(("   подпись мелким шрифтом (<small>)", f"не принята: {e}"))
            post_article(SAMPLE, small=False)
    attempt("Вариант B: статья (расширенное сообщение), опрос ответом", article)
    attempt("Вариант C: цитата картинкой-карточкой, опрос ответом", lambda: post_card(SAMPLE))
    attempt("Вариант D: обычное сообщение (жирный текст и курсив), опрос ответом", lambda: post_classic(SAMPLE))

    print("\nИтоги проверки оформления:")
    for title, res in results:
        print(f" - {title}: {res}")
    return 0


# ---------- основная логика ----------

def main():
    if FORMAT_TEST:
        if not DRY_RUN and (not TOKEN or not CHAT_ID):
            print("Не заданы TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID.")
            return 1
        return run_format_test()

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
    if FORMAT != "auto" and FORMAT not in POSTERS:
        print(f"Неизвестный POST_FORMAT «{FORMAT}». Допустимо: auto, {', '.join(OPTION_ORDER)}.")
        return 1

    try:
        used = post_item(item, FORMAT)
    except PartialPost as e:
        save_json(STATE_PATH, new_state)          # цитата уже в канале: не публикуем её повторно
        print(f"ВНИМАНИЕ: {e}")
        return 1
    save_json(STATE_PATH, new_state)              # состояние сохраняем только после успешной отправки
    print(f"Опубликовано (оформление: {used}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
