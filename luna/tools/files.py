# Перенесено из BENDER: bender/tools/files.py (коммит 6552ed660bfabf5202569f9905212b77dbf6d31a), сокращено.
"""Файловые тулы: read, write, edit (точная замена), glob, grep."""

from __future__ import annotations

import re
from pathlib import Path

from .registry import Tool, ToolResult

SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv"}


def _resolve(ctx, raw: str) -> Path:
    path = Path(raw or ".")
    if not path.is_absolute():
        path = ctx.cwd / path
    return path


def _read(ctx, args) -> ToolResult:
    path = _resolve(ctx, args.get("path", ""))
    if not path.is_file():
        return ToolResult(False, "Нет такого файла: " + str(path))
    raw = path.read_bytes()
    if b"\x00" in raw[:4096]:
        return ToolResult(False, "Файл похож на бинарный, текстом не читаю: " + str(path))
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    offset = int(args.get("offset") or 1)
    limit = int(args.get("limit") or 2000)
    start = max(0, offset - 1)
    chunk = lines[start:start + limit]
    numbered = "\n".join(str(start + i + 1) + "\t" + line for i, line in enumerate(chunk))
    return ToolResult(True, numbered or "(пустой файл)")


def _checkpoint(ctx, path) -> None:
    """Снимок прежнего содержимого для отката (ЗАДАЧА-13), если включён."""
    store = getattr(ctx, "checkpoint", None)
    if store is not None:
        store.note_file(path)


def _write(ctx, args) -> ToolResult:
    path = _resolve(ctx, args.get("path", ""))
    content = args.get("content", "")
    _checkpoint(ctx, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    lines = content.count("\n") + (1 if content else 0)
    return ToolResult(True, "Записано: " + str(path) + " (" + str(len(content)) +
                      " символов, " + str(lines) + " строк)")


def _edit(ctx, args) -> ToolResult:
    path = _resolve(ctx, args.get("path", ""))
    if not path.is_file():
        return ToolResult(False, "Нет такого файла: " + str(path))
    text = path.read_text(encoding="utf-8")
    old = args.get("old", "")
    new = args.get("new", "")
    if not old:
        return ToolResult(False, "Пустой old: нечего заменять.")
    count = text.count(old)
    if count == 0:
        return ToolResult(False, "Фрагмент не найден в файле (замена должна быть точной, с отступами).")
    if count > 1 and not args.get("replace_all"):
        found = str(count)
        return ToolResult(False, "Фрагмент встречается " + found +
                          " раз; уточни контекст или поставь replace_all.")
    _checkpoint(ctx, path)
    path.write_text(text.replace(old, new), encoding="utf-8")
    return ToolResult(True, "Заменено в " + str(path) + ": вхождений " + str(count))


def _glob(ctx, args) -> ToolResult:
    pattern = args.get("pattern", "**/*")
    base = _resolve(ctx, args.get("path", "."))
    if not base.is_dir():
        return ToolResult(False, "Нет такого каталога: " + str(base))
    out = []
    for found in sorted(base.glob(pattern)):
        if any(part in SKIP_DIRS for part in found.parts):
            continue
        if found.is_file():
            out.append(str(found.relative_to(base)))
        if len(out) >= 200:
            break
    return ToolResult(True, "\n".join(out) if out else "(ничего не нашлось)")


def _grep(ctx, args) -> ToolResult:
    pattern = args.get("pattern", "")
    if not pattern:
        return ToolResult(False, "Пустой pattern.")
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return ToolResult(False, "Плохой regex: " + str(exc))
    base = _resolve(ctx, args.get("path", "."))
    out: list[str] = []
    files = [base] if base.is_file() else sorted(
        p for p in base.rglob("*") if p.is_file() and not any(d in SKIP_DIRS for d in p.parts))
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeDecodeError):
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                rel = path.name if base.is_file() else str(path.relative_to(base))
                out.append(rel + ":" + str(number) + ": " + line.strip()[:200])
                if len(out) >= 100:
                    return ToolResult(True, "\n".join(out) + "\n…[больше 100 совпадений]")
    return ToolResult(True, "\n".join(out) if out else "(совпадений нет)")


def read_tool() -> Tool:
    return Tool("read", "Прочитать текстовый файл с номерами строк.",
                {"type": "object", "properties": {
                    "path": {"type": "string"},
                    "offset": {"type": "integer", "description": "первая строка, с 1"},
                    "limit": {"type": "integer", "description": "сколько строк"}},
                 "required": ["path"]}, _read)


def write_tool() -> Tool:
    return Tool("write", "Записать файл целиком (создаёт каталоги).",
                {"type": "object", "properties": {
                    "path": {"type": "string"}, "content": {"type": "string"}},
                 "required": ["path", "content"]}, _write, mutating=True)


def edit_tool() -> Tool:
    return Tool("edit", "Точная замена фрагмента в файле (old должен встречаться один раз).",
                {"type": "object", "properties": {
                    "path": {"type": "string"}, "old": {"type": "string"},
                    "new": {"type": "string"}, "replace_all": {"type": "boolean"}},
                 "required": ["path", "old", "new"]}, _edit, mutating=True)


def glob_tool() -> Tool:
    return Tool("glob", "Найти файлы по шаблону (например **/*.py).",
                {"type": "object", "properties": {
                    "pattern": {"type": "string"}, "path": {"type": "string"}},
                 "required": ["pattern"]}, _glob)


def grep_tool() -> Tool:
    return Tool("grep", "Поиск по содержимому файлов (регулярное выражение).",
                {"type": "object", "properties": {
                    "pattern": {"type": "string"}, "path": {"type": "string"}},
                 "required": ["pattern"]}, _grep)
