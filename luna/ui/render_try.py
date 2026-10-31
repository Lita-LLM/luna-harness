"""luna try render: витрина рендерера на одном экране.

Палитра темы, градиенты, деградация цвета всеми уровнями рядом, все фазы
луны, спиннер, строка CJK+эмодзи против линейки (проверка ширины), счётчик
кадров и последнее событие ввода — сюда смотрят, когда чистят renderer.
Выход: q, Esc или Ctrl+C.
"""

from __future__ import annotations

import time

from ..core.bus import EventBus
from ..core.settings import Settings
from . import color as _color
from . import moon as _moon
from . import phrases
from .anim import Scheduler, Spinner
from .keys import Key, Mouse, Paste, Resize
from .screen import Buffer, Screen
from .term import Terminal
from .width import str_width


class _Demo:
    def __init__(self, glyph_mode="emoji"):
        self.glyph_mode = _moon.normalize_mode(glyph_mode)
        self.done = False
        self.frames = 0
        self.fps = 0
        self._fps_at = time.monotonic()
        self._fps_count = 0
        self.last_event = "—"
        self.spinner = Spinner(self.glyph_mode)
        self.phrase = phrases.random_spinner()

    def on_key(self, ev) -> None:
        self.last_event = repr(ev)
        if isinstance(ev, Key) and (ev.label == "ctrl+c" or ev.name == "eof"
                                    or ev.name == "esc" or (ev.char == "q" and not ev.mods)):
            self.done = True

    def draw(self, cols: int, rows: int, now: float) -> Buffer:
        buf = Buffer(cols, rows)
        buf.clear(bg=_color.SPACE_BG)
        y = 0
        buf.text(0, y, "ВИТРИНА РЕНДЕРЕРА", fg=_color.SILVER, bg=_color.SPACE_BG, attrs=_color.BOLD)
        buf.text(20, y, "luna try render", fg=_color.VIOLET, bg=_color.SPACE_BG, attrs=_color.DIM)
        y += 2
        # палитра
        x = 0
        for name, rgb in _color.THEME.items():
            if x + 14 > cols:
                break
            buf.fill_rect(x, y, 4, 1, " ", bg=rgb)
            buf.text(x + 5, y, _color.THEME_TITLES[name], fg=_color.SILVER, bg=_color.SPACE_BG)
            buf.text(x + 5, y + 1, _color.hex_of(rgb), fg=_color.SILVER,
                     bg=_color.SPACE_BG, attrs=_color.DIM)
            x += 22
        y += 3
        # градиенты
        bar_w = max(10, cols - 2)
        buf.gradient_rect(0, y, bar_w, 1, _color.NEON_STOPS)
        buf.gradient_rect(0, y + 1, bar_w, 1, (_color.SILVER, _color.AMBER))
        buf.gradient_rect(0, y + 2, bar_w, 1, (_color.CYAN, _color.SILVER))
        y += 4
        # деградация: один градиент, цвета преддеградированы под каждый уровень
        grad = _color.gradient(_color.NEON_STOPS, bar_w)
        for level in _color.LEVELS:
            buf.text(0, y, f"{level:<9}", fg=_color.SILVER, bg=_color.SPACE_BG)
            for i, c in enumerate(grad):
                buf.set(10 + i, y, " ", bg=_color.degrade(c, level))
            y += 1
        y += 1
        # Фазы луны двумя рядами на одной шкале процентов: «цикл» —
        # phase(pct) (50% — полнолуние, 100% — снова новолуние);
        # «контекст» — context_phase(pct), как фаза выглядит в строке
        # состояния по заполненности контекста (100% — полнолуние).
        buf.text(0, y, "цикл:", fg=_color.SILVER, bg=_color.SPACE_BG, attrs=_color.DIM)
        buf.text(0, y + 2, "контекст:", fg=_color.SILVER, bg=_color.SPACE_BG, attrs=_color.DIM)
        x = 10
        for pct in (0, 12.5, 25, 37.5, 50, 62.5, 75, 87.5, 100):
            glyph = _moon.phase(pct, self.glyph_mode)
            w = str_width(glyph)
            buf.text(x, y, glyph, fg=_color.SILVER, bg=_color.SPACE_BG)
            buf.text(x, y + 1, f"{pct:g}%", fg=_color.SILVER, bg=_color.SPACE_BG,
                     attrs=_color.DIM)
            buf.text(x, y + 2, _moon.context_phase(pct, self.glyph_mode),
                     fg=_color.SILVER, bg=_color.SPACE_BG)
            x += w + 4
            if x > cols - 8:
                break
        y += 4
        # ширина: линейка и строка с CJK и эмодзи должны совпасть по правому краю
        ruler = "".join(str(i % 10) for i in range(min(cols, 40)))
        buf.text(0, y, ruler, fg=_color.SILVER, bg=_color.SPACE_BG, attrs=_color.DIM)
        # Образец ширины честно следует режиму глифов: эмодзи — только
        # в emoji-режиме; в узком вместо них узкая полная фаза, без ракеты.
        sample_glyphs = {"emoji": "🌕🚀", "narrow": "●", "ascii": ""}[self.glyph_mode]
        sample = "ширина: 你好世界 " + sample_glyphs + (" " if sample_glyphs else "") + "кот"
        buf.text(0, y + 1, sample, fg=_color.SILVER, bg=_color.SPACE_BG)
        buf.text(0, y + 2, f"str_width(sample) = {str_width(sample)} ячеек",
                 fg=_color.SILVER, bg=_color.SPACE_BG, attrs=_color.DIM)
        y += 4
        # спиннер и счётчики
        self.frames += 1
        self._fps_count += 1
        if now - self._fps_at >= 1.0:
            self.fps = round(self._fps_count / (now - self._fps_at))
            self._fps_at, self._fps_count = now, 0
        spin = f"{self.spinner.glyph(now)} {self.phrase}…"
        buf.text(0, y, spin, fg=_color.AMBER, bg=_color.SPACE_BG)
        buf.text(0, y + 1, f"кадр {self.frames} · {self.fps} кадр/с · последнее событие: {self.last_event}",
                 fg=_color.SILVER, bg=_color.SPACE_BG)
        if rows - 1 > y + 2:
            buf.text(0, rows - 1, "q / Esc / Ctrl+C — выход; покликай мышью и покрути колесо",
                     fg=_color.SILVER, bg=_color.SPACE_BG, attrs=_color.DIM)
        return buf


def run_render_demo(started_at: float) -> int:  # noqa: ARG001 — единый интерфейс try-команд
    settings = Settings()
    bus = EventBus()
    term = Terminal(settings=settings)
    if not term.caps.is_tty:
        print("luna try render нужен живой терминал.")
        return 0
    demo = _Demo(term.caps.glyph_mode)
    scheduler = Scheduler(fps=30)
    scheduler.add(lambda now: True)  # витрина живёт, пока открыта: кадры идут всегда
    with term:
        cols, rows = term.size()
        screen = Screen(term.write, term.caps.color_level, cols, rows)
        while not demo.done:
            events = term.read_events(timeout=scheduler.next_timeout())
            size = term.size()
            if size != (screen.cols, screen.rows):
                screen.resize(*size)
            for ev in events:
                if isinstance(ev, (Key, Mouse, Paste, Resize)):
                    demo.on_key(ev)
                    bus.publish({"type": "ui.input", "event": repr(ev)})
            scheduler.tick()
            screen.present(demo.draw(size[0], size[1], time.monotonic()))
    return 0
