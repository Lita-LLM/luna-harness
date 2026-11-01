"""Панель «Дерево файлов» (ЗАДАЧА-16): каталог сессии деревом.

Файлы, которых касалась сессия (PanelData.files), помечены; Enter в фокусе
вставляет «@путь» в поле ввода — вложить файл в следующее сообщение.
Обход дерева — не чаще раза в 3 секунды и только пока панель открыта,
глубина и число строк ограничены, служебные каталоги пропущены.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from . import PanelContent, register_panel
from .builtin import panel_data

_REFRESH_SEC = 3.0
_MAX_ROWS = 400
_MAX_DEPTH = 5
_SKIP = {".git", "__pycache__", "node_modules", ".venv", "venv",
         ".idea", ".mypy_cache", ".pytest_cache", ".tox"}


def _changed_set(app) -> set:
    """Пути изменённых/прочитанных сессией файлов — в относительном виде."""
    cwd = Path(str(getattr(app, "cwd", "") or "."))
    out = set()
    for raw in panel_data(app).files:
        path = Path(raw)
        try:
            rel = path.relative_to(cwd) if path.is_absolute() else path
        except ValueError:
            continue
        out.add(rel.as_posix())
    return out


class TreePanel(PanelContent):
    """Дерево каталога сессии с пометками файлов, тронутых сессией."""

    def __init__(self):
        super().__init__("Дерево файлов")
        self.selected = 0
        self._cache_at = 0.0
        self._rows: list = []  # (глубина, относительный путь, это каталог)

    def _refresh(self, app) -> list:
        now = time.monotonic()
        if self._rows and now - self._cache_at < _REFRESH_SEC:
            return self._rows
        self._cache_at = now
        root = Path(str(getattr(app, "cwd", "") or "."))
        rows: list = []

        def walk(directory: Path, depth: int) -> None:
            if len(rows) >= _MAX_ROWS or depth > _MAX_DEPTH:
                return
            try:
                names = sorted(os.listdir(directory))
            except OSError:
                return
            dirs = [n for n in names
                    if (directory / n).is_dir() and n not in _SKIP]
            files = [n for n in names if not (directory / n).is_dir()]
            for name in dirs + files:
                if len(rows) >= _MAX_ROWS:
                    return
                path = directory / name
                try:
                    rel = path.relative_to(root).as_posix()
                except ValueError:
                    continue
                is_dir = name in dirs
                rows.append((depth, rel, is_dir))
                if is_dir:
                    walk(path, depth + 1)

        walk(root, 0)
        self._rows = rows
        return rows

    def lines(self, app, width: int) -> list:
        rows = self._refresh(app)
        if not rows:
            return ["каталог пуст или недоступен"]
        changed = _changed_set(app)
        self.selected = max(0, min(self.selected, len(rows) - 1))
        height = self.height_hint or 24
        start = max(0, min(self.selected - height + 1,
                           max(0, len(rows) - height)))
        out = []
        for i in range(start, min(len(rows), start + height)):
            depth, rel, is_dir = rows[i]
            name = rel.rsplit("/", 1)[-1] + ("/" if is_dir else "")
            mark = "▸ " if i == self.selected else "  "
            note = " · тронут сессией" if rel in changed else ""
            out.append(mark + "  " * depth + name + note)
        out.append("Enter — вложить @файл в ввод")
        return out

    def handle_key(self, app, key) -> bool:
        rows = self._refresh(app)
        if not rows:
            return False
        self.selected = max(0, min(self.selected, len(rows) - 1))
        if key.name == "up":
            self.selected = max(0, self.selected - 1)
            return True
        if key.name == "down":
            self.selected = min(len(rows) - 1, self.selected + 1)
            return True
        if key.name == "pageup":
            self.selected = max(0, self.selected - 10)
            return True
        if key.name == "pagedown":
            self.selected = min(len(rows) - 1, self.selected + 10)
            return True
        if key.name == "enter":
            _depth, rel, is_dir = rows[self.selected]
            if is_dir:
                return True
            editor = getattr(app, "editor", None)
            if editor is not None:
                editor.insert("@" + rel + " ")
            app.panel_focus = False
            return True
        return False


register_panel("tree", "Дерево файлов", TreePanel,
               aliases=("tree", "дерево"))
