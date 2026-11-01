"""Раскладка клавиатуры ЙЦУКЕН ↔ QWERTY (ЗАДАЧА-17).

Управление Луной не должно зависеть от включённой раскладки: команда,
набранная «не в том языке», всё равно команда. Здесь — одна таблица
соответствия физических клавиш и помощники поверх неё:

- to_latin / to_cyrillic — перевод строки между раскладками;
- variants — все написания строки (как есть и в обеих раскладках);
- match_name — найти объект по id, названию или названию, набранному
  не в той раскладке (kbvbns → «лимиты», дшьшеы → limits);
- physical_key — клавиша Ctrl/Alt по физическому месту: Alt+д русской
  раскладки — это Alt+L, потому что буква там — от клавиши, а не языка.

Выключатель у всего этого — настройка ui.layout_fix (включена по умолчанию);
выключена — вызывающие просто не зовут эти функции.
"""

from __future__ import annotations

# Физическая клавиша QWERTY → буква ЙЦУКЕН на ней же (нижний регистр).
_QWERTY_TO_RU = {
    "q": "й", "w": "ц", "e": "у", "r": "к", "t": "е", "y": "н", "u": "г",
    "i": "ш", "o": "щ", "p": "з", "[": "х", "]": "ъ",
    "a": "ф", "s": "ы", "d": "в", "f": "а", "g": "п", "h": "р", "j": "о",
    "k": "л", "l": "д", ";": "ж", "'": "э",
    "z": "я", "x": "ч", "c": "с", "v": "м", "b": "и", "n": "т", "m": "ь",
    ",": "б", ".": "ю", "/": ".", "`": "ё",
}

_RU_TO_QWERTY: dict = {}
for _lat, _ru in _QWERTY_TO_RU.items():
    _RU_TO_QWERTY.setdefault(_ru, _lat)
# Клавиша «.» русской раскладки — это «/» QWERTY; обратная пара уже есть
# («/» → «.»), а вот «ю» живёт на клавише «.» — обе связи нужны.
_RU_TO_QWERTY["."] = "/"


def _translate(text: str, table: dict) -> str:
    out = []
    for ch in text:
        low = ch.lower()
        if low in table:
            rep = table[low]
            out.append(rep.upper() if ch.isupper() else rep)
        else:
            out.append(ch)
    return "".join(out)


def to_latin(text: str) -> str:
    """Строка, набранная в ЙЦУКЕН, — как она выглядела бы в QWERTY."""
    return _translate(text, _RU_TO_QWERTY)


def to_cyrillic(text: str) -> str:
    """Строка, набранная в QWERTY, — как она выглядела бы в ЙЦУКЕН."""
    return _translate(text, _QWERTY_TO_RU)


def variants(text: str) -> list:
    """Все написания строки: как есть, перевод в латиницу, в кириллицу."""
    out: list = []
    for cand in (text, to_latin(text), to_cyrillic(text)):
        if cand and cand not in out:
            out.append(cand)
    return out


def match_name(query: str, names: dict):
    """Найти ключ словаря по любому написанию любого из его имён.

    names: {ключ: (имя, имя, …)} — id, названия, алиасы. Совпадение —
    без учёта регистра, по самой строке или её переводу раскладкой.
    Сначала точные совпадения как есть, потом — по переводам: родное
    написание всегда важнее чужой раскладки.
    """
    q = (query or "").strip().lower()
    if not q:
        return None
    for key, aliases in names.items():
        for name in aliases:
            if q == str(name).lower():
                return key
    options = {v.lower() for v in variants(query)} - {q}
    if not options:
        return None
    for key, aliases in names.items():
        for name in aliases:
            if str(name).lower() in options:
                return key
    return None


def has_cyrillic(text: str) -> bool:
    return any("\u0400" <= ch <= "\u04ff" for ch in text)


def physical_key(key):
    """Клавиша с Ctrl/Alt — по физическому месту, а не по раскладке.

    POSIX-терминал в русской раскладке отдаёт Alt+д как есть — букву «д»;
    физически это клавиша L. Обычные буквы (без модификаторов) не трогаем:
    это текст, а не сочетание.
    """
    if not key.mods or not ({"ctrl", "alt"} & set(key.mods)):
        return key
    name = key.name
    if len(name) == 1 and has_cyrillic(name):
        lat = to_latin(name.lower())
        if len(lat) == 1 and lat.isalpha():
            from .keys import Key

            return Key(lat, char="", mods=key.mods)
    return key
