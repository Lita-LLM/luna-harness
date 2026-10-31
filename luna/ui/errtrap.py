"""Ловушка ошибок на время полноэкранного режима (ЗАДАЧА-06, п. 2).

Пока чат владеет экраном, сырой трейсбек в stderr ломает картинку:
упавший фоновый поток печатает себя прямо поверх ленты. ErrorTrap на
время сеанса перехватывает все три пути, которыми ошибка может попасть
на экран:

- threading.excepthook — трейсбек в лог-файл, событие error в очередь;
- sys.excepthook — то же для главного потока (+ прежний хук в конце:
  его печать уйдёт в тот же лог, потому что stderr уже перенаправлен);
- sys.stderr — перенаправлен в тот же лог-файл в LUNA_HOME/logs.

Главный цикл чата забирает события из drain() в ленту, как события
источника. uninstall() возвращает прежние хуки и stderr.
"""

from __future__ import annotations

import queue
import sys
import threading
import time
import traceback
from pathlib import Path


class ErrorTrap:
    def __init__(self, log_path):
        self.log_path = Path(log_path)
        self._queue: queue.Queue = queue.Queue()
        self._file = None
        self._old_sys_hook = None
        self._old_thread_hook = None
        self._old_stderr = None
        self._installed = False

    # --- установка/снятие ---------------------------------------------------

    def install(self) -> None:
        if self._installed:
            return
        self._installed = True
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self.log_path, "a", encoding="utf-8",
                          errors="replace", buffering=1)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        self._file.write("=== сеанс чата, ловушка ошибок включена: "
                         + stamp + " ===\n")
        self._old_sys_hook = sys.excepthook
        self._old_thread_hook = threading.excepthook
        self._old_stderr = sys.stderr
        sys.excepthook = self._sys_hook
        threading.excepthook = self._thread_hook
        sys.stderr = self._file

    def uninstall(self) -> None:
        if not self._installed:
            return
        self._installed = False
        if self._old_sys_hook is not None:
            sys.excepthook = self._old_sys_hook
        if self._old_thread_hook is not None:
            threading.excepthook = self._old_thread_hook
        if self._old_stderr is not None:
            sys.stderr = self._old_stderr
        self._old_sys_hook = self._old_thread_hook = self._old_stderr = None
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                pass
            self._file = None

    # --- события для ленты -----------------------------------------------------

    @property
    def pending(self) -> bool:
        return not self._queue.empty()

    def drain(self) -> list:
        out = []
        while True:
            try:
                out.append(self._queue.get_nowait())
            except queue.Empty:
                return out

    # --- хуки -------------------------------------------------------------------

    def _report(self, where: str, exc_type, exc, tb) -> None:
        try:
            detail = "".join(traceback.format_exception(exc_type, exc, tb))
            if self._file is not None:
                self._file.write("--- " + where + " ---\n" + detail)
                self._file.flush()
            message = where + ": " + exc_type.__name__ + ": " + str(exc)
            self._queue.put({"type": "error", "message": message,
                             "detail": detail})
        except Exception:  # noqa: BLE001 — ловушка не имеет права падать сама
            pass

    def _thread_hook(self, args) -> None:
        name = getattr(args.thread, "name", "?")
        self._report("Фоновый поток " + str(name) + " упал",
                     args.exc_type, args.exc_value, args.exc_traceback)

    def _sys_hook(self, exc_type, exc, tb) -> None:
        self._report("Главный поток упал", exc_type, exc, tb)
        if self._old_sys_hook is not None:
            # Прежний хук печатает в sys.stderr — а это уже лог-файл.
            self._old_sys_hook(exc_type, exc, tb)
