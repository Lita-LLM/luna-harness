"""Открыть файл во внешнем редакторе из полноэкранного чата.

Редактор — $EDITOR, на Windows без него — notepad, на остальных —
первый найденный из vi/nano. Терминал на время редактора ставится на
паузу хозяином (term.suspend()/resume()): редактор получает настоящий
терминал, а не наш альтернативный экран.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path


def editor_argv(path) -> list:
    """Команда редактора для файла: $EDITOR + путь, иначе запасной."""
    env = (os.environ.get("EDITOR") or "").strip()
    if env:
        if sys.platform == "win32":
            return [env, str(path)]
        return shlex.split(env) + [str(path)]
    if sys.platform == "win32":
        return ["notepad", str(path)]
    for name in ("vi", "nano", "editor"):
        found = shutil.which(name)
        if found:
            return [found, str(path)]
    return ["vi", str(path)]


def run_editor(path) -> None:
    """Запустить редактор и дождаться; OSError — если не вышло."""
    subprocess.run(editor_argv(Path(path)), check=False)
