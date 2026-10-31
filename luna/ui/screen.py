"""Буфер ячеек и дифф-вывод: в терминал уходят только изменившиеся ячейки.

Ячейка — кортеж (символ, fg, bg, attrs). Широкий символ (CJK, эмодзи) занимает
две ячейки: свою и следующую с символом "" (продолжение). Двойной буфер:
Screen.present сравнивает новый буфер с показанным и собирает весь кадр в
одну строку — одна запись на кадр, без мерцания.

Ширине глифов терминала доверять нельзя (ConPTY считает эмодзи по-своему):
после каждого широкого символа present ставит курсор заново абсолютной
координатой на следующую ожидаемую ячейку, поэтому чужая ошибка ширины
умирает на одном глифе, а не расползается по всему спану.
"""

from __future__ import annotations

import os
import time

from . import color as _color
from .width import char_width

MARKER_ENV = "LUNA_FIRST_FRAME_FILE"  # протокол замеров, см. devtools.measure

BLANK = (" ", None, None, 0)


class Buffer:
    def __init__(self, cols: int, rows: int):
        self.cols = max(1, cols)
        self.rows = max(1, rows)
        self.cells = [[BLANK] * self.cols for _ in range(self.rows)]

    def resize(self, cols: int, rows: int) -> None:
        cols, rows = max(1, cols), max(1, rows)
        new = [[BLANK] * cols for _ in range(rows)]
        for y in range(min(rows, self.rows)):
            for x in range(min(cols, self.cols)):
                new[y][x] = self.cells[y][x]
        self.cols, self.rows, self.cells = cols, rows, new

    def cell(self, x: int, y: int):
        return self.cells[y][x]

    def clear(self, bg=None) -> None:
        blank = (" ", None, bg, 0)
        self.cells = [[blank] * self.cols for _ in range(self.rows)]

    def set(self, x: int, y: int, ch: str, fg=None, bg=None, attrs: int = 0) -> None:
        if not (0 <= x < self.cols and 0 <= y < self.rows) or not ch:
            return
        w = char_width(ch)
        if w == 0:
            return
        if w == 2 and x + 1 >= self.cols:
            ch, w = " ", 1  # широкий символ у правого края не влезает
        self._unwide(x, y)
        if w == 2:
            self._unwide(x + 1, y)
        self.cells[y][x] = (ch, fg, bg, attrs)
        if w == 2:
            self.cells[y][x + 1] = ("", fg, bg, attrs)

    def _unwide(self, x: int, y: int) -> None:
        """Если в ячейке половина широкого символа — стереть и вторую половину."""
        ch, fg, bg, attrs = self.cells[y][x]
        if ch == "" and x > 0:
            self.cells[y][x - 1] = (" ", fg, bg, attrs)
        elif char_width(ch) == 2 and x + 1 < self.cols and self.cells[y][x + 1][0] == "":
            self.cells[y][x + 1] = (" ", fg, bg, attrs)

    def text(self, x: int, y: int, s: str, fg=None, bg=None, attrs: int = 0) -> int:
        """Написать строку с учётом ширины символов; вернуть занятую ширину."""
        used = 0
        for ch in s:
            if x + used >= self.cols:
                break
            w = char_width(ch)
            if w == 0:
                continue
            self.set(x + used, y, ch, fg, bg, attrs)
            used += w
        return used

    def fill_rect(self, x: int, y: int, w: int, h: int, ch: str = " ",
                  fg=None, bg=None, attrs: int = 0) -> None:
        for yy in range(y, min(y + h, self.rows)):
            for xx in range(x, min(x + w, self.cols)):
                self.set(xx, yy, ch, fg, bg, attrs)

    def gradient_rect(self, x: int, y: int, w: int, h: int, stops,
                      vertical: bool = False, ch: str = " ") -> None:
        """Прямоугольник, залитый градиентом (по строке или по столбцу) как фон."""
        if w <= 0 or h <= 0:
            return
        if vertical:
            line = _color.gradient(stops, h)
            for i in range(h):
                self.fill_rect(x, y + i, w, 1, ch, bg=line[i])
        else:
            line = _color.gradient(stops, w)
            for i in range(w):
                self.fill_rect(x + i, y, 1, h, ch, bg=line[i])


