# Перенесено из BENDER: bender/guard/policy.py (коммит 6552ed660bfabf5202569f9905212b77dbf6d31a), поведение режимов и необратимого — по ta (docs/research/TA.md).
"""Предохранитель: одна функция решения для каждого вызова тула.

Порядок: явные правила deny → список необратимого (спрашивает даже в yolo) →
утечки секретов и локальная сеть (спросить) → правила allow/ask → режим.
Охранник в режиме review видит только тул и аргументы, без диалога.
Отказ однозначен: DENIED_PREFIX, модель не должна обходить его другим путём.
"""

from __future__ import annotations

import fnmatch
import ipaddress
import json
import re
import urllib.parse
from dataclasses import dataclass

DENIED_PREFIX = "НЕ ВЫПОЛНЕНО"

MODES = ("normal", "plan", "review", "auto", "yolo")

_RISK_ORDER = {"low": 0, "medium": 1, "high": 2}

# Необратимое и опасное: спрашиваем всегда, даже в yolo (список из ta).
IRREVERSIBLE = [
    (re.compile(r"\brm\s+-[a-z]*r[a-z]*f|\brm\s+-[a-z]*f[a-z]*r"), "rm -rf: рекурсивное удаление без корзины"),
    (re.compile(r"\bdd\s+.*\b(if|of)="), "dd: прямая запись на устройство или в файл"),
    (re.compile(r"\bmkfs\b|\bformat\s+[a-z]:"), "форматирование носителя"),
    (re.compile(r"(curl|wget)\b[^|]*\|\s*(sudo\s+)?(sh|bash)"), "скачать и сразу выполнить (curl|sh)"),
    (re.compile(r"\|\s*(sudo\s+)?(sh|bash)\b"), "вывод команды — сразу в оболочку"),
    (re.compile(r"secrets\.json|id_rsa|id_ed25519|\.env\b|credentials"), "доступ к ключам и секретам"),
    (re.compile(r"\bcrontab\b|systemctl\s+enable|schtasks\s+/create|launchctl\s+load"), "автозапуск и расписания системы"),
    (re.compile(r"\b(shutdown|reboot|poweroff)\b"), "выключение или перезагрузка машины"),
    (re.compile(r":\(\)\s*\{"), "вилка-бомба"),
]

SECRET_PATHS = re.compile(r"(^|[/\\])(\.env|secrets\.json|id_rsa|id_ed25519)([/\\]|$)|\\.(pem|key)$")


@dataclass
class Decision:
    decision: str  # allow | deny | ask
    by: str        # rules | guard_model | human | mode
    reason: str
    risk: str = "low"  # low | medium | high


