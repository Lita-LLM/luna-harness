"""Выбиралка: общий компонент списка поверх ленты (ЗАДАЧА-10).

Один компонент на все «выбрать из списка»: сессии (/sessions), манеры
(/manners), дальше — модели, MCP-серверы, настройки. Живёт без терминала:
состояние и клавиши — здесь, отрисовку делает хозяин (чат — своими
стилями поверх ленты, отдельная команда — здешним run_picker).

Строка списка — PickerItem: заголовок, подпись второй строкой, пометка
слева, превью текстом. Клавиши: ↑↓, PgUp/PgDn, Home/End, ввод фильтрует,
Enter — выбрать, Esc — закрыть. Хозяин может добавить свои клавиши
(extra: буква → действие) — они работают, пока фильтр пуст. Два особых
состояния: подтверждение («Enter — да, Esc — назад») и ввод одной строки
(имя новой манеры) — оба спрашивает хозяин через ask_confirm/ask_input.

Вид — по docs/ДИЗАЙН.md: мягкий блок без рамок, появление — затуханием
(хозяин знает born и красит по своей анимации).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import color as _color
from .keys import Key
from .markdown import wrap_text
from .screen import Buffer

DIM_FG = (0x8B, 0x94, 0xAC)
PICKER_BG = (0x10, 0x13, 0x24)
SELECT_BG = (0x16, 0x1B, 0x30)

# Роль отрезка строки → (цвет, атрибуты) для отдельной отрисовки.
# Чат красит те же роли своими стилями (см. ui/chat.py).
ROLE_PAINT = {
    "title": (_color.SILVER, _color.BOLD),
    "text": (_color.SILVER, 0),
    "dim": (DIM_FG, 0),
    "accent": (_color.CYAN, _color.BOLD),
    "mark": (_color.AMBER, _color.BOLD),
    "hint": (DIM_FG, _color.DIM),
}


@dataclass
class PickerItem:
    key: str                      # стабильный id для действий хозяина
    title: str                    # главная строка
    detail: str = ""              # вторая строка, приглушённо
    mark: str = ""                # пометка слева: «→» текущая, «·» свой каталог
    preview: str = ""             # текст превью выбранного пункта
    payload: object = None        # что хозяину нужно о пункте (путь, запись…)


class Picker:
    def __init__(self, title: str, items: list, *, hint: str = "",
                 extra: dict | None = None, preview_lines: int = 5,
                 now: float | None = None):
        self.title = title
        self.items = list(items)
        self.hint = hint
        self.extra = dict(extra or {})
        self.preview_lines = preview_lines
        self.born = time.monotonic() if now is None else now
        self.filter = ""
        self.scroll = 0
        self._sel_key = self.items[0].key if self.items else None
        self._confirm_prompt = ""
        self._confirm_action = None
        self._input_prompt = ""
        self._input_text = ""
        self._input_action = None

    # --- состояние ---------------------------------------------------------

    @property
    def mode(self) -> str:
        if self._confirm_prompt:
            return "confirm"
        if self._input_prompt:
            return "input"
        return "list"

    def set_items(self, items: list) -> None:
        """Обновить пункты (после создания/правки), выбор — по ключу."""
        self.items = list(items)
        keys = {it.key for it in self.items}
        if self._sel_key not in keys:
            self._sel_key = self.items[0].key if self.items else None

    def filtered(self) -> list:
        if not self.filter:
            return list(self.items)
        needle = self.filter.lower()
        return [it for it in self.items
                if needle in (it.title + " " + it.detail + " " + it.key).lower()]

    def current(self) -> PickerItem | None:
        items = self.filtered()
        for item in items:
            if item.key == self._sel_key:
                return item
        return items[0] if items else None

    def ask_confirm(self, prompt: str, action) -> None:
        self._confirm_prompt = prompt
        self._confirm_action = action

    def ask_input(self, prompt: str, action, initial: str = "") -> None:
        self._input_prompt = prompt
        self._input_text = initial
        self._input_action = action

    # --- клавиши -------------------------------------------------------------

    def handle_key(self, key: Key):
        """Действие хозяина или None: ("pick", item) · ("close",) ·
        ("action", имя, item) · ("confirmed", действие) · ("input", текст, контекст)."""
        if self.mode == "confirm":
            if key.name == "enter":
                action, self._confirm_action = self._confirm_action, None
                self._confirm_prompt = ""
                return ("confirmed", action)
            if key.name == "esc":
                self._confirm_prompt = ""
                self._confirm_action = None
            return None
        if self.mode == "input":
            if key.name == "enter":
                text = self._input_text.strip()
                action, self._input_action = self._input_action, None
                self._input_prompt = ""
                self._input_text = ""
                return ("input", text, action)
            if key.name == "esc":
                self._input_prompt = ""
                self._input_action = None
                return None
            if key.name == "backspace":
                self._input_text = self._input_text[:-1]
            elif key.char and not key.mods:
                self._input_text += key.char
            return None
        if key.name == "esc":
            return ("close",)
        if key.name == "enter":
            item = self.current()
            return ("pick", item) if item is not None else None
        if key.name in ("up", "down"):
            self._move(-1 if key.name == "up" else 1)
            return None
        if key.name in ("pageup", "pagedown"):
            self._move(-10 if key.name == "pageup" else 10)
            return None
        if key.name == "home":
            items = self.filtered()
            if items:
                self._sel_key = items[0].key
            return None
        if key.name == "end":
            items = self.filtered()
            if items:
                self._sel_key = items[-1].key
            return None
        if key.name == "backspace":
            self.filter = self.filter[:-1]
            self._fix_selection()
            return None
        if key.char and not key.mods:
            if not self.filter and key.char in self.extra:
                return ("action", self.extra[key.char], self.current())
            self.filter += key.char
            self._fix_selection()
        return None

    def _move(self, step: int) -> None:
        items = self.filtered()
        if not items:
            return
        idx = next((i for i, it in enumerate(items) if it.key == self._sel_key), 0)
        idx = max(0, min(len(items) - 1, idx + step))
        self._sel_key = items[idx].key

    def _fix_selection(self) -> None:
        items = self.filtered()
        if items and all(it.key != self._sel_key for it in items):
            self._sel_key = items[0].key

    # --- вид -------------------------------------------------------------------

    def render(self, width: int, max_rows: int) -> list:
        """Строки блока: список строк, строка — список (текст, роль)."""
        lines, _sel = self.render_block(width, max_rows)
        return lines

    def render_block(self, width: int, max_rows: int):
        """Строки блока и индекс выбранной строки (для подсветки хозяином)."""
        w = max(10, width)
        lines: list = []
        head = [(self.title, "title")]
        if self.filter:
            head.append(("  ·  фильтр: " + self.filter, "accent"))
        lines.append(head)
        if self.mode == "confirm":
            lines.append([(self._confirm_prompt, "accent")])
            lines.append([("Enter — да · Esc — назад", "hint")])
            return lines, None
        if self.mode == "input":
            lines.append([(self._input_prompt + self._input_text + "▌", "accent")])
            lines.append([("Enter — создать · Esc — назад", "hint")])
            return lines, None
        current = self.current()
        preview: list = []
        if current is not None and current.preview and self.preview_lines > 0:
            wrapped = wrap_text(current.preview.replace("\n", " "), w - 2)
            preview = [[("  " + line, "dim")]
                       for line in wrapped[:self.preview_lines]]
        budget = max(1, max_rows - len(lines) - len(preview) - 1)
        items = self.filtered()
        sel_row = None
        if not items:
            lines.append([("Ничего не нашлось.", "dim")])
        else:
            idx = next((i for i, it in enumerate(items)
                        if current is not None and it.key == current.key), 0)
            per = 2 if any(it.detail for it in items) else 1
            visible = max(1, budget // per)
            start = self.scroll
            if idx < start:
                start = idx
            if idx >= start + visible:
                start = idx - visible + 1
            start = max(0, min(start, max(0, len(items) - visible)))
            self.scroll = start
            for item in items[start:start + visible]:
                sel = current is not None and item.key == current.key
                row = []
                if item.mark:
                    row.append((item.mark + " ", "mark"))
                row.append((item.title, "accent" if sel else "text"))
                if sel:
                    sel_row = len(lines)
                lines.append(row)
                if per == 2:
                    lines.append([("  " + item.detail, "dim")] if item.detail
                                 else [("", "dim")])
        lines.extend(preview)
        if self.hint:
            lines.append([(self.hint, "hint")])
        return lines, sel_row


# --- пункты: сессии и манеры ---------------------------------------------------


def _one_line(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    if len(text) > limit:
        text = text[:max(1, limit - 1)] + "…"
    return text


def session_items(sessions: list, cwd: str, current_path=None) -> list:
    """Пункты выбиралки сессий из сводок core.sessions.list_sessions."""
    import time as _time

    items = []
    for s in sessions:
        title = s.get("title") or _one_line(s.get("first") or "", 56) or "сессия без реплик"
        when = _time.strftime("%d.%m %H:%M", _time.localtime(s.get("started") or 0))
        detail = (when + " · " + str(s.get("cwd") or "")
                  + " · ходов: " + str(s.get("turns") or 0))
        mark = ""
        if current_path is not None and str(s.get("path")) == str(current_path):
            mark = "→"
        elif s.get("cwd") == cwd:
            mark = "·"
        items.append(PickerItem(key=str(s.get("id")), title=title,
                                detail=detail, mark=mark, payload=s))
    return items


def manners_items(settings, cwd=None) -> list:
    """Пункты выбиралки манер: текущая отмечена, источник и путь — подписью."""
    from ..mind import manners

    current_id = str(settings.get("agent.manner", "") or "") if settings else ""
    items = []
    for entry in manners.library(settings, cwd):
        detail = manners.SOURCE_NAMES[entry["source"]]
        if entry["description"]:
            detail += " · " + entry["description"]
        if entry["source"] != "builtin":
            detail += " · " + str(entry.get("path") or "")
        items.append(PickerItem(
            key=entry["id"], title="«" + entry["name"] + "» (" + entry["id"] + ")",
            detail=detail, mark="→" if entry["id"] == current_id else "",
            preview=entry["text"][:600], payload=entry))
    return items


# --- отдельная полноэкранная выбиралка (для CLI-команд) -----------------------


def run_picker(picker: Picker, dispatch, *, refresh=None) -> object:
    """Выбиралка отдельным экраном: luna manners без аргументов в терминале.

    dispatch(действие, picker) -> "close" | None — хозяин применяет выбор;
    действие ("edit", ...) может само поставить терминал на паузу через
    term.suspend()/resume() — runner отдаёт ему терминал вторым аргументом
    при dispatch3: если dispatch принимает 3 аргумента, вызовется с ним.
    refresh() -> новый список пунктов после действий (создание, правка).
    Возвращает последнее действие.
    """
    import inspect

    from .term import Terminal

    term = Terminal()
    if not term.caps.is_tty:
        return None
    last = None
    with term:
        from .screen import Screen

        cols, rows = term.size()
        screen = Screen(term.write, term.caps.color_level, cols, rows)
        while True:
            size = term.size()
            if size != (screen.cols, screen.rows):
                screen.resize(*size)
            buf = Buffer(size[0], size[1])
            buf.clear(bg=_color.SPACE_BG)
            lines, sel_row = picker.render_block(max(10, size[0] - 4),
                                                 max(4, size[1] - 4))
            for row, spans in enumerate(lines):
                is_sel = row == sel_row
                buf.fill_rect(0, row + 1, size[0], 1, " ",
                              bg=SELECT_BG if is_sel else PICKER_BG)
                x = 2
                for text, role in spans:
                    fg, attrs = ROLE_PAINT.get(role, ROLE_PAINT["text"])
                    bg = SELECT_BG if is_sel else PICKER_BG
                    x += buf.text(x, row + 1, text, fg=fg, bg=bg, attrs=attrs)
            screen.present(buf, cursor=None)
            events = term.read_events(timeout=None)
            for ev in events:
                from .keys import Key as _Key

                if not isinstance(ev, _Key):
                    continue
                action = picker.handle_key(ev)
                if action is None:
                    continue
                last = action
                if len(inspect.signature(dispatch).parameters) >= 3:
                    result = dispatch(action, picker, term)
                else:
                    result = dispatch(action, picker)
                if refresh is not None:
                    picker.set_items(refresh())
                # Действие могло ставить терминал на паузу (редактор):
                # следующий кадр рисуем целиком, а не диффом.
                screen.resize(*term.size())
                if result == "close" or action[0] == "close":
                    return last
    return last
