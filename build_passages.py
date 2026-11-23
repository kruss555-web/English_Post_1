#!/usr/bin/env python3
"""Собирает банк заданий «отрывок из классики + иллюстрация из той же книги + пропуски».

Источник книг: Standard Ebooks (github.com/standardebooks): выверенные издания в общественном достоянии,
которые лежат на GitHub, поэтому GitHub Actions получает их без блокировок. Проект Gutenberg не отдаёт
файлы облачным серверам (503), поэтому он здесь не используется.

Как это работает:
  1. Из books.json берутся книги: скачивается оглавление и главы (только нужные файлы, без архивов).
     В этих изданиях у каждой иллюстрации есть описание того, что на ней нарисовано.
  2. Для иллюстрации берётся окружающий текст, а Gemini выбирает отрывок из 3-4 предложений.
     Отрывок обязан дословно совпасть с текстом книги, поэтому выдуманных цитат не бывает.
  3. Gemini выбирает 2-3 слова для пропуска и придумывает неверные варианты.
  4. Вторым запросом модель решает задание без подсказок: берутся только однозначные.
  5. Иллюстрация приводится к холсту 16:9 без размытого фона:
       crop:  пропорции близки к 16:9: лёгкая подрезка по краям, без полей;
       mat:   гравюра или рисунок на светлой «бумаге»: целиком на ровном фоне того же цвета, шва не видно;
       frame: цветная картинка во весь лист: целиком, в тёмной рамке тона самой картинки.
Прогресс сохраняется после каждого задания, лимиты бесплатного тарифа учитываются.
"""
import io
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser
from xml.etree import ElementTree

import llm
from common import (BASE, CANVAS_H, CANVAS_W, CROP_MAX_LOSS, PASSAGES_PATH,
                    load_bank, load_json, save_bank, validate_passage)

TARGET = int(os.environ.get("PASSAGES_TARGET") or 365)
PER_RUN = int(os.environ.get("PASSAGES_PER_RUN") or 20)
COUNT = (os.environ.get("PASSAGES_COUNT") or "").strip()
BOOKS_PER_RUN = int(os.environ.get("BOOKS_PER_RUN") or 3)   # сколько книг загружать за один запуск
MAX_FAILURES = 8
BOOKS_PATH = os.path.join(BASE, "books.json")
IMAGES_DIR = os.path.join(BASE, "images")
UA = "english-post-lit/1.0 (personal educational channel; low volume)"
RAW = "https://raw.githubusercontent.com/standardebooks/{repo}/master/{path}"

# Замены символ-в-символ: позволяют сравнивать «умные» и прямые кавычки без сдвига позиций
CHARMAP = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'", "—": "-", "–": "-", "\u00a0": " "})
# Невидимые символы, которыми Standard Ebooks размечает типографику
INVISIBLE = dict.fromkeys(map(ord, "\u2060\u200b\u00ad\ufeff"), None)
THIN_SPACES = dict.fromkeys(map(ord, "\u2009\u200a\u202f\u00a0"), " ")


# ---------- Сеть ----------

def http_get(url, tries=3):
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404 or attempt == tries - 1:
                raise
            time.sleep(3 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError):
            if attempt == tries - 1:
                raise
            time.sleep(3 * (attempt + 1))


# ---------- Разбор текста книги (XHTML Standard Ebooks) ----------

class BookParser(HTMLParser):
    BLOCK = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "li", "tr", "table", "pre",
             "section", "figure", "figcaption", "header", "hgroup", "article", "aside", "ul", "ol", "dd", "dt"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tokens = []
        self.buf = []
        self.skip = 0
        self.skip_until = None   # номера страниц и сноски внутри текста пропускаем

    def flush(self):
        text = "".join(self.buf).translate(INVISIBLE).translate(THIN_SPACES)
        text = re.sub(r"\s+", " ", text).strip()
        self.buf = []
        if text:
            self.tokens.append({"t": "p", "text": text})

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("head", "script", "style"):
            self.skip += 1
            return
        if self.skip or self.skip_until:
            return
        etype = (a.get("epub:type") or "") + " " + (a.get("class") or "")
        if (tag == "span" and "pagenum" in etype) or (tag == "a" and "noteref" in etype):
            self.skip_until = tag
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
        if self.skip_until == tag:
            self.skip_until = None
        elif tag in self.BLOCK:
            self.flush()

    def handle_data(self, data):
        if not self.skip and not self.skip_until:
            self.buf.append(data)

    def close(self):
        super().close()
        self.flush()


