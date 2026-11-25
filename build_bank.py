#!/usr/bin/env python3
"""Собирает банк постов «цитата + вопрос на смысл».

Два типа записей:
  excerpt  — отрывок из книги или стихотворения в общественном достоянии (Standard Ebooks на GitHub).
             Отрывок обязан дословно совпасть с текстом книги: выдуманных цитат не бывает.
  original — новый оригинальный текст «в духе» рока, рэпа, попа и т. п. (не цитата и не пересказ чужих песен).

Как проверяется качество (главное требование: никакой двусмысленности):
  1. Gemini выбирает отрывок (или пишет оригинальный текст) и составляет вопрос на смысл.
  2. Несколько независимых проверок: модель решает вопрос, не видя ответа, при перемешанных вариантах.
     Берутся только задания, где все проверки дали верный ответ, не нашли двусмысленности
     и подтвердили, что язык не выше B2.
Прогресс сохраняется после каждого задания, лимиты бесплатного тарифа учитываются.
"""
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
from common import (BANK_PATH, BASE, load_bank, load_json, save_bank, validate_item)

TARGET = int(os.environ.get("BANK_TARGET") or 365)
PER_RUN = int(os.environ.get("BANK_PER_RUN") or 20)
COUNT = (os.environ.get("BANK_COUNT") or "").strip()
BOOKS_PER_RUN = int(os.environ.get("BOOKS_PER_RUN") or 3)
FILES_PER_BOOK = int(os.environ.get("FILES_PER_BOOK") or 15)   # сколько глав брать из книги за запуск
VERIFY_PASSES = max(1, int(os.environ.get("VERIFY_PASSES") or 2))
EXPLANATION_LANG = os.environ.get("EXPLANATION_LANG") or "Russian"
MAX_FAILURES = 10
SOURCES_PATH = os.path.join(BASE, "sources.json")
UA = "english-post-lit/2.0 (personal educational channel; low volume)"
RAW = "https://raw.githubusercontent.com/standardebooks/{repo}/master/{path}"
SKIP_FILES = re.compile(r"colophon|imprint|uncopyright|titlepage|halftitle|endnotes|loi|dedication|epigraph|"
                        r"copyright|toc|preface|introduction|foreword|afterword|glossary")

CHARMAP = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'", "—": "-", "–": "-", "\u00a0": " "})
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
    """Собирает абзацы. Переносы строк внутри абзаца (стихи) сохраняются как \\n."""
    BLOCK = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "li", "tr", "table", "pre",
             "section", "figure", "figcaption", "header", "hgroup", "article", "aside", "ul", "ol", "dd", "dt"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.paras = []
        self.buf = []
        self.skip = 0
        self.skip_until = None   # номера страниц и сноски внутри текста пропускаем

    def flush(self):
        text = "".join(self.buf).translate(INVISIBLE).translate(THIN_SPACES)
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r" ?\n ?", "\n", text)
        text = re.sub(r"\n{2,}", "\n", text).strip()
        self.buf = []
        if text:
            self.paras.append(text)

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
        elif tag == "br":
            self.buf.append("\n")
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
            self.buf.append(re.sub(r"\s+", " ", data))   # переводы строк из исходника HTML не считаем разрывами

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
        if href.startswith("text/") and href.endswith(".xhtml") and not SKIP_FILES.search(href):
            out.append(href)
    return out


def load_book(book, rng):
    """Скачивает несколько случайных глав книги. Возвращает список глав (каждая список абзацев)."""
    repo = book["id"]
    opf = decode_html(http_get(RAW.format(repo=repo, path="src/epub/content.opf")))
    files = spine_files(opf)
    rng.shuffle(files)
    chapters = []
    for href in files[:FILES_PER_BOOK]:
        try:
            text = decode_html(http_get(RAW.format(repo=repo, path=f"src/epub/{href}")))
        except urllib.error.HTTPError:
            continue
        p = BookParser()
        p.feed(text)
        p.close()
        if sum(len(x) for x in p.paras) >= 1500:
            chapters.append(p.paras)
        time.sleep(0.2)
    print(f"  «{book['title']}»: загружено глав: {len(chapters)}")
    return chapters