class Screen:
    """Показывает буферы в вывод write(str) одной записью на кадр."""

    def __init__(self, write, level: str, cols: int, rows: int):
        self._write = write
        self.level = level
        self.cols = max(1, cols)
        self.rows = max(1, rows)
        self._prev: Buffer | None = None
        self._cursor_shown = False
        self._mark_path = os.environ.get(MARKER_ENV) or None
        self._marked = False

    def resize(self, cols: int, rows: int) -> None:
        self.cols, self.rows = max(1, cols), max(1, rows)
        self._prev = None  # перерисовать всё

    def present(self, buf: Buffer, cursor: tuple[int, int] | None = None) -> str:
        """Сравнить с предыдущим кадром и вывести разницу. Возвращает строку кадра."""
        prev = self._prev
        full = prev is None or prev.cols != buf.cols or prev.rows != buf.rows
        out: list[str] = []
        cur_style = None

        def style_of(cell) -> str:
            return _color.sgr(cell[1], cell[2], cell[3], self.level)

        for y in range(buf.rows):
            row_new = buf.cells[y]
            row_old = prev.cells[y] if (prev is not None and y < prev.rows) else None
            if full or row_old is None:
                changed = [True] * buf.cols
            else:
                changed = [row_new[x] != row_old[x] for x in range(buf.cols)]
            x = 0
            while x < buf.cols:
                if not changed[x]:
                    x += 1
                    continue
                start = end = x
                while end + 1 < buf.cols and changed[end + 1]:
                    end += 1
                # Спан не должен разрезать широкий символ — ни новый, ни старый.
                while start > 0 and (row_new[start][0] == ""
                                     or (row_old is not None and row_old[start][0] == "")):
                    start -= 1
                while end + 1 < buf.cols and (char_width(row_new[end][0]) == 2
                                              or (row_old is not None
                                                  and char_width(row_old[end][0]) == 2)):
                    end += 1
                out.append(f"\x1b[{y + 1};{start + 1}H")
                cur_style = None
                for xx in range(start, end + 1):
                    cell = row_new[xx]
                    if cell[0] == "":
                        continue
                    st = style_of(cell)
                    if st != cur_style:
                        out.append(st)
                        cur_style = st
                    out.append(cell[0])
                    if xx < end and char_width(cell[0]) == 2:
                        # Якорь после широкого символа: курсор — абсолютно
                        # на следующую ячейку по нашей модели; если терминал
                        # продвинул его иначе, дальше всё равно не уедет.
                        out.append(f"\x1b[{y + 1};{xx + 3}H")
                x = end + 1
        if cursor is not None:
            cx, cy = cursor
            out.append(f"\x1b[{cy + 1};{cx + 1}H\x1b[?25h")
            self._cursor_shown = True
        elif self._cursor_shown:
            out.append("\x1b[?25l")
            self._cursor_shown = False
        if cur_style is not None:
            out.append("\x1b[0m")
        frame = "".join(out)
        self._prev = _copy(buf)
        if frame:
            self._write(frame)
            self._note_first_frame()
        return frame

    def _note_first_frame(self) -> None:
        """Метка первого кадра для замеров: если задан LUNA_FIRST_FRAME_FILE,
        один раз пишем туда time.monotonic() первой записи кадра.
        Часы монотонные системные — родитель сравнивает со своими.
        Без переменной окружения — полное бездействие."""
        if self._marked or not self._mark_path:
            return
        self._marked = True
        try:
            with open(self._mark_path, "w", encoding="ascii") as f:
                f.write(repr(time.monotonic()))
        except OSError:
            pass


def _copy(buf: Buffer) -> Buffer:
    dup = Buffer(buf.cols, buf.rows)
    dup.cells = [row[:] for row in buf.cells]
    return dup
