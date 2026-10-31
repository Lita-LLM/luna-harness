"""Полноэкранный режим вехи 0: заставка → минимальный экран.

Экран — клиент шины: ввод приходит событиями ui.key / ui.mouse / ui.paste,
состояние меняется только в подписчике, отрисовка — из состояния. Ядро позже
станет отдельным сервером, а этот клиент не изменится.
"""

from __future__ import annotations

import time

from ..core.bus import EventBus
from ..core.paths import DataDir
from ..core.run_lock import RunLock
from ..core.settings import Settings
from . import color as _color
from . import moon as _moon
from . import phrases
from .anim import Scheduler, Spinner
from .keys import Key, Mouse, Paste, Resize
from .screen import Buffer, Screen
from .splash import play_splash
from .term import Terminal
from .width import str_width

SPIN_SECONDS = 2.0


class MiniApp:
    """Состояние минимального экрана и реакции на события шины."""

    def __init__(self, bus: EventBus, settings: Settings, scheduler: Scheduler, unicode_ok: bool):
        self.bus = bus
        self.settings = settings
        self.scheduler = scheduler
        self.unicode_ok = unicode_ok
        self.lines: list[tuple[str, str]] = []  # (роль, текст): user | note
        self.input = ""
        self.cursor = 0
        self.done = False
        self.scroll = 0
        self._spinner: Spinner | None = None
        self._spin_phrase = ""
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
            self._spinner = Spinner(self.unicode_ok)
            self._spin_phrase = phrases.random_spinner()
            self._spin_until = time.monotonic() + SPIN_SECONDS
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
                glyph = "🌙 " if self.unicode_ok else "» "
                buf.text(0, y, glyph, fg=_color.VIOLET, bg=_color.SPACE_BG)
                buf.text(2 if not self.unicode_ok else 3, y, text,
                         fg=_color.SILVER, bg=_color.SPACE_BG, attrs=_color.DIM)
            y += 1
        # строка состояния
        sy = rows - 2
        buf.fill_rect(0, sy, cols, 1, " ", bg=(0x16, 0x1B, 0x30))
        pct = self.context_pct()
        left = f" {_moon.context_phase(pct, self.unicode_ok)} контекст {pct}%"
        buf.text(0, sy, left, fg=_color.SILVER, bg=(0x16, 0x1B, 0x30))
        if self._spinner is not None:
            mid = f"{self._spinner.glyph(now)} {self._spin_phrase}…"
            mx = max(0, (cols - str_width(mid)) // 2)
            buf.text(mx, sy, mid, fg=_color.AMBER, bg=(0x16, 0x1B, 0x30))
        clock = time.strftime("%H:%M:%S")
        buf.text(cols - len(clock) - 1, sy, clock, fg=_color.SILVER,
                 bg=(0x16, 0x1B, 0x30), attrs=_color.DIM)
        # строка ввода
        iy = rows - 1
        buf.text(0, iy, "› ", fg=_color.CYAN, bg=_color.SPACE_BG, attrs=_color.BOLD)
        buf.text(2, iy, self.input, fg=_color.SILVER, bg=_color.SPACE_BG)
        cursor_x = 2 + str_width(self.input[:self.cursor])
        return buf, (min(cursor_x, cols - 1), iy)


def run_fullscreen(started_at: float) -> int:
    settings = Settings()
    bus = EventBus()
    term = Terminal(settings=settings)
    if not term.caps.is_tty:
        print("Лунапарк: нужен живой терминал. Справка: luna --help, проверка: luna doctor")
        return 0
    data = DataDir()
    data.ensure()
    with RunLock(data.root):
        with term:
            cols, rows = term.size()
            screen = Screen(term.write, term.caps.color_level, cols, rows)
            if play_splash(term, screen, settings, bus, started_at) == "exit":
                return 0
            scheduler = Scheduler(fps=30)
            app = MiniApp(bus, settings, scheduler, term.caps.unicode)
            while not app.done:
                events = term.read_events(timeout=scheduler.next_timeout())
                size = term.size()
                if size != (screen.cols, screen.rows):
                    screen.resize(*size)
                    bus.publish({"type": "ui.resize", "cols": size[0], "rows": size[1]})
                for ev in events:
                    if isinstance(ev, Key):
                        bus.publish({"type": "ui.key", "key": ev})
                    elif isinstance(ev, Mouse):
                        bus.publish({"type": "ui.mouse", "mouse": ev})
                    elif isinstance(ev, Paste):
                        bus.publish({"type": "ui.paste", "paste": ev})
                    elif isinstance(ev, Resize):
                        bus.publish({"type": "ui.resize", "cols": ev.cols, "rows": ev.rows})
                scheduler.tick()
                buf, cursor = app.draw(size[0], size[1], time.monotonic())
                screen.present(buf, cursor=cursor)
    print(phrases.event_phrase("end") or "Орёл сел.")
    return 0
