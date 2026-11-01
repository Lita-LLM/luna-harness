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
        # Readline-правка (ЗАДАЧА-20): кольцо вырезанного (Ctrl+Y — вернуть)
        # и стек отмены правок (Ctrl+_ — назад по снапшотам).
        self.kill_ring: list = []
        self._undo: list = []
        self._undo_lock = False

    # --- отмена правок ------------------------------------------------------

    def _snapshot(self) -> None:
        """Снапшот перед изменением; серия одного рода действий — один шаг."""
        if self._undo_lock:
            return
        state = (self.text, self.cursor)
        if self._undo and self._undo[-1] == state:
            return
        self._undo.append(state)
        if len(self._undo) > 100:
            self._undo.pop(0)

    def undo_edit(self) -> bool:
        """Откатить последнюю правку поля ввода (Ctrl+_ / Ctrl+Shift+-)."""
        if not self._undo:
            return False
        self._undo_lock = True
        try:
            self.text, self.cursor = self._undo.pop()
        finally:
            self._undo_lock = False
        self._hist_idx = None
        return True

    # --- базовые операции ---------------------------------------------------

    def clear(self) -> None:
        self._snapshot()
        self.text = ""
        self.cursor = 0
        self._hist_idx = None
        self._draft = ""

    def set_text(self, text: str) -> None:
        self._snapshot()
        self.text = text
        self.cursor = len(text)
        self._hist_idx = None

    def insert(self, text: str) -> None:
        self._snapshot()
        self._hist_idx = None
        self.text = self.text[:self.cursor] + text + self.text[self.cursor:]
        self.cursor += len(text)

    def newline(self) -> None:
        self.insert("\n")

    def backspace(self) -> None:
        if self.cursor > 0:
            self._snapshot()
            self.text = self.text[:self.cursor - 1] + self.text[self.cursor:]
            self.cursor -= 1

    def delete(self) -> None:
        if self.cursor < len(self.text):
            self._snapshot()
            self.text = self.text[:self.cursor] + self.text[self.cursor + 1:]

    # --- readline: слова, вырезание, кольцо (ЗАДАЧА-20) ----------------------

    def word_left(self) -> None:
        """Курсор — на начало предыдущего слова (Alt+←)."""
        i = self.cursor
        while i > 0 and self.text[i - 1].isspace():
            i -= 1
        while i > 0 and not self.text[i - 1].isspace():
            i -= 1
        self.cursor = i

    def word_right(self) -> None:
        """Курсор — на начало следующего слова (Alt+→)."""
        i = self.cursor
        n = len(self.text)
        while i < n and not self.text[i].isspace():
            i += 1
        while i < n and self.text[i].isspace():
            i += 1
        self.cursor = i

    def _kill(self, a: int, b: int) -> None:
        """Вырезать [a..b) в кольцо; курсор — в a."""
        if a >= b:
            return
        self._snapshot()
        self.kill_ring.append(self.text[a:b])
        if len(self.kill_ring) > 20:
            self.kill_ring.pop(0)
        self.text = self.text[:a] + self.text[b:]
        self.cursor = a
        self._hist_idx = None

    def kill_to_start(self) -> None:
        """Ctrl+U: от курсора до начала строки — в кольцо."""
        start = self.text.rfind("\n", 0, self.cursor) + 1
        self._kill(start, self.cursor)

    def kill_to_end(self) -> None:
        """Ctrl+K: от курсора до конца строки — в кольцо."""
        end = self.text.find("\n", self.cursor)
        if end < 0:
            end = len(self.text)
        self._kill(self.cursor, end)

    def kill_word_back(self) -> None:
        """Ctrl+W: слово назад целиком (как в Claude Code — один путь за
        нажатие): от курсора до начала предыдущего слова, с пробелами."""
        if self.cursor == 0:
            return
        start = self.cursor
        self.word_left()
        j, self.cursor = self.cursor, start
        self._kill(j, start)

    def yank(self) -> None:
        """Ctrl+Y: вставить последнее вырезанное из кольца."""
        if self.kill_ring:
            self.insert(self.kill_ring[-1])

    def push_history(self, text: str) -> None:
        """Положить текст в историю ввода (его достанут стрелкой вверх)."""
        if text.strip() and (not self.history or self.history[-1] != text):
            self.history.append(text)

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
