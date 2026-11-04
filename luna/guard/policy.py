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

# Настройки самой Луны: агент меняет их только тулом luna, с подтверждением.
# Правка файла в обход тула или `luna set` защиты через оболочку — спросить всегда.
LUNA_SET_PROTECTED = re.compile(r"\bluna(\.cmd|\.bat)?\s+set\s+(guard\.|agent\.mode|model\.(provider|kind|base_url))")
def _luna_tool_changing() -> tuple:
    """Меняющие действия тула luna — из самого тула, чтобы списки не разошлись."""
    try:
        from ..tools.luna_tool import CHANGING
    except Exception:  # noqa: BLE001 — без тула: всё, что не чтение, считаем изменением
        return ("set", "mcp_add", "mcp_remove", "manners_use", "theme_use", "theme_create")
    return CHANGING

SECRET_PATHS = re.compile(r"(^|[/\\])(\.env(\.[\w.-]+)?|\.envrc|secrets\.json|id_rsa|id_ed25519)([/\\]|$)|\.(pem|key)$")


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


# Имена, которые по определению не публичные (RFC 6761 и обычаи домашних сетей).
_LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa")
# Зарезервированные зоны, которые не резолвятся никогда: DNS не трогаем.
_NEVER_RESOLVES = (".test", ".example", ".invalid")
_DNS_TTL_S = 60.0
_dns_cache: dict = {}


def _addr_is_local(addr) -> bool:
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    return bool(addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified
                or not addr.is_global)


def _resolves_local(host: str) -> bool:
    """Имя резолвится хотя бы в ОДИН локальный адрес.

    Ошибка DNS (офлайн, нет имени) — не локальный: решает web_fetch, который
    сам проверяет адрес после DNS и на редиректах. Кэш короткий, чтобы
    смена записи (rebinding) не жила долго.
    """
    import socket
    import time

    now = time.monotonic()
    hit = _dns_cache.get(host)
    if hit is not None and now - hit[0] < _DNS_TTL_S:
        return hit[1]
    local = False
    try:
        infos = socket.getaddrinfo(host, None)
    except (OSError, UnicodeError, ValueError):
        infos = []
    for info in infos:
        try:
            raw = str(info[4][0]).split("%", 1)[0]
            if _addr_is_local(ipaddress.ip_address(raw)):
                local = True
                break
        except (ValueError, IndexError, TypeError):
            continue
    if len(_dns_cache) > 512:
        _dns_cache.clear()
    _dns_cache[host] = (now, local)
    return local


