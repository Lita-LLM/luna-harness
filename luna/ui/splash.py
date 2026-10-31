"""Заставка: ночное небо, мерцающие звёзды, восход луны, неоновое имя.

Идёт ~1,2 с, любая клавиша пропускает, Ctrl+C — сразу выход. В углу —
время старта процесса до первого кадра: это витрина лёгкости проекта.
Если анимации выключены — один статичный кадр; если вывод не в терминал —
заставки нет.

Мягкость (ЗАДАЧА-03): луна — круглый диск из ui/disc.py (полублоки,
сглаженный край, пепельный свет, ореол); восход — с ease-out, небо —
мягкий градиент с виньеткой по краям, звёзды мерцают медленно и каждая
в своём темпе.
"""

from __future__ import annotations

import math
import random
import time

from . import color as _color
from . import moon as _moon
from . import phrases
from .anim import Scheduler, ease_in_out, ease_out
from .disc import draw_disc
from .keys import Key
from .screen import Buffer

DURATION = 1.2
_SKY_STOPS = ((3, 4, 10), (11, 13, 23), (23, 16, 50), (40, 22, 84))


def _stars(cols: int, rows: int, count: int) -> list:
    """Звёзды: (x, y, фаза мерцания, глиф, скорость) — темп у каждой свой."""
    rng = random.Random(cols * 100003 + rows)
    glyphs = ("·", "•", "+", "✦", "*")
    out = []
    for _ in range(count):
        out.append((rng.randrange(cols), rng.randrange(rows),
                    rng.uniform(0, 6.28), rng.choice(glyphs),
                    rng.uniform(0.5, 1.1)))
    return out


def _sky(buf: Buffer) -> None:
    """Небо: вертикальный градиент из четырёх опор, сканлайны, виньетка.

    Буфер у вызывающих всегда свежий, поэтому фон кладём прямой записью
    в ячейки — без полной цены Buffer.set на каждую клетку.
    """
    cols, rows = buf.cols, buf.rows
    line = _color.gradient(_SKY_STOPS, rows)
    cxv = max(1.0, (cols - 1) / 2.0)
    cyv = max(1.0, (rows - 1) / 2.0)
    for y in range(rows):
        c = line[y]
        if y % 2 == 1:  # сканлайны — мягко, это настроение, а не решётка
            c = tuple(int(v * 0.93) for v in c)
        ny = (y - cyv) / cyv
        for x in range(cols):
            nx = (x - cxv) / cxv
            f = 1.0 - 0.22 * min(1.0, (nx * nx + ny * ny) / 2.0)
            buf.cells[y][x] = (" ", None, tuple(round(v * f) for v in c), 0)


