"""Панели-тайлинг: дерево разбиений экрана (docs/ДИЗАЙН.md, «Панели»).

Механизм, а не конкретный экран: дерево сплитов с весами, фокус, открыть /
закрыть / переставить / растянуть / развернуть, сохранённые раскладки
(сериализация в dict) и реестр панелей — любой модуль регистрирует свою
панель фабрикой и она появляется в дереве наравне со встроенными.
По умолчанию дерево пусто: одна лента, минимализм.

Встроенные панели с наполнением — в panels/builtin.py (Луна, Лимиты,
Обратная сторона, Файлы, Ниблер): данные они берут из событий шины через
PanelData. Новая панель — один модуль с register_panel, движок трогать
не нужно (так позже добавится «Рой»).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PanelSpec:
    panel_id: str
    title: str
    factory: object  # callable() -> PanelContent
    aliases: tuple = ()


class PanelContent:
    """Содержимое панели: строки на ширину. База — статичный текст.

    height_hint ставит экран перед lines(): сколько строк влезет в панель
    (для панелей-журналов, показывающих хвост). handle_key — клавиши
    панели в режиме фокуса (F6): True — клавиша съедена панелью.
    """

    height_hint: int = 0

    def __init__(self, title: str, lines_provider=None):
        self.title = title
        self._provider = lines_provider

    def lines(self, app, width: int) -> list:
        if self._provider is None:
            return []
        return list(self._provider(app, width))

    def handle_key(self, app, key) -> bool:
        return False


_REGISTRY: dict = {}


def register_panel(panel_id: str, title: str, factory, aliases: tuple = ()) -> PanelSpec:
    """Зарегистрировать панель модуля: дальше она открывается как встроенная."""
    spec = PanelSpec(panel_id, title, factory, tuple(aliases))
    _REGISTRY[panel_id] = spec
    return spec


def panel_spec(panel_id: str):
    return _REGISTRY.get(panel_id)


def registered_panels() -> dict:
    return dict(_REGISTRY)


def find_panel(query: str):
    """Панель по id, названию или алиасу (без учёта регистра) — для /panel <имя>."""
    q = (query or "").strip().lower()
    if not q:
        return None
    for spec in _REGISTRY.values():
        if q == spec.panel_id or q == spec.title.lower() or q in spec.aliases:
            return spec
    return None


def create_panel(panel_id: str):
    spec = _REGISTRY.get(panel_id)
    if spec is None:
        return None
    return spec.factory()


def _todo_lines(app, width: int) -> list:
    items = getattr(app, "todos", []) or []
    if not items:
        return ["плана пока нет"]
    out = []
    marks = {"done": "✓", "active": "◐", "pending": "○"}
    for item in items:
        mark = marks.get(item.get("status", "pending"), "○")
        text = item.get("text", "")
        out.append(mark + " " + text)
    return out


register_panel("todo", "План", lambda: PanelContent("План", _todo_lines),
               aliases=("план", "todo"))


# --- дерево -----------------------------------------------------------------

@dataclass
class Node:
    kind: str  # leaf | split
    panel_id: str = ""
    direction: str = "h"  # h — дети в ряд по горизонтали, v — стопкой
    children: list = field(default_factory=list)
    weights: list = field(default_factory=list)

    def leaves(self) -> list:
        if self.kind == "leaf":
            return [self.panel_id]
        out: list = []
        for child in self.children:
            out.extend(child.leaves())
        return out

    def to_dict(self) -> dict:
        if self.kind == "leaf":
            return {"kind": "leaf", "panel": self.panel_id}
        return {"kind": "split", "direction": self.direction,
                "weights": list(self.weights),
                "children": [c.to_dict() for c in self.children]}

    @staticmethod
    def from_dict(data: dict) -> "Node":
        if data.get("kind") == "leaf":
            return Node("leaf", panel_id=data.get("panel", ""))
        children = [Node.from_dict(c) for c in data.get("children", [])]
        weights = list(data.get("weights") or [1.0] * len(children))
        return Node("split", direction=data.get("direction", "h"),
                    children=children, weights=weights)


class LayoutTree:
    """Дерево панелей одной стороны экрана (в чате — правой колонки)."""

    def __init__(self):
        self.root: Node | None = None
        self.focused: str | None = None
        self.maximized: str | None = None
        self.saved: dict = {}

    def clone(self) -> "LayoutTree":
        """Независимая копия дерева (для анимации закрытия колонки)."""
        other = LayoutTree()
        other.root = Node.from_dict(self.root.to_dict()) if self.root is not None else None
        other.focused = self.focused
        other.maximized = self.maximized
        other.saved = dict(self.saved)
        return other

    # --- запросы -------------------------------------------------------------

    def leaves(self) -> list:
        return self.root.leaves() if self.root is not None else []

    def __contains__(self, panel_id: str) -> bool:
        return panel_id in self.leaves()

    def visible_leaves(self) -> list:
        if self.maximized is not None and self.maximized in self:
            return [self.maximized]
        return self.leaves()

    # --- операции -------------------------------------------------------------

    def open(self, panel_id: str, direction: str = "v") -> bool:
        """Открыть панель: первая — корнем, дальше — сплит с фокусной."""
        if panel_id in self:
            self.focused = panel_id
            return False
        leaf = Node("leaf", panel_id=panel_id)
        if self.root is None:
            self.root = leaf
        else:
            anchor = self.focused if self.focused in self else self.leaves()[-1]
            self.root = self._split_at(self.root, anchor, leaf, direction)
        self.focused = panel_id
        return True

    def _split_at(self, node: Node, anchor: str, leaf: Node, direction: str) -> Node:
        if node.kind == "leaf":
            if node.panel_id == anchor:
                return Node("split", direction=direction,
                            children=[node, leaf], weights=[1.0, 1.0])
            return node
        # Сплит того же направления, где якорь — прямой ребёнок: новая панель
        # встаёт соседом, а не делит якорь пополам. Иначе каждая следующая
        # панель получала половину предыдущей («лесенка»). Место делим поровну.
        if node.direction == direction:
            for i, child in enumerate(node.children):
                if child.kind == "leaf" and child.panel_id == anchor:
                    node.children.insert(i + 1, leaf)
                    node.weights = [1.0] * len(node.children)
                    return node
        node.children = [self._split_at(c, anchor, leaf, direction)
                         for c in node.children]
        return node

    def close(self, panel_id: str) -> bool:
        if panel_id not in self:
            return False
        if self.maximized == panel_id:
            self.maximized = None
        self.root = self._remove(self.root, panel_id)
        leaves = self.leaves()
        if self.focused == panel_id:
            self.focused = leaves[-1] if leaves else None
        return True

    def _remove(self, node: Node | None, panel_id: str):
        if node is None:
            return None
        if node.kind == "leaf":
            return None if node.panel_id == panel_id else node
        kept = []
        weights = []
        for child, weight in zip(node.children, node.weights):
            new_child = self._remove(child, panel_id)
            if new_child is not None:
                kept.append(new_child)
                weights.append(weight)
        if not kept:
            return None
        if len(kept) == 1:
            return kept[0]
        node.children, node.weights = kept, weights
        return node

    def toggle(self, panel_id: str) -> bool:
        """Открыть, если закрыта; закрыть, если открыта. True — теперь открыта."""
        if panel_id in self:
            self.close(panel_id)
            return False
        self.open(panel_id)
        return True

    def focus_next(self, step: int = 1) -> str | None:
        leaves = self.leaves()
        if not leaves:
            self.focused = None
            return None
        if self.focused not in leaves:
            self.focused = leaves[0]
        else:
            idx = leaves.index(self.focused)
            self.focused = leaves[(idx + step) % len(leaves)]
        return self.focused

    def _parent_of(self, panel_id: str):
        """(родитель-сплит, индекс ребёнка) для листа или (None, -1)."""
        def walk(node):
            if node.kind != "split":
                return None, -1
            for i, child in enumerate(node.children):
                if child.kind == "leaf" and child.panel_id == panel_id:
                    return node, i
                found, idx = walk(child)
                if found is not None:
                    return found, idx
            return None, -1

        if self.root is None:
            return None, -1
        return walk(self.root)

    def move(self, panel_id: str, step: int = 1) -> bool:
        """Переставить панель среди соседей по сплиту."""
        parent, idx = self._parent_of(panel_id)
        if parent is None:
            return False
        other = idx + step
        if other < 0 or other >= len(parent.children):
            return False
        parent.children[idx], parent.children[other] = (
            parent.children[other], parent.children[idx])
        parent.weights[idx], parent.weights[other] = (
            parent.weights[other], parent.weights[idx])
        return True

    def resize(self, panel_id: str, delta: float) -> bool:
        """Растянуть/сжать панель внутри её сплита (веса соседей сохраняют сумму)."""
        parent, idx = self._parent_of(panel_id)
        if parent is None or len(parent.children) < 2:
            return False
        other = idx + 1 if idx + 1 < len(parent.children) else idx - 1
        new_w = parent.weights[idx] + delta
        new_o = parent.weights[other] - delta
        if new_w < 0.2 or new_o < 0.2:
            return False
        parent.weights[idx], parent.weights[other] = new_w, new_o
        return True

    def toggle_maximize(self, panel_id: str) -> bool:
        if panel_id not in self:
            return False
        self.maximized = None if self.maximized == panel_id else panel_id
        self.focused = panel_id
        return True

    # --- раскладки ---------------------------------------------------------------

    def save_layout(self, name: str) -> None:
        self.saved[name] = {
            "tree": self.root.to_dict() if self.root is not None else None,
            "focused": self.focused,
            "maximized": self.maximized,
        }

    def load_layout(self, name: str) -> bool:
        data = self.saved.get(name)
        if data is None:
            return False
        tree = data.get("tree")
        self.root = Node.from_dict(tree) if tree else None
        self.focused = data.get("focused")
        self.maximized = data.get("maximized")
        if self.focused not in self:
            leaves = self.leaves()
            self.focused = leaves[0] if leaves else None
        return True

    def layout_names(self) -> list:
        return sorted(self.saved)

    # --- геометрия -----------------------------------------------------------------

    def rects(self, x: int, y: int, w: int, h: int) -> dict:
        """Прямоугольники видимых панелей: {panel_id: (x, y, w, h)}."""
        out: dict = {}
        if self.root is None or w <= 0 or h <= 0:
            return out
        if self.maximized is not None and self.maximized in self:
            out[self.maximized] = (x, y, w, h)
            return out
        self._place(self.root, x, y, w, h, out)
        return out

    def _place(self, node: Node, x: int, y: int, w: int, h: int, out: dict) -> None:
        if node.kind == "leaf":
            out[node.panel_id] = (x, y, w, h)
            return
        total = sum(node.weights) or 1.0
        if node.direction == "h":
            cx = x
            for child, weight in zip(node.children, node.weights):
                cw = int(w * weight / total)
                if child is node.children[-1]:
                    cw = x + w - cx
                self._place(child, cx, y, max(1, cw), h, out)
                cx += cw
        else:
            cy = y
            for child, weight in zip(node.children, node.weights):
                ch = int(h * weight / total)
                if child is node.children[-1]:
                    ch = y + h - cy
                self._place(child, x, cy, w, max(1, ch), out)
                cy += ch


# Встроенные панели с наполнением: регистрация — импортом модуля.
from . import builtin as _builtin  # noqa: E402,F401
from .builtin import PanelData, panel_data  # noqa: E402,F401
