"""Визуальная механика мышления (ЗАДАЧА-19): чистые функции кадра.

Луна наливается светом, пока модель думает; звёзды вспыхивают на новых
кусках рассуждений; глубина effort видна числом орбит. Всё считается из
времени кадра и не хранит состояния: в простое кадров нет, ЦП ~0, а
снимки честно повторяются от одного _FAKE_NOW.
"""

from __future__ import annotations

import math

# Фазы налитой луны по режимам глифов: пустая → полная.
MOON_FILL_GLYPHS = {
    "emoji": ("🌑", "🌒", "🌓", "🌔", "🌕"),
    "narrow": ("○", "◔", "◑", "◕", "●"),
    "ascii": (".", ":", "o", "O", "@"),
}

_STARS = {
    "emoji": ("✦", "✧", "·"),
    "narrow": ("✦", "✧", "·"),
    "ascii": ("*", "+", "."),
}

# Глубина усилия — числом орбит вокруг луны.
ORBITS = {"none": 0, "minimal": 1, "low": 2, "medium": 3,
          "high": 4, "xhigh": 5, "max": 6}
DEFAULT_ORBITS = 3  # auto: средняя глубина

# Русские имена уровней для подсказок и выбиралок.
EFFORT_TITLES = {"auto": "авто (решает провайдер)", "none": "выключено",
                 "minimal": "минимальное", "low": "низкое",
                 "medium": "среднее", "high": "высокое",
                 "xhigh": "очень высокое", "max": "максимум"}


def moon_fill(elapsed: float) -> float:
    """Доля света луны по длительности раздумья: быстро вначале,
    к минуте почти полная (насыщение экспонентой)."""
    if elapsed <= 0:
        return 0.0
    return 1.0 - math.exp(-elapsed / 25.0)


def moon_glyph(elapsed: float, mode: str) -> str:
    glyphs = MOON_FILL_GLYPHS.get(mode, MOON_FILL_GLYPHS["ascii"])
    idx = min(len(glyphs) - 1, int(moon_fill(elapsed) * len(glyphs)))
    return glyphs[idx]


def star_sparkle(now: float, last_chunk_at: float, width: int,
                 mode: str) -> str:
    """Строка звёзд: мерцание детерминировано от времени кадра, а свежий
    кусок рассуждений (последние полторы секунды) вспыхивает ярче."""
    if width <= 0:
        return ""
    bright, mid, dim = _STARS.get(mode, _STARS["ascii"])
    fresh = last_chunk_at > 0 and (now - last_chunk_at) < 1.5
    out = []
    for i in range(width):
        wave = math.sin(now * 2.0 + i * 2.399)
        if fresh:
            wave += 0.7
        out.append(bright if wave > 0.75 else mid if wave > 0.1 else dim)
    return "".join(out)


def orbit_count(effort: str) -> int:
    return ORBITS.get(str(effort or "").strip().lower(), DEFAULT_ORBITS)


def summary_alpha(born: float, now: float) -> float:
    """Появление строки пересказа: градиент за 0.8 секунды."""
    if born <= 0:
        return 1.0
    return max(0.0, min(1.0, (now - born) / 0.8))
