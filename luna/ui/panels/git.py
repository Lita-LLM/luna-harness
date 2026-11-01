"""Панель «Git» (ЗАДАЧА-16): ветка, изменённые файлы, коммиты, дифф.

Git опрашивается подпроцессом только пока панель открыта и не чаще раза
в 3 секунды (кэш на экземпляре панели): в простое — ноль работы. Дифф —
по Enter в фокусе панели. Не репозиторий — честная строка, не ошибка.
"""

from __future__ import annotations

import subprocess
import sys
import time

from . import PanelContent, register_panel

_REFRESH_SEC = 3.0
_DIFF_LIMIT = 300


def run_git(cwd: str, *args: str) -> tuple:
    """(успех, вывод) одной git-команды; любая беда — (False, '')."""
    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW: без вспышки консоли
    try:
        proc = subprocess.run(["git", "-C", cwd, *args],
                              capture_output=True, timeout=5, **kwargs)
    except (OSError, subprocess.SubprocessError):
        return False, ""
    return proc.returncode == 0, proc.stdout.decode("utf-8", "replace")


class GitPanel(PanelContent):
    """Состояние репозитория каталога сессии; Enter — дифф и обратно."""

    def __init__(self):
        super().__init__("Git")
        self._cache_at = 0.0
        self._cache: list = []
        self._diff: list | None = None
        self.scroll = 0

    def _refresh(self, app) -> list:
        now = time.monotonic()
        if self._cache and now - self._cache_at < _REFRESH_SEC:
            return self._cache
        self._cache_at = now
        cwd = str(getattr(app, "cwd", "") or "")
        ok, _inside = run_git(cwd, "rev-parse", "--is-inside-work-tree")
        if not ok:
            self._cache = ["не git-репозиторий",
                           "каталог сессии не под git — смотреть нечего"]
            return self._cache
        _ok, branch = run_git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
        _ok, status = run_git(cwd, "status", "--porcelain")
        _ok, log = run_git(cwd, "log", "--oneline", "-5")
        changed = [line for line in status.splitlines() if line.strip()]
        out = ["ветка: " + (branch.strip() or "?"),
               "изменено файлов: " + str(len(changed))]
        out.extend("  " + line for line in changed[:40])
        out.append("")
        out.append("последние коммиты:")
        out.extend("  " + line for line in log.splitlines() if line.strip())
        out.append("")
        out.append("Enter — дифф рабочих файлов")
        self._cache = out
        return out

    def lines(self, app, width: int) -> list:
        if self._diff is not None:
            height = self.height_hint or 24
            body = self._diff[self.scroll:self.scroll + height]
            return ["дифф (Enter — назад, ↑↓ — листать)"] + body
        return self._refresh(app)

    def handle_key(self, app, key) -> bool:
        if key.name == "enter":
            if self._diff is not None:
                self._diff = None
                self.scroll = 0
                return True
            cwd = str(getattr(app, "cwd", "") or "")
            ok, diff = run_git(cwd, "diff", "--no-color")
            _ok2, staged = run_git(cwd, "diff", "--cached", "--no-color")
            text = (diff + staged).strip()
            self._diff = (text.splitlines()[:_DIFF_LIMIT]
                          if text else ["изменений в рабочих файлах нет"])
            self.scroll = 0
            return True
        if self._diff is not None:
            if key.name == "up":
                self.scroll = max(0, self.scroll - 1)
                return True
            if key.name == "down":
                self.scroll = min(max(0, len(self._diff) - 1), self.scroll + 1)
                return True
            if key.name == "pageup":
                self.scroll = max(0, self.scroll - 10)
                return True
            if key.name == "pagedown":
                self.scroll = min(max(0, len(self._diff) - 1), self.scroll + 10)
                return True
        return False


register_panel("git", "Git", GitPanel, aliases=("git", "гит"))
