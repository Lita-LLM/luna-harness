# Собран из BENDER: bender/mcp/manager.py (коммит 6552ed660bfabf5202569f9905212b77dbf6d31a), сокращено под Луну.
"""Подключения MCP: конфиги, ленивый старт серверов, тулы в реестр Луны.

Что отрезано от Бендера сознательно: одобрения-карантин определений с хэшами
доверия, песочница и модули Мастерской, OAuth, фоновые задачи и подписки.
У Луны подключения описывают файлы, а решение по каждому вызову принимает
Предохранитель (MCP-тулы — изменяющие, в обычном режиме он спрашивает).

Источники конфигов (поздний сильнее раннего, серверы — по имени):
`LUNA_HOME/mcp.json` → `.mcp.json` проекта (Claude Code) →
`opencode.json` проекта (раздел mcp) → `.luna/mcp.json` проекта.
Формат лунных файлов и `.mcp.json` — `{"mcpServers": {имя: {...}}}`:
stdio — `command`/`args`/`env`/`cwd`, удалённый — `type: http` и `url`/`headers`.
Секреты в значениях env и заголовков — только ссылками: `{{secret:имя}}`
(значение — из ~/.luna/secrets.json) и `${ПЕРЕМЕННАЯ}` (из окружения).
`timeout` в лунных файлах — секунды; в совместимых форматах значения от 1000
считаем миллисекундами (так принято у Claude Code и OpenCode).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

from ..core.errors import HumanError
from ..core.fsutil import atomic_write_text
from ..core.paths import luna_home
from ..core.secrets import Secrets
from .client import Client, HttpTransport, StdioTransport
from .content import render_result

NAME_RE = re.compile(r"[a-z][a-z0-9_-]{0,19}")
TOOL_NAME_RE = re.compile(r"[A-Za-z0-9_.\-]{1,128}")
SECRET_REF = re.compile(r"\{\{secret:([^\s{}]{1,128})\}\}")
ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
DEFAULT_TIMEOUT = 30.0

# Что наследуют stdio-серверы из окружения Луны (по образцу Бендера + PYTHONPATH,
# чтобы фальшивые и свои серверы на Python находились без установки).
ENV_KEEP = {
    "PATH", "HOME", "USER", "LANG", "LC_ALL", "TMP", "TEMP", "TMPDIR",
    "SYSTEMROOT", "SystemRoot", "PATHEXT", "COMSPEC", "PYTHONPATH",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR",
}


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _slug(value: str, limit: int) -> str:
    """Безопасный кусок имени тула: провайдеры принимают только [a-zA-Z0-9_-]."""
    slug = re.sub(r"[^a-zA-Z0-9_-]", "_", value)
    if slug != value or len(slug) > limit:
        slug = slug[:20] + "_" + digest(value)[:8]
    return slug


def tool_alias(sid: str, name: str) -> str:
    """Имя тула в реестре: mcp__<сервер>__<тул> (как у Бендера и Claude Code)."""
    return "mcp__" + _slug(sid, 20) + "__" + _slug(name, 30)


@dataclass
class ServerConfig:
    name: str
    transport: str = "stdio"           # stdio | http | unsupported
    command: list = field(default_factory=list)
    cwd: str = ""
    env: dict = field(default_factory=dict)
    url: str = ""
    headers: dict = field(default_factory=dict)
    enabled: bool = True
    timeout: float = DEFAULT_TIMEOUT
    source: str = ""                   # файл, откуда сервер прочитан (для luna mcp list)
    note: str = ""                     # например: транспорт sse пока не поддерживается


# --- разбор конфигов ---------------------------------------------------------


def _timeout(raw) -> float:
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0:
        value = float(raw)
        return value / 1000.0 if value >= 1000 else value
    return DEFAULT_TIMEOUT


def _str_map(raw, problems: list, label: str) -> dict:
    if raw is None:
        return {}
    if not isinstance(raw, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                        for k, v in raw.items()):
        problems.append(label + ": env/headers должны быть словарём строк")
        return {}
    return dict(raw)


def _from_entry(name: str, entry, source: str, problems: list, *, opencode: bool = False) -> ServerConfig | None:
    if not isinstance(entry, dict):
        problems.append(source + ": сервер " + name + " — не объект, пропущен")
        return None
    cfg = ServerConfig(name=name, source=source)
    cfg.enabled = entry.get("enabled", True) is not False
    cfg.timeout = _timeout(entry.get("timeout"))
    kind = str(entry.get("type") or "").lower()
    env = entry.get("environment") if opencode else entry.get("env")
    cfg.env = _str_map(env, problems, source + ": " + name)
    cfg.headers = _str_map(entry.get("headers"), problems, source + ": " + name)
    url = entry.get("url")
    command = entry.get("command")
    if kind in ("sse", "ws", "websocket"):
        cfg.transport = "unsupported"
        cfg.note = "транспорт " + kind + " пока не поддерживается (есть stdio и Streamable HTTP)"
        cfg.url = str(url or "")
        return cfg
    if kind == "remote" or (url and kind in ("http", "streamable-http", "")):
        if not isinstance(url, str) or not url:
            problems.append(source + ": сервер " + name + " — нет url, пропущен")
            return None
        cfg.transport = "http"
        cfg.url = url
        return cfg
    # stdio: command строкой (+args) или списком (OpenCode — всегда список)
    argv: list = []
    if isinstance(command, str) and command:
        argv = [command]
        args = entry.get("args")
        if isinstance(args, list):
            argv += [str(a) for a in args]
    elif isinstance(command, list) and command:
        argv = [str(a) for a in command]
    if not argv:
        problems.append(source + ": сервер " + name + " — нет ни command, ни url, пропущен")
        return None
    cfg.transport = "stdio"
    cfg.command = argv
    cwd = entry.get("cwd")
    if isinstance(cwd, str) and cwd:
        cfg.cwd = cwd
    return cfg


def _load_json(path: Path, problems: list) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        return None
    except ValueError as exc:
        problems.append(str(path) + ": не JSON (" + str(exc)[:120] + ")")
        return None
    return data if isinstance(data, dict) else None


def config_paths(cwd, home) -> list:
    """Файлы конфигов в порядке возрастания силы."""
    cwd, home = Path(cwd), Path(home)
    return [home / "mcp.json", cwd / ".mcp.json", cwd / "opencode.json", cwd / ".luna" / "mcp.json"]


def has_mcp_config(cwd, home) -> bool:
    return any(path.exists() for path in config_paths(cwd, home))


def load_configs(cwd, home) -> tuple:
    """(серверы по имени, проблемы разбора). Поздний источник перекрывает ранний."""
    cwd, home = Path(cwd), Path(home)
    servers: dict[str, ServerConfig] = {}
    problems: list[str] = []
    sources = [
        (home / "mcp.json", "LUNA_HOME/mcp.json", False),
        (cwd / ".mcp.json", ".mcp.json", False),
        (cwd / "opencode.json", "opencode.json", True),
        (cwd / ".luna" / "mcp.json", ".luna/mcp.json", False),
    ]
    for path, label, is_opencode in sources:
        if not path.exists():
            continue
        data = _load_json(path, problems)
        if data is None:
            continue
        entries = (data.get("mcp") if is_opencode else data.get("mcpServers"))
        if entries is None and not is_opencode:
            entries = data.get("servers") if isinstance(data.get("servers"), dict) else None
        if not isinstance(entries, dict):
            if is_opencode and "mcp" not in data:
                continue  # opencode.json без раздела mcp — не наша забота
            problems.append(label + ": нет объекта " + ("mcp" if is_opencode else "mcpServers"))
            continue
        for name, entry in entries.items():
            cfg = _from_entry(str(name), entry, label, problems, opencode=is_opencode)
            if cfg is not None:
                servers[cfg.name] = cfg
    return servers, problems


# --- секреты-ссылки ------------------------------------------------------------


def resolve_value(value: str, secrets, environ) -> str:
    """Подставить {{secret:имя}} из сейфа и ${ПЕРЕМЕННАЯ} из окружения."""
    def secret_sub(match):
        name = match.group(1)
        resolved = secrets.get(name) if secrets is not None else None
        if resolved is None:
            raise HumanError("mcp_secret", "В сейфе нет секрета для MCP: " + name,
                             "Добавь его в ~/.luna/secrets.json или поправь ссылку в mcp.json.")
        return resolved

    value = SECRET_REF.sub(secret_sub, value)
    return ENV_REF.sub(lambda m: environ.get(m.group(1), ""), value)


def resolve_map(values: dict, secrets, environ) -> dict:
    return {key: resolve_value(value, secrets, environ) for key, value in values.items()}


# --- менеджер ------------------------------------------------------------------


class Manager:
    """Живые подключения одного набора конфигов; создаётся лениво, стартует серверы по требованию."""

    def __init__(self, *, cwd, home=None, secrets=None, environ=None, opener=None):
        self.cwd = Path(cwd)
        self.home = Path(home) if home is not None else luna_home()
        self.secrets = secrets if secrets is not None else Secrets(self.home / "secrets.json")
        self.environ = environ if environ is not None else os.environ
        self._opener = opener
        self._servers, self.problems = load_configs(self.cwd, self.home)
        self._clients: dict[str, Client] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._tools: dict[str, list] = {}

    # конфиги

    def configs(self) -> list:
        return [self._servers[name] for name in sorted(self._servers)]

    def get_config(self, name: str) -> ServerConfig:
        cfg = self._servers.get(name)
        if cfg is None:
            raise HumanError("mcp_missing", "Нет такого MCP-сервера: " + name,
                             "Список: luna mcp list. Добавить: luna mcp add.")
        return cfg

    def cache_path(self) -> Path:
        return self.home / "local" / "mcp_cache.json"

    def cached_tools(self, name: str) -> list | None:
        """Имена тулов из кэша прошлого открытия (без подключения) или None."""
        try:
            data = json.loads(self.cache_path().read_text(encoding="utf-8"))
            entry = data.get(name) if isinstance(data, dict) else None
            tools = entry.get("tools") if isinstance(entry, dict) else None
            return [str(t) for t in tools] if isinstance(tools, list) else None
        except (OSError, ValueError):
            return None

    def _write_cache(self, name: str, aliases: list) -> None:
        try:
            path = self.cache_path()
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    data = {}
            except (OSError, ValueError):
                data = {}
            from datetime import datetime, timezone

            data[name] = {"tools": aliases, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=1) + "\n")
        except OSError:
            pass  # кэш — удобство списка, не источник истины

    # подключения

    def _lock(self, name: str) -> threading.Lock:
        return self._locks.setdefault(name, threading.Lock())

    def _make_client(self, cfg: ServerConfig) -> Client:
        if cfg.transport == "http":
            headers = resolve_map(cfg.headers, self.secrets, self.environ)
            transport = HttpTransport(cfg.url, headers, opener=self._opener)
        elif cfg.transport == "stdio":
            env = {key: self.environ[key] for key in ENV_KEEP if key in self.environ}
            env.update(resolve_map(cfg.env, self.secrets, self.environ))
            cwd = Path(cfg.cwd).expanduser() if cfg.cwd else self.cwd
            if not cwd.is_absolute():
                cwd = self.cwd / cwd
            transport = StdioTransport(cfg.command, cwd=cwd, env=env)
        else:
            raise HumanError("mcp_config", "Транспорт сервера " + cfg.name + " не поддерживается",
                             cfg.note or "Есть stdio и Streamable HTTP.")
        return Client(transport, timeout=cfg.timeout)

    def connect(self, name: str) -> Client:
        """Подключиться (лениво) и вернуть живой клиент; упавший stdio перезапускается клиентом."""
        cfg = self.get_config(name)
        if not cfg.enabled:
            raise HumanError("mcp_disabled", "MCP-сервер выключен: " + name,
                             "Включи его полем enabled в конфиге.")
        with self._lock(name):
            client = self._clients.get(name)
            if client is not None and client.version and client.transport.alive():
                return client
            if client is not None:
                client.close()  # протухшая сессия: процесс мёртв или initialize не завершился
            client = self._make_client(cfg)
            self._clients[name] = client
            client.connect()
            return client

    def list_tools(self, name: str) -> list:
        """Открыть сервер и перечислить тулы; definitions валидируются и кэшируются."""
        with self._lock(name):
            if name in self._tools:
                return self._tools[name]
        client = self.connect(name)
        definitions = client.tools()
        found: dict[str, dict] = {}
        for definition in definitions:
            tool_name = definition.get("name")
            if not isinstance(tool_name, str) or not TOOL_NAME_RE.fullmatch(tool_name) or tool_name in found:
                raise HumanError("mcp_tools", "Некорректное или повторяющееся имя MCP-тула",
                                 "Проверь сервер: " + str(tool_name)[:80])
            schema = definition.get("inputSchema")
            if schema is None:
                schema = {"type": "object", "properties": {}}
            if not isinstance(schema, dict):
                raise HumanError("mcp_tools", "Некорректная схема MCP-тула " + tool_name,
                                 "inputSchema должна быть объектом JSON Schema.")
            found[tool_name] = {**definition, "inputSchema": schema}
        result = [found[key] for key in sorted(found)]
        with self._lock(name):
            self._tools[name] = result
        self._write_cache(name, [tool_alias(name, key) for key in sorted(found)])
        return result

    def call(self, name: str, tool_name: str, arguments: dict) -> tuple:
        """Вызов тула: (текст, картинки, is_error). Соединение чинит клиент (см. его правила повторов)."""
        client = self.connect(name)
        schema = None
        for definition in self.list_tools(name):
            if definition["name"] == tool_name:
                schema = definition.get("inputSchema")
                break
        result = client.call_tool(tool_name, arguments or {}, schema=schema)
        return render_result(result)

    def connected(self, name: str) -> bool:
        client = self._clients.get(name)
        return bool(client is not None and client.version and client.transport.alive())

    def close(self, name: str = "") -> None:
        names = [name] if name else list(self._clients)
        for key in names:
            client = self._clients.pop(key, None)
            if client is not None:
                client.close()
            self._tools.pop(key, None)


# --- склейка с реестром тулов ---------------------------------------------------


def build_mcp_tools(manager: Manager) -> tuple:
    """(тулы реестра, ошибки по серверам). Один битый сервер не роняет остальные."""
    from ..tools.registry import Tool, ToolResult

    tools: dict[str, Tool] = {}
    errors: list[str] = []
    for cfg in manager.configs():
        if not cfg.enabled or cfg.transport == "unsupported":
            continue
        try:
            definitions = manager.list_tools(cfg.name)
        except HumanError as exc:
            errors.append(cfg.name + ": " + exc.title)
            continue
        except Exception as exc:  # noqa: BLE001 — чужой сервер не должен ронять реестр
            errors.append(cfg.name + ": " + str(exc)[:200])
            continue
        for definition in definitions:
            raw_name = definition["name"]
            alias = tool_alias(cfg.name, raw_name)
            description = "MCP-сервер " + cfg.name + ", тул " + raw_name + "."
            text = str(definition.get("description") or "").strip()
            if text:
                description += " " + text[:1000]

            def make_handler(server_name, tool):
                def handler(ctx, args):  # noqa: ARG001 — секреты/окружение уже в менеджере
                    text_out, images, is_error = manager.call(server_name, tool, args or {})
                    data = {"images": images} if images else None
                    return ToolResult(ok=not is_error, text=text_out, data=data)
                return handler

            tools[alias] = Tool(alias, description, definition["inputSchema"],
                                make_handler(cfg.name, raw_name), mutating=True)
    return tools, errors
