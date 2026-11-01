"""Тул luna: справка о себе и настройка удобств по просьбе человека.

Смысл — человек говорит «сделай потише» или «подключи MCP-сервер», а не лезет
в конфиги. Это удобство интерфейса, а не право агента управлять собой:
- чтение (about, settings, paths, doctor, models, mcp, manners, compat, logs,
  version) — свободно;
- изменение (set, mcp_add, mcp_remove, manners_use) — всегда с подтверждением
  человека, даже в auto/yolo (Предохранитель, hard_block);
- поля «только человек» (guard.*, agent.mode, адрес и тип провайдера) тул не
  меняет никогда — отказ с подсказкой, как это сделать самому;
- секреты тул не читает и не показывает; в логах всё похожее на ключ вырезано.
"""

from __future__ import annotations

import contextlib
import io
import re
from pathlib import Path

from .registry import Tool, ToolResult

CHANGING = ("set", "mcp_add", "mcp_remove", "manners_use")
READING = ("about", "settings", "paths", "doctor", "models", "mcp", "manners",
           "compat", "logs", "version")

# Похожее на ключи и токены: длинные непрерывные строки и «key=…».
_KEYLIKE = re.compile(
    r"(sk-[A-Za-z0-9_\-]{12,}|AIza[0-9A-Za-z_\-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|xox[abposr]-[A-Za-z0-9\-]{10,}|Bearer\s+[A-Za-z0-9._\-]{12,}"
    r"|[A-Za-z0-9_\-]{32,})")
_ASSIGN = re.compile(r"(?i)((?:api[_-]?key|token|secret|password|passwd|authorization)\s*[:=]\s*)\S+")


def is_changing(args) -> bool:
    return (args or {}).get("action", "about") in CHANGING


def scrub(text: str, secrets=None) -> str:
    """Вырезать секреты: известные значения из сейфа и всё похожее на ключ."""
    from ..core.secrets import redact

    text = redact(text, secrets)
    text = _ASSIGN.sub(lambda m: m.group(1) + "[скрыто]", text)
    return _KEYLIKE.sub("[скрыто]", text)


def _ok(text: str) -> ToolResult:
    return ToolResult(True, text)


def _fail(text: str) -> ToolResult:
    return ToolResult(False, text)


def _home(ctx) -> Path:
    from ..core.paths import luna_home

    settings = ctx.settings
    if settings is not None and hasattr(settings, "home_dir"):
        return Path(settings.home_dir)
    return luna_home()


def _capture(fn, *args, **kwargs) -> str:
    """Вывод функции командной строки — строкой (print и out=)."""
    lines: list = []
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            if "out" in kwargs:
                kwargs["out"] = lines.append
            fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 — человекочитаемо, а не трейсбек
            title = getattr(exc, "title", "") or str(exc)
            hint = getattr(exc, "hint", "")
            lines.append("Ошибка: " + title + ((" " + hint) if hint else ""))
    text = buf.getvalue()
    if lines:
        text += "\n".join(str(x) for x in lines)
    return text.strip()


# --- чтение -----------------------------------------------------------------------------

def _about(ctx, args) -> ToolResult:
    from .. import __version__

    return _ok("\n".join([
        "Луна " + __version__ + " — сессионный агент «Лунапарк»: код, анализ, дизайн, монтаж.",
        "Умею: файлы (read/write/edit/glob/grep), оболочка (bash), веб (web_fetch/web_search), "
        "план (todo), вопросы человеку (ask_user), манеры речи (manner), MCP-серверы, скиллы (skill), "
        "откат ходов (/undo, /rewind) и сжатие контекста (/compact), сессии (/sessions, luna --continue), "
        "панели (/panels), компаньон Ниблер.",
        "Читаю инструкции других агентов: AGENTS.md, CLAUDE.md, GEMINI.md, .cursorrules, "
        "copilot-instructions; скиллы и слэш-команды из .claude/.opencode — action=compat покажет, что найдено.",
        "Настройки: action=settings (с фильтром prefix), менять — action=set (подтверждает человек).",
        "Только человек меняет: защиту (guard.*), режим (agent.mode, Shift+Tab в чате), "
        "провайдера (luna setup). Ключи API — только через luna setup.",
        "Документация проекта: README.md и docs/ в каталоге программы.",
    ]))


def _settings(ctx, args) -> ToolResult:
    settings = ctx.settings
    if settings is None:
        return _fail("Настройки недоступны в этом запуске.")
    prefix = str(args.get("prefix") or args.get("key") or "")
    rows = []
    for item in settings.describe():
        if prefix and not item["key"].startswith(prefix):
            continue
        mark = " [только человек]" if item.get("human_only") else ""
        value = settings.value_text(item["type"], item["value"])
        rows.append(item["key"] + " = " + value + " (" + item["source"] + ")" + mark +
                    " — " + item["title"] +
                    (" Варианты: " + ", ".join(item["choices"]) + "." if item["choices"] else ""))
    if not rows:
        return _ok("Нет настроек с префиксом " + repr(prefix) + ".")
    return _ok("\n".join(rows))


