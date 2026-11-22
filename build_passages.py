#!/usr/bin/env python3
"""Собирает банк заданий «отрывок из классики + иллюстрация из той же книги».

Как это работает:
  1. Из books.json берутся книги Project Gutenberg (HTML-версия с картинками).
     В таких изданиях иллюстрация стоит рядом с тем местом текста, которое она показывает.
  2. Для иллюстрации берётся окружающий текст, а Gemini выбирает отрывок из 3-4 предложений.
     Отрывок обязан дословно совпасть с текстом книги, поэтому выдуманных цитат не бывает.
  3. Gemini выбирает 2-3 слова для пропуска и придумывает неверные варианты.
  4. Вторым запросом модель решает задание без подсказок: берутся только однозначные.
  5. Картинка вписывается без обрезки в холст 16:9 на размытом фоне и сохраняется в images/.
Лимиты бесплатного тарифа учитываются так же, как в generate.py: прогресс сохраняется
после каждого задания.
"""
import io
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

import generate as gen
from common import (BASE, CANVAS_H, CANVAS_W, PASSAGES_PATH, load_bank, load_json,
                    save_bank, validate_passage)

TARGET = int(os.environ.get("PASSAGES_TARGET") or 365)
PER_RUN = int(os.environ.get("PASSAGES_PER_RUN") or 20)
COUNT = (os.environ.get("PASSAGES_COUNT") or "").strip()
BOOKS_PATH = os.path.join(BASE, "books.json")
IMAGES_DIR = os.path.join(BASE, "images")
UA = "english-quiz-bot/1.0 (personal educational channel; low volume)"
GUTENBERG_HTML = "https://www.gutenberg.org/cache/epub/{id}/pg{id}-images.html"

# Замены символ-в-символ: позволяют сравнивать «умные» и прямые кавычки без сдвига позиций
CHARMAP = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'", "—": "-", "–": "-", "\u00a0": " "})


# ---------- Сеть ----------

def http_get(url, tries=3):
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except (urllib.error.URLError, TimeoutError):
            if attempt == tries - 1:
                raise
            time.sleep(3 * (attempt + 1))


# ---------- Разбор HTML книги ----------

class BookParser(HTMLParser):
    BLOCK = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "li", "tr", "table", "pre"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tokens = []
        self.buf = []
        self.skip = 0
        self.in_pagenum = False

    def flush(self):
        text = re.sub(r"\s+", " ", "".join(self.buf)).strip()
        self.buf = []
        if text:
            self.tokens.append({"t": "p", "text": text})

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("head", "script", "style"):
            self.skip += 1
            return
        if self.skip:
            return
        if tag == "span" and "pagenum" in (a.get("class") or ""):
            self.in_pagenum = True
        elif tag == "img":
            self.flush()
            self.tokens.append({"t": "img", "src": a.get("src") or "", "alt": a.get("alt") or ""})
        elif tag == "br":
            self.buf.append(" ")
        elif tag in self.BLOCK:
            self.flush()

    def handle_endtag(self, tag):
        if tag in ("head", "script", "style"):
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag == "span":
            self.in_pagenum = False
        elif tag in self.BLOCK:
            self.flush()

    def handle_data(self, data):
        if not self.skip and not self.in_pagenum:
            self.buf.append(data)

    def close(self):
        super().close()
        self.flush()


