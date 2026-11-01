"""Совместимость с другими харнессами: инструкции, скиллы, слэш-команды.

Луна читает то, что человек уже написал для Claude Code, Codex, Gemini CLI,
OpenCode, Cursor и Copilot, — чтобы переезд не требовал ничего переписывать.
Всё только читается, ничего не исполняется: скилл и команда — это текст для
модели, а действия она делает обычными тулами через Предохранитель.
"""

from __future__ import annotations

import re
from pathlib import Path

# Инструкции проекта: ищем в каталоге и выше, ближе к каталогу — раньше.
PROJECT_FILES = (
    "AGENTS.md", "CLAUDE.md", "CLAUDE.local.md", "GEMINI.md",
    ".cursorrules", ".github/copilot-instructions.md",
)
# Глобальные инструкции человека для других агентов (настройка compat.global_instructions).
GLOBAL_FILES = (
    ".claude/CLAUDE.md", ".codex/AGENTS.md", ".gemini/GEMINI.md",
    ".config/opencode/AGENTS.md",
)
MAX_FILE_CHARS = 4000
MAX_TOTAL_CHARS = 12000
MAX_IMPORT_DEPTH = 3

_IMPORT = re.compile(r"^@(\S+)\s*$", re.M)


def _home() -> Path:
    return Path.home()


def _setting(settings, key: str, default):
    if settings is None:
        return default
    return settings.get(key, default)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _expand_imports(text: str, base: Path, depth: int, seen: set) -> str:
    """`@путь` на отдельной строке (как в CLAUDE.md) — вставить файл."""
    if depth >= MAX_IMPORT_DEPTH:
        return text

    def repl(match: re.Match) -> str:
        raw = match.group(1)
        target = Path(raw).expanduser()
        if not target.is_absolute():
            target = base / target
        try:
            key = str(target.resolve())
        except OSError:
            return match.group(0)
        if key in seen or not target.is_file():
            return match.group(0)
        seen.add(key)
        inner = _read(target)[:MAX_FILE_CHARS]
        return _expand_imports(inner, target.parent, depth + 1, seen)

    return _IMPORT.sub(repl, text)


def is_global_instruction(path: str) -> bool:
    norm = str(path).replace("\\", "/")
    home = str(_home()).replace("\\", "/")
    return any(norm == home + "/" + name for name in GLOBAL_FILES)


def collect_instructions(cwd: Path, settings=None) -> list[tuple[str, str]]:
    """(путь, текст) инструкций: проект от каталога вверх, затем глобальные."""
    candidates: list[Path] = []
    current = Path(cwd).resolve()
    for directory in (current, *current.parents):
        for name in PROJECT_FILES:
            candidates.append(directory / name)
    if _setting(settings, "compat.global_instructions", True):
        home = _home()
        for name in GLOBAL_FILES:
            candidates.append(home / name)

    found: list[tuple[str, str]] = []
    seen_paths: set = set()
    seen_texts: set = set()
    total = 0
    for path in candidates:
        if not path.is_file():
            continue
        try:
            key = str(path.resolve())
        except OSError:
            continue
        if key in seen_paths:
            continue
        seen_paths.add(key)
        text = _expand_imports(_read(path), path.parent, 0, {key})[:MAX_FILE_CHARS].strip()
        # один и тот же текст под разными именами (CLAUDE.md = копия AGENTS.md) — один раз
        if not text or text in seen_texts:
            continue
        seen_texts.add(text)
        text = text[: max(0, MAX_TOTAL_CHARS - total)]
        if text:
            found.append((str(path), text))
            total += len(text)
        if total >= MAX_TOTAL_CHARS:
            break
    return found


# --- скиллы (формат Agent Skills: папка с SKILL.md и фронтматтером) ---------------------

def _frontmatter(raw: str) -> tuple[dict, str]:
    """Простой YAML-фронтматтер «ключ: значение» — без зависимостей."""
    if not raw.startswith("---"):
        return {}, raw
    end = raw.find("\n---", 3)
    if end < 0:
        return {}, raw
    meta: dict = {}
    for line in raw[3:end].splitlines():
        if ":" in line and not line.startswith((" ", "\t", "#")):
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip().strip("\"'")
    body = raw[end + 4:].lstrip("\n")
    return meta, body


def skill_dirs(cwd: Path, settings=None) -> list[tuple[Path, str]]:
    """(каталог, источник) — где искать скиллы, сильные раньше."""
    from ..core.paths import luna_home

    cwd = Path(cwd)
    home = _home()
    luna = Path(settings.home_dir) if settings is not None and hasattr(settings, "home_dir") else luna_home()
    return [
        (cwd / ".luna" / "skills", "проект (.luna)"),
        (cwd / ".claude" / "skills", "проект (.claude)"),
        (cwd / ".opencode" / "skill", "проект (.opencode)"),
        (luna / "skills", "свои (Луна)"),
        (home / ".claude" / "skills", "свои (.claude)"),
        (home / ".config" / "opencode" / "skill", "свои (opencode)"),
    ]


