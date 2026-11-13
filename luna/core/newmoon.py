"""Новолуние — тёмный режим без следов.

Что делает режим (включается на весь процесс, `luna --newmoon` или
`/newmoon` в чате, который перезапускает Луну в режиме):
  * без журнала сессии и без записи настроек: всё, что меняется в
    режиме, живёт в памяти до выхода (Settings.ephemeral);
  * то, что Луне всё же нужно писать (вывод тулов, чекпоинты, рой,
    логи ошибок, worktree) — во временную папку, она стирается при
    выходе (scratch());
  * каждый адрес+модель, куда уходят запросы, человек подтверждает сам:
    через прокси не понять, локально оно или в облаке, поэтому без
    подтверждения Router не отправит ни одного запроса (gate());
  * Луна знает, что она в Новолунии (prompt.py добавляет блок).

Состояние — на процесс: агент, ассистенты и рой живут в потоках того же
процесса и видят одно и то же разрешение.
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile
import threading
from pathlib import Path

ENV = "LUNA_NEWMOON"
ENV_DIR = "LUNA_NEWMOON_DIR"

_lock = threading.Lock()
_confirmed: set = set()
_cleanup_registered = False
_alive = None  # открытый и запертый файл «жив» в своей временной папке
_ALIVE = "alive.lock"


def active() -> bool:
    return os.environ.get(ENV) == "1"


def enable() -> Path:
    """Включить режим в этом процессе (и в дочерних — через окружение)."""
    global _cleanup_registered
    os.environ[ENV] = "1"
    path = os.environ.get(ENV_DIR)
    if not path or not Path(path).is_dir():
        sweep_orphans()
        path = tempfile.mkdtemp(prefix="luna-nm-")
        os.environ[ENV_DIR] = path
        _hold(Path(path))
    if not _cleanup_registered:
        atexit.register(wipe)
        _cleanup_registered = True
    return Path(path)


def scratch(kind: str = "") -> Path:
    """Временная папка режима (подпапка kind); вне режима — ошибка."""
    root = os.environ.get(ENV_DIR)
    if not active() or not root:
        raise RuntimeError("Новолуние не включено")
    path = Path(root) / kind if kind else Path(root)
    path.mkdir(parents=True, exist_ok=True)
    return path


def redirect(default: Path, kind: str) -> Path:
    """Куда писать: вне режима — default, в режиме — во временную папку."""
    return scratch(kind) if active() else default


def wipe() -> None:
    """Стереть временную папку режима (при выходе)."""
    global _alive
    root = os.environ.get(ENV_DIR)
    if _alive is not None:
        try:
            _alive.close()  # Windows не даст стереть открытый файл
        except OSError:
            pass
        _alive = None
    if root and Path(root).name.startswith("luna-nm-"):
        shutil.rmtree(root, ignore_errors=True)


def _hold(path: Path) -> None:
    """Запереть файл «жив»: пока процесс жив, папку никто не сочтёт сиротой."""
    global _alive
    from .run_lock import _lock as lock_file

    try:
        handle = open(path / _ALIVE, "a+b")
        lock_file(handle)
        _alive = handle
    except OSError:
        pass


def sweep_orphans(max_age_s: float = 86400.0) -> int:
    """Стереть временные папки Новолуния от упавших или закрытых окном
    процессов: atexit не успевает, если окно просто закрыли. Папка чужая
    живая — её файл «жив» заперт; без файла — по возрасту."""
    import time

    from .run_lock import _lock as lock_file

    gone = 0
    base = Path(tempfile.gettempdir())
    own = os.environ.get(ENV_DIR, "")
    for path in base.glob("luna-nm-*"):
        if not path.is_dir() or str(path) == own:
            continue
        marker = path / _ALIVE
        if marker.exists():
            try:
                with open(marker, "a+b") as handle:
                    lock_file(handle)  # занят — OSError: процесс жив
                    lock_file(handle, False)
            except OSError:
                continue
        else:
            try:
                if time.time() - path.stat().st_mtime < max_age_s:
                    continue  # возможно, только что создана — не трогаем
            except OSError:
                continue
        shutil.rmtree(path, ignore_errors=True)
        gone += 1
    return gone


# --- подтверждение адресов моделей -----------------------------------------


def target_key(base_url: str, model: str) -> str:
    return (base_url or "").rstrip("/").lower() + " | " + (model or "")


def confirm(base_url: str, model: str) -> None:
    with _lock:
        _confirmed.add(target_key(base_url, model))


def is_confirmed(base_url: str, model: str) -> bool:
    with _lock:
        return target_key(base_url, model) in _confirmed


def confirmed_keys() -> set:
    with _lock:
        return set(_confirmed)


def restore_confirmed(keys) -> None:
    """После горячей перезагрузки: модуль новый, подтверждения — прежние."""
    with _lock:
        _confirmed.update(str(k) for k in keys or ())


def forget_all() -> None:
    with _lock:
        _confirmed.clear()


def gate(kind: str, base_url: str, model: str) -> None:
    """Router перед каждым запросом: в Новолунии — только подтверждённое."""
    if not active() or kind == "fake":
        return
    if is_confirmed(base_url, model):
        return
    from .errors import HumanError

    raise HumanError(
        "newmoon_unconfirmed",
        "Новолуние: запросы на " + (base_url or "?") + " (" + model + ") не подтверждены",
        "Подтверди адрес и модель — выбиралка откроется сама; потом повтори сообщение.",
        data={"base_url": base_url, "model": model})


def prompt_block() -> str:
    """Что Луна знает о режиме — в системный промпт."""
    return (
        "## Новолуние\n"
        "Ты в режиме Новолуние: тёмная сторона, без следов. Сессия не "
        "журналируется, настройки не сохраняются, временные файлы Луны "
        "стираются при выходе, каждый адрес модели человек подтвердил сам. "
        "Скорее всего, работа сейчас супер-приватная или коварная 😈 — "
        "держи это в голове:\n"
        "- не записывай ничего «на память» (AGENTS.md, заметки, память "
        "проекта), если человек прямо не попросил;\n"
        "- не создавай лишних файлов и не оставляй черновиков, а если "
        "временный файл нужен — удали его, когда закончишь;\n"
        "- веб-поиск и запросы к сайтам уходят наружу и видны их владельцам: "
        "используй их, только когда без них никак, и скажи об этом;\n"
        "- не пересказывай секретное без нужды и не задавай лишних вопросов "
        "о мотивах — человек выбрал этот режим осознанно."
    )
