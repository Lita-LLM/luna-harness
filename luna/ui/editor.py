"""Многострочный редактор поля ввода чата.

Текст и курсор (позиция в символах), история отправленных сообщений
(стрелки вверх/вниз, черновик не теряется), вставка как есть — с новыми
строками. Раскладка по визуальным строкам считается от ширины поля и не
режет широкие символы.
"""

from __future__ import annotations

from .width import str_width


class TextEditor:
    def __init__(self):
        self.text = ""
        self.cursor = 0
        self.history: list = []
        self._hist_idx: int | None = None
        self._draft = ""

    # --- базовые операции ---------------------------------------------------

    def clear(self) -> None:
        self.text = ""
        self.cursor = 0
        self._hist_idx = None
        self._draft = ""

    def set_text(self, text: str) -> None:
        self.text = text
        self.cursor = len(text)
        self._hist_idx = None

    def insert(self, text: str) -> None:
        self._hist_idx = None
        self.text = self.text[:self.cursor] + text + self.text[self.cursor:]
        self.cursor += len(text)

    def newline(self) -> None:
        self.insert("\n")

    def backspace(self) -> None:
        if self.cursor > 0:
            self.text = self.text[:self.cursor - 1] + self.text[self.cursor:]
            self.cursor -= 1

    def delete(self) -> None:
        if self.cursor < len(self.text):
            self.text = self.text[:self.cursor] + self.text[self.cursor + 1:]

    def left(self) -> None:
        self.cursor = max(0, self.cursor - 1)

    def right(self) -> None:
        self.cursor = min(len(self.text), self.cursor + 1)

    def line_home(self) -> None:
        self.cursor = self.text.rfind("\n", 0, self.cursor) + 1

    def line_end(self) -> None:
        nxt = self.text.find("\n", self.cursor)
        self.cursor = len(self.text) if nxt < 0 else nxt

    def _visual_column(self) -> int:
        start = self.text.rfind("\n", 0, self.cursor) + 1
        return str_width(self.text[start:self.cursor])

    def up(self) -> bool:
        """Курсор строкой выше; False — строки выше нет (зовущий смотрит историю)."""
        start = self.text.rfind("\n", 0, self.cursor) + 1
        if start == 0:
            return False
        col = self._visual_column()
        prev_end = start - 1
        prev_start = self.text.rfind("\n", 0, prev_end) + 1
        pos = prev_start
        used = 0
        while pos < prev_end:
            w = str_width(self.text[pos])
            if used + w > col:
                break
            used += w
            pos += 1
        self.cursor = pos
        return True

    def down(self) -> bool:
        """Курсор строкой ниже; False — строки ниже нет."""
        nxt = self.text.find("\n", self.cursor)
        if nxt < 0:
            return False
        col = self._visual_column()
        start = nxt + 1
        end = self.text.find("\n", start)
        if end < 0:
            end = len(self.text)
        pos = start
        used = 0
        while pos < end:
            w = str_width(self.text[pos])
            if used + w > col:
                break
            used += w
            pos += 1
        self.cursor = pos
        return True

    # --- история ---------------------------------------------------------------

    def submit(self) -> str:
        """Отправить: текст уходит, сообщение — в историю."""
        text = self.text
        if text.strip():
            if not self.history or self.history[-1] != text:
                self.history.append(text)
        self.clear()
        return text

    def history_prev(self) -> bool:
        if not self.history:
            return False
        if self._hist_idx is None:
            self._draft = self.text
            self._hist_idx = len(self.history) - 1
        elif self._hist_idx > 0:
            self._hist_idx -= 1
        else:
            return True
        self.text = self.history[self._hist_idx]
        self.cursor = len(self.text)
        return True

    def history_next(self) -> bool:
        if self._hist_idx is None:
            return False
        if self._hist_idx < len(self.history) - 1:
            self._hist_idx += 1
            self.text = self.history[self._hist_idx]
        else:
            self._hist_idx = None
            self.text = self._draft
        self.cursor = len(self.text)
        return True

    # --- раскладка ----------------------------------------------------------------

    def visual_lines(self, width: int) -> tuple:
        """(строки показа, (строка, колонка) курсора) при ширине поля width.

        Перенос в поле ввода — жёсткий, по ячейкам и без потери символов
        (пробелы — тоже текст): смещения строк поэтому точны, и курсор
        считается честно по символам до него.
        """
        width = max(1, width)
        lines: list = []
        spans: list = []  # (начало в тексте, длина) каждой визуальной строки
        offset = 0
        for chunk in self.text.split("\n"):
            if not chunk:
                lines.append("")
                spans.append((offset, 0))
            else:
                cur = ""
                cur_w = 0
                start = offset
                for ch in chunk:
                    w = str_width(ch)
                    if cur and cur_w + w > width:
                        lines.append(cur)
                        spans.append((start, len(cur)))
                        start += len(cur)
                        cur, cur_w = "", 0
                    cur += ch
                    cur_w += w
                lines.append(cur)
                spans.append((start, len(cur)))
            offset += len(chunk) + 1
        cursor_pos = (len(lines) - 1, str_width(lines[-1]) if lines else 0)
        for idx, (start, length) in enumerate(spans):
            if start <= self.cursor <= start + length:
                cursor_pos = (idx, str_width(lines[idx][:self.cursor - start]))
                break
        if self.text.endswith("\n"):
            cursor_pos = (len(lines) - 1, 0) if self.cursor == len(self.text) else cursor_pos
        return lines, cursor_pos
