"""Общие функции: уровни, проверка вопроса, чтение/запись JSON."""
import html
import json
import os
import tempfile

BASE = os.path.dirname(os.path.abspath(__file__))
BANK_PATH = os.environ.get("QUESTIONS_FILE") or os.path.join(BASE, "questions.json")
STATE_PATH = os.environ.get("STATE_FILE") or os.path.join(BASE, "state.json")
PASSAGES_PATH = os.environ.get("PASSAGES_FILE") or os.path.join(BASE, "passages.json")
PASSAGES_STATE_PATH = os.environ.get("PASSAGES_STATE_FILE") or os.path.join(BASE, "state_passages.json")

# Иллюстрации к отрывкам: единый холст 16:9 (для 21:9 поставьте 1280 x 549)
CANVAS_W, CANVAS_H = 1280, 720
MAX_CAPTION = 1024

# Что показывать под спойлером в посте с отрывком (ответы показываются всегда)
SHOW_EXPLANATION = True
SHOW_SOURCE = True

# Подпись уровня в заголовке опроса
LEVELS = {
    "B1": "B1 (Intermediate)",
    "B2": "B2 (Upper-Intermediate)",
    "C1": "C1 (Advanced)",
}

# Разделитель между строкой уровня и вопросом. Если в вашем Telegram-клиенте
# перенос строки в опросе выглядит плохо, замените на " | "
HEADER_SEP = "\n\n"

# Где показывать строку «уровень · тема»:
#   "question"    — первой строкой самого вопроса (шрифт такой же, как у вопроса; работает всегда)
#   "description" — в описании опроса, где текст обычный, а не жирный (экспериментально).
#                   Если Telegram не примет описание, бот сам вернётся к варианту "question".
LEVEL_LINE = os.environ.get("LEVEL_LINE") or "question"  # можно задать переменной репозитория LEVEL_LINE

# Лимиты Telegram для опросов-викторин
MAX_POLL_QUESTION = 300
MAX_OPTION = 100
MAX_EXPLANATION = 200


def level_line(q):
    """Строка вида «C1 (Advanced) · Vocabulary: Prepositions»."""
    # return f"{LEVELS[q['level']]} · {q['topic']}"
    return f"{LEVELS[q['level']]} <i>12311111</i>· {q['topic']}"


def poll_text(q):
    """Текст вопроса в опросе: строка с уровнем (без эмодзи) + сам вопрос."""
    # return f"{level_line(q)}{HEADER_SEP}{q['question']}"
    # return f"{q['question']}"
    return f"🎯 {LEVELS[q['level']]} · {q['topic']}{HEADER_SEP}{q['question']}"


def validate(q):
    """Возвращает текст ошибки или None, если вопрос корректный."""
    if not isinstance(q, dict):
        return "не объект"
    if q.get("level") not in LEVELS:
        return "неизвестный уровень"
    for key in ("topic", "question", "explanation"):
        if not isinstance(q.get(key), str) or not q[key].strip():
            return f"пустое поле {key}"
    opts = q.get("options")
    if not isinstance(opts, list) or len(opts) != 4:
        return "нужно ровно 4 варианта"
    if not all(isinstance(o, str) and 1 <= len(o) <= MAX_OPTION for o in opts):
        return f"вариант пустой или длиннее {MAX_OPTION} символов"
    if len({o.strip().lower() for o in opts}) != 4:
        return "варианты повторяются"
    c = q.get("correct")
    if not isinstance(c, int) or isinstance(c, bool) or not 0 <= c <= 3:
        return "correct должен быть числом 0-3"
    if len(q["explanation"]) > MAX_EXPLANATION or q["explanation"].count("\n") > 2:
        return f"пояснение длиннее {MAX_EXPLANATION} символов или более 2 переносов"
    if len(poll_text(q)) > MAX_POLL_QUESTION:
        return f"вопрос с заголовком длиннее {MAX_POLL_QUESTION} символов"
    return None


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


# ---------- Отрывки из классики с иллюстрацией ----------
#
# Запись в passages.json:
#   id, book, author, image ("images/p0001.jpg"),
#   text (отрывок с метками пропусков [[1]], [[2]]...),
#   gaps [{"options": [3 слова], "correct": 0-2}], explanation


def _esc(x):
    return html.escape(x, quote=False)


def passage_caption(p):
    """Подпись под картинкой (HTML для Telegram): текст с пропусками, варианты, ответ под спойлером."""
    text = _esc(p["text"])
    for i in range(1, len(p["gaps"]) + 1):
        text = text.replace(f"[[{i}]]", f"({i}) ____")
    lines = [
        f"{i}) " + " / ".join(_esc(o) for o in g["options"])
        for i, g in enumerate(p["gaps"], 1)
    ]
    hidden = [" · ".join(f"{i} {_esc(g['options'][g['correct']])}" for i, g in enumerate(p["gaps"], 1))]
    if SHOW_EXPLANATION and p.get("explanation"):
        hidden.append(_esc(p["explanation"]))
    if SHOW_SOURCE and p.get("book"):
        hidden.append(f"— {_esc(p.get('author', ''))}, {_esc(p['book'])}".replace("— , ", "— "))
    return text + "\n\n" + "\n".join(lines) + "\n\n<tg-spoiler>" + "\n".join(hidden) + "</tg-spoiler>"


def validate_passage(p, check_image=False):
    """Возвращает текст ошибки или None."""
    if not isinstance(p, dict):
        return "не объект"
    for key in ("id", "book", "text", "image", "explanation"):
        if not isinstance(p.get(key), str) or not p[key].strip():
            return f"пустое поле {key}"
    gaps = p.get("gaps")
    if not isinstance(gaps, list) or not 2 <= len(gaps) <= 4:
        return "нужно от 2 до 4 пропусков"
    for i, g in enumerate(gaps, 1):
        opts = g.get("options") if isinstance(g, dict) else None
        if not isinstance(opts, list) or len(opts) != 3:
            return f"пропуск {i}: нужно ровно 3 варианта"
        if not all(isinstance(o, str) and 1 <= len(o) <= 40 for o in opts):
            return f"пропуск {i}: вариант пустой или длиннее 40 символов"
        if len({o.strip().lower() for o in opts}) != 3:
            return f"пропуск {i}: варианты повторяются"
        c = g.get("correct")
        if not isinstance(c, int) or isinstance(c, bool) or not 0 <= c <= 2:
            return f"пропуск {i}: correct должен быть 0-2"
        if p["text"].count(f"[[{i}]]") != 1:
            return f"в тексте должна быть ровно одна метка [[{i}]]"
    if len(passage_caption(p)) > MAX_CAPTION:
        return f"подпись длиннее {MAX_CAPTION} символов"
    if check_image and not os.path.exists(os.path.join(BASE, p["image"])):
        return f"нет файла картинки {p['image']}"
    return None
