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
    cage: object = None                   # клетка работника (luna.worker.cage.Cage):
                                          # файлы, bash и веб сверяются с ней сами,
                                          # не полагаясь только на фильтр реестра
    scratch_dir: Path | None = None       # временная папка bash клетки без rw-корня;
                                          # её удаляет Agent.cleanup()


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
    # MCP: (сервер, сырое имя тула). Псевдоним в реестре бывает с хэшем
    # (_slug), а правила клетки пишутся по сырым именам — сверяем по нему.
    origin: tuple | None = None

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

    def __init__(self, base: dict, *, cage=None, cwd=None, settings=None):
        super().__init__(base)
        self._mcp_loaded = False
        self._mcp_lock = threading.Lock()
        self.mcp_errors: list = []
        self.mcp_required_failed: list[str] = []
        self.mcp_manager = None
        self._mcp_generation = _MCP_GENERATION
        self.cage = cage
        self.cwd = Path(cwd) if cwd is not None else None
        self.settings = settings

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
                tools, errors, manager, required_failed = _load_mcp_tools(
                    self.cage, self.cwd, self.settings)
            except Exception as exc:  # noqa: BLE001 — реестр встроенных тулов важнее MCP
                self.mcp_errors = ["MCP не загрузился: " + str(exc)[:200]]
                if self.cage is not None:
                    self.mcp_required_failed = [name for name, rule in self.cage.mcp.items()
                                                if getattr(rule, "required", False)]
                return
            self.mcp_errors = errors
            self.mcp_required_failed = required_failed
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
            self.mcp_errors = []
            self.mcp_required_failed = []
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


class _SecretsView:
    """Сейф глазами клетки: наружу — только имена из cage.secrets.

    Остальные выглядят отсутствующими, и resolve_value честно падает
    обычной ошибкой mcp_secret — сервер не стартует с чужим ключом.
    """

    def __init__(self, inner, cage):
        self._inner = inner
        self._cage = cage

    def get(self, name, default=None):
        if self._inner is None or not self._cage.secret_allowed(name):
            return default
        value = self._inner.get(name)
        return default if value is None else value


def _project_trusted(settings) -> bool:
    # Нет настроек или старые настройки без project_trusted — считаем
    # доверенным, как было до клетки (обратная совместимость).
    check = getattr(settings, "project_trusted", None) if settings is not None else None
    if not callable(check):
        return True
    try:
        return bool(check())
    except Exception:  # noqa: BLE001 — сломанная проверка доверия не открывает проект
        return False


def _home_of(settings) -> Path:
    from ..core.paths import luna_home

    home = getattr(settings, "home_dir", None) if settings is not None else None
    return Path(home) if home else luna_home()


def _load_mcp_tools(cage=None, cwd=None, settings=None) -> tuple:
    """(тулы, ошибки, менеджер, упавшие обязательные серверы клетки).

    Нет конфигов — пусто и мгновенно; есть — поднять серверы и перечислить тулы.
    """
    from ..mcp.manager import Manager, build_mcp_tools, has_mcp_config, project_config_labels

    home = _home_of(settings)
    cwd = Path(cwd) if cwd is not None else Path.cwd()
    if cage is not None:
        return _load_cage_mcp(cage, cwd, home)
    errors: list = []
    trusted = _project_trusted(settings)
    if not trusted:
        # Проектный конфиг MCP — это команды, которые сами запустятся при
        # первом обращении к реестру: из недоверенной папки их не исполняем.
        for label in project_config_labels(cwd):
            errors.append("проектный " + label + " пропущен: папка не доверена — luna trust")
    if not has_mcp_config(cwd, home, project_configs=trusted):
        return {}, errors, None, []
    manager = Manager(cwd=cwd, home=home, project_configs=trusted)
    tools, build_errors = build_mcp_tools(manager)
    return tools, errors + build_errors, manager, []


def _load_cage_mcp(cage, cwd: Path, home: Path) -> tuple:
    """MCP в клетке: только конфиг хозяина (или cage.mcp_config), только
    серверы из cage.mcp, только их разрешённые тулы, только выданные секреты."""
    from ..core.secrets import Secrets
    from ..mcp.manager import Manager, build_mcp_tools

    if not cage.mcp:
        return {}, [], None, []
    required = [name for name, rule in cage.mcp.items() if getattr(rule, "required", False)]
    global_config = Path(cage.mcp_config).expanduser() if cage.mcp_config else home / "mcp.json"
    secrets = _SecretsView(Secrets(home / "secrets.json"), cage)
    manager = Manager(cwd=cwd, home=home, secrets=secrets, project_configs=False,
                      global_config=global_config, only_servers=set(cage.mcp),
                      write_cache=not cage.ephemeral)
    errors = list(manager.problems)
    failed: list[str] = []
    configs = {cfg.name: cfg for cfg in manager.configs()}
    for name in sorted(cage.mcp):
        cfg = configs.get(name)
        if cfg is None:
            errors.append(name + ": сервера нет в " + str(global_config))
        elif not cfg.enabled or cfg.transport == "unsupported":
            errors.append(name + ": сервер выключен или его транспорт не поддерживается")
        else:
            continue
        if name in required:
            failed.append(name)
    tools, build_errors = build_mcp_tools(
        manager, tool_filter=lambda server, raw, alias: cage.mcp_tool_allowed(server, raw, alias))
    errors += build_errors
    broken = {err.split(":", 1)[0] for err in build_errors}
    failed += [name for name in required if name in broken and name not in failed]
    return tools, errors, manager, failed


def build_registry(cage=None, cwd=None, settings=None) -> dict[str, Tool]:
    """Все встроенные тулы вехи 1; MCP-тулы догружаются лениво (LazyRegistry).

    С клеткой в реестр попадают только выданные ей тулы — модель не видит
    остальных даже в списке. cwd — каталог агента (по умолчанию текущий
    на момент догрузки): проектные конфиги MCP ищутся в нём.
    """
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
    if cage is not None:
        tools = [tool for tool in tools if cage.builtin_allowed(tool.name)]
    return LazyRegistry({tool.name: tool for tool in tools},
                        cage=cage, cwd=cwd, settings=settings)
