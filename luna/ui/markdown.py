"""Простой markdown ленты чата и перенос строк с учётом юникода.

Поддерживается ровно то, что нужно ленте: заголовки, жирный и курсив,
списки, `код` в строке, блоки кода с лёгкой подсветкой, цитаты и ссылки
(показываются текстом). Разбор — в блоки и спаны; перенос режет спаны на
визуальные строки, не ломая широкие символы (CJK, эмодзи) пополам.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .width import char_width, str_width

# Стили спанов: имена, цвета назначает экран (ui/chat.py).
PLAIN = "text"
BOLD = "bold"
ITALIC = "italic"
CODE = "code"
HEADING = "heading"
BULLET = "bullet"
DIM = "dim"
LINK = "link"
KEYWORD = "keyword"
STRING = "string"
NUMBER = "number"
COMMENT = "comment"


@dataclass
class Span:
    text: str
    style: str = PLAIN


@dataclass
class Block:
    kind: str  # heading | para | list | code | quote
    spans: list = field(default_factory=list)   # для heading/para/quote: спаны
    items: list = field(default_factory=list)   # для list: список списков спанов
    lines: list = field(default_factory=list)   # для code: строки исходника
    lang: str = ""
    level: int = 0                              # для heading


_INLINE_RE = re.compile(
    r"(\*\*.+?\*\*|__.+?__|`[^`]+`|\*[^*\n]+?\*|_[^_\n]+?_|\[[^\]]+\]\([^)]+\))",
    re.DOTALL,
)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_LIST_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
_KEYWORDS = frozenset(
    "def return if elif else for while import from as class try except finally "
    "with lambda pass break continue raise yield global nonlocal assert del in is "
    "not and or None True False print len range str int float bool list dict set "
    "function const let var new await async typeof".split()
)


def parse_inline(text: str) -> list:
    """Строка → спаны: **жирный**, *курсив*, `код`, [текст](адрес)."""
    spans: list = []
    pos = 0
    for m in _INLINE_RE.finditer(text):
        if m.start() > pos:
            spans.append(Span(text[pos:m.start()]))
        tok = m.group(0)
        if tok.startswith("**") or tok.startswith("__"):
            spans.append(Span(tok[2:-2], BOLD))
        elif tok.startswith("`"):
            spans.append(Span(tok[1:-1], CODE))
        elif tok.startswith("["):
            label = tok[1:tok.index("]")]
            spans.append(Span(label, LINK))
        else:
            spans.append(Span(tok[1:-1], ITALIC))
        pos = m.end()
    if pos < len(text):
        spans.append(Span(text[pos:]))
    return spans or [Span("")]


def parse_markdown(text: str) -> list:
    """Текст ответа → блоки ленты."""
    blocks: list = []
    lines = text.split("\n")
    i = 0
    para: list = []

    def flush_para() -> None:
        if para:
            blocks.append(Block("para", spans=parse_inline(" ".join(para))))
            para.clear()

    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()
        if stripped.startswith("```"):
            flush_para()
            lang = stripped[3:].strip()
            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            i += 1  # закрывающий ```
            blocks.append(Block("code", lines=code_lines, lang=lang))
            continue
        m = _HEADING_RE.match(raw)
        if m:
            flush_para()
            blocks.append(Block("heading", spans=parse_inline(m.group(2)),
                                level=len(m.group(1))))
            i += 1
            continue
        m = _LIST_RE.match(raw)
        if m:
            flush_para()
            items = []
            while i < len(lines):
                mm = _LIST_RE.match(lines[i])
                if not mm:
                    break
                items.append(parse_inline(mm.group(3)))
                i += 1
            blocks.append(Block("list", items=items))
            continue
        if stripped.startswith("> "):
            flush_para()
            blocks.append(Block("quote", spans=parse_inline(stripped[2:])))
            i += 1
            continue
        if not stripped:
            flush_para()
            i += 1
            continue
        para.append(stripped)
        i += 1
    flush_para()
    return blocks


# --- подсветка кода ---------------------------------------------------------

_TOKEN_RE = re.compile(
    r"(?P<comment>\#.*$|//.*$)"
    r"|(?P<string>'[^'\n]*'|\"[^\"\n]*\")"
    r"|(?P<number>\b\d+(?:\.\d+)?\b)"
    r"|(?P<word>[A-Za-z_][A-Za-z0-9_]*)"
    r"|(?P<other>.)",
    re.DOTALL,
)


def highlight_code_line(line: str) -> list:
    """Одна строка кода → спаны лёгкой подсветки (ключевые слова, строки, числа)."""
    spans: list = []
    for m in _TOKEN_RE.finditer(line):
        kind = m.lastgroup or PLAIN
        text = m.group(0)
        if kind == "word":
            style = KEYWORD if text in _KEYWORDS else PLAIN
        elif kind == "other":
            style = PLAIN
        else:
            style = kind
        if spans and spans[-1].style == style:
            spans[-1].text += text
        else:
            spans.append(Span(text, style))
    return spans or [Span("")]


# --- перенос -----------------------------------------------------------------

def _split_long_word(word: str, style: str, width: int) -> list:
    """Разрезать слишком длинное слово по границам символов, не деля широкий."""
    chunks = []
    cur = ""
    cur_w = 0
    for ch in word:
        w = char_width(ch)
        if cur and cur_w + w > width:
            chunks.append(Span(cur, style))
            cur, cur_w = "", 0
        cur += ch
        cur_w += w
    if cur:
        chunks.append(Span(cur, style))
    return chunks


def wrap_spans(spans: list, width: int) -> list:
    """Спаны → визуальные строки спанов шириной не больше width ячеек.

    Слова не рвутся, пока влезают; слово длиннее строки режется по символам.
    Широкий символ в строке шириной 1 встанет один и честно займёт свои 2.
    """
    width = max(1, width)
    words: list = []  # (текст, стиль)
    for span in spans:
        parts = re.split(r"(\s+)", span.text)
        for part in parts:
            if part:
                words.append((part, span.style))
    lines: list = []
    cur: list = []
    cur_w = 0

    def flush() -> None:
        nonlocal cur, cur_w
        while cur and cur[-1][0].strip() == "":
            cur.pop()
        lines.append([Span(t, s) for t, s in cur if t])
        cur, cur_w = [], 0

    for text, style in words:
        w = str_width(text)
        if text.strip() == "":
            if cur and cur_w + w <= width:
                cur.append((text, style))
                cur_w += w
            continue
        if w > width:
            if cur:
                flush()
            chunks = _split_long_word(text, style, width)
            for chunk in chunks[:-1]:
                lines.append([chunk])
            if chunks:
                cur = [(chunks[-1].text, style)]
                cur_w = str_width(chunks[-1].text)
            continue
        if cur and cur_w + w > width:
            flush()
        cur.append((text, style))
        cur_w += w
    if cur or not lines:
        flush()
    return lines


def wrap_text(text: str, width: int) -> list:
    """Чистый текст → строки не шире width ячеек (по словам, с учётом юникода)."""
    out: list = []
    for logical in text.split("\n"):
        for line in wrap_spans([Span(logical)], width):
            out.append("".join(s.text for s in line))
    return out or [""]


def render_markdown(text: str, width: int) -> list:
    """Текст → визуальные строки спанов для ленты шириной width."""
    out: list = []
    for block in parse_markdown(text):
        if block.kind == "heading":
            for line in wrap_spans(block.spans, width):
                out.append([Span("─ ", DIM)] + [Span(s.text, HEADING) for s in line])
        elif block.kind == "quote":
            for line in wrap_spans(block.spans, max(1, width - 2)):
                out.append([Span("│ ", DIM)] + line)
        elif block.kind == "list":
            for item in block.items:
                wrapped = wrap_spans(item, max(1, width - 2))
                for idx, line in enumerate(wrapped):
                    marker = "• " if idx == 0 else "  "
                    out.append([Span(marker, BULLET)] + line)
        elif block.kind == "code":
            for raw in block.lines:
                for line in wrap_spans(highlight_code_line(raw), width):
                    out.append(line)
        else:  # para
            out.extend(wrap_spans(block.spans, width))
    return out or [[Span("")]]
