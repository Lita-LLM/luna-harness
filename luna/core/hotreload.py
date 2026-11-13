"""Горячая перезагрузка: свежий код Луны в том же процессе, без потери сессии.

Чат складывает снимок (обычные словари — без объектов старого кода),
модули luna.* выгружаются и импортируются заново, новый run_chat
поднимается из снимка. На диск ничего не пишется: сессия Новолуния
переживает перезагрузку, оставаясь только в памяти. Если новый код не
поднялся — старые модули возвращаются на место и чат идёт на них.
"""

from __future__ import annotations

import importlib
import sys

PACKAGE = "luna"


def _ours(name: str) -> bool:
    return name == PACKAGE or name.startswith(PACKAGE + ".")


def purge() -> dict:
    """Выгрузить модули Луны; вернуть их, чтобы можно было откатиться."""
    saved = {name: mod for name, mod in sys.modules.items() if _ours(name)}
    for name in saved:
        del sys.modules[name]
    importlib.invalidate_caches()
    return saved


def restore(saved: dict) -> None:
    """Откат: убрать недогруженные новые модули, вернуть старые."""
    for name in [n for n in sys.modules if _ours(n)]:
        del sys.modules[name]
    sys.modules.update(saved)


def fresh_chat_module():
    return importlib.import_module(PACKAGE + ".ui.chat")
