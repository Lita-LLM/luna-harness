"""Клетка работника: единственный источник правды об ограничениях запуска.

Точный эфемерный работник (luna -p --worker …) получает Cage, собранную из
профиля и флагов (luna/worker/profile.py). Клетку читают все, кто что-то
пропускает наружу: реестр тулов и MCP, файловые тулы, bash, веб, сборка
промпта, headless. Внутри клетки запрещено всё, что не разрешено явно, —
поэтому каждое поле по умолчанию самое строгое.

Флаги командной строки только сужают клетку (narrowed): расширить её можно
лишь правкой профиля, а профиль лежит у хозяина, не в проекте.
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from pathlib import Path

# Тулы, которых в клетке не бывает: дети и рой строят свои реестры мимо
# клетки, тул luna читает журналы и конфиги хозяина, manner/skill тянут
# файлы из проекта и меняют настройки.
FORBIDDEN_TOOLS = frozenset({"assistant", "crew", "starfall", "cells", "luna",
                             "manner", "skill"})
FS_TOOLS = frozenset({"read", "write", "edit", "glob", "grep"})
FS_WRITE_TOOLS = frozenset({"write", "edit"})
FS_MODES = ("none", "ro", "rw")
BASH_MODES = ("off", "sandbox")
INSTRUCTION_MODES = ("none", "project", "all")

# Запрещено даже внутри корня: сейф и журналы Луны, ключи хозяина, чаты
# других харнессов. Корень мог случайно накрыть домашнюю папку целиком.
_HOME_DENY = (".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker",
              ".config/gh", ".config/gcloud", ".password-store", ".claude",
              ".codex", ".gemini", ".netrc", ".git-credentials", ".pgpass",
              ".npmrc", ".pypirc",
              # Мессенджеры, браузеры и хранилища паролей — ровно то, за чем
              # пришёл бы взломанный промптом работник (атака 04.11.2026).
              "AppData/Roaming", "AppData/Local/Google", "AppData/Local/Microsoft",
              "AppData/Local/Packages", "AppData/Local/Telegram Desktop",
              ".mozilla", ".thunderbird", ".local/share/TelegramDesktop",
              ".local/share/keyrings", ".config/google-chrome", ".config/chromium",
              ".config/BraveSoftware", ".config/Signal", ".config/discord",
              ".var/app", "Library",
              ".bash_history", ".zsh_history", ".python_history", ".lesshst",
              ".viminfo", ".psql_history", ".mysql_history", ".node_repl_history")


def _match_any(name: str, patterns) -> bool:
    return any(fnmatch.fnmatchcase(name, p) for p in patterns or ())


@dataclass
class McpRule:
    """Один разрешённый MCP-сервер: какие его тулы видны модели."""
    tools: list = field(default_factory=lambda: ["*"])   # шаблоны сырых имён
    deny: list = field(default_factory=list)             # сильнее tools
    required: bool = False                               # не поднялся — код 6
    # Ограничение аргументов: {шаблон тула: {поле: [шаблоны значений]}}.
    # Разрешено только то, что совпало; поля нет в вызове — отказ. Так тул
    # «читать сообщения» привязывается к чатам из списка, а не ко всем.
    args: dict = field(default_factory=dict)


@dataclass
class Cage:
    name: str = ""
    description: str = ""
    prompt: str = ""                    # инструкции работника (system-хвост)
    model: str = ""
    mode: str = "normal"
    tools: list = field(default_factory=list)       # встроенные тулы, шаблоны
    deny: list = field(default_factory=list)        # шаблоны по ЛЮБЫМ именам, сильнее всего
    allow: list = field(default_factory=list)       # авто-«да» Предохранителя (как --allow)
    mcp: dict = field(default_factory=dict)         # имя сервера → McpRule
    mcp_config: str = ""                # свой файл серверов вместо LUNA_HOME/mcp.json
    secrets: list = field(default_factory=list)     # имена для {{secret:…}} в MCP
    fs_mode: str = "none"
    fs_roots: list = field(default_factory=list)    # Path, уже абсолютные
    bash: str = "off"
    fetch_hosts: list | None = field(default_factory=list)  # None — любой публичный
    fetch_query: bool = False           # можно ли ?параметры в web_fetch
    instructions: str = "none"
    max_usd: float = 0.0
    max_steps: int = 0
    timeout_s: float = 0.0
    max_tokens: int = 0                 # потолок токенов: работает и без цен моделей
    deny_dirs: list = field(default_factory=list)   # ещё закрытые папки (свой home_dir)
    output_schema: dict | None = None
    schema_retries: int = 1
    ephemeral: bool = True
    fail_on_deny: bool = False
    source: str = ""                    # откуда профиль — для --check и ошибок

    # --- тулы ----------------------------------------------------------------

    def builtin_allowed(self, name: str) -> bool:
        if name in FORBIDDEN_TOOLS or _match_any(name, self.deny):
            return False
        if name in FS_TOOLS and self.fs_mode == "none":
            return False
        if name in FS_WRITE_TOOLS and self.fs_mode != "rw":
            return False
        if name == "bash" and self.bash == "off":
            return False
        return _match_any(name, self.tools)

    def mcp_server_allowed(self, server: str) -> bool:
        return server in self.mcp

    def mcp_tool_allowed(self, server: str, raw_tool: str, alias: str = "") -> bool:
        rule = self.mcp.get(server)
        if rule is None:
            return False
        if alias and _match_any(alias, self.deny):
            return False
        if _match_any(raw_tool, rule.deny):
            return False
        return _match_any(raw_tool, rule.tools)

    def mcp_args_check(self, server: str, raw_tool: str, args) -> str:
        """Пусто — можно; иначе причина. Каждое ограниченное поле обязано быть
        в вызове, и все его значения (у списка — каждый элемент) совпасть."""
        rule = self.mcp.get(server)
        if rule is None:
            return "сервер не выдан"
        args = args if isinstance(args, dict) else {}
        for tool_pat, fields in (rule.args or {}).items():
            if not fnmatch.fnmatchcase(raw_tool, tool_pat):
                continue
            for name, patterns in fields.items():
                if name not in args:
                    return "поле «" + name + "» обязательно и ограничено списком"
                value = args[name]
                values = value if isinstance(value, (list, tuple)) else [value]
                if not values or isinstance(value, (dict, bool)) or any(
                        isinstance(v, (dict, list, tuple, bool, type(None))) for v in values):
                    return "поле «" + name + "» должно быть значением из списка"
                for v in values:
                    if not _match_any(str(v), patterns):
                        return "значение поля «" + name + "» вне разрешённого списка"
        return ""

    def tool_allowed(self, name: str) -> bool:
        """Любое имя реестра: встроенное или mcp__сервер__тул."""
        if name.startswith("mcp__"):
            _p, server, raw = (name.split("__", 2) + ["", ""])[:3]
            return self.mcp_tool_allowed(server, raw, name)
        return self.builtin_allowed(name)

    def secret_allowed(self, name: str) -> bool:
        return name in (self.secrets or ())

    # --- файлы ---------------------------------------------------------------

    def path_check(self, path, *, write: bool = False) -> str:
        """Пусто — можно; иначе причина отказа. Путь сверяется после
        resolve(): симлинки и «..» из корня не выводят."""
        if self.fs_mode == "none":
            return "файловая система клетке не выдана (fs: none)"
        if write and self.fs_mode != "rw":
            return "клетка только читает (fs: ro)"
        try:
            real = Path(path).expanduser().resolve()
        except (OSError, RuntimeError):
            return "путь не разрешается"
        home = Path.home().resolve()
        from ..core.paths import luna_home
        denied = [luna_home().expanduser().resolve()] + [home / d for d in _HOME_DENY]
        for extra in self.deny_dirs:
            try:
                denied.append(Path(extra).expanduser().resolve())
            except (OSError, RuntimeError):
                continue
        for d in denied:
            if real == d or d in real.parents:
                return "закрыто всегда: " + str(d)
        for root in self.fs_roots:
            r = Path(root).resolve()
            if real == r or r in real.parents:
                return ""
        return "вне корней клетки: " + ", ".join(str(r) for r in self.fs_roots)

    # --- сеть ----------------------------------------------------------------

    def host_check(self, url: str) -> str:
        """Пусто — можно открыть; иначе причина. Приватные адреса после
        DNS проверяет web_fetch сам (и на редиректах)."""
        from urllib.parse import urlsplit

        try:
            parts = urlsplit(url)
        except ValueError:
            return "кривой адрес"
        host = (parts.hostname or "").lower().rstrip(".")
        if not host:
            return "в адресе нет хоста"
        if parts.query and not self.fetch_query:
            return "параметры в адресе клетке запрещены (fetch_query: false)"
        if self.fetch_hosts is None:
            return ""
        if _match_any(host, self.fetch_hosts):
            return ""
        return "хост не из списка клетки: " + host

    # --- отчёт ---------------------------------------------------------------

    def describe(self, visible_tools=None) -> dict:
        """Итоговая клетка для --check: что увидит модель и что закрыто."""
        return {
            "worker": self.name, "source": self.source,
            "description": self.description, "model": self.model,
            "mode": self.mode,
            "tools": sorted(visible_tools) if visible_tools is not None else list(self.tools),
            "deny": list(self.deny), "allow": list(self.allow),
            "mcp": {k: {"tools": v.tools, "deny": v.deny, "required": v.required}
                    for k, v in self.mcp.items()},
            "mcp_config": self.mcp_config, "secrets": list(self.secrets),
            "fs": {"mode": self.fs_mode, "roots": [str(r) for r in self.fs_roots]},
            "bash": self.bash,
            "net": {"fetch_hosts": self.fetch_hosts, "fetch_query": self.fetch_query},
            "instructions": self.instructions,
            "limits": {"max_usd": self.max_usd, "max_steps": self.max_steps,
                       "timeout_s": self.timeout_s, "max_tokens": self.max_tokens},
            "output_schema": self.output_schema is not None,
            "ephemeral": self.ephemeral,
        }


def env_for_bash() -> dict:
    """Окружение bash в клетке: без ключей API и токенов хозяина."""
    keep = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TZ", "TMPDIR", "SYSTEMROOT",
            "COMSPEC", "PATHEXT", "WINDIR", "TEMP", "TMP")
    return {k: v for k, v in os.environ.items() if k in keep}
