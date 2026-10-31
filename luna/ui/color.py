"""Цвет: RGB, градиенты, деградация до возможностей терминала, палитра темы.

Уровни цвета: truecolor → 256 → 16 → mono. Стиль ячейки — кортеж
(fg, bg, attrs), где fg/bg — RGB-кортеж или None, attrs — битовая маска.
"""

from __future__ import annotations

RGB = tuple  # (r, g, b), каждый 0..255

BOLD = 1
DIM = 2
ITALIC = 4
UNDERLINE = 8
REVERSE = 16

LEVELS = ("truecolor", "256", "16", "mono")

# Тема «luna» — палитра из docs/ДИЗАЙН.md.
SPACE_BG = (0x0B, 0x0D, 0x17)
SILVER = (0xC9, 0xD1, 0xE0)
CYAN = (0x00, 0xE5, 0xFF)
VIOLET = (0x7B, 0x5C, 0xFF)
MAGENTA = (0xFF, 0x2B, 0xD6)
AMBER = (0xFF, 0xB3, 0x47)
OK = (0x4D, 0xFF, 0xB0)
ERR = (0xFF, 0x4D, 0x6D)

THEME = {
    "space_bg": SPACE_BG,
    "silver": SILVER,
    "cyan": CYAN,
    "violet": VIOLET,
    "magenta": MAGENTA,
    "amber": AMBER,
    "ok": OK,
    "err": ERR,
}

THEME_TITLES = {
    "space_bg": "фон космоса",
    "silver": "лунное серебро",
    "cyan": "неон-циан",
    "violet": "фиолет",
    "magenta": "маджента",
    "amber": "тёплый янтарь",
    "ok": "успех",
    "err": "ошибка",
}

NEON_STOPS = (CYAN, VIOLET, MAGENTA)


def hex_of(rgb) -> str:
    return "#%02X%02X%02X" % tuple(rgb)


def lerp(a, b, t: float):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def gradient(stops, n: int) -> list:
    """n цветов линейно по опорным stops (несколько опорных цветов)."""
    if n <= 0:
        return []
    if n == 1 or len(stops) == 1:
        return [tuple(stops[0])] * n
    out = []
    segments = len(stops) - 1
    for i in range(n):
        pos = i / (n - 1) * segments
        seg = min(segments - 1, int(pos))
        out.append(lerp(stops[seg], stops[seg + 1], pos - seg))
    return out


def gradient_at(stops, t: float):
    """Один цвет градиента в точке t 0..1."""
    if len(stops) == 1:
        return tuple(stops[0])
    t = max(0.0, min(1.0, t))
    pos = t * (len(stops) - 1)
    seg = min(len(stops) - 2, int(pos))
    return lerp(stops[seg], stops[seg + 1], pos - seg)


# --- деградация -----------------------------------------------------------

_CUBE_LEVELS = (0, 95, 135, 175, 215, 255)


def _cube_component(v: int) -> int:
    best = 0
    for i, level in enumerate(_CUBE_LEVELS):
        if abs(level - v) < abs(_CUBE_LEVELS[best] - v):
            best = i
    return best


def quantize256(rgb) -> int:
    """RGB → номер цвета xterm-256."""
    r, g, b = rgb
    if r == g == b:
        if r < 8:
            return 16
        if r > 248:
            return 231
        return round((r - 8) / 247 * 24) + 232
    return 16 + 36 * _cube_component(r) + 6 * _cube_component(g) + _cube_component(b)


def rgb_of_256(n: int):
    """Обратное: представитель RGB для номера 256-палитры (для витрины деградации)."""
    if n < 16:
        return PALETTE16[n]
    if n >= 232:
        v = 8 + (n - 232) * 10
        return (v, v, v)
    n -= 16
    return (_CUBE_LEVELS[n // 36], _CUBE_LEVELS[(n % 36) // 6], _CUBE_LEVELS[n % 6])


# Стандартная 16-цветная палитра (xterm/VGA): (RGB, код fg, код bg).
PALETTE16 = (
    (0, 0, 0), (205, 0, 0), (0, 205, 0), (205, 205, 0),
    (0, 0, 238), (205, 0, 205), (0, 205, 205), (229, 229, 229),
    (127, 127, 127), (255, 0, 0), (0, 255, 0), (255, 255, 0),
    (0, 0, 255), (255, 0, 255), (0, 255, 255), (255, 255, 255),
)


def nearest16(rgb) -> int:
    """Индекс ближайшего цвета 16-палитры (взвешенная дистанция, как видит глаз)."""
    r, g, b = rgb
    best, best_d = 0, None
    for i, (pr, pg, pb) in enumerate(PALETTE16):
        d = (r - pr) ** 2 * 0.30 + (g - pg) ** 2 * 0.59 + (b - pb) ** 2 * 0.11
        if best_d is None or d < best_d:
            best, best_d = i, d
    return best


def degrade(rgb, level: str):
    """RGB, каким его реально покажет терминал уровня level (для витрины и тестов)."""
    if level == "truecolor":
        return tuple(rgb)
    if level == "256":
        return rgb_of_256(quantize256(rgb))
    if level == "16":
        return PALETTE16[nearest16(rgb)]
    return SILVER  # mono: цвета нет, остаётся яркость текста по умолчанию


# --- SGR ------------------------------------------------------------------

def sgr(fg, bg, attrs: int, level: str) -> str:
    """Escape-последовательность стиля для уровня цвета."""
    parts = ["0"]
    if attrs & BOLD:
        parts.append("1")
    if attrs & DIM:
        parts.append("2")
    if attrs & ITALIC:
        parts.append("3")
    if attrs & UNDERLINE:
        parts.append("4")
    if attrs & REVERSE:
        parts.append("7")
    if level != "mono":
        if fg is not None:
            parts.append(_color_code(fg, level, fore=True))
        if bg is not None:
            parts.append(_color_code(bg, level, fore=False))
    return "\x1b[" + ";".join(parts) + "m"


def _color_code(rgb, level: str, fore: bool) -> str:
    if level == "truecolor":
        base = "38" if fore else "48"
        return f"{base};2;{rgb[0]};{rgb[1]};{rgb[2]}"
    if level == "256":
        base = "38" if fore else "48"
        return f"{base};5;{quantize256(rgb)}"
    idx = nearest16(rgb)
    if fore:
        return str(30 + idx) if idx < 8 else str(90 + idx - 8)
    return str(40 + idx) if idx < 8 else str(100 + idx - 8)


def gradient_line(text: str, stops, level: str, attrs: int = 0, bg=None) -> str:
    """Строка, где каждый символ окрашен по градиенту (для не-буферного вывода)."""
    if level == "mono":
        return text
    colors = gradient(stops, max(1, len(text)))
    out = []
    for ch, c in zip(text, colors):
        out.append(sgr(c, bg, attrs, level) + ch)
    out.append("\x1b[0m")
    return "".join(out)
