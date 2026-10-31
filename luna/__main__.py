"""Точка входа: python -m luna [doctor | try <что> | settings | set <ключ> <значение> | --version].

Импорты тяжёлого — ленивые, внутри команд: --version и doctor не тянут рендерер.
Время старта фиксируем при импорте модуля: заставка покажет, за сколько миллисекунд
процесс дошёл до первого кадра.
"""

from __future__ import annotations

import os
import sys
import time

_T0 = time.monotonic()

from . import __version__  # noqa: E402 — лёгкий импорт, только строка версии
from .core.stdio import ensure_utf8_stdio  # noqa: E402 — лёгкий: внутри только sys


def _note_start_marker() -> None:
    """Метка старта процесса для замеров: если задан LUNA_START_FILE, пишем
    туда монотонное время старта и PID самой Луны. Родитель (luna try
    measure на Windows) считает старт от этой метки внутри процесса,
    а не от Popen — иначе в замер падает создание новой консоли —
    и меряет память/ЦП по этому PID, а не по обёртке (ЗАДАЧА-03)."""
    path = os.environ.get("LUNA_START_FILE")
    if not path:
        return
    try:
        with open(path, "w", encoding="ascii") as f:
            f.write(repr(_T0) + "\n" + str(os.getpid()) + "\n")
    except OSError:
        pass


_note_start_marker()

USAGE = """Лунапарк — сессионный ИИ-агент.

  luna                      полноэкранный режим: заставка и минимальный экран
  luna doctor               самопроверка: Python, терминал, каталоги, настройки
  luna settings             список настроек со значениями и источниками
  luna set <ключ> <значение>  изменить настройку (например: luna set ui.splash off)
  luna try render           витрина рендерера: палитра, градиенты, фазы луны
  luna try splash           заставка ещё раз
  luna try snapshot <экран> снимок экрана в HTML: splash | render | main
  luna try measure [--json] замеры: старт, память и ЦП в простое
  luna --version            версия
"""


def _print_error(err) -> int:
    print(f"Хьюстон, у нас проблема: {err.title}")
    if err.hint:
        print(f"  {err.hint}")
    if err.detail:
        print("  Подробности:")
        for ln in err.detail.strip().splitlines()[-15:]:
            print(f"    {ln}")
    return 2


def _cmd_settings() -> int:
    from .core.settings import SOURCE_NAMES, Settings

    settings = Settings()
    rows = settings.describe()
    width = max(len(r["key"]) for r in rows)
    for r in rows:
        value = Settings.value_text(r["type"], r["value"])
        key = r["key"]
        source_name = SOURCE_NAMES[r["source"]]
        title = r["title"]
        print(f"{key:<{width}}  {value:<10}  [{source_name}]  {title}")
    unknown = settings.unknown_keys()
    if unknown:
        print()
        print("В файлах есть ключи вне схемы (сохраняются, но пока не действуют):")
        for key in unknown:
            print(f"  {key} = {settings.get(key)!r}")
    print()
    print(f"Глобальный файл: {settings.global_path}")
    print(f"Проектный файл:  {settings.project_path} (сильнее глобального)")
    return 0


def _cmd_set(argv: list[str]) -> int:
    from .core.errors import HumanError
    from .core.settings import Settings

    if len(argv) != 2:
        raise HumanError("usage", "Нужно: luna set <ключ> <значение>",
                         "Например: luna set ui.splash off. Список: luna settings.")
    key, text = argv
    settings = Settings()
    value = settings.parse_value(key, text)
    settings.set(key, value)
    print(f"{key} = {Settings.value_text(settings_value_type(key), value)} (записано в {settings.global_path})")
    return 0


def settings_value_type(key: str) -> str:
    from .core.settings import SCHEMA

    field = SCHEMA.get(key)
    return field.type if field else "str"


def main(argv: list[str] | None = None) -> int:
    ensure_utf8_stdio()
    argv = list(sys.argv[1:] if argv is None else argv)
    from .core.errors import HumanError

    try:
        if not argv:
            from .ui.app import run_fullscreen

            return run_fullscreen(started_at=_T0)
        cmd, rest = argv[0], argv[1:]
        if cmd in ("--version", "-V", "version"):
            print(f"Лунапарк {__version__}")
            return 0
        if cmd in ("--help", "-h", "help"):
            print(USAGE)
            return 0
        if cmd == "doctor":
            from .diagnostics.doctor import run_doctor

            return run_doctor()
        if cmd == "settings":
            return _cmd_settings()
        if cmd == "set":
            return _cmd_set(rest)
        if cmd == "try":
            if not rest:
                raise HumanError("usage", "Нужно: luna try render | splash | measure", "")
            what = rest[0]
            if what == "render":
                from .ui.render_try import run_render_demo

                return run_render_demo(started_at=_T0)
            if what == "splash":
                from .ui.splash import run_splash_demo

                return run_splash_demo(started_at=_T0)
            if what == "measure":
                from .devtools.measure import run_measure

                return run_measure(as_json="--json" in rest)
            if what == "snapshot":
                from .devtools.snapshot import run_snapshot

                return run_snapshot(rest[1:])
            raise HumanError("usage", f"Не знаю такого try: {what}",
                             "Есть: render, splash, measure, snapshot.")
        raise HumanError("usage", f"Неизвестная команда: {cmd}", "Справка: luna --help.")
    except HumanError as err:
        return _print_error(err)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