def decode_html(raw):
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def extract_candidates(tokens, base_url):
    """Для каждой иллюстрации собирает текст до и после неё."""
    out = []
    for i, t in enumerate(tokens):
        if t["t"] != "img" or not t["src"] or t["src"].startswith("data:"):
            continue
        before, j = [], i - 1
        while j >= 0 and tokens[j]["t"] == "p" and len(before) < 3:
            before.insert(0, tokens[j]["text"])
            j -= 1
        after, k = [], i + 1
        while k < len(tokens) and tokens[k]["t"] == "p" and len(after) < 6:
            after.append(tokens[k]["text"])
            k += 1
        caption = ""
        if after and len(after[0]) <= 160:
            caption = after.pop(0)
        elif before and len(before[-1]) <= 160:
            caption = before.pop()
        keep = lambda ps: [p for p in ps if len(p) >= 60 and "Gutenberg" not in p]
        before, after = keep(before), keep(after)
        if sum(map(len, before)) + sum(map(len, after)) < 250:
            continue
        out.append({
            "url": urllib.parse.urljoin(base_url, t["src"]),
            "caption": caption,
            "before": " ".join(before),
            "after": " ".join(after),
        })
    return out


# ---------- Картинки ----------

def image_ok(raw):
    """Отсеивает декоративные заставки, буквицы и тонкие линии."""
    from PIL import Image
    try:
        im = Image.open(io.BytesIO(raw))
        w, h = im.size
    except Exception:
        return False
    return min(w, h) >= 220 and w * h >= 90000 and 0.35 <= w / h <= 3.0


