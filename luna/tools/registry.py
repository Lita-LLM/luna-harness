# Перенесено из BENDER: bender/tools/registry.py (коммит 6552ed660bfabf5202569f9905212b77dbf6d31a), сокращено.
"""Реестр тулов: имя, описание, JSON-схема, признак «меняет систему», обработчик.

intent не живёт здесь: его добавляет адаптер провайдера в схему каждого тула,
а цикл агента достаёт его из вызова модели.
"""

from __future__ import annotations

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
    todos: list = field(default_factory=list)
    background: dict = field(default_factory=dict)


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


def build_registry() -> dict[str, Tool]:
    """Все встроенные тулы вехи 1."""
    from .files import edit_tool, glob_tool, grep_tool, read_tool, write_tool
    from .manner import manner_tool
    from .shell import bash_tool
    from .web import ask_user_tool, todo_tool, web_fetch_tool, web_search_tool

    tools = [bash_tool(), read_tool(), write_tool(), edit_tool(), glob_tool(),
             grep_tool(), web_fetch_tool(), web_search_tool(), todo_tool(), ask_user_tool(),
             manner_tool()]
    return {tool.name: tool for tool in tools}