def _paths(ctx, args) -> ToolResult:
    home = _home(ctx)
    settings = ctx.settings
    lines = ["Каталог данных (LUNA_HOME): " + str(home)]
    if settings is not None and hasattr(settings, "global_path"):
        lines.append("Настройки глобальные: " + str(settings.global_path))
        lines.append("Настройки проекта: " + str(settings.project_path))
    for name, what in (("sessions", "сессии"), ("logs", "логи"), ("manners", "свои манеры"),
                       ("skills", "свои скиллы"), ("commands", "свои слэш-команды"),
                       ("checkpoints", "снимки файлов для отката")):
        path = home / name
        lines.append(what.capitalize() + ": " + str(path) + ("" if path.exists() else " (пока нет)"))
    lines.append("MCP-серверы: " + str(home / "mcp.json") + ", а также .mcp.json и .luna/mcp.json проекта")
    return _ok("\n".join(lines))


def _doctor(ctx, args) -> ToolResult:
    from ..diagnostics.doctor import run_doctor

    return _ok(scrub(_capture(run_doctor), ctx.secrets))


def _models(ctx, args) -> ToolResult:
    from ..setup import run_models

    text = _capture(run_models, settings=ctx.settings, secrets=ctx.secrets,
                    check=bool(args.get("check", False)), out=None)
    return _ok(scrub(text, ctx.secrets))


def _mcp(ctx, args) -> ToolResult:
    from ..mcp import cli

    text = _capture(cli._cmd_list, Path(ctx.cwd), _home(ctx), out=None)
    return _ok(scrub(text or "MCP-серверов нет.", ctx.secrets))


def _manners(ctx, args) -> ToolResult:
    from ..mind import manners

    items = manners.library(ctx.settings, ctx.cwd)
    current = str(ctx.settings.get("agent.manner", "") if ctx.settings is not None else "")
    lines = []
    for item in items:
        mark = "* " if item["id"] == current else "  "
        lines.append(mark + item["id"] + " — " + item["name"] + " (" +
                     manners.SOURCE_NAMES[item["source"]] + ")")
    lines.append("Свои манеры: " + str(manners.home_dir(ctx.settings)))
    return _ok("\n".join(lines))


def _compat(ctx, args) -> ToolResult:
    from ..mind.compat import report

    return _ok(report(Path(ctx.cwd), ctx.settings))


def _logs(ctx, args) -> ToolResult:
    logs = _home(ctx) / "logs"
    files = sorted(logs.glob("*.log"), key=lambda p: p.stat().st_mtime) if logs.is_dir() else []
    if not files:
        return _ok("Логов пока нет (" + str(logs) + ").")
    path = files[-1]
    lines = int(args.get("lines") or 60)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return _fail("Не читается лог " + str(path) + ": " + str(exc))
    tail = "\n".join(text.splitlines()[-max(1, min(lines, 400)):])
    return _ok(str(path) + ":\n" + (scrub(tail, ctx.secrets) or "(пусто)"))


def _version(ctx, args) -> ToolResult:
    import platform
    import sys

    from .. import __version__

    return _ok("Луна " + __version__ + " · Python " + sys.version.split()[0] + " · " +
               platform.system() + " " + platform.release())


# --- изменение (всегда с подтверждением человека) ---------------------------------------

def _set(ctx, args) -> ToolResult:
    from ..core.errors import HumanError
    from ..core.settings import SCHEMA, is_human_only

    settings = ctx.settings
    if settings is None:
        return _fail("Настройки недоступны в этом запуске.")
    key = str(args.get("key") or "")
    if key not in SCHEMA:
        return _fail("Нет настройки " + repr(key) + ". Список: action=settings.")
    if is_human_only(key):
        hint = "Shift+Tab в чате или /mode" if key == "agent.mode" else (
            "luna setup" if key.startswith("model.") else "luna set " + key + " <значение> в своём терминале")
        return _fail("Эту настройку меняет только человек (" + key + "). Подскажи ему: " + hint + ".")
    raw = args.get("value")
    if raw is None:
        return _fail("Нужно значение: value.")
    layer = "project" if str(args.get("layer") or "") == "project" else "global"
    try:
        value = settings.parse_value(key, str(raw)) if isinstance(raw, str) else settings.validate(key, raw)
        old = settings.get(key)
        settings.set(key, value, layer=layer)
    except HumanError as err:
        return _fail(err.title + ((" " + err.hint) if err.hint else ""))
    field = SCHEMA[key]
    return _ok(key + ": " + settings.value_text(field.type, old) + " → " +
               settings.value_text(field.type, value) +
               " (" + ("проект" if layer == "project" else "глобально") + "). Действует сразу.")