def pick_window(chapters, rng, form):
    """Случайный кусок текста из главы: из него модель выберет отрывок."""
    limit, minimum = (2600, 700) if form == "prose" else (1800, 350)
    for _ in range(20):
        ch = rng.choice(chapters)
        start = rng.randrange(len(ch))
        if len(ch[start]) < 40:
            continue
        paras, total, i = [], 0, start
        while i < len(ch) and total < limit:
            paras.append(ch[i])
            total += len(ch[i]) + 1
            i += 1
        if total >= minimum:
            return "\n".join(paras)
    return None


# ---------- Запросы к Gemini ----------

def size_rule(form):
    if form == "verse":
        return "4 to 8 consecutive lines (150-450 characters), keeping the line breaks"
    return "3 to 5 consecutive sentences (250-600 characters)"


QUESTION_RULES = """Then write ONE question about the MEANING of the text (not grammar, not vocabulary trivia). Pick one type: what is happening; what does the character want or intend; why does someone say or do something; what does an expression mean in this context; what is the mood or tone; what will probably happen next.
Write exactly {n} answer options:
- exactly ONE option is correct, and it must follow clearly from the text alone (no outside knowledge);
- every wrong option must look plausible at first glance but be clearly contradicted by, or not supported by, the text;
- options are short (at most 60 characters), in simple B1 English, similar in length and style, none is partly correct, no "all/none of the above";
- the question itself is in simple English too, at most 120 characters, and it is fun, not boring or pedantic.
"explanation": written in {lang}, at most 150 characters, plain text: quote the key phrase of the text that proves the correct answer."""


def excerpt_prompt(book, window, n_opts):
    return f"""You are an experienced English teacher writing a reading-comprehension post for a Telegram channel for learners at level B1-B2. Posts must be fun, clear and absolutely fair.
Source: "{book['title']}" by {book['author']} ({book['genre']}).

TEXT (consecutive passage from the book):
{window}

First choose ONE excerpt copied EXACTLY, character for character, from TEXT above: {size_rule(book['form'])}. It must make sense on its own without knowing the book, show a clear situation, motive or joke, and be easy enough for a B2 learner (common words, no heavy archaic or dialect language, no names that need explanation). Prefer vivid, funny or surprising moments. If TEXT contains no good excerpt, return {{"skip": true}}.
{QUESTION_RULES.format(n=n_opts, lang=EXPLANATION_LANG)}

Return ONLY JSON: {{"excerpt": "...", "question": "...", "options": ["..."], "correct": 0, "explanation": "..."}}
where "correct" is the 0-based index of the correct option."""


def original_prompt(orig, n_opts):
    shape = ("4 to 8 short lines with a clear rhythm (150-420 characters), one line per row" if orig["form"] == "verse"
             else "3 to 5 sentences (250-500 characters)")
    return f"""You are a witty writer and an English teacher. Write a NEW, completely original short text for a Telegram channel for learners at level B1-B2: {orig['brief']}.
Rules: the text must be 100% original: do not quote, paraphrase or imitate any existing song, poem, film or book, and do not mention real artists, bands, brands or characters. Use simple B1-B2 English (no rare words), and make it funny or vivid. Length: {shape}. It must make sense on its own and contain a clear situation, motive or joke.
{QUESTION_RULES.format(n=n_opts, lang=EXPLANATION_LANG)}

Return ONLY JSON: {{"text": "...", "question": "...", "options": ["..."], "correct": 0, "explanation": "..."}}
where "correct" is the 0-based index of the correct option."""


def verification_prompt(item, order):
    opts = "\n".join(f"{'ABCDE'[j]}) {item['options'][i]}" for j, i in enumerate(order))
    return f"""You are a strict exam checker and an attentive B2-level reader. Answer the question using ONLY the text.

TEXT:
{item['text']}

QUESTION: {item['question']}
{opts}

Return ONLY JSON like {{"answer": "B", "ambiguous": false, "level_ok": true}}.
- "ambiguous": true if a careful reader could defend more than one option from the text alone, or if no option fits well.
- "level_ok": false if the text or the options need vocabulary or grammar clearly above B2, or need outside knowledge of the story."""


