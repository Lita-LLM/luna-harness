"""luna doctor: Python, ОС, терминал и его возможности, каталоги, настройки.

Работает без рендерера и без живого терминала (можно из пайпа): возможности
терминала берутся из ui.term.detect_caps — это лёгкий модуль, не экраны.
"""

from __future__ import annotations

import os
import platform
import sys
import tempfile
from pathlib import Path

from .. import __version__
from ..core.paths import DataDir
from ..core.settings import SOURCE_NAMES, Settings

_OK, _WARN, _BAD = "✓", "!", "✗"


def _writable(path: Path) -> tuple[bool, str]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path, prefix=".luna-doctor-")
        os.close(fd)
        os.unlink(tmp)
        return True, "доступен на запись"
    except OSError as exc:
        return False, f"нет записи: {exc.strerror or exc}"


def run_doctor() -> int:
    settings = Settings()
    from ..ui.term import detect_caps

    caps = detect_caps(settings=settings)
    hard_fail = False
    rows: list[tuple[str, str, str]] = []

    def add(mark: str, name: str, text: str, *, hard: bool = False) -> None:
        nonlocal hard_fail
        rows.append((mark, name, text))
        if hard and mark == _BAD:
            hard_fail = True

    v = sys.version_info
    add(_OK if v >= (3, 10) else _BAD, "Python",
        f"{v.major}.{v.minor}.{v.micro} ({sys.executable})"
        + ("" if v >= (3, 10) else " — нужен 3.10 и новее"), hard=True)
    add(_OK, "ОС", f"{platform.platform()} · {sys.platform}")
    add(_OK, "Лунапарк", f"версия {__version__}")

    if caps.is_tty:
        add(_OK, "Терминал", f"{caps.cols}x{caps.rows}, TERM={caps.term or '—'}")
        add(_OK if caps.color_level != "mono" else _WARN, "Цвет",
            f"{caps.color_level} ({caps.reason})" if caps.reason else caps.color_level)
        add(_OK if caps.unicode else _WARN, "Юникод",
            "есть: эмодзи и широкие символы" if caps.unicode
            else "нет: интерфейс перейдёт на ASCII-наборы")
        if caps.unicode:
            add(_OK, "Эмодзи",
                "если координаты в терминале разъезжаются — luna set ui.unicode off "
                "(ASCII-фазы; рендерер и так якорно переставляет курсор)")
    else:
        add(_WARN, "Терминал", "вывод не в терминал: полноэкранный режим недоступен, "
                               "doctor, settings и set работают")

    data = DataDir()
    ok, text = _writable(data.root)
    add(_OK if ok else _BAD, "LUNA_HOME", f"{data.root} — {text}", hard=True)
    project_file = settings.project_path
    if project_file.exists():
        ok, text = _writable(project_file.parent)
        add(_OK if ok else _WARN, "Проект", f"{project_file.parent} — {text}")
    else:
        add(_OK, "Проект", f"{project_file} пока нет — настройки проекта не заданы")

    changed = [r for r in settings.describe() if r["source"] != "default"]
    if changed:
        desc = "; ".join(f"{r['key']}={Settings.value_text(r['type'], r['value'])} "
                         f"[{SOURCE_NAMES[r['source']]}]" for r in changed)
        add(_OK, "Настройки", f"изменено от умолчаний: {desc}")
    else:
        add(_OK, "Настройки", "все по умолчанию (luna settings — список)")
    unknown = settings.unknown_keys()
    if unknown:
        add(_WARN, "Настройки", f"ключи вне схемы (сохранятся при записи): {', '.join(unknown)}")
    add(_OK, "Файлы", f"глобальный: {settings.global_path}")

    width = max(len(name) for _, name, _ in rows)
    for mark, name, text in rows:
        print(f"  {mark} {name:<{width}}  {text}")
    if hard_fail:
        print("\nЕсть проблемы, без которых Луна не заработает — смотри строки с ✗.")
        return 1
    print("\nВсё, что помечено !, — не поломка, а урезанный режим.")
    return 0
