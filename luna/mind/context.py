# Перенесено из BENDER: bender/mind/context.py (коммит 6552ed660bfabf5202569f9905212b77dbf6d31a), сокращено.
"""Сборка контекста: системный промпт + инструкции проекта.

Инструкции ищем в рабочем каталоге и выше: AGENTS.md, CLAUDE.md, GEMINI.md
(совместимость с чужими харнессами — ПЛАН, раздел 3). Объём ограничен,
чтобы чужие инструкции не съели контекст.
"""

from __future__ import annotations

from pathlib import Path

INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md", "GEMINI.md")
MAX_FILE_CHARS = 4000
MAX_TOTAL_CHARS = 12000

def collect_instructions(cwd: Path) -> list[tuple[str, str]]:
    """(путь, текст) инструкций из каталога и родителей, ближе к каталогу — раньше."""
    found: list[tuple[str, str]] = []
    total = 0
    current = Path(cwd).resolve()
    chain = [current, *current.parents]
    for directory in chain:
        for name in INSTRUCTION_FILES:
            path = directory / name
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            text = text[:MAX_FILE_CHARS]
            if total + len(text) > MAX_TOTAL_CHARS:
                text = text[: max(0, MAX_TOTAL_CHARS - total)]
            if text:
                found.append((str(path), text))
                total += len(text)
            if total >= MAX_TOTAL_CHARS:
                return found
    return found


def build_system_prompt(cwd: Path, mode: str = "normal", settings=None) -> str:
    """Совместимый вход для старых импортов: настоящая сборка слоёв — luna.mind.prompt."""
    from .prompt import build_system_prompt as _build

    return _build(cwd, mode, settings=settings)