def is_local_host(host: str, *, resolve: bool = True) -> bool:
    """localhost, частные и служебные адреса: запрос туда — повод спросить человека.

    Сначала быстрые проверки по написанию, DNS — последним (имя вроде
    suspect114 из hosts или 2130706433 тоже ведёт в локальную сеть).
    """
    host = (host or "").strip().lower().rstrip(".")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if not host:
        return False
    if host == "localhost" or host.endswith(_LOCAL_SUFFIXES):
        return True
    try:
        addr = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        addr = None
    if addr is not None:
        return _addr_is_local(addr)
    if not resolve or host.endswith(_NEVER_RESOLVES):
        return False
    return _resolves_local(host)


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
    def __init__(self, settings=None, secrets=None, guard_judge=None, enforce: bool = False):
        self.settings = settings
        self.secrets = secrets
        # enforce — клетка работника: guard.enabled=false не выключает Предохранитель.
        self.enforce = bool(enforce)
        # guard_judge(tool, args, intent) -> (bool, reason): модель-охранник режима review
        self.guard_judge = guard_judge
        self._always_rank = -1  # «дальше без вопросов» на этом уровне риска и ниже

    def remember_always(self, risk: str) -> None:
        self._always_rank = max(self._always_rank, _RISK_ORDER.get(risk, 0))

    def _project_key(self) -> str:
        key_fn = getattr(self.settings, "project_key", None)
        if key_fn is None:
            return ""
        try:
            return str(key_fn())
        except Exception:  # noqa: BLE001 — без ключа проекта остаётся сессионное «всегда»
            return ""

    def _project_ranks(self) -> dict:
        try:
            ranks = self.settings.get("guard.always_rank_projects", {})
        except Exception:  # noqa: BLE001
            return {}
        return dict(ranks) if isinstance(ranks, dict) else {}

    def remember_always_project(self, risk: str) -> None:
        """«Больше не спрашивать для такого в этом проекте» (ЗАДАЧА-20).

        Уровень пишется в ГЛОБАЛЬНУЮ карту guard.always_rank_projects по
        пути проекта: проектный файл чужого репозитория мог бы иначе сам
        выдать себе «без вопросов».
        """
        rank = _RISK_ORDER.get(risk, 0)
        self._always_rank = max(self._always_rank, rank)
        key = self._project_key() if self.settings is not None else ""
        if not key:
            return
        ranks = self._project_ranks()
        try:
            current = int(ranks.get(key, -1))
        except (TypeError, ValueError):
            current = -1
        if rank > current:
            ranks[key] = rank
            self.settings.set("guard.always_rank_projects", ranks)

    def _always_rank_effective(self) -> int:
        """Уровень «без вопросов»: сессионный и проектный — больший."""
        rank = self._always_rank
        if self.enforce:
            # В клетке «больше не спрашивать», сказанное хозяином в чате,
            # не действует: единственный пропуск — allow профиля.
            return rank
        if self.settings is not None:
            # Старый ключ: глобальный слой или доверенный проект (недоверенный
            # Settings уже отбросил).
            try:
                rank = max(rank, int(self.settings.get("guard.always_rank_project", -1)))
            except (TypeError, ValueError):
                pass
            key = self._project_key()
            if key:
                try:
                    rank = max(rank, int(self._project_ranks().get(key, -1)))
                except (TypeError, ValueError):
                    pass
        return rank

    def _rule(self, tool_name: str, args_text: str) -> str | None:
        raw = self.settings.get("guard.rules", "") if self.settings else ""
        for tool_pattern, needle, action in _parse_rules(raw):
            if not fnmatch.fnmatch(tool_name, tool_pattern):
                continue
            if needle and needle not in args_text:
                continue
            if action == "allow" and self.enforce:
                continue  # клетке allow-правила хозяина не передаются, deny/ask — да
            return action
        return None

    def _secret_in_args(self, args_text: str) -> bool:
        if self.secrets is None:
            return False
        for value in self.secrets.values():
            if value and value in args_text:
                return True
        return False

    def _swarm_risk(self, tool_name: str, args: dict) -> str | None:
        """Рой, чьи ячейки без вопросов пишут в проект или зовут оболочку, — спросить всегда.

        Флаг project_confirmed ставит сама модель, поэтому он не считается
        подтверждением: в auto человека иначе никто бы не спросил. Профили
        только из памяти сессии Предохранителю не видны — тоже спрашиваем.
        """
        if tool_name != "starfall":
            return None
        action = str(args.get("action", ""))
        if action == "resume":
            return "Докрутка потока роя: его ячейки снова будут действовать без вопросов"
        if action not in ("run", "estimate"):  # смета — настоящий пробный прогон на 3 входах
            return None
        if str(args.get("write_mode", "")) == "project" or args.get("project_confirmed"):
            return "Рой с записью прямо в проект: ячейки будут менять файлы без вопросов"
        castes = [c.strip() for c in str(args.get("profile", "") or "").split(",") if c.strip()]
        castes += [str(c).strip() for c in (args.get("profiles") or []) if str(c).strip()]
        try:
            from ..starfall.profiles import get_profile
        except Exception:  # noqa: BLE001 — без модуля роя судить не о чем
            return None
        write_override = str(args.get("write_mode", "") or "")
        for caste in castes:
            prof = get_profile(caste, self.settings)
            if prof is None:
                return "Рой с профилем «" + caste + "» из памяти сессии: его права не проверить заранее"
            if prof.write_mode == "project" or write_override == "project":
                return "Рой с записью прямо в проект (профиль " + caste + ")"
            if "bash" in prof.tools:
                # Изоляция bash (ЗАДАЧА-21): вопрос снимает только бэкенд,
                # который есть И проверен пробным запуском (ячейка не
                # смогла выйти за свою папку и в сеть без net). Без
                # изоляции — как раньше: спрашиваем всегда.
                try:
                    from ..starfall.sandbox import isolation_verified

                    verified, _desc = isolation_verified(self.settings)
                except Exception:  # noqa: BLE001 — модуль изоляции упал: честнее спросить
                    verified = False
                if not verified:
                    return ("Рой с оболочкой (профиль " + caste + "): ячейки выполнят "
                            "команды без вопросов, а проверенной изоляции bash нет")
        return None

    def _is_luna_config(self, raw: str) -> bool:
        """Файл настроек или MCP Луны — глобальный (LUNA_HOME) или проектный (.luna/)."""
        from pathlib import Path

        if not raw:
            return False
        norm = raw.replace("\\", "/")
        if "/.luna/" in "/" + norm or norm.startswith(".luna/"):
            return True
        home = getattr(self.settings, "home_dir", None)
        if home is not None:
            try:
                target = Path(raw).expanduser().resolve()
                return (target.name in ("settings.json", "mcp.json")
                        and target.parent == Path(home).expanduser().resolve())
            except OSError:
                return False
        return False

    def hard_block(self, tool_name: str, args: dict) -> Decision | None:
        """Жёсткие блоки вне режимов: правило deny, необратимое, утечки, локальная сеть.

        Возвращает Decision (deny/ask) или None, если жёстких причин нет.
        Используется и для правленых человеком аргументов: их режимный ask
        уже отвечен человеком, а жёсткое обязано сработать и на новых данных.
        """
        args_text = _args_text(args)
        rule = self._rule(tool_name, args_text)
        if rule == "deny":
            return Decision("deny", "rules", "Запрещено правилом в настройках (guard.rules)", "medium")
        swarm_risk = self._swarm_risk(tool_name, args)
        if swarm_risk:
            return Decision("ask", "rules", swarm_risk, "high")
        if tool_name == "luna" and str(args.get("action", "")) in _luna_tool_changing():
            return Decision("ask", "rules", "Изменение настроек Луны: подтверждает человек "
                            "в любом режиме", "high")
        if tool_name in ("write", "edit") and self._is_luna_config(str(args.get("path", ""))):
            return Decision("ask", "rules", "Правка настроек Луны в обход тула luna", "high")
        if tool_name == "bash" and LUNA_SET_PROTECTED.search(args_text):
            return Decision("ask", "rules", "Смена защиты, режима или провайдера Луны через оболочку — "
                            "это решает человек", "high")
        irreversible = check_irreversible(tool_name, args)
        if irreversible:
            return Decision("ask", "rules", "Необратимое: " + irreversible +
                            ". Спрашиваю даже в режиме yolo", "high")
        # MCP тоже: разрешённый send_message юзербота не должен выносить секреты.
        if ((tool_name in ("web_fetch", "web_search", "bash") or tool_name.startswith("mcp__"))
                and self._secret_in_args(args_text)):
            return Decision("ask", "rules", "В аргументах есть значение секрета: "
                            "оно уйдёт наружу, если выполнить", "high")
        if tool_name == "web_fetch":
            host = _host_of(str(args.get("url", "")))
            if is_local_host(host):
                return Decision("ask", "rules", "Запрос в локальную сеть: " + host, "high")
        return None

    def decide(self, tool_name: str, mutating: bool, args: dict, intent: str = "",
               mode: str = "normal") -> Decision:
        """Решение по вызову. ask означает: цикл спрашивает человека (guard.ask)."""
        mode = mode if mode in MODES else "normal"
        args_text = _args_text(args)
        risk = "medium" if mutating else "low"

        enabled = True
        if self.settings is not None and not self.enforce:
            enabled = bool(self.settings.get("guard.enabled", True))
        if not enabled:
            return Decision("allow", "mode", "Предохранитель выключен настройкой guard.enabled", risk)

        hard = self.hard_block(tool_name, args)
        if hard is not None:
            return Decision(hard.decision, hard.by, hard.reason, hard.risk)
        rule = self._rule(tool_name, args_text)

        if _RISK_ORDER[risk] <= self._always_rank_effective():
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
