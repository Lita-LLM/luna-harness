"""Адреса роя (ЗАДАЧА-15, docs/РОЙ.md §4): метеорные потоки и «тактический IP».

Поток называется по ближайшему к дате настоящему метеорному потоку:
первый рой сессии — по календарю, дальше по кругу (next_stream).
Адрес ячейки: leo/rev.3.12 — поток / каста . звено . индекс в звене.
Звеньевой — индекс 0 (leo/rev.3.0), сам хозяин потока — leo/0.
Маска плотности — для красоты в панелях и логах: leo/12 — до 100 ячеек,
leo/23 — до 1000, leo/34 — до 10 000.
"""

from __future__ import annotations

import datetime

# (код, русское имя, месяц пика, день пика) настоящих метеорных потоков.
STREAMS = [
    ("qua", "Квадрантиды", 1, 3),
    ("lyr", "Лириды", 4, 22),
    ("eta", "Эта-Аквариды", 5, 6),
    ("per", "Персеиды", 8, 12),
    ("ori", "Ориониды", 10, 21),
    ("leo", "Леониды", 11, 17),
    ("gem", "Геминиды", 12, 14),
    ("urs", "Урсиды", 12, 22),
]

_BY_CODE = {row[0]: row for row in STREAMS}


def stream_name(code: str) -> str:
    row = _BY_CODE.get(code)
    return row[1] if row else code


def stream_for_date(day: datetime.date) -> str:
    """Код потока, чей пик ближе всего к дате (по кругу года)."""
    best_code, best_dist = STREAMS[0][0], 400
    for code, _name, month, dom in STREAMS:
        peak = datetime.date(day.year, month, dom)
        dist = abs((day - peak).days)
        dist = min(dist, 365 - dist)
        if dist < best_dist:
            best_code, best_dist = code, dist
    return best_code


def next_stream(code: str) -> str:
    """Следующий поток по кругу (второй рой сессии и дальше)."""
    codes = [row[0] for row in STREAMS]
    if code not in codes:
        return codes[0]
    return codes[(codes.index(code) + 1) % len(codes)]


def density_mask(total: int) -> str:
    """Маска плотности роя: /12 до 100, /23 до 1000, /34 до 10 000, /45 выше."""
    if total <= 100:
        return "12"
    if total <= 1000:
        return "23"
    if total <= 10000:
        return "34"
    return "45"


def address(stream: str, caste: str, link: int, index: int) -> str:
    """Адрес ячейки: leo/rev.3.12. Звеньевой звена — индекс 0."""
    return stream + "/" + caste + "." + str(link) + "." + str(index)


def link_address(stream: str, caste: str, link: int) -> str:
    """Адрес звеньевого: leo/rev.3.0."""
    return address(stream, caste, link, 0)


def host_address(stream: str) -> str:
    """Адрес хозяина потока: leo/0."""
    return stream + "/0"


def parse_address(addr: str) -> dict | None:
    """Разобрать адрес обратно: {stream, caste, link, index} или None."""
    try:
        stream, rest = addr.split("/", 1)
        caste, link, index = rest.rsplit(".", 2)
        return {"stream": stream, "caste": caste,
                "link": int(link), "index": int(index)}
    except (ValueError, AttributeError):
        return None
