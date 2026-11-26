"""Общие настройки и функции English_Post_Lit: посты «цитата + вопрос на смысл»."""
import html
import json
import os
import tempfile

BASE = os.path.dirname(os.path.abspath(__file__))
BANK_PATH = os.environ.get("BANK_FILE") or os.path.join(BASE, "bank.json")
STATE_PATH = os.environ.get("STATE_FILE") or os.path.join(BASE, "state.json")

QUESTION_ICON = "❓"           # значок перед вопросом опроса
MAX_QUESTION = 300             # лимиты Telegram для опросов-викторин
MAX_OPTION = 100
MAX_EXPLANATION = 200
MAX_DESCRIPTION = 1024
MIN_OPTIONS, MAX_OPTIONS = 3, 5


# ---------- файлы ----------

def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_json(path, data):
    """Атомарная запись, чтобы файл не повредился при сбое."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def load_bank(path=None):
    return load_json(path or BANK_PATH, [])


def save_bank(bank, path=None):
    """Одна запись на строку: файл компактный, а изменения в git читаются легко."""
    path = path or BANK_PATH
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("[\n")
        f.write(",\n".join(json.dumps(q, ensure_ascii=False) for q in bank))
        f.write("\n]\n")
    os.replace(tmp, path)


# ---------- оформление поста ----------
# Запись банка:
#   id, type ("excerpt" из книги или "original" собственный текст), title, author, genre,
#   text (цитата, переносы строк сохранены), question, options (3-5), correct (индекс), explanation

def _esc(x):
    return html.escape(x, quote=False)


def source_line(item):
    """Подпись под цитатой без тире: «Alice's Adventures in Wonderland», Lewis Carroll."""
    if item.get("type") == "original":
        return f"original text · {item.get('genre') or 'humor'}"
    title = f"“{item['title']}”"
    return f"{title}, {item['author']}" if item.get("author") else title


def poll_question(item):
    return f"{QUESTION_ICON} {item['question']}"


def quote_html(item):
    """Цитата жирным, ниже курсивом источник: обычный HTML Telegram (подходит и для сообщения, и для описания опроса)."""
    return f"📗  <b>{_esc(item['text'])}</b>\n<i>— {_esc(source_line(item))}</i>\n\n〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️\n\n"


def validate_item(it):
    """Возвращает текст ошибки или None."""
    if not isinstance(it, dict):
        return "не объект"
    if it.get("type") not in ("excerpt", "original"):
        return "type должен быть excerpt или original"
    for key in ("id", "text", "question", "explanation"):
        if not isinstance(it.get(key), str) or not it[key].strip():
            return f"пустое поле {key}"
    if it["type"] == "excerpt" and not (isinstance(it.get("title"), str) and it["title"].strip()):
        return "у цитаты нужно название источника title"
    if not 80 <= len(it["text"]) <= 700:
        return "текст должен быть 80-700 символов"
    if len(poll_question(it)) > MAX_QUESTION:
        return f"вопрос длиннее {MAX_QUESTION} символов"
    opts = it.get("options")
    if not isinstance(opts, list) or not MIN_OPTIONS <= len(opts) <= MAX_OPTIONS:
        return f"нужно от {MIN_OPTIONS} до {MAX_OPTIONS} вариантов"
    if not all(isinstance(o, str) and 1 <= len(o) <= MAX_OPTION for o in opts):
        return f"вариант пустой или длиннее {MAX_OPTION} символов"
    if len({o.strip().lower() for o in opts}) != len(opts):
        return "варианты повторяются"
    c = it.get("correct")
    if not isinstance(c, int) or isinstance(c, bool) or not 0 <= c < len(opts):
        return "correct: номер правильного варианта"
    if len(it["explanation"]) > MAX_EXPLANATION or it["explanation"].count("\n") > 2:
        return f"пояснение длиннее {MAX_EXPLANATION} символов или более 2 переносов"
    if len(quote_html(it)) > MAX_DESCRIPTION:
        return f"цитата с подписью длиннее {MAX_DESCRIPTION} символов"
    return None
