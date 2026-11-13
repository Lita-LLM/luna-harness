# Перенесено из BENDER: bender/core/paths.py (коммит 6552ed660bfabf5202569f9905212b77dbf6d31a), адаптировано: каталог данных — ~/.luna (LUNA_HOME), раскладка Луне нужна своя.
"""Где лежит программа и данные."""

from __future__ import annotations

import os
from pathlib import Path

PROGRAM_DIR = Path(__file__).resolve().parents[2]


def luna_home() -> Path:
    """Каталог данных: LUNA_HOME, иначе ~/.luna."""
    env = os.environ.get("LUNA_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".luna"


def default_data_dir() -> Path:
    return luna_home()


class DataDir:
    """Раскладка папки данных Луны."""

    def __init__(self, root: Path | str | None = None):
        self.root = Path(root).expanduser().resolve() if root is not None else luna_home()
        self.settings = self.root / "settings.json"
        self.secrets = self.root / "secrets.json"
        self.sessions = self.root / "sessions"
        self.local = self.root / "local"
        self.logs = self.root / "logs"
        from . import newmoon

        if newmoon.active():
            # Новолуние: всё пишущееся — во временную папку режима.
            self.sessions = newmoon.scratch("sessions")
            self.local = newmoon.scratch("local")
            self.logs = newmoon.scratch("logs")

    def ensure(self) -> None:
        for d in (self.root, self.sessions, self.local, self.logs):
            d.mkdir(parents=True, exist_ok=True)
