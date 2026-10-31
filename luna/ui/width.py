"""Ширина символов в ячейках терминала.

CJK и эмодзи занимают две ячейки, комбинирующиеся знаки и управляющие — ноль.
База — unicodedata.east_asian_width, сверху — диапазоны эмодзи, которых в
east_asian_width не хватает (луны, ракеты и прочая живность из строки состояния).
"""

from __future__ import annotations

import unicodedata

_EMOJI_RANGES = (
    (0x1F000, 0x1FAFF),  # маджонг, смайлы, транспорт, символы и пиктограммы
    (0x2600, 0x26FF),    # ☀ ★ ☾ и разное
    (0x2700, 0x27BF),    # дингбаты
    (0x2B00, 0x2BFF),    # звёзды и стрелки-символы
    (0x1F1E6, 0x1F1FF),  # флаги (региональные индикаторы)
)

_ZERO_CATEGORIES = frozenset({"Mn", "Me", "Cf", "Cc"})


def char_width(ch: str) -> int:
    """Ширина одного символа: 0, 1 или 2 ячейки."""
    if not ch:
        return 0
    code = ord(ch)
    if unicodedata.combining(ch) or unicodedata.category(ch) in _ZERO_CATEGORIES:
        return 0
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    for lo, hi in _EMOJI_RANGES:
        if lo <= code <= hi:
            return 2
    return 1


def str_width(text: str) -> int:
    """Ширина строки в ячейках (без управляющих последовательностей)."""
    return sum(char_width(ch) for ch in text)


def fits(text: str, width: int) -> str:
    """Обрезать строку до ширины, не разрезая широкий символ пополам."""
    out = []
    used = 0
    for ch in text:
        w = char_width(ch)
        if used + w > width:
            break
        out.append(ch)
        used += w
    return "".join(out)
