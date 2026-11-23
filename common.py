"""Общие функции и настройки English_Post_Lit."""
import html
import json
import os
import tempfile

BASE = os.path.dirname(os.path.abspath(__file__))
PASSAGES_PATH = os.environ.get("PASSAGES_FILE") or os.path.join(BASE, "passages.json")
STATE_PATH = os.environ.get("STATE_FILE") or os.path.join(BASE, "state_passages.json")

# Единый холст для иллюстраций: 16:9 (для 21:9 поставьте 1280 x 549)
CANVAS_W, CANVAS_H = 1280, 720
# Насколько можно подрезать картинку по длинной стороне, чтобы вписать её в холст без полей (0.18 = до 18%)
CROP_MAX_LOSS = 0.18
# Допустимый разброс цвета по краям, чтобы подложить под картинку сплошной «бумажный» фон
EDGE_STD_MAX = 22

MAX_CAPTION = 1024  # лимит Telegram на подпись под фото

# Что показывать под спойлером (ответы показываются всегда)
SHOW_EXPLANATION = True
SHOW_SOURCE = True


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
    return load_json(path or PASSAGES_PATH, [])


def save_bank(bank, path=None):
    """Одна запись на строку: файл компактный, а изменения в git читаются легко."""
    path = path or PASSAGES_PATH
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("[\n")
        f.write(",\n".join(json.dumps(q, ensure_ascii=False) for q in bank))
        f.write("\n]\n")
    os.replace(tmp, path)


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