def decode_html(raw):
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def spine_files(opf_text):
    """Список файлов глав в порядке чтения (из content.opf)."""
    root = ElementTree.fromstring(opf_text)
    ns = {"o": "http://www.idpf.org/2007/opf"}
    hrefs = {it.get("id"): it.get("href") for it in root.findall(".//o:manifest/o:item", ns)}
    out = []
    for ref in root.findall(".//o:spine/o:itemref", ns):
        href = hrefs.get(ref.get("idref"), "")
        if href.startswith("text/") and href.endswith(".xhtml"):
            out.append(href)
    return out


def image_urls(repo, src):
    """Где искать файл картинки: готовый растр в epub либо исходник в images/ в корне репозитория."""
    name = src.rsplit("/", 1)[-1]
    stem, _, ext = name.rpartition(".")
    urls = []
    if ext.lower() in ("png", "jpg", "jpeg"):
        urls.append(RAW.format(repo=repo, path=f"src/epub/images/{name}"))
    for e in ("png", "jpg", "jpeg"):
        urls.append(RAW.format(repo=repo, path=f"images/{stem}.{e}"))
    return list(dict.fromkeys(urls))


ILLUSTRATION_STEMS = ("illustration", "frontispiece", "plate")


def extract_candidates(tokens, repo):
    """Для каждой иллюстрации собирает описание и текст до и после неё."""
    out = []
    for i, t in enumerate(tokens):
        if t["t"] != "img":
            continue
        stem = t["src"].rsplit("/", 1)[-1].rpartition(".")[0]
        if not stem.startswith(ILLUSTRATION_STEMS):
            continue  # обложка, титул, декоративные значки
        before, j = [], i - 1
        while j >= 0 and tokens[j]["t"] == "p" and len(before) < 3:
            before.insert(0, tokens[j]["text"])
            j -= 1
        after, k = [], i + 1
        while k < len(tokens) and tokens[k]["t"] == "p" and len(after) < 8:
            after.append(tokens[k]["text"])
            k += 1
        if sum(map(len, before)) + sum(map(len, after)) < 250:
            continue
        out.append({
            "key": f"{repo}/{stem}",
            "urls": image_urls(repo, t["src"]),
            "alt": t["alt"],
            "before": " ".join(before),
            "after": " ".join(after),
        })
    return out


# ---------- Картинки ----------

def to_rgb(raw):
    from PIL import Image
    im = Image.open(io.BytesIO(raw))
    im.load()
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        white = Image.new("RGBA", im.size, (255, 255, 255, 255))
        im = Image.alpha_composite(white, im)
    im = im.convert("RGB")
    if max(im.size) > 2400:  # исходники бывают огромными (5000+ пикселей): уменьшаем для скорости
        im.thumbnail((2400, 2400), Image.LANCZOS)
    return im


def image_ok(raw):
    """Отсеивает декоративные заставки, буквицы и тонкие линии."""
    from PIL import Image
    try:
        w, h = Image.open(io.BytesIO(raw)).size
    except Exception:
        return False
    return min(w, h) >= 220 and w * h >= 90000 and 0.35 <= w / h <= 3.0