def _mcp_add(ctx, args) -> ToolResult:
    from ..core.errors import HumanError
    from ..mcp import cli
    from .registry import request_mcp_reload

    name = str(args.get("name") or "")
    url = str(args.get("url") or "")
    command = args.get("command")
    if isinstance(command, str):
        import shlex

        command = shlex.split(command, posix=True)
    if url:
        rest = [name, "--url", url]
    elif isinstance(command, list) and command:
        rest = [name, "--"] + [str(x) for x in command]
    else:
        return _fail("Нужно name и command (список или строка) или url.")
    out: list = []
    try:
        cli._cmd_add(rest, _home(ctx), out.append)
    except HumanError as err:
        return _fail(err.title + ((" " + err.hint) if err.hint else ""))
    request_mcp_reload()
    return _ok("\n".join(out) + "\nТулы сервера появятся со следующего шага.")


def _mcp_remove(ctx, args) -> ToolResult:
    from ..core.errors import HumanError
    from ..mcp import cli
    from .registry import request_mcp_reload

    out: list = []
    try:
        cli._cmd_remove(str(args.get("name") or ""), Path(ctx.cwd), _home(ctx), out.append)
    except HumanError as err:
        return _fail(err.title + ((" " + err.hint) if err.hint else ""))
    request_mcp_reload()
    return _ok("\n".join(out))


def _manners_use(ctx, args) -> ToolResult:
    from ..core.errors import HumanError
    from ..mind import manners

    name = str(args.get("name") or "")
    try:
        item = manners.choose(ctx.settings, name, ctx.cwd)
    except HumanError as err:
        return _fail(err.title + ((" " + err.hint) if err.hint else ""))
    if item is None:
        return _ok("Манера выключена: говорю своим голосом. Со следующего хода.")
    return _ok("Манера «" + item["name"] + "» включена со следующего хода.")


ACTIONS = {
    "about": _about, "settings": _settings, "paths": _paths, "doctor": _doctor,
    "models": _models, "mcp": _mcp, "manners": _manners, "compat": _compat,
    "logs": _logs, "version": _version,
    "set": _set, "mcp_add": _mcp_add, "mcp_remove": _mcp_remove, "manners_use": _manners_use,
}


def _luna(ctx, args) -> ToolResult:
    action = str((args or {}).get("action") or "about")
    fn = ACTIONS.get(action)
    if fn is None:
        return _fail("Нет действия " + repr(action) + ". Есть: " + ", ".join(ACTIONS) + ".")
    return fn(ctx, args or {})


def luna_tool() -> Tool:
    return Tool(
        "luna",
        "Ты сама: справка о себе и настройка удобств по просьбе человека. "
        "Чтение: action=about | settings (prefix — фильтр) | paths | doctor | models | mcp | manners | "
        "compat (инструкции, скиллы, команды других агентов) | logs (lines) | version. "
        "Изменение — человек подтверждает каждое: set (key, value, layer=global|project), "
        "mcp_add (name + command или url), mcp_remove (name), manners_use (name; пусто — свой голос). "
        "Защиту (guard.*), режим (agent.mode) и провайдера (model.provider/kind/base_url) меняет только человек — "
        "тул откажет; подскажи человеку, как сделать самому.",
        {"type": "object", "properties": {
            "action": {"type": "string", "enum": list(ACTIONS)},
            "key": {"type": "string"},
            "value": {},
            "layer": {"type": "string"},
            "prefix": {"type": "string"},
            "name": {"type": "string"},
            "command": {},
            "url": {"type": "string"},
            "lines": {"type": "integer"},
            "check": {"type": "boolean"}},
         "required": ["action"]},
        _luna, mutating_for=is_changing)


# --- скиллы -----------------------------------------------------------------------------

def _skill(ctx, args) -> ToolResult:
    from ..mind.compat import list_skills, load_skill

    name = str((args or {}).get("name") or "")
    if not name:
        items = list_skills(Path(ctx.cwd), ctx.settings)
        if not items:
            return _ok("Скиллов не найдено. Где ищу — luna action=compat.")
        return _ok("\n".join(i["name"] + " — " + i["description"][:160] for i in items))
    item = load_skill(Path(ctx.cwd), name, ctx.settings)
    if item is None:
        return _fail("Нет скилла " + repr(name) + ". Список — skill без имени.")
    return _ok("Скилл " + item["name"] + " (" + item["dir"] + "; файлы рядом читай тулом read):\n\n" +
               item["body"][:20000])


def skill_tool() -> Tool:
    return Tool(
        "skill",
        "Загрузить скилл — готовую инструкцию для типовой задачи (формат Agent Skills, "
        "как у Claude Code). name — имя скилла из списка в системном промпте; без name — список.",
        {"type": "object", "properties": {"name": {"type": "string"}}},
        _skill)