def list_skills(cwd: Path, settings=None) -> list[dict]:
    """Скиллы: имя, описание, путь к SKILL.md, источник. Первый по имени побеждает."""
    if not _setting(settings, "compat.skills", True):
        return []
    out: dict = {}
    for directory, source in skill_dirs(cwd, settings):
        if not directory.is_dir():
            continue
        for skill_md in sorted(directory.glob("*/SKILL.md")):
            meta, _ = _frontmatter(_read(skill_md)[:4000])
            name = meta.get("name") or skill_md.parent.name
            if name in out:
                continue
            out[name] = {"name": name, "description": meta.get("description", ""),
                         "path": str(skill_md), "source": source}
    return list(out.values())


def load_skill(cwd: Path, name: str, settings=None) -> dict | None:
    for item in list_skills(cwd, settings):
        if item["name"] == name:
            meta, body = _frontmatter(_read(Path(item["path"])))
            return dict(item, body=body, dir=str(Path(item["path"]).parent))
    return None


def skills_prompt_section(cwd: Path, settings=None, limit: int = 40) -> str:
    """Коротко в промпт: только имена и описания; тело — тулом skill по требованию."""
    items = list_skills(cwd, settings)
    if not items:
        return ""
    lines = ["# Скиллы",
             "",
             "Доступны готовые инструкции для типовых задач. Если задача подходит под описание, "
             "сначала загрузи скилл тулом skill, потом следуй ему."]
    for item in items[:limit]:
        desc = item["description"][:200]
        lines.append("- " + item["name"] + (" — " + desc if desc else ""))
    if len(items) > limit:
        lines.append("- …и ещё " + str(len(items) - limit) + " (список — тул skill без имени)")
    return "\n".join(lines)


# --- слэш-команды (.claude/commands/*.md и родственники) ---------------------------------

def command_dirs(cwd: Path, settings=None) -> list[tuple[Path, str]]:
    from ..core.paths import luna_home

    cwd = Path(cwd)
    home = _home()
    luna = Path(settings.home_dir) if settings is not None and hasattr(settings, "home_dir") else luna_home()
    return [
        (cwd / ".luna" / "commands", "проект (.luna)"),
        (cwd / ".claude" / "commands", "проект (.claude)"),
        (cwd / ".opencode" / "command", "проект (.opencode)"),
        (luna / "commands", "свои (Луна)"),
        (home / ".claude" / "commands", "свои (.claude)"),
        (home / ".config" / "opencode" / "command", "свои (opencode)"),
    ]


def list_commands(cwd: Path, settings=None) -> list[dict]:
    """Команды из файлов: имя (с подкаталогом через «:»), описание, путь."""
    if not _setting(settings, "compat.commands", True):
        return []
    out: dict = {}
    for directory, source in command_dirs(cwd, settings):
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.md")):
            rel = path.relative_to(directory).with_suffix("")
            name = ":".join(rel.parts)
            if name in out:
                continue
            meta, body = _frontmatter(_read(path)[:4000])
            desc = meta.get("description") or (body.strip().splitlines() or [""])[0][:120]
            out[name] = {"name": name, "description": desc, "path": str(path), "source": source}
    return list(out.values())


def resolve_command(cwd: Path, name: str, args: str = "", settings=None) -> str | None:
    """Текст сообщения для `/имя аргументы` или None, если такой команды нет.

    Подстановки как у Claude Code: $ARGUMENTS — вся строка, $1…$9 — по словам.
    Строки «!`команда`» не исполняем — это обошло бы Предохранитель; модель
    увидит их как текст и сама решит, звать ли bash.
    """
    for item in list_commands(cwd, settings):
        if item["name"] != name:
            continue
        _meta, body = _frontmatter(_read(Path(item["path"])))
        words = args.split()
        text = body.replace("$ARGUMENTS", args)
        for i in range(9, 0, -1):
            text = text.replace("$" + str(i), words[i - 1] if i <= len(words) else "")
        return text.strip()
    return None


def report(cwd: Path, settings=None) -> str:
    """Что найдено в каталоге: для `luna try compat` и тула luna."""
    cwd = Path(cwd)
    lines = ["Совместимость — что Луна видит отсюда (" + str(cwd) + "):", ""]
    instr = collect_instructions(cwd, settings)
    lines.append("Инструкции: " + (str(len(instr)) if instr else "нет"))
    for path, text in instr:
        lines.append("  " + path + " — " + str(len(text)) + " симв.")
    skills = list_skills(cwd, settings)
    lines.append("Скиллы: " + (str(len(skills)) if skills else "нет"))
    for item in skills:
        lines.append("  " + item["name"] + " · " + item["source"] +
                     (" — " + item["description"][:80] if item["description"] else ""))
    commands = list_commands(cwd, settings)
    lines.append("Команды: " + (str(len(commands)) if commands else "нет"))
    for item in commands:
        lines.append("  /" + item["name"] + " · " + item["source"])
    lines.append("MCP: свои mcp.json, .mcp.json (Claude Code), opencode.json — luna mcp list")
    return "\n".join(lines)