def render_frame(buf: Buffer, t: float, *, unicode_ok=None, glyph_mode=None,
                 phrase: str, startup_ms: float | None, stars, now: float) -> None:
    """Кадр заставки при прогрессе t 0..1 в готовый буфер.

    Режим глифов (emoji | narrow | ascii) приходит из Caps.glyph_mode:
    декоративные звёзды ✦ шириной 2 в узком режиме заменяются на •,
    в ASCII — весь набор схлопывается в точки и звёздочки.
    """
    mode = _moon.normalize_mode(glyph_mode if glyph_mode is not None
                                else (True if unicode_ok is None else unicode_ok))
    cols, rows = buf.cols, buf.rows
    _sky(buf)
    # звёзды: яркость дышит медленно, у каждой своя фаза и свой темп
    for x, y, phase, glyph, speed in stars:
        if mode == "ascii" and glyph not in (".", "*", "+"):
            glyph = "."
        elif mode == "narrow" and glyph == "✦":
            glyph = "•"
        bright = 0.45 + 0.55 * (0.5 + 0.5 * math.sin(now * speed + phase))
        fg = _color.lerp((70, 78, 105), (230, 238, 255), bright)
        buf.set(x, y, glyph, fg=fg, bg=buf.cell(x, y)[2])
    # луна: круглый диск; подъём с замедлением в конце (ease-out),
    # освещённость растёт вместе с подъёмом, как было задумано в вехе 0
    r = max(4.0, rows / 3.0)
    cy_top = rows // 3
    cy = rows + r / 2.0 + (cy_top - rows - r / 2.0) * ease_out(t)
    draw_disc(buf, cols / 2.0, cy, r, t, mode=mode)
    # имя — после подъёма луны, неоновым градиентом, появление затуханием
    if t > 0.3:
        fade = ease_out(min(1.0, (t - 0.3) / 0.4))
        title = "Л У Н А П А Р К"
        grad = _color.gradient(_color.NEON_STOPS, len(title))
        x0 = max(0, (cols - len(title)) // 2)
        y0 = min(rows - 3, int(cy + r / 2.0) + 2)
        if 0 <= y0 < rows:
            for i, ch in enumerate(title):
                if ch == " " or x0 + i >= cols:
                    continue
                bg_here = buf.cell(x0 + i, y0)[2]
                c = _color.lerp(bg_here or (0, 0, 0), grad[i], fade)
                buf.set(x0 + i, y0, ch, fg=c, bg=bg_here, attrs=_color.BOLD)
            p_fade = ease_in_out(max(0.0, min(1.0, (t - 0.5) / 0.5)))
            if p_fade > 0.0 and phrase:
                px = max(0, (cols - len(phrase)) // 2)
                if y0 + 2 < rows:
                    bg_phrase = buf.cell(px, y0 + 2)[2]
                    fg_phrase = _color.lerp((40, 45, 70), _color.SILVER, p_fade)
                    buf.text(px, y0 + 2, phrase, fg=fg_phrase,
                             bg=bg_phrase, attrs=_color.DIM)
    if startup_ms is not None:
        label = f"старт: {startup_ms:.0f} мс"
        buf.text(cols - len(label) - 1, rows - 1, label,
                 fg=(90, 98, 125), bg=buf.cell(cols - 1, rows - 1)[2], attrs=_color.DIM)


def play_splash(term, screen, settings, bus, started_at: float) -> str:
    """Показать заставку. Возвращает "done" или "exit" (Ctrl+C)."""
    if not term.caps.is_tty or not settings.get("ui.splash"):
        return "done"
    phrase = phrases.random_splash()
    cols, rows = term.size()
    stars = _stars(cols, rows, max(20, cols * rows // 28))
    animations = bool(settings.get("ui.animations"))
    bus.publish({"type": "ui.splash", "phase": "start"})
    startup_ms: float | None = None
    if not animations:
        buf = Buffer(cols, rows)
        render_frame(buf, 1.0, glyph_mode=term.caps.glyph_mode, phrase=phrase,
                     startup_ms=None, stars=stars, now=0.0)
        screen.present(buf)
        bus.publish({"type": "ui.splash", "phase": "done", "animated": False})
        return "done"
    scheduler = Scheduler(fps=30)
    start = time.monotonic()
    while True:
        now = time.monotonic()
        t = min(1.0, (now - start) / DURATION)
        size = term.size()
        if size != (cols, rows):
            cols, rows = size
            stars = _stars(cols, rows, max(20, cols * rows // 28))
            screen.resize(cols, rows)
        buf = Buffer(cols, rows)
        render_frame(buf, t, glyph_mode=term.caps.glyph_mode, phrase=phrase,
                     startup_ms=startup_ms, stars=stars, now=now)
        screen.present(buf)
        if startup_ms is None:
            startup_ms = (time.monotonic() - started_at) * 1000
        if t >= 1.0:
            break
        remaining = DURATION - (now - start)
        events = term.read_events(timeout=min(scheduler.frame_interval, max(0.0, remaining)))
        for ev in events:
            if isinstance(ev, Key):
                if ev.label == "ctrl+c" or ev.name == "eof":
                    bus.publish({"type": "ui.splash", "phase": "exit"})
                    return "exit"
                bus.publish({"type": "ui.splash", "phase": "skipped"})
                return "done"
    bus.publish({"type": "ui.splash", "phase": "done", "animated": True})
    return "done"


def run_splash_demo(started_at: float) -> int:
    """luna try splash: заставка отдельным экраном."""
    from ..core.bus import EventBus
    from ..core.settings import Settings
    from .screen import Screen
    from .term import Terminal

    settings = Settings()
    bus = EventBus()
    term = Terminal(settings=settings)
    if not term.caps.is_tty:
        print(phrases.random_splash())
        print("(заставка видна только в живом терминале)")
        return 0
    with term:
        cols, rows = term.size()
        screen = Screen(term.write, term.caps.color_level, cols, rows)
        result = play_splash(term, screen, settings, bus, started_at)
    return 130 if result == "exit" else 0