def verified(item, rng):
    """Несколько независимых проверок с перемешанными вариантами. True, только если все прошли."""
    for k in range(VERIFY_PASSES):
        order = list(range(len(item["options"])))
        rng.shuffle(order)
        res = llm.parse_json(llm.gemini(verification_prompt(item, order), 0))
        time.sleep(llm.REQUEST_DELAY)
        if not isinstance(res, dict) or res.get("ambiguous") or res.get("level_ok") is False:
            return False
        try:
            picked = order["ABCDE".index(str(res.get("answer", "")).strip().upper()[:1])]
        except (ValueError, IndexError):
            return False
        if picked != item["correct"]:
            return False
    return True


# ---------- Сборка записи ----------

def collapse(s):
    return re.sub(r"\s+", " ", s).strip()


def norm(s):
    return re.sub(r"[^a-z0-9а-яё]+", " ", s.lower()).strip()


def clean_question(raw, n_opts, rng):
    """Проверяет вопрос и варианты ответа, перемешивает варианты. Возвращает dict или None."""
    if not isinstance(raw, dict):
        return None
    opts = raw.get("options")
    if not isinstance(opts, list) or len(opts) != n_opts:
        return None
    opts = [str(o).strip() for o in opts]
    try:
        correct = int(raw.get("correct"))
    except (TypeError, ValueError):
        return None
    question = str(raw.get("question", "")).strip()
    explanation = str(raw.get("explanation", "")).strip()
    if not 0 <= correct < n_opts or not question or not explanation:
        return None
    if len(question) > 160 or any(not o or len(o) > 80 for o in opts) or len({o.lower() for o in opts}) != n_opts:
        return None
    right = opts[correct]
    rng.shuffle(opts)           # модели любят ставить верный ответ на B/C: перемешиваем сами
    return {"question": question, "options": opts, "correct": opts.index(right), "explanation": explanation}


def build_excerpt(raw, window, book, n_opts, rng):
    if not isinstance(raw, dict) or raw.get("skip"):
        return None
    q = clean_question(raw, n_opts, rng)
    if not q:
        return None
    ex = collapse(str(raw.get("excerpt", "")))
    low, high = (200, 650) if book["form"] == "prose" else (100, 520)
    if not low <= len(ex) <= high:
        return None
    # Отрывок должен дословно присутствовать в тексте (с точностью до кавычек и тире)
    flat = re.sub(r"\s", " ", window).translate(CHARMAP)
    idx = flat.find(ex.translate(CHARMAP))
    if idx < 0:
        return None
    text = window[idx: idx + len(ex)]
    if book["form"] == "verse" and text.count("\n") < 2:
        return None
    return {"type": "excerpt", "title": book["title"], "author": book["author"], "genre": book["genre"],
            "source_id": book["id"], "text": text, **q}


def build_original(raw, orig, n_opts, rng):
    if not isinstance(raw, dict):
        return None
    q = clean_question(raw, n_opts, rng)
    text = str(raw.get("text", "")).replace("\r", "").strip()
    text = re.sub(r"[ \t]+\n", "\n", text)
    low, high = (150, 450) if orig["form"] == "verse" else (200, 600)
    if not q or not low <= len(text) <= high:
        return None
    if orig["form"] == "verse" and text.count("\n") < 2:
        return None
    return {"type": "original", "genre": orig["genre"], "source_id": f"original:{orig['genre']}",
            "text": text, **q}


# ---------- Основной цикл ----------