def border_info(im):
    """Цвет по краям картинки (медиана) и доля краевых пикселей, близких к нему."""
    small = im.copy()
    small.thumbnail((200, 200))
    w, h = small.size
    px = small.load()
    ring = ([px[x, 0] for x in range(w)] + [px[x, h - 1] for x in range(w)]
            + [px[0, y] for y in range(h)] + [px[w - 1, y] for y in range(h)])
    med = tuple(sorted(p[c] for p in ring)[len(ring) // 2] for c in range(3))
    near = sum(1 for p in ring if max(abs(p[c] - med[c]) for c in range(3)) <= 30)
    return med, near / len(ring)


def crop_box(w, h):
    """Область центральной подрезки до пропорций холста."""
    target = CANVAS_W / CANVAS_H
    if w / h >= target:
        cw, ch = min(w, int(h * target)), h
    else:
        cw, ch = w, min(h, int(w / target))
    left, top = (w - cw) // 2, (h - ch) // 2
    return (left, top, left + cw, top + ch)


def classify(im):
    """crop / mat / frame: чем левее в списке, тем меньше полей у картинки в итоге."""
    w, h = im.size
    target = CANVAS_W / CANVAS_H
    keep = min((w / h) / target, target / (w / h))   # какая доля картинки останется при подрезке
    left, top, right, bottom = crop_box(w, h)
    upscale = CANVAS_W / max(1, right - left)
    if keep >= 1 - CROP_MAX_LOSS and upscale <= 2.5:
        return "crop"
    med, share = border_info(im)
    lum = 0.299 * med[0] + 0.587 * med[1] + 0.114 * med[2]
    if share >= 0.8 or (lum >= 225 and share >= 0.55):
        return "mat"     # однородная светлая «бумага» по краям
    return "frame"


def render(im, klass):
    """Приводит иллюстрацию к холсту CANVAS_W x CANVAS_H выбранным способом."""
    from PIL import Image
    w, h = im.size
    if klass == "crop":
        return im.crop(crop_box(w, h)).resize((CANVAS_W, CANVAS_H), Image.LANCZOS)
    if klass == "mat":
        color, margin = border_info(im)[0], 0          # фон тоже цвета «бумаги»: границы не видно
    else:
        avg = im.resize((1, 1), Image.BOX).getpixel((0, 0))
        color, margin = tuple(int(c * 0.3) for c in avg), 24   # тёмная рамка тона картинки
    fscale = min((CANVAS_W - 2 * margin) / w, (CANVAS_H - 2 * margin) / h, 2.5)
    fg = im.resize((max(1, int(w * fscale)), max(1, int(h * fscale))), Image.LANCZOS)
    canvas = Image.new("RGB", (CANVAS_W, CANVAS_H), color)
    canvas.paste(fg, ((CANVAS_W - fg.width) // 2, (CANVAS_H - fg.height) // 2))
    return canvas


# ---------- Gemini ----------

def collapse(s):
    return re.sub(r"\s+", " ", s).strip()


def selection_prompt(book, cand):
    return f"""You are preparing a fill-in-the-gaps English exercise from a public-domain book.
Book: "{book['title']}" by {book['author']}.
The book has an illustration. Description of what it shows: "{cand['alt'] or '(no description)'}".

TEXT BEFORE THE ILLUSTRATION:
{cand['before'] or '(none)'}

TEXT AFTER THE ILLUSTRATION:
{cand['after'] or '(none)'}

Task:
1. Choose ONE excerpt of 3-4 consecutive sentences (200-480 characters) copied EXACTLY, character for character, from ONE of the two text sections above. Pick the passage that best matches what the illustration most likely shows (a concrete scene, not just dialogue tags). If nothing suitable exists, return {{"skip": true}}.
2. In the excerpt choose 2-3 words or short phrases to blank out. Each must occur exactly once in the excerpt as a whole word, must test vocabulary, word choice, prepositions or grammar at an upper-intermediate or advanced level, and must NOT be a name or a trivial word like "a", "the", "and".
3. For each blanked item give exactly 2 WRONG alternatives: same part of speech or a typical learner confusion, plausible but clearly wrong in this context. Never use misspelled or non-existent words as alternatives. Exactly one option must fit.
4. "explanation": in {os.environ.get('EXPLANATION_LANG') or 'Russian'}, at most 150 characters, plain text, briefly says why each correct answer fits (e.g. "1 — be tired of: устать от; 2 — ...").

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
    res = llm.parse_json(llm.gemini(verification_prompt(task["text"], task["gaps"]), 0))
    time.sleep(llm.REQUEST_DELAY)
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

def load_book(book, used):
    """Скачивает оглавление и главы одной книги. Возвращает список кандидатов (без уже использованных)."""
    repo = book["id"]
    opf = decode_html(http_get(RAW.format(repo=repo, path="src/epub/content.opf")))
    tokens = []
    for href in spine_files(opf):
        try:
            text = decode_html(http_get(RAW.format(repo=repo, path=f"src/epub/{href}")))
        except urllib.error.HTTPError:
            continue
        parser = BookParser()
        parser.feed(text)
        parser.close()
        tokens += parser.tokens + [{"t": "sep"}]   # граница глав: окно текста её не пересекает
        time.sleep(0.2)
    n_img = sum(1 for t in tokens if t["t"] == "img")
    cands = [c for c in extract_candidates(tokens, repo) if c["key"] not in used]
    print(f"  «{book['title']}»: картинок в тексте: {n_img}, годных (с описанием и текстом рядом): {len(cands)}")
    return cands


def fetch_image(cand):
    for url in cand["urls"]:
        try:
            return http_get(url)
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
    return None


class Builder:
    def __init__(self, bank, books, rng):
        self.bank, self.rng = bank, rng
        self.counts = {b["id"]: sum(1 for p in bank if p.get("book_id") == b["id"]) for b in books}
        self.next_num = max((int(p["id"][1:]) for p in bank if str(p.get("id", "")).startswith("p")),
                            default=0) + 1
        self.added = 0
        self.failures = 0

    def attempt(self, book, cand, im, klass):
        """Одна попытка сделать задание из иллюстрации. True, если задание добавлено."""
        print(f"Пробую [{klass}]: {book['title']} / {cand['alt'][:70] or cand['key']}")
        raw = llm.parse_json(llm.gemini(selection_prompt(book, cand), 0.7))
        time.sleep(llm.REQUEST_DELAY)
        task = build_task(raw, cand, self.rng)
        entry = None
        if task:
            num = self.next_num
            entry = {"id": f"p{num:04d}", "book_id": book["id"], "book": book["title"],
                     "author": book["author"], "image": f"images/p{num:04d}.jpg",
                     "fit": klass, "src": cand["key"], **task}
        if not entry or validate_passage(entry) or not verified(task):
            self.failures += 1
            return False

        os.makedirs(IMAGES_DIR, exist_ok=True)
        render(im, klass).save(os.path.join(BASE, entry["image"]), "JPEG", quality=88)
        self.bank.append(entry)
        save_bank(self.bank, PASSAGES_PATH)
        self.next_num += 1
        self.counts[book["id"]] += 1
        self.added += 1
        self.failures = 0
        print(f"  принято: {entry['id']} (всего {len(self.bank)})")
        return True


def main():
    if not llm.API_KEY:
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
    b = Builder(bank, books, rng)

    # За запуск берём несколько книг, в которых отрывков меньше всего (остальные подтянутся в другие дни)
    order = sorted(books, key=lambda x: (b.counts[x["id"]], rng.random()))
    pools, by_id, tried, broken = {}, {}, 0, 0
    for book in order:
        if len(pools) >= BOOKS_PER_RUN:
            break
        tried += 1
        try:
            cands = load_book(book, used)
        except Exception as e:
            broken += 1
            print(f"  «{book['title']}»: книга не скачалась ({e}), пропускаю.")
            continue
        if cands:
            rng.shuffle(cands)
            pools[book["id"]], by_id[book["id"]] = cands, book
    if not pools:
        if broken == tried:
            print("Ни одна книга не скачалась. Проверьте имена репозиториев в books.json и доступ к GitHub.")
            return 1
        print("Новых иллюстраций не осталось: в книгах из books.json все картинки уже использованы. "
              "Добавьте книги в books.json.")
        return 0

    try:
        while b.added < to_add and b.failures < MAX_FAILURES:
            alive = [bid for bid, pool in pools.items() if pool]
            if not alive:
                break
            low = min(b.counts[x] for x in alive)
            book = by_id[rng.choice([x for x in alive if b.counts[x] == low])]
            cand = pools[book["id"]].pop()
            try:
                raw_img = fetch_image(cand)
                time.sleep(0.5)
                if raw_img is None or not image_ok(raw_img):
                    continue  # нет файла, заставка или слишком мелкая картинка
                im = to_rgb(raw_img)
            except Exception as e:
                print(f"  картинка не обработалась: {e}")
                continue
            b.attempt(book, cand, im, classify(im))
    except llm.SoftStop as e:
        print(f"Остановка (прогресс сохранён): {e}")
    except llm.Fatal as e:
        print(f"ОШИБКА: {e}")
        return 1

    if b.failures >= MAX_FAILURES:
        print(f"{MAX_FAILURES} неудачных попыток подряд, останавливаюсь, чтобы не тратить лимит.")
    fits = {}
    for p in bank:
        fits[p.get("fit", "?")] = fits.get(p.get("fit", "?"), 0) + 1
    print(f"Добавлено: {b.added}. Всего отрывков: {len(bank)}. По способу подгонки картинки: {fits}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
