"""Команда `luna mcp`: list / add / remove / test.

add пишет в глобальный LUNA_HOME/mcp.json (проектный .luna/mcp.json и
совместимые .mcp.json / opencode.json читаются, но отсюда не правятся).
Секреты в конфиг открытым текстом не пишем и не принимаем: значения env и
заголовков — ссылками {{secret:имя}} на ~/.luna/secrets.json.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..core.errors import HumanError
from ..core.fsutil import atomic_write_text
from ..core.paths import luna_home
from ..core.stdio import ensure_utf8_stdio
from .manager import NAME_RE, Manager, load_configs

USAGE = ("Нужно: luna mcp list | add <имя> -- <команда …> | add <имя> --url <адрес> | "
         "remove <имя> | test <имя>")


def _global_file(home: Path) -> Path:
    return home / "mcp.json"


def _read_store(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise HumanError("mcp_config", "Файл MCP-конфига не читается: " + str(path),
                         "Поправь JSON вручную; файл не перезаписан.", detail=str(exc)[:300]) from None
    if not isinstance(data, dict):
        raise HumanError("mcp_config", "В файле MCP-конфига не объект JSON: " + str(path),
                         "Нужен объект вида {\"mcpServers\": {...}}.")
    return data


def _write_store(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=1) + "\n")


def _cmd_list(cwd: Path, home: Path, out) -> int:
    manager = Manager(cwd=cwd, home=home)
    servers = manager.configs()
    if not servers:
        out("MCP-серверы не настроены.")
        out("Добавить: luna mcp add <имя> -- <команда …> или luna mcp add <имя> --url <адрес>.")
        out("Читаются также .luna/mcp.json и .mcp.json проекта и раздел mcp из opencode.json.")
    for cfg in servers:
        state = "включён" if cfg.enabled else "выключен"
        cached = manager.cached_tools(cfg.name)
        tools_text = "тулов: " + (str(len(cached)) if cached is not None else "— (ещё не открывался)")
        line = cfg.name + "  " + cfg.transport + "  " + state + "  " + tools_text + "  [" + cfg.source + "]"
        out(line)
        if cfg.transport == "stdio":
            out("    команда: " + " ".join(cfg.command))
        elif cfg.url:
            out("    адрес: " + cfg.url)
        if cfg.note:
            out("    " + cfg.note)
    for problem in manager.problems:
        out("Внимание: " + problem)
    return 0


def _cmd_add(rest: list, home: Path, out) -> int:
    if not rest:
        raise HumanError("usage", USAGE, "")
    name = rest[0]
    if not NAME_RE.fullmatch(name):
        raise HumanError("mcp_config", "Некорректное имя MCP-сервера: " + name,
                         "1–20 символов: строчная латинская буква, дальше буквы, цифры, _ и -.")
    tail = rest[1:]
    entry: dict
    if tail[:1] == ["--"] and len(tail) > 1:
        entry = {"command": tail[1], "args": tail[2:]}
    elif len(tail) == 2 and tail[0] == "--url":
        entry = {"type": "http", "url": tail[1]}
    else:
        raise HumanError("usage",
                         "Нужно: luna mcp add <имя> -- <команда …> или luna mcp add <имя> --url <адрес>", "")
    path = _global_file(home)
    data = _read_store(path)
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
        data["mcpServers"] = servers
    existed = name in servers
    servers[name] = entry
    _write_store(path, data)
    out(("Обновлён" if existed else "Добавлен") + " MCP-сервер " + name + " в " + str(path))
    out("Проверка: luna mcp test " + name)
    return 0


def _cmd_remove(name: str, cwd: Path, home: Path, out) -> int:
    if not NAME_RE.fullmatch(name):
        raise HumanError("mcp_config", "Некорректное имя MCP-сервера: " + name, "")
    for path in (_global_file(home), cwd / ".luna" / "mcp.json"):
        if not path.exists():
            continue
        data = _read_store(path)
        servers = data.get("mcpServers")
        if isinstance(servers, dict) and name in servers:
            del servers[name]
            _write_store(path, data)
            out("Удалён MCP-сервер " + name + " из " + str(path))
            return 0
    servers, _problems = load_configs(cwd, home)
    if name in servers:
        raise HumanError("mcp_config", "Сервер " + name + " описан в совместимом файле (" +
                         servers[name].source + ")",
                         "Это конфиг другого инструмента (.mcp.json или opencode.json); удали запись там вручную.")
    raise HumanError("mcp_missing", "Нет такого MCP-сервера: " + name, "Список: luna mcp list.")


def _cmd_test(name: str, cwd: Path, home: Path, out) -> int:
    manager = Manager(cwd=cwd, home=home)
    try:
        cfg = manager.get_config(name)
        definitions = manager.list_tools(name)
        client = manager.connect(name)
        info = client.info or {}
        label = str(info.get("name") or "?") + " " + str(info.get("version") or "")
        out("Сервер " + name + " (" + cfg.transport + "): подключён, протокол " +
            str(client.version) + ", сервер: " + label.strip())
        out("Тулов: " + str(len(definitions)))
        for definition in definitions:
            text = str(definition.get("description") or "").strip()
            out("  " + definition["name"] + (" — " + text[:120] if text else ""))
        return 0
    finally:
        manager.close()


def run_mcp(argv: list, *, cwd=None, home=None, out=None) -> int:
    ensure_utf8_stdio()
    stream = out or print
    cwd = Path(cwd) if cwd is not None else Path.cwd()
    home = Path(home) if home is not None else luna_home()
    if not argv:
        raise HumanError("usage", USAGE, "")
    action, rest = argv[0], argv[1:]
    if action == "list":
        return _cmd_list(cwd, home, stream)
    if action == "add":
        return _cmd_add(rest, home, stream)
    if action == "remove":
        if len(rest) != 1:
            raise HumanError("usage", "Нужно: luna mcp remove <имя>", "")
        return _cmd_remove(rest[0], cwd, home, stream)
    if action == "test":
        if len(rest) != 1:
            raise HumanError("usage", "Нужно: luna mcp test <имя>", "")
        return _cmd_test(rest[0], cwd, home, stream)
    raise HumanError("usage", USAGE, "")