class Builder:
    def __init__(self, bank, rng):
        self.bank, self.rng = bank, rng
        self.next_num = max((int(p["id"][1:]) for p in bank if str(p.get("id", "")).startswith("q")),
                            default=0) + 1
        self.added = 0
        self.failures = 0
        self.seen = {norm(p["text"])[:80] for p in bank}

    def count(self, source_id):
        return sum(1 for p in self.bank if p.get("source_id") == source_id)

    def accept(self, item):
        """Проверки: дубликат, формат, независимые проверки модели. True, если запись добавлена."""
        key = norm(item["text"])[:80]
        if key in self.seen or any(key in norm(p["text"]) or norm(p["text"])[:80] in norm(item["text"])
                                   for p in self.bank if p.get("source_id") == item["source_id"]):
            return self.fail("повтор отрывка")
        item = {"id": f"q{self.next_num:04d}", **item}
        err = validate_item(item)
        if err:
            return self.fail(err)
        if not verified(item, self.rng):
            return self.fail("не прошло независимую проверку")
        self.bank.append(item)
        save_bank(self.bank, BANK_PATH)
        self.seen.add(key)
        self.next_num += 1
        self.added += 1
        self.failures = 0
        print(f"  принято: {item['id']} ({item['type']}, {item['options'] and len(item['options'])} вар.), всего {len(self.bank)}")
        return True

    def fail(self, why):
        self.failures += 1
        print(f"  отклонено: {why}")
        return False

    def n_options(self):
        return self.rng.choice([3, 4, 4, 4, 5])

    def attempt_excerpt(self, book, window):
        n = self.n_options()
        print(f"Пробую цитату: «{book['title']}»")
        raw = llm.parse_json(llm.gemini(excerpt_prompt(book, window, n), 0.8))
        time.sleep(llm.REQUEST_DELAY)
        item = build_excerpt(raw, window, book, n, self.rng)
        return self.accept(item) if item else self.fail("модель отказалась или формат не подошёл")

    def attempt_original(self, orig):
        n = self.n_options()
        print(f"Пробую оригинальный текст: {orig['genre']}")
        raw = llm.parse_json(llm.gemini(original_prompt(orig, n), 1.0))
        time.sleep(llm.REQUEST_DELAY)
        item = build_original(raw, orig, n, self.rng)
        return self.accept(item) if item else self.fail("формат не подошёл")


def main():
    if not llm.API_KEY:
        print("Не задан GEMINI_API_KEY (секрет репозитория).")
        return 1
    bank = load_bank(BANK_PATH)
    to_add = int(COUNT) if COUNT else min(PER_RUN, TARGET - len(bank))
    if to_add <= 0:
        print(f"В банке {len(bank)} постов, цель {TARGET} достигнута. Ничего не делаю.")
        return 0
    cfg = load_json(SOURCES_PATH, {})
    books, originals = cfg.get("books", []), cfg.get("originals", [])
    share = float(cfg.get("original_share", 0.25))
    if not books and not originals:
        print("sources.json пуст.")
        return 1
    print(f"Постов в банке: {len(bank)}. Нужно добавить: {to_add}.")

    rng = random.Random()
    b = Builder(bank, rng)

    # За запуск берём несколько книг, в которых постов меньше всего (остальные подтянутся в другие дни)
    chapters, by_id, broken = {}, {}, 0
    for book in sorted(books, key=lambda x: (b.count(x["id"]), rng.random())):
        if len(chapters) >= BOOKS_PER_RUN:
            break
        try:
            ch = load_book(book, rng)
        except Exception as e:
            broken += 1
            print(f"  «{book['title']}»: не скачалась ({e}), пропускаю.")
            continue
        if ch:
            chapters[book["id"]], by_id[book["id"]] = ch, book
    if books and not chapters and not originals:
        print("Ни одна книга не скачалась. Проверьте имена репозиториев в sources.json и доступ к GitHub.")
        return 1

    try:
        while b.added < to_add and b.failures < MAX_FAILURES:
            use_original = originals and (not chapters or rng.random() < share)
            if use_original:
                low = min(b.count(f"original:{o['genre']}") for o in originals)
                orig = rng.choice([o for o in originals if b.count(f"original:{o['genre']}") == low])
                b.attempt_original(orig)
            else:
                low = min(b.count(x) for x in chapters)
                book = by_id[rng.choice([x for x in chapters if b.count(x) == low])]
                window = pick_window(chapters[book["id"]], rng, book["form"])
                if window is None:
                    b.fail("не нашлось подходящего куска текста")
                    continue
                b.attempt_excerpt(book, window)
    except llm.SoftStop as e:
        print(f"Остановка (прогресс сохранён): {e}")
        print("Это временная перегрузка или лимит Gemini. Запустите пополнение снова через 15-30 минут "
              "(или завтра, если исчерпан суточный лимит).")
    except llm.Fatal as e:
        print(f"ОШИБКА: {e}")
        return 1

    if b.failures >= MAX_FAILURES:
        print(f"{MAX_FAILURES} отклонённых попыток подряд, останавливаюсь, чтобы не тратить лимит.")
    kinds = {}
    for p in bank:
        kinds[p["type"]] = kinds.get(p["type"], 0) + 1
    print(f"Добавлено: {b.added}. Всего постов: {len(bank)} ({kinds}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
