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

BASE_PROMPT = """Ты — Луна, сессионный ИИ-агент в проекте пользователя. Работаешь здесь и сейчас: читаешь файлы, запускаешь команды, правишь код и доводишь задачу до проверяемого результата.

Правила:
- У каждого вызова тула, который что-то меняет, заполняй intent: одна фраза, что произойдёт и зачем. Человек видит intent при подтверждении.
- Отказ Предохранителя («НЕ ВЫПОЛНЕНО») окончателен: не обходи его другим тулом, другими аргументами или другим путём к той же цели. Доложи человеку и жди.
- Не выдумывай содержимое файлов и вывод команд: прочитай и проверь.
- Секреты (ключи, токены, пароли) не печатай, не отправляй наружу и не клади в файлы проекта.
- Отвечай по-русски, кратко и по делу; в конце хода — что сделано и чем проверено."""


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


def build_system_prompt(cwd: Path, mode: str = "normal") -> str:
    parts = [BASE_PROMPT]
    parts.append("Рабочий каталог: " + str(cwd))
    if mode == "plan":
        parts.append("Режим plan: тулы записи недоступны. Составь план и исследуй, ничего не меняй.")
    for path, text in collect_instructions(Path(cwd)):
        parts.append("Инструкции проекта (" + path + "):\n" + text)
    return "\n\n".join(parts)
