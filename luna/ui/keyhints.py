"""Кликабельные подсказки клавиш: «Esc — закрыть» нажимается мышью и пальцем.

Подсказки по всей Луне пишутся одним видом: «Клавиша — действие» через « · ».
Здесь такая строка разбирается на кусочки с колонками и клавишей, а клик по
кусочку подаёт ту же клавишу, что и с клавиатуры. Без клавиатуры (нетбук,
сенсор) так доступно всё, что обещают подсказки, — и новые подсказки
становятся кликабельными сами, если написаны тем же видом.
"""

from __future__ import annotations

from .keys import Key
from .width import str_width

_SEP = " — "
# Что обрывает кусочек слева и справа: соседние подсказки, скобки, запятые.
_LEFT_STOPS = (" · ", "(", ", ", ": ")
_RIGHT_STOPS = (" · ", ")", "; ")

_NAMED = {
    "enter": "enter", "esc": "esc", "tab": "tab", "backspace": "backspace",
    "space": "space", "пробел": "space", "delete": "delete", "del": "delete",
    "home": "home", "end": "end", "pgup": "pageup", "pgdn": "pagedown",
    "↑": "up", "↓": "down", "←": "left", "→": "right",
}
_MODS = {"ctrl": "ctrl", "alt": "alt", "shift": "shift"}


def parse_key(spec: str):
    """«Alt+S», «Shift+Tab», «F6», «n», «Enter/Esc» → Key первой клавиши
    или None, если это не клавиша («печатайте», «↑↓», «стрелки»)."""
    spec = spec.strip()
    if not spec:
        return None
    if len(spec) > 1 and "/" in spec:
        # «↑↓/Tab», «Esc/F6»: первая альтернатива, которая — клавиша.
        for alt in spec.split("/"):
            key = parse_key(alt) if alt and alt != spec else None
            if key is not None:
                return key
        return None
    parts = spec.split("+")
    if len(parts) > 1 and parts[-1] == "":
        parts = parts[:-2] + ["+"]  # «Ctrl++» — сама клавиша «+»
    mods = set()
    for part in parts[:-1]:
        mod = _MODS.get(part.strip().lower())
        if mod is None:
            return None
        mods.add(mod)
    base = parts[-1].strip()
    low = base.lower()
    if low in _NAMED:
        name = _NAMED[low]
        char = " " if name == "space" else ""
        return Key(name, char=char, mods=frozenset(mods))
    if len(low) in (2, 3) and low[0] == "f" and low[1:].isdigit() \
            and 1 <= int(low[1:]) <= 12:
        return Key(low, mods=frozenset(mods))
    if len(base) == 1 and base.isprintable() and not base.isspace():
        if mods & {"ctrl", "alt"}:
            # Сочетания Луна узнаёт по label («alt+s»): имя — строчное.
            return Key(base.lower(), char=base.lower(), mods=frozenset(mods))
        return Key(base, char=base, mods=frozenset(mods))
    return None


def find_key_hints(text: str) -> list:
    """Кусочки «Клавиша — действие» строки: [(колонка_от, колонка_до, Key)].

    Колонки — ячейки терминала (широкие символы считаются за две), зона
    накрывает и клавишу, и действие: целиться пальцем по слову удобнее."""
    out: list = []
    if not text or _SEP not in text:
        return out
    pos = 0
    while True:
        i = text.find(_SEP, pos)
        if i < 0:
            break
        left = 0
        for stop in _LEFT_STOPS:
            j = text.rfind(stop, 0, i)
            if j >= 0:
                left = max(left, j + len(stop))
        # Ведущие пробелы и маркеры строк — не часть клавиши.
        while left < i and text[left] in " \t":
            left += 1
        key = parse_key(text[left:i])
        right = len(text)
        for stop in _RIGHT_STOPS:
            j = text.find(stop, i + len(_SEP))
            if j >= 0:
                right = min(right, j)
        # Соседняя подсказка через запятую («…, Esc — отмена») попадёт в эту
        # зону, но её собственная зона идёт позже и побеждает (_zone_at).
        if key is not None:
            a = str_width(text[:left])
            b = str_width(text[:right])
            if b > a:
                out.append((a, b, key))
        pos = i + len(_SEP)
    return out
