# Перенесено из BENDER: bender/mind/context.py (коммит 6552ed660bfabf5202569f9905212b77dbf6d31a), сокращено.
"""Сборка контекста: системный промпт + инструкции проекта.

Инструкции, скиллы и команды других харнессов — luna.mind.compat.
"""

from __future__ import annotations

from pathlib import Path

from .compat import MAX_FILE_CHARS, MAX_TOTAL_CHARS, PROJECT_FILES  # noqa: F401 — старые импорты

INSTRUCTION_FILES = PROJECT_FILES


def collect_instructions(cwd: Path, settings=None) -> list[tuple[str, str]]:
    """(путь, текст) инструкций проекта и глобальных — см. luna.mind.compat."""
    from .compat import collect_instructions as _collect

    return _collect(cwd, settings)


def build_system_prompt(cwd: Path, mode: str = "normal", settings=None) -> str:
    """Совместимый вход для старых импортов: настоящая сборка слоёв — luna.mind.prompt."""
    from .prompt import build_system_prompt as _build

    return _build(cwd, mode, settings=settings)
