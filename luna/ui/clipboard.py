"""Буфер обмена из полноэкранного режима (ЗАДАЧА-06, п. 4).

Основной путь — OSC 52: приложение пишет escape-последовательность,
а терминал сам кладёт текст в системный буфер — доступ к системе не
нужен, работает и по ssh. На Windows добавляем запасной путь clip.exe:
старые ConPTY-хосты OSC 52 не понимают. Что сработало — возвращает
copy_text строкой («osc52», «clip», «osc52+clip» или «none»).
"""

from __future__ import annotations

import base64
import subprocess
import sys

IS_WINDOWS = sys.platform == "win32"


def osc52_sequence(text: str) -> str:
    """OSC 52: задать буфер обмена терминала текстом (UTF-8, base64)."""
    payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
    return "\x1b]52;c;" + payload + "\a"


def _clip_exe(text: str) -> bool:
    """clip.exe принимает текст в кодовой странице консоли — отдаём байты."""
    try:
        import locale

        encoding = locale.getpreferredencoding(False) or "utf-8"
        proc = subprocess.run(["clip.exe"],
                              input=text.encode(encoding, errors="replace"),
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=5, check=False)
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def copy_text(text: str, write=None) -> str:
    """Скопировать текст всеми доступными путями; вернуть, что сработало.

    write — функция записи в терминал (для OSC 52); без неё — только clip.exe.
    """
    ways = []
    if write is not None:
        write(osc52_sequence(text))
        ways.append("osc52")
    if IS_WINDOWS and _clip_exe(text):
        ways.append("clip")
    return "+".join(ways) if ways else "none"
