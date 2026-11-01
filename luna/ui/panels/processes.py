"""Панель «Процессы» (ЗАДАЧА-16): фоновые bash сессии.

Задачи берутся из событий (PanelData.jobs: вызов bash с background=true и
pid из текста результата). Живость процесса и хвост лога проверяются
только пока панель открыта — в фоне никакого опроса нет. Остановка —
клавиша s в фокусе панели: SIGTERM на POSIX, TerminateProcess на Windows.
"""

from __future__ import annotations

import os
import sys

from . import PanelContent, register_panel
from .builtin import panel_data

_TAIL_LINES = 8
_TAIL_BYTES = 8192


def proc_alive(pid: int) -> bool:
    """Жив ли процесс — без psutil: POSIX kill(0), Windows OpenProcess."""
    if not pid:
        return False
    if sys.platform == "win32":
        try:
            import ctypes

            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:  # noqa: BLE001 — не вышло проверить — считаем завершённым
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def proc_stop(pid: int) -> bool:
    """Остановить процесс; True — сигнал/завершение ушло."""
    if not pid:
        return False
    if sys.platform == "win32":
        try:
            import ctypes

            handle = ctypes.windll.kernel32.OpenProcess(0x0001, False, pid)
            if not handle:
                return False
            try:
                return bool(ctypes.windll.kernel32.TerminateProcess(handle, 1))
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001
            return False
    try:
        import signal

        os.kill(pid, signal.SIGTERM)
        return True
    except OSError:
        return False


def _log_tail(path: str) -> list:
    """Последние строки файла лога (читаем только хвост, не весь файл)."""
    if not path:
        return []
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - _TAIL_BYTES))
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        return []
    return [line for line in text.splitlines() if line.strip()][-_TAIL_LINES:]


class ProcessesPanel(PanelContent):
    """Фоновые задачи: живы/завершены, хвост вывода, остановка."""

    def __init__(self):
        super().__init__("Процессы")
        self.selected = 0
        self.tail_for: int | None = None  # pid, чей хвост развёрнут

    def lines(self, app, width: int) -> list:
        jobs = panel_data(app).jobs
        if not jobs:
            return ["фоновых задач нет",
                    "сюда попадут bash с background=true"]
        self.selected = max(0, min(self.selected, len(jobs) - 1))
        out = []
        for i, job in enumerate(jobs):
            alive = proc_alive(job["pid"])
            mark = "▸ " if i == self.selected else "  "
            state = "жив" if alive else "завершён"
            out.append(mark + "pid " + str(job["pid"]) + " · " + state)
            out.append("    " + (job["command"] or "")[:max(8, width - 4)])
            if self.tail_for == job["pid"] and job["pid"]:
                tail = _log_tail(job["log"])
                if tail:
                    out.extend("    " + line for line in tail)
                else:
                    out.append("    (лог пуст или недоступен: "
                               + (job["log"] or "путь не пришёл") + ")")
        out.append("Enter — хвост лога · s — остановить выбранный")
        return out

    def handle_key(self, app, key) -> bool:
        jobs = panel_data(app).jobs
        if not jobs:
            return False
        self.selected = max(0, min(self.selected, len(jobs) - 1))
        if key.name == "up":
            self.selected = max(0, self.selected - 1)
            return True
        if key.name == "down":
            self.selected = min(len(jobs) - 1, self.selected + 1)
            return True
        if key.name == "enter":
            pid = jobs[self.selected]["pid"]
            self.tail_for = None if self.tail_for == pid else pid
            return True
        if key.char in ("s", "x") and not key.mods:
            job = jobs[self.selected]
            if proc_alive(job["pid"]):
                proc_stop(job["pid"])
            return True
        return False


register_panel("processes", "Процессы", ProcessesPanel,
               aliases=("processes", "процессы", "ps"))
