"""Панель «Созвездие» (ЗАДАЧА-15, docs/РОЙ.md §8): рой звёздами на Брайле.

Каждая клетка шрифта Брайля — 2×4 точки, звезда — точка на поле.
Состояния: действует — яркое мерцание; ждёт — ровный тусклый свет;
сдал — гаснет со следом-метеором; упал — полная клетка (в заголовке —
счётчик упавших, цвет у панелей общий); не начата — еле видна. Письма —
бегущая точка между звёздами; в большом рое звёзды — звенья целиком
(данные swarmdata уже агрегированы). Enter — приближение: звено за
звеном, внутри — адреса ячеек и их состояния списком.

Кадры считаются только когда панель открыта и рой идёт; анимации
выключаются настройкой ui.animations — тогда кадр статичный.
"""

from __future__ import annotations

import math
import time

from . import PanelContent, register_panel

# Биты точек Брайля по (столбец, строка) клетки 2×4.
_BITS = {(0, 0): 0x01, (0, 1): 0x02, (0, 2): 0x04, (0, 3): 0x40,
         (1, 0): 0x08, (1, 1): 0x10, (1, 2): 0x20, (1, 3): 0x80}
# Порядок зажигания точек при росте яркости (спиралью от угла).
_ORDER = [(0, 0), (1, 1), (0, 2), (1, 3), (1, 0), (0, 1), (1, 2), (0, 3)]
_LEVEL_DOTS = (1, 1, 2, 4, 6, 8)  # уровень 0..5 → сколько точек горит (0 — еле видна)
_CANVAS_ROWS = 10                 # строк Брайля в обзоре (40 точек по вертикали)

_STATE_RU = {"done": "сдал", "failed": "упал", "run": "действует",
             "wait": "ждёт", "skipped": "не начата"}


def _star_pos(index: int, width_dots: int, height_dots: int) -> tuple:
    """Детерминированная «случайная» позиция звезды по её номеру."""
    x = (index * 37 + 11) % max(1, width_dots)
    y = (index * 23 + 5) % max(1, height_dots)
    return x, y


def _level(state: str, index: int, t: float, animated: bool) -> int:
    """Яркость звезды 0..5 по состоянию и времени."""
    if state == "failed":
        return 5
    if state == "done":
        return 1  # уже гаснет, остался след
    if state == "skipped":
        return 0
    if state == "run":
        if not animated:
            return 4
        wave = math.sin(t * 5.0 + index * 1.7)
        return 4 if wave > -0.3 else 3  # яркое мерцание
    # wait: думает/ждёт — тусклое медленное мигание
    if not animated:
        return 1
    return 2 if math.sin(t * 1.2 + index * 0.9) > 0 else 1


class ConstellationPanel(PanelContent):
    """Рой звёздами; Enter в фокусе панели — приближение по звеньям."""

    def __init__(self):
        super().__init__("Созвездие")
        self.zoom_link: int | None = None

    # --- клавиши --------------------------------------------------------------

    def handle_key(self, app, key) -> bool:
        if getattr(key, "name", "") != "enter":
            return False
        from . import panel_data

        data = panel_data(app).swarm
        links = sorted({self._link_of(addr) for addr in data.cells
                        if self._link_of(addr) is not None})
        if not links:
            return False
        if self.zoom_link is None:
            self.zoom_link = links[0]
        else:
            nxt = links.index(self.zoom_link) + 1 if self.zoom_link in links else 0
            self.zoom_link = links[nxt] if nxt < len(links) else None
        return True

    @staticmethod
    def _link_of(addr: str):
        from ...starfall.addressing import parse_address

        parsed = parse_address(addr)
        return parsed["link"] if parsed else None

    # --- отрисовка --------------------------------------------------------------

    def lines(self, app, width: int) -> list:
        from . import panel_data

        data = panel_data(app).swarm
        if not data.swarm:
            return ["Роя пока нет.", "Звездопад запускает мать тулом starfall."]
        head = data.line()
        if self.zoom_link is not None:
            return [head, ""] + self._zoom_lines(data)
        animated = True
        settings = getattr(app, "settings", None)
        if settings is not None and not settings.get("ui.animations", True):
            animated = False
        clock = getattr(app, "_clock", None)
        t = float(clock()) if callable(clock) else time.monotonic()
        if not animated:
            t = 0.0
        out = [head, self._legend(data)]
        out.extend(self._canvas(data, width, t, animated))
        letter = data.letters[-1] if data.letters else None
        if letter:
            out.append("✉ " + letter["from"] + " → " + letter["to"] + ": " +
                       letter["text"][:max(10, width - 24)])
        out.append("Enter — приближение по звеньям")
        return out

    def _legend(self, data) -> str:
        counts = {"run": 0, "wait": 0, "done": 0, "failed": 0, "skipped": 0}
        for state in data.cells.values():
            if state in counts:
                counts[state] += 1
        return ("действует " + str(counts["run"]) + " · ждёт " + str(counts["wait"]) +
                " · сдало " + str(counts["done"]) + " · упало " + str(counts["failed"]))

    def _zoom_lines(self, data) -> list:
        rows = ["Звено " + str(self.zoom_link) + " — ячейки:"]
        for addr, state in data.cells.items():
            if self._link_of(addr) == self.zoom_link:
                rows.append("  " + addr + " — " + _STATE_RU.get(state, state))
        if len(rows) == 1:
            rows.append("  (пусто)")
        rows.append("Enter — следующее звено / обзор")
        return rows

    def _canvas(self, data, width: int, t: float, animated: bool) -> list:
        width_dots = max(2, width * 2)
        height_dots = _CANVAS_ROWS * 4
        grid: dict = {}  # (x, y) -> уровень яркости

        def put(x: int, y: int, level: int) -> None:
            if 0 <= x < width_dots and 0 <= y < height_dots:
                if level > grid.get((x, y), -1):
                    grid[(x, y)] = level

        addrs = list(data.cells.items())
        positions = {}
        for index, (addr, state) in enumerate(addrs):
            x, y = _star_pos(index, width_dots, height_dots)
            positions[addr] = (x, y)
            put(x, y, _level(state, index, t, animated))
            if state == "done":
                for step in (1, 2, 3):  # след-метеор по диагонали
                    put(x - step, y + step, 1 if step < 3 else 0)
        # Письма: бегущая точка между звёздами (у хозяина потока — угол поля).
        if data.letters:
            letter = data.letters[-1]
            start = positions.get(letter["from"], (1, 1))
            end = positions.get(letter["to"], (1, 1))
            if start != end:
                phase = (t * 0.45) % 1.0 if animated else 0.5
                x = round(start[0] + (end[0] - start[0]) * phase)
                y = round(start[1] + (end[1] - start[1]) * phase)
                put(x, y, 5)
                put(1, 1, 2)  # хозяин потока — leo/0 в углу
        lines = []
        for row in range(_CANVAS_ROWS):
            chars = []
            for col in range(width):
                bits = 0
                for (dx, dy), bit in _BITS.items():
                    level = grid.get((col * 2 + dx, row * 4 + dy), -1)
                    if level >= 0:
                        order_pos = _ORDER.index((dx, dy))
                        if order_pos < _LEVEL_DOTS[level]:
                            bits |= bit
                chars.append(chr(0x2800 + bits))
            lines.append("".join(chars))
        return lines


register_panel("constellation", "Созвездие", ConstellationPanel,
               aliases=("созвездие", "рой", "swarm", "constellation"))