def make_canvas(raw):
    """Вписывает иллюстрацию целиком (без обрезки) в холст CANVAS_W x CANVAS_H на размытом фоне."""
    from PIL import Image, ImageEnhance, ImageFilter
    im = Image.open(io.BytesIO(raw))
    im.load()
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        white = Image.new("RGBA", im.size, (255, 255, 255, 255))
        im = Image.alpha_composite(white, im)
    im = im.convert("RGB")
    w, h = im.size

    scale = max(CANVAS_W / w, CANVAS_H / h)
    bg = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    left, top = (bg.width - CANVAS_W) // 2, (bg.height - CANVAS_H) // 2
    bg = bg.crop((left, top, left + CANVAS_W, top + CANVAS_H)).filter(ImageFilter.GaussianBlur(28))
    bg = ImageEnhance.Brightness(bg).enhance(0.8)

    margin = 24
    fscale = min((CANVAS_W - 2 * margin) / w, (CANVAS_H - 2 * margin) / h, 2.5)
    fg = im.resize((max(1, int(w * fscale)), max(1, int(h * fscale))), Image.LANCZOS)
    bg.paste(fg, ((CANVAS_W - fg.width) // 2, (CANVAS_H - fg.height) // 2))
    return bg


# ---------- Gemini ----------

def parse_json(text):
    if not text:
        return None
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        return None


def collapse(s):
    return re.sub(r"\s+", " ", s).strip()


def selection_prompt(book, cand):
    return f"""You are preparing a fill-in-the-gaps English exercise from a public-domain book.
Book: "{book['title']}" by {book['author']}.
The book has an illustration with the caption: "{cand['caption'] or '(no caption)'}".

TEXT BEFORE THE ILLUSTRATION:
{cand['before'] or '(none)'}

TEXT AFTER THE ILLUSTRATION:
{cand['after'] or '(none)'}

Task:
1. Choose ONE excerpt of 3-4 consecutive sentences (200-480 characters) copied EXACTLY, character for character, from ONE of the two text sections above. Pick the passage that best matches what the illustration most likely shows (a concrete scene, not just dialogue tags). If nothing suitable exists, return {{"skip": true}}.
2. In the excerpt choose 2-3 words or short phrases to blank out. Each must occur exactly once in the excerpt as a whole word, must test vocabulary, word choice, prepositions or grammar at an upper-intermediate or advanced level, and must NOT be a name or a trivial word like "a", "the", "and".
3. For each blanked item give exactly 2 WRONG alternatives: same part of speech or a typical learner confusion, plausible but clearly wrong in this context. Never use misspelled or non-existent words as alternatives. Exactly one option must fit.
4. "explanation": in Russian, at most 150 characters, plain text, briefly says why each correct answer fits (e.g. "1 — be tired of: устать от; 2 — ...").

Return ONLY JSON:
{{"excerpt": "...", "gaps": [{{"answer": "...", "wrong": ["...", "..."]}}], "explanation": "..."}}"""


def verification_prompt(text, gaps):
    blocks = []
    for i, g in enumerate(gaps, 1):
        opts = "  ".join(f"{'ABC'[j]}) {o}" for j, o in enumerate(g["options"]))
        blocks.append(f"({i}) {opts}")
    cloze = text
    for i in range(1, len(gaps) + 1):
        cloze = cloze.replace(f"[[{i}]]", f"({i}) ____")
    return (
        "You are a strict English exam checker. In the text below, choose the best option for each numbered gap. "
        "If more than one option is acceptable in standard English for any gap, set ambiguous to true.\n"
        'Return ONLY JSON like {"answers": ["B", "A"], "ambiguous": false}.\n\n'
        f"TEXT:\n{cloze}\n\nOPTIONS:\n" + "\n".join(blocks)
    )


def cap_like(answer, word):
    """Подгоняет регистр первой буквы под правильный ответ."""
    if not word:
        return word
    return (word[0].upper() if answer[0].isupper() else word[0].lower()) + word[1:]


def build_task(raw, cand, rng):
    """Проверяет ответ модели и собирает запись. Возвращает dict или None."""
    if not isinstance(raw, dict) or raw.get("skip"):
        return None
    excerpt = collapse(str(raw.get("excerpt", "")))
    if not 180 <= len(excerpt) <= 520:
        return None

    # Отрывок должен дословно присутствовать в тексте книги (с точностью до кавычек и тире)
    ex_n = excerpt.translate(CHARMAP)
    source = None
    for section in (collapse(cand["after"]), collapse(cand["before"])):
        idx = section.translate(CHARMAP).find(ex_n)
        if idx >= 0:
            source = section[idx: idx + len(excerpt)]
            break
    if source is None:
        return None

    src_n = source.translate(CHARMAP)
    found = []
    for g in raw.get("gaps") or []:
        if not isinstance(g, dict) or not isinstance(g.get("answer"), str):
            return None
        ans = g["answer"].strip()
        ans_n = ans.translate(CHARMAP)
        if not ans or len(ans) > 30:
            return None
        pattern = r"(?<![\w'])" + re.escape(ans_n) + r"(?![\w'])"
        hits = list(re.finditer(pattern, src_n))
        if len(hits) != 1:
            return None
        wrong = []
        for w in g.get("wrong") or []:
            w = cap_like(ans, str(w).strip())
            low = {x.lower() for x in wrong} | {ans_n.lower()}
            if w and len(w) <= 30 and w.lower() not in low:
                wrong.append(w)
        if len(wrong) < 2:
            return None
        found.append((hits[0].start(), hits[0].end(), source[hits[0].start():hits[0].end()], wrong[:2]))

    found.sort()
    if not 2 <= len(found) <= 4:
        return None
    for a, b in zip(found, found[1:]):
        if a[1] > b[0]:
            return None  # пересекающиеся пропуски

    text, pos, gaps = "", 0, []
    for n, (start, end, answer, wrong) in enumerate(found, 1):
        text += source[pos:start] + f"[[{n}]]"
        pos = end
        options = [answer] + wrong
        rng.shuffle(options)
        gaps.append({"options": options, "correct": options.index(answer)})
    text += source[pos:]

    return {"text": text, "gaps": gaps, "explanation": str(raw.get("explanation", "")).strip()}


def verified(task):
    res = parse_json(gen.gemini(verification_prompt(task["text"], task["gaps"]), 0))
    time.sleep(gen.REQUEST_DELAY)
    if not isinstance(res, dict) or res.get("ambiguous"):
        return False
    answers = res.get("answers")
    if not isinstance(answers, list) or len(answers) != len(task["gaps"]):
        return False
    try:
        got = ["ABC".index(str(a).strip().upper()[:1]) for a in answers]
    except ValueError:
        return False
    return got == [g["correct"] for g in task["gaps"]]


# ---------- Основной цикл ----------

def load_candidates(books, used):
    """Скачивает книги и возвращает {id_книги: [кандидаты]} (без уже использованных картинок)."""
    result = {}
    for book in books:
        url = GUTENBERG_HTML.format(id=book["id"])
        try:
            html_text = decode_html(http_get(url))
        except Exception as e:
            print(f"  не удалось скачать «{book['title']}»: {e}")
            continue
        parser = BookParser()
        parser.feed(html_text)
        parser.close()
        cands = [c for c in extract_candidates(parser.tokens, url) if c["url"] not in used]
        print(f"  «{book['title']}»: найдено иллюстраций с текстом: {len(cands)}")
        result[book["id"]] = cands
        time.sleep(2)
    return result


def main():
    if not gen.API_KEY:
        print("Не задан GEMINI_API_KEY (секрет репозитория).")
        return 1
    bank = load_bank(PASSAGES_PATH)
    to_add = int(COUNT) if COUNT else min(PER_RUN, TARGET - len(bank))
    if to_add <= 0:
        print(f"В банке {len(bank)} отрывков, цель {TARGET} достигнута. Ничего не делаю.")
        return 0
    books = load_json(BOOKS_PATH, [])
    if not books:
        print("books.json пуст.")
        return 1
    print(f"Отрывков в банке: {len(bank)}. Нужно добавить: {to_add}.")

    rng = random.Random()
    used = {p.get("src") for p in bank}
    pools = load_candidates(books, used)
    by_id = {b["id"]: b for b in books}
    for pool in pools.values():
        rng.shuffle(pool)
    counts = {b["id"]: sum(1 for p in bank if p.get("book_id") == b["id"]) for b in books}
    next_num = max((int(p["id"][1:]) for p in bank if str(p.get("id", "")).startswith("p")), default=0) + 1
    os.makedirs(IMAGES_DIR, exist_ok=True)

    added, failures = 0, 0
    try:
        while added < to_add and failures < 8:
            alive = [bid for bid, pool in pools.items() if pool]
            if not alive:
                print("Иллюстрации закончились во всех книгах из books.json.")
                break
            low = min(counts[b] for b in alive)
            book = by_id[rng.choice([b for b in alive if counts[b] == low])]
            cand = pools[book["id"]].pop()

            try:
                raw_img = http_get(cand["url"])
                time.sleep(1)
            except Exception as e:
                print(f"  картинка не скачалась: {e}")
                continue
            if not image_ok(raw_img):
                continue  # заставка, буквица или слишком мелкая картинка

            print(f"Пробую: {book['title']} / {cand['caption'][:60] or cand['url'][-30:]}")
            raw = parse_json(gen.gemini(selection_prompt(book, cand), 0.7))
            time.sleep(gen.REQUEST_DELAY)
            task = build_task(raw, cand, rng)
            if not task:
                failures += 1
                continue
            entry = {
                "id": f"p{next_num:04d}",
                "book_id": book["id"],
                "book": book["title"],
                "author": book["author"],
                "image": f"images/p{next_num:04d}.jpg",
                "src": cand["url"],
                **task,
            }
            if validate_passage(entry):
                failures += 1
                continue
            if not verified(task):
                failures += 1
                continue

            make_canvas(raw_img).save(os.path.join(BASE, entry["image"]), "JPEG", quality=88)
            bank.append(entry)
            save_bank(bank, PASSAGES_PATH)
            next_num += 1
            counts[book["id"]] += 1
            added += 1
            failures = 0
            print(f"  принято: {entry['id']} (всего {len(bank)})")
    except gen.SoftStop as e:
        print(f"Остановка (прогресс сохранён): {e}")
    except gen.Fatal as e:
        print(f"ОШИБКА: {e}")
        return 1

    if failures >= 8:
        print("Восемь неудачных попыток подряд, останавливаюсь, чтобы не тратить лимит.")
    print(f"Добавлено: {added}. Всего отрывков: {len(bank)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
