"""Круглый диск луны для заставки и будущих экранов.

draw_disc(buf, cx, cy, r, phase) рисует луну в готовый буфер поверх того,
что там уже есть (фон читается из ячеек — обычно это небо заставки).

Как достигается круглость (docs/ДИЗАЙН.md, «Мягкость»):
- рисуем «подпикселями»: у клетки два пикселя по вертикали — полублок ▀
  (верхний — цвет текста, нижний — фон); клетка терминала примерно вдвое
  выше, чем шире, поэтому подпиксель почти квадратный;
- покрытие подпикселя диском считается подвыборками (4x4) и цвет луны
  смешивается с фоном по этой доле — край гладкий, а не ступенчатый;
- терминатор фазы — плавный градиент; тёмная сторона не чёрная, а в
  пепельном свете; вокруг диска — мягкий ореол, затухающий в фон;
- поверхность — несколько размытых кратеров без контуров.

phase — доля освещённости 0..1, свет приходит справа: терминатор едет
справа налево. В режиме ascii полублока нет — диск рисуется обычными
ячейками фона: скромнее, но без мусора.
"""

from __future__ import annotations

import math

from . import color as _color
from . import moon as _moon

MOON_TOP = (0xE8, 0xED, 0xF7)
MOON_BOTTOM = (0x9F, 0xAC, 0xC4)
EARTHSHINE = (0x2C, 0x34, 0x54)  # тёмная сторона в пепельном свете
HALO_COLOR = (0x93, 0xA3, 0xCC)

# Кратеры: (u, v, радиус, глубина) в координатах диска -1..1.
# Размыты гауссовым спадом — пятно потемнения без контура.
_CRATERS = (
    (-0.34, -0.22, 0.30, 0.26),
    (0.27, 0.16, 0.21, 0.20),
    (0.02, -0.48, 0.16, 0.17),
    (-0.18, 0.44, 0.14, 0.15),
    (0.52, -0.30, 0.12, 0.14),
)

_SAMPLES = 4          # подвыборок по стороне подпикселя на покрытие
_TERM_SOFT = 0.09     # полуширина мягкой зоны терминатора (в долях радиуса)
_HALO_W = 0.5         # ширина ореола в долях радиуса
_HALO_A = 0.38        # сила ореола у самого края диска


def _smooth01(x: float) -> float:
    x = 0.0 if x < 0.0 else (1.0 if x > 1.0 else x)
    return x * x * (3.0 - 2.0 * x)


def _coverage(sx: float, sy: float, cx: float, cy: float, r: float) -> float:
    """Доля покрытия подпикселя (sx, sy — его центр, координаты подпикселей) диском."""
    inside = 0
    step = 1.0 / _SAMPLES
    for i in range(_SAMPLES):
        for j in range(_SAMPLES):
            dx = (sx - 0.5 + (i + 0.5) * step - cx) / r
            dy = (sy - 0.5 + (j + 0.5) * step - cy) / r
            if dx * dx + dy * dy <= 1.0:
                inside += 1
    return inside / float(_SAMPLES * _SAMPLES)


def _surface(u: float, v: float, phase: float, mirror: bool = False):
    """Цвет точки диска по нормированным координатам u, v (-1..1).

    mirror — свет приходит слева, а не справа: убывающая луна северного
    полушария и растущая — южного (см. ui/astro.py).
    """
    if mirror:
        u = -u
    # вертикальное затенение: верх светлее
    base = _color.lerp(MOON_BOTTOM, MOON_TOP, 1.0 - (v + 1.0) / 2.0)
    factor = 1.0
    for cu, cv, cr, depth in _CRATERS:
        d2 = (u - cu) * (u - cu) + (v - cv) * (v - cv)
        factor *= 1.0 - depth * math.exp(-d2 / (cr * cr) * 1.8)
    lit = tuple(round(c * factor) for c in base)
    dark = tuple(round(c * (0.85 + 0.15 * factor)) for c in EARTHSHINE)
    term = 1.0 - 2.0 * phase
    light = _smooth01((u - term) / (2.0 * _TERM_SOFT) + 0.5)
    return _color.lerp(dark, lit, light)


def _subpixel(sx: float, sy: float, cx: float, cy: float, r: float,
              phase: float, sky, mirror: bool = False):
    """Итоговый цвет подпикселя: небо + ореол, поверх — диск по покрытию."""
    u = (sx - cx) / r
    v = (sy - cy) / r
    dist = math.sqrt(u * u + v * v)
    out = sky
    if dist > 1.0:  # ореол живёт только снаружи диска
        glow = 1.0 - (dist - 1.0) / _HALO_W
        if glow > 0.0:
            out = _color.lerp(sky, HALO_COLOR, glow * glow * _HALO_A)
    # Подвыборки нужны только у края: глубоко внутри покрытие полное,
    # далеко снаружи — нулевое (полудиагональ подпикселя — 0,75/r).
    edge = 0.75 / r
    if dist < 1.0 - edge:
        cov = 1.0
    elif dist > 1.0 + edge:
        cov = 0.0
    else:
        cov = _coverage(sx, sy, cx, cy, r)
    if cov > 0.0:
        out = _color.lerp(out, _surface(u, v, phase, mirror), cov)
    return out


def draw_disc(buf, cx: float, cy: float, r: float, phase: float, *,
              mode: str = "emoji", mirror: bool = False) -> None:
    """Нарисовать диск луны в буфер.

    cx, r — в клетках по горизонтали; cy — в строках (вертикальный радиус
    диска — r/2 строк: клетка вдвое выше, чем шире). Фон (небо) читается
    из фонов ячеек буфера; ячейки вне диска и ореола не трогаются.
    """
    mode = _moon.normalize_mode(mode)
    if r <= 0:
        return
    phase = 0.0 if phase < 0.0 else (1.0 if phase > 1.0 else phase)
    scx, scy = cx, 2.0 * cy  # центр в подпикселях: по вертикали их вдвое больше
    reach = r * (1.0 + _HALO_W)
    x0 = max(0, int(math.floor(cx - reach)))
    x1 = min(buf.cols - 1, int(math.ceil(cx + reach)))
    y0 = max(0, int(math.floor(cy - reach / 2.0)))
    y1 = min(buf.rows - 1, int(math.ceil(cy + reach / 2.0)))
    for y in range(y0, y1 + 1):
        for x in range(x0, x1 + 1):
            sky = buf.cell(x, y)[2]
            if sky is None:
                sky = _color.SPACE_BG
            top = _subpixel(x + 0.5, 2 * y + 0.5, scx, scy, r, phase, sky, mirror)
            bottom = _subpixel(x + 0.5, 2 * y + 1.5, scx, scy, r, phase, sky, mirror)
            if top == sky and bottom == sky:
                continue  # ни диска, ни ореола: ячейку (и звёзды на ней) не трогаем
            if mode == "ascii":
                mid = _color.lerp(top, bottom, 0.5)
                buf.set(x, y, " ", bg=mid)
            elif top == bottom:
                buf.set(x, y, " ", bg=top)
            else:
                buf.set(x, y, "▀", fg=top, bg=bottom)