def _args_text(args: dict) -> str:
    try:
        return json.dumps(args, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(args)


def _host_of(url: str) -> str:
    try:
        return (urllib.parse.urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def is_local_host(host: str) -> bool:
    """localhost и частные адреса: запрос туда — повод спросить человека."""
    if not host:
        return False
    if host == "localhost" or host.endswith(".local"):
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return bool(addr.is_private or addr.is_loopback or addr.is_link_local)


def check_irreversible(tool_name: str, args: dict) -> str | None:
    """Причина из списка необратимого или None."""
    text = _args_text(args)
    if tool_name == "read" and SECRET_PATHS.search(str(args.get("path", ""))):
        return "чтение файла с секретами"
    for pattern, reason in IRREVERSIBLE:
        if pattern.search(text):
            return reason
    return None


def _parse_rules(raw: str) -> list[tuple[str, str, str]]:
    """Строки «шаблон=действие»: шаблон — имя тула (fnmatch) или «тул:подстрока аргументов»."""
    rules = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        pattern, action = line.rsplit("=", 1)
        action = action.strip().lower()
        if action not in ("allow", "ask", "deny"):
            continue
        tool_pattern, _, needle = pattern.strip().partition(":")
        rules.append((tool_pattern, needle, action))
    return rules


class Guard:
    def __init__(self, settings=None, secrets=None, guard_judge=None):
        self.settings = settings
        self.secrets = secrets
        # guard_judge(tool, args, intent) -> (bool, reason): модель-охранник режима review
        self.guard_judge = guard_judge
        self._always_rank = -1  # «дальше без вопросов» на этом уровне риска и ниже

    def remember_always(self, risk: str) -> None:
        self._always_rank = max(self._always_rank, _RISK_ORDER.get(risk, 0))

    def _rule(self, tool_name: str, args_text: str) -> str | None:
        raw = self.settings.get("guard.rules", "") if self.settings else ""
        for tool_pattern, needle, action in _parse_rules(raw):
            if not fnmatch.fnmatch(tool_name, tool_pattern):
                continue
            if needle and needle not in args_text:
                continue
            return action
        return None

    def _secret_in_args(self, args_text: str) -> bool:
        if self.secrets is None:
            return False
        for value in self.secrets.values():
            if value and value in args_text:
                return True
        return False

    def decide(self, tool_name: str, mutating: bool, args: dict, intent: str = "",
               mode: str = "normal") -> Decision:
        """Решение по вызову. ask означает: цикл спрашивает человека (guard.ask)."""
        mode = mode if mode in MODES else "normal"
        args_text = _args_text(args)
        risk = "medium" if mutating else "low"

        enabled = True
        if self.settings is not None:
            enabled = bool(self.settings.get("guard.enabled", True))
        if not enabled:
            return Decision("allow", "mode", "Предохранитель выключен настройкой guard.enabled", risk)

        rule = self._rule(tool_name, args_text)
        if rule == "deny":
            return Decision("deny", "rules", "Запрещено правилом в настройках (guard.rules)", risk)

        irreversible = check_irreversible(tool_name, args)
        if irreversible:
            return Decision("ask", "rules", "Необратимое: " + irreversible +
                            ". Спрашиваю даже в режиме " + mode, "high")

        if tool_name in ("web_fetch", "web_search", "bash") and self._secret_in_args(args_text):
            return Decision("ask", "rules", "В аргументах есть значение секрета: "
                            "оно уйдёт наружу, если выполнить", "high")
        if tool_name == "web_fetch":
            host = _host_of(str(args.get("url", "")))
            if is_local_host(host):
                return Decision("ask", "rules", "Запрос в локальную сеть: " + host, "high")

        if _RISK_ORDER[risk] <= self._always_rank:
            return Decision("allow", "human", "Человек разрешил «дальше без вопросов» на этом уровне", risk)

        if mode == "plan":
            if mutating:
                return Decision("deny", "mode", "Режим plan: тулы записи запрещены", risk)
            return Decision("allow", "mode", "Режим plan: чтение разрешено", risk)

        if rule == "ask":
            return Decision("ask", "rules", "Правило в настройках требует спрашивать", risk)
        if rule == "allow":
            return Decision("allow", "rules", "Разрешено правилом в настройках", risk)

        if mode == "yolo":
            return Decision("allow", "mode", "Режим yolo: разрешено без вопросов", risk)
        if mode == "auto":
            return Decision("allow", "mode", "Режим auto: без вопросов; "
                            "необратимое всё равно спрашивается выше по списку", risk)
        if mode == "review":
            if not mutating:
                return Decision("allow", "mode", "Режим review: чтение разрешено", risk)
            if self.guard_judge is not None:
                allowed, reason = self.guard_judge(tool_name, args, intent)
                verdict = "allow" if allowed else "deny"
                return Decision(verdict, "guard_model", reason or "Вердикт модели-охранника", risk)
            return Decision("ask", "guard_model", "Охранник недоступен: спрашиваю человека", risk)
        # normal
        if mutating:
            return Decision("ask", "mode", "Меняющее действие: нужно подтверждение человека", risk)
        return Decision("allow", "mode", "Чтение разрешено", risk)


def denied_text(reason: str) -> str:
    """Однозначный отказ модели (поведение ta): обходить другим путём запрещено."""
    return (DENIED_PREFIX + ": действие не выполнено. Причина: " + reason +
            ". Не обходи этот отказ другим путём и не повторяй вызов с другими "
            "аргументами ради той же цели; доложи человеку и жди его решения.")
