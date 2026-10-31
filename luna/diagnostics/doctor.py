"""luna doctor: Python, ОС, терминал и его возможности, каталоги, настройки.

Работает без рендерера и без живого терминала (можно из пайпа): возможности
терминала берутся из ui.term.detect_caps — это лёгкий модуль, не экраны.
"""

from __future__ import annotations

import os
import platform
import re
import sys
import tempfile
from pathlib import Path

from .. import __version__
from ..core.paths import DataDir
from ..core.settings import SOURCE_NAMES, Settings
from ..core.stdio import ensure_utf8_stdio

_OK, _WARN, _BAD = "✓", "!", "✗"


def launcher_path() -> Path:
    """Куда установщики кладут команду luna (install.sh / install.ps1)."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
        return root / "Luna" / "bin" / "luna.cmd"
    prefix = os.environ.get("PREFIX", "")
    if os.environ.get("TERMUX_VERSION") or "com.termux" in prefix:
        return Path(prefix or "/data/data/com.termux/files/usr") / "bin" / "luna"
    return Path.home() / ".local" / "bin" / "luna"


def _expand_windows_vars(value: str) -> str:
    """Раскрыть %VAR% по текущему окружению; неизвестные оставить как есть.

    Имена переменных Windows нечувствительны к регистру — ищем без учёта
    регистра, поэтому функция честна и в тестах на POSIX.
    """
    env = {key.upper(): val for key, val in os.environ.items()}

    def repl(match: re.Match) -> str:
        return env.get(match.group(1).upper(), match.group(0))

    return re.sub(r"%([^%]+)%", repl, value)


def _long_path(value: str) -> str:
    """Длинное имя пути через WinAPI: 8.3 вроде RUNNER~1 — в обычное.

    Только живая Windows и существующий путь; во всех остальных случаях
    (и при любой ошибке WinAPI) возвращаем строку как есть.
    """
    if sys.platform != "win32" or not os.path.exists(value):
        return value
    try:
        import ctypes

        get_long = ctypes.windll.kernel32.GetLongPathNameW
        get_long.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
        get_long.restype = ctypes.c_uint
        buf = ctypes.create_unicode_buffer(32768)
        if get_long(value, buf, len(buf)):
            return buf.value
    except (AttributeError, OSError):
        pass
    return value


def _normalize_dir(value: str, *, windows: bool) -> str:
    """Привести запись каталога к виду, пригодному для сравнения с PATH.

    windows=True включает семантику Windows независимо от текущей ОС:
    раскрываются %VAR%, разделители / и \\ равны, регистр не важен,
    на живой Windows разрешаются и короткие имена 8.3.
    """
    out = value.strip().strip('"').strip()
    if windows:
        out = _long_path(_expand_windows_vars(out))
        out = out.replace("/", "\\")
    out = out.rstrip("\\/")
    return out.casefold() if windows else out


def _dir_in_path(directory: Path, *, windows: bool | None = None,
                 path: str | None = None) -> bool:
    """Есть ли каталог в PATH. windows/path — явные переопределения для тестов:
    «окно» на POSIX гоняется тем же кодом, что живое на Windows."""
    if windows is None:
        windows = sys.platform == "win32"
    raw = os.environ.get("PATH", "") if path is None else path
    target = _normalize_dir(str(directory), windows=windows)
    if not target:
        return False
    sep = ";" if windows else os.pathsep
    for entry in raw.split(sep):
        if entry.strip() and _normalize_dir(entry, windows=windows) == target:
            return True
    return False


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
    ensure_utf8_stdio()  # печатаем сами (✓/✗) — кодировка наша забота, не main()
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
    launcher = launcher_path()
    if launcher.exists():
        if _dir_in_path(launcher.parent):
            add(_OK, "Команда luna", f"установлена: {launcher}")
        else:
            add(_WARN, "Команда luna",
                f"установлена: {launcher} — но папка не в PATH: "
                "запускай полным путём или добавь папку в PATH")
    else:
        add(_WARN, "Команда luna",
            "не установлена — запусти install.sh (Linux, macOS, Termux) "
            "или install.bat (Windows) из папки программы")

    if caps.is_tty:
        term_name = caps.term or "—"
        add(_OK, "Терминал", f"{caps.cols}x{caps.rows}, TERM={term_name}")
        add(_OK if caps.color_level != "mono" else _WARN, "Цвет",
            f"{caps.color_level} ({caps.reason})" if caps.reason else caps.color_level)
        add(_OK if caps.unicode else _WARN, "Юникод",
            "есть: широкие символы" if caps.unicode
            else "нет: интерфейс перейдёт на ASCII-наборы")
        if caps.emoji:
            emoji_text = "включены (" + caps.emoji_reason + ")"
        else:
            emoji_text = ("выключены (" + caps.emoji_reason + "); фазы луны — "
                          "узким набором, вернуть: luna set ui.emoji on")
        add(_OK if caps.emoji else _WARN, "Эмодзи", emoji_text)
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
        parts = []
        for r in changed:
            key = r["key"]
            value_text = Settings.value_text(r["type"], r["value"])
            source_name = SOURCE_NAMES[r["source"]]
            parts.append(f"{key}={value_text} [{source_name}]")
        desc = "; ".join(parts)
        add(_OK, "Настройки", f"изменено от умолчаний: {desc}")
    else:
        add(_OK, "Настройки", "все по умолчанию (luna settings — список)")
    unknown = settings.unknown_keys()
    if unknown:
        unknown_text = ", ".join(unknown)
        add(_WARN, "Настройки", f"ключи вне схемы (сохранятся при записи): {unknown_text}")
    add(_OK, "Файлы", f"глобальный: {settings.global_path}")

    width = max(len(name) for _, name, _ in rows)
    for mark, name, text in rows:
        print(f"  {mark} {name:<{width}}  {text}")
    if hard_fail:
        print("\nЕсть проблемы, без которых Луна не заработает — смотри строки с ✗.")
        return 1
    print("\nВсё, что помечено !, — не поломка, а урезанный режим.")
    return 0
