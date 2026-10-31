"""luna try prompt: показать системный промпт, собранный слоями для текущего каталога.

Видно всё, что уйдёт модели: ядро, окружение этой машины, самоосознанность,
инструкции проекта и активную манеру последним слоем.
"""

from __future__ import annotations

from pathlib import Path

from ..core.stdio import ensure_utf8_stdio


def run_prompt_try(out=None) -> int:
    ensure_utf8_stdio()
    stream = out or print
    from ..core.settings import Settings
    from ..mind.prompt import build_system_prompt

    settings = Settings()
    stream(build_system_prompt(Path.cwd(), settings=settings))
    return 0
