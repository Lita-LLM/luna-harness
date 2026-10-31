"""Минимальный экран вехи 0 (MiniApp) — витрина и основа тестов.

Живой полноэкранный путь с вехи 1 — чат (ui/chat.py): run_fullscreen ниже
делегирует ему. MiniApp оставлен как есть: экран — клиент шины, ввод
приходит событиями ui.key / ui.mouse / ui.paste, состояние меняется только
в подписчике, отрисовка — из состояния.
"""

from __future__ import annotations

import time

from ..core.bus import EventBus
from ..core.settings import Settings
from . import color as _color
from . import moon as _moon
from . import phrases
from .anim import Scheduler, Spinner, ease_in_out
from .keys import Key, Mouse, Paste, Resize
from .screen import Buffer
from .width import str_width

SPIN_SECONDS = 2.0


class MiniApp:
    """Состояние минимального экрана и реакции на события шины."""

    def __init__(self, bus: EventBus, settings: Settings, scheduler: Scheduler,
                 glyph_mode="emoji"):
        self.bus = bus
        self.settings = settings
        self.scheduler = scheduler
        self.glyph_mode = _moon.normalize_mode(glyph_mode)
        self.lines: list[tuple[str, str]] = []  # (роль, текст): user | note
        self.input = ""
        self.cursor = 0
        self.done = False
        self.scroll = 0
        self._spinner: Spinner | None = None
        self._spin_phrase = ""
        self._spin_from = 0.0
        self._spin_until = 0.0
        bus.listen(self._on_event)

    # --- события ------------------------------------------------------------

    def _on_event(self, ev: dict) -> None:
        kind = ev.get("type")
        if kind == "ui.key":
            self.handle_key(ev["key"])
        elif kind == "ui.paste":
            self.insert_text(ev["paste"].text.replace("\r", " ").replace("\n", " "))
        elif kind == "ui.mouse":
            mouse: Mouse = ev["mouse"]
            if mouse.kind == "wheel_up":
                self.scroll = min(len(self.lines), self.scroll + 3)
            elif mouse.kind == "wheel_down":
                self.scroll = max(0, self.scroll - 3)

    def handle_key(self, key: Key) -> None:
        if key.label == "ctrl+c" or key.name == "eof":
            self.done = True
            return
        if key.char and not key.mods:
            self.insert_text(key.char)
            return
        if key.name == "enter":
            self.submit()
        elif key.name == "backspace":
            if self.cursor > 0:
                self.input = self.input[:self.cursor - 1] + self.input[self.cursor:]
                self.cursor -= 1
        elif key.name == "delete":
            if self.cursor < len(self.input):
                self.input = self.input[:self.cursor] + self.input[self.cursor + 1:]
        elif key.name == "left":
            self.cursor = max(0, self.cursor - 1)
        elif key.name == "right":
            self.cursor = min(len(self.input), self.cursor + 1)
        elif key.name == "home":
            self.cursor = 0
        elif key.name == "end":
            self.cursor = len(self.input)
        elif key.label == "ctrl+u":
            self.input, self.cursor = "", 0

    def insert_text(self, text: str) -> None:
        self.input = self.input[:self.cursor] + text + self.input[self.cursor:]
        self.cursor += len(text)

    def submit(self) -> None:
        text = self.input.strip()
        self.input, self.cursor = "", 0
        self.scroll = 0
        if text == "/exit":
            self.done = True
            return
        if not text:
            return
        self.lines.append(("user", text))
        self.bus.publish({"type": "chat.message", "role": "user", "text": text})
        if self.settings.get("ui.animations"):
            self._spinner = Spinner(self.glyph_mode)
            self._spin_phrase = phrases.random_spinner()
            self._spin_from = time.monotonic()
            self._spin_until = self._spin_from + SPIN_SECONDS
            self.scheduler.add(self._spin_tick)
        else:
            self._note()

    def _spin_tick(self, now: float) -> bool:
        if now >= self._spin_until:
            self._spinner = None
            self._note()
            return False
        return True

    def _note(self) -> None:
        note = "агент появится в следующей вехе"
        self.lines.append(("note", note))
        self.bus.publish({"type": "chat.note", "text": note})

    # --- отрисовка ------------------------------------------------------------

    def context_pct(self) -> int:
        chars = sum(len(t) for _, t in self.lines)
        return min(100, chars // 4)

    def draw(self, cols: int, rows: int, now: float) -> tuple[Buffer, tuple[int, int]]:
        buf = Buffer(cols, rows)
        buf.clear(bg=_color.SPACE_BG)
        content_rows = max(0, rows - 2)
        end = max(0, len(self.lines) - self.scroll)
        visible = self.lines[max(0, end - content_rows):end]
        y = 0
        for role, text in visible:
            if role == "user":
                buf.text(0, y, "› ", fg=_color.CYAN, bg=_color.SPACE_BG, attrs=_color.BOLD)
                buf.text(2, y, text, fg=_color.SILVER, bg=_color.SPACE_BG)
            else:
                if self.glyph_mode == "emoji":
                    glyph, text_x = "🌙 ", 3
                elif self.glyph_mode == "narrow":
                    glyph, text_x = "◑ ", 2
                else:
                    glyph, text_x = "» ", 2
                buf.text(0, y, glyph, fg=_color.VIOLET, bg=_color.SPACE_BG)
                buf.text(text_x, y, text,
                         fg=_color.SILVER, bg=_color.SPACE_BG, attrs=_color.DIM)
            y += 1
        # строка состояния
        sy = rows - 2
        buf.fill_rect(0, sy, cols, 1, " ", bg=(0x16, 0x1B, 0x30))
        pct = self.context_pct()
        left = f" {_moon.context_phase(pct, self.glyph_mode)} контекст {pct}%"
        buf.text(0, sy, left, fg=_color.SILVER, bg=(0x16, 0x1B, 0x30))
        if self._spinner is not None:
            mid = f"{self._spinner.glyph(now)} {self._spin_phrase}…"
            mx = max(0, (cols - str_width(mid)) // 2)
            # строка думания появляется и уходит затуханием, а не прыжком
            alpha = min(ease_in_out((now - self._spin_from) / 0.3),
                        ease_in_out((self._spin_until - now) / 0.3))
            spin_fg = _color.lerp((0x16, 0x1B, 0x30), _color.AMBER, alpha)
            buf.text(mx, sy, mid, fg=spin_fg, bg=(0x16, 0x1B, 0x30))
        clock = time.strftime("%H:%M:%S")
        buf.text(cols - len(clock) - 1, sy, clock, fg=_color.SILVER,
                 bg=(0x16, 0x1B, 0x30), attrs=_color.DIM)
        # строка ввода
        iy = rows - 1
        buf.text(0, iy, "› ", fg=_color.CYAN, bg=_color.SPACE_BG, attrs=_color.BOLD)
        buf.text(2, iy, self.input, fg=_color.SILVER, bg=_color.SPACE_BG)
        cursor_x = 2 + str_width(self.input[:self.cursor])
        return buf, (min(cursor_x, cols - 1), iy)


def run_fullscreen(started_at: float, continue_last: bool = False) -> int:
    """Полноэкранный режим: с вехи 1 минимальный экран заменён чатом.

    MiniApp выше оставлен как витрина и для тестов; живой путь `luna`
    без аргументов — ui/chat.py (источник событий — его create_source).
    """
    from .chat import run_chat

    return run_chat(started_at=started_at, splash_first=True, fake=False,
                    continue_last=continue_last)
