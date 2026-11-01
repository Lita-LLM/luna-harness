# Перенесено из BENDER: bender/tools/registry.py (коммит 6552ed660bfabf5202569f9905212b77dbf6d31a), сокращено.
"""Реестр тулов: имя, описание, JSON-схема, признак «меняет систему», обработчик.

intent не живёт здесь: его добавляет адаптер провайдера в схему каждого тула,
а цикл агента достаёт его из вызова модели.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..router.types import ToolSpec

WRITE_TOOLS = {"bash", "write", "edit"}


@dataclass
class ToolResult:
    ok: bool
    text: str
    data: dict | None = None


@dataclass
class ToolContext:
    cwd: Path
    settings: object = None
    secrets: object = None
    ask_user_fn: Callable | None = None   # (question, options) -> str | None
    output_dir: Path | None = None        # куда складывать полный вывод при обрезке
    web_fetcher: Callable | None = None   # (url) -> str, подмена сети в тестах
    mcp_caller: Callable | None = None    # (url, headers, tool, args) -> dict:
                                          # подмена MCP-вызовов поиска в тестах
    todos: list = field(default_factory=list)
    background: dict = field(default_factory=dict)
    agent: object = None                  # агент-владелец (для тулов детей: assistant, crew)
    cancel_event: object = None           # threading.Event прерывания хода (ЗАДАЧА-18):
                                          # ставит агент; долгие тулы (bash) рвут работу
    sandbox: object = None                # рецепт изоляции bash (ЗАДАЧА-21):
                                          # ставит только исполнитель ячеек роя
                                          # (luna.starfall.sandbox.SandboxSpec);
                                          # у обычного агента всегда None


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    handler: Callable  # (ctx, args) -> ToolResult
    mutating: bool = False
    # (args) -> bool: точнее общего флага, когда один тул и читает, и меняет
    # (manner: list/show — чтение, use/create/edit — изменение).
    mutating_for: Callable | None = None

    def is_mutating(self, args: dict) -> bool:
        if self.mutating_for is not None:
            return bool(self.mutating_for(args or {}))
        return self.mutating

    def spec(self) -> ToolSpec:
        return ToolSpec(self.name, self.description, self.parameters)

    def run(self, ctx: ToolContext, args: dict) -> ToolResult:
        try:
            return self.handler(ctx, args or {})
        except Exception as exc:  # noqa: BLE001 — тул не роняет цикл, а докладывает
            return ToolResult(False, "Ошибка тула " + self.name + ": " + str(exc))


def truncate_output(text: str, limit: int, output_dir: Path | None, name: str) -> tuple[str, str | None]:
    """Обрезать большой вывод: начало и хвост в ответ, полный — в файл."""
    if limit <= 0 or len(text) <= limit:
        return text, None
    path = None
    if output_dir is not None:
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%H%M%S") + "-" + uuid.uuid4().hex[:6]
            target = output_dir / ("вывод-" + name + "-" + stamp + ".txt")
            target.write_text(text, encoding="utf-8")
            path = str(target)
        except OSError:
            path = None
    half = max(200, limit // 2)
    note = "\n…[обрезано: " + str(len(text)) + " символов"
    if path:
        note += ", полный вывод в файле " + path
    note += "]…\n"
    return text[:half] + note + text[-half:], path


# Поколение конфигов MCP: тул luna после mcp_add/mcp_remove поднимает его,
# и реестры перечитывают серверы при следующем обращении (без перезапуска).
_MCP_GENERATION = 0


def request_mcp_reload() -> None:
    global _MCP_GENERATION
    _MCP_GENERATION += 1


class LazyRegistry(dict):
    """Реестр с ленивой догрузкой MCP-тулов при первом обращении.

    build_registry() вызывается при создании агента, а MCP-серверы обязаны
    подниматься только при первом ходе (ЗАДАЧА-09): словарь обычный на вид,
    но первое чтение (values/get/items/…) один раз догружает MCP-тулы.
    Сбой догрузки не роняет встроенные тулы: MCP просто не появится, а
    причины останутся в mcp_errors для диагностики.
    """

    def __init__(self, base: dict):
        super().__init__(base)
        self._mcp_loaded = False
        self._mcp_lock = threading.Lock()
        self.mcp_errors: list = []
        self.mcp_manager = None
        self._mcp_generation = _MCP_GENERATION

    def _ensure_mcp(self) -> None:
        if self._mcp_generation != _MCP_GENERATION:
            self._drop_mcp()
        if self._mcp_loaded:
            return
        with self._mcp_lock:
            if self._mcp_loaded:
                return
            self._mcp_loaded = True
            try:
                tools, errors, manager = _load_mcp_tools()
            except Exception:  # noqa: BLE001 — реестр встроенных тулов важнее MCP
                return
            self.mcp_errors = errors
            self.mcp_manager = manager
            for name, tool in tools.items():
                self.setdefault(name, tool)

    def _drop_mcp(self) -> None:
        """Забыть MCP-тулы и серверы: следующее обращение поднимет их заново."""
        with self._mcp_lock:
            self._mcp_generation = _MCP_GENERATION
            for name in [n for n in dict.keys(self) if n.startswith("mcp__")]:
                dict.pop(self, name, None)
            manager, self.mcp_manager = self.mcp_manager, None
            self._mcp_loaded = False
        closer = getattr(manager, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:  # noqa: BLE001 — упавший сервер не мешает перечитать конфиг
                pass

    def values(self):
        self._ensure_mcp()
        return super().values()

    def items(self):
        self._ensure_mcp()
        return super().items()

    def keys(self):
        self._ensure_mcp()
        return super().keys()

    def get(self, key, default=None):
        self._ensure_mcp()
        return super().get(key, default)

    def __getitem__(self, key):
        self._ensure_mcp()
        return super().__getitem__(key)

    def __contains__(self, key):
        self._ensure_mcp()
        return super().__contains__(key)

    def __iter__(self):
        self._ensure_mcp()
        return super().__iter__()

    def __len__(self):
        self._ensure_mcp()
        return super().__len__()


def _load_mcp_tools() -> tuple:
    """Догрузка MCP: нет конфигов — пусто и мгновенно; есть — поднять серверы и перечислить тулы."""
    from ..core.paths import luna_home
    from ..mcp.manager import Manager, build_mcp_tools, has_mcp_config

    home = luna_home()
    cwd = Path.cwd()
    if not has_mcp_config(cwd, home):
        return {}, [], None
    manager = Manager(cwd=cwd, home=home)
    tools, errors = build_mcp_tools(manager)
    return tools, errors, manager


def build_registry() -> dict[str, Tool]:
    """Все встроенные тулы вехи 1; MCP-тулы догружаются лениво (LazyRegistry)."""
    from ..agents.tools import assistant_tool, crew_tool
    from ..starfall.tools import cells_tool, starfall_tool
    from .files import edit_tool, glob_tool, grep_tool, read_tool, write_tool
    from .luna_tool import luna_tool, skill_tool
    from .manner import manner_tool
    from .shell import bash_tool
    from .web import ask_user_tool, todo_tool, web_fetch_tool, web_search_tool

    tools = [bash_tool(), read_tool(), write_tool(), edit_tool(), glob_tool(),
             grep_tool(), web_fetch_tool(), web_search_tool(), todo_tool(), ask_user_tool(),
             manner_tool(), luna_tool(), skill_tool(), assistant_tool(), crew_tool(),
             cells_tool(), starfall_tool()]
    return LazyRegistry({tool.name: tool for tool in tools})
