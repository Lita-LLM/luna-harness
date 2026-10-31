"""luna update [--check]: обновление git-копии программы.

Идеи — из опыта Бендера (bender/updates/git.py), без его слотов и подписей:
git работает с окружением хозяина, но GIT_TERMINAL_PROMPT=0 — никаких висящих
вопросов про пароль; наружу идут только понятные причины, а не сырой вывод
git (в нём бывают адреса с токенами). Обновление — только fast-forward:
локальные правки и разошедшиеся ветки автоматика не переписывает.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from ..core.errors import HumanError
from ..core.paths import PROGRAM_DIR
from ..core.stdio import ensure_utf8_stdio

_ARCHIVE_HINT = ("Обновись архивом: скачай свежий архив программы с GitHub, "
                 "распакуй в новую папку и запускай оттуда. Данные переносить "
                 "не нужно — они лежат отдельно (LUNA_HOME, по умолчанию "
                 "папка .luna в домашнем каталоге) и подхватятся сами.")


def _error(title: str, hint: str) -> HumanError:
    return HumanError("update", title, hint)


def _git_env() -> dict:
    env = dict(os.environ)
    # Как у Бендера: systemd и терминалы могут не наследовать PATH с gh,
    # а вопросы о пароле в фоне недопустимы — пусть git честно падает.
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "never"
    return env


def _git(root: Path, *args: str, timeout: int = 60) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args],
            stdin=subprocess.DEVNULL, capture_output=True,
            env=_git_env(), timeout=timeout,
        )
    except FileNotFoundError:
        raise _error("Не найден git",
                     "Поставь git (пакет git в системе или с git-scm.com) — "
                     "или обновляйся архивом: " + _ARCHIVE_HINT) from None
    except subprocess.TimeoutExpired:
        raise _error("Git не ответил вовремя",
                     "Проверь интернет и повтори. Если репозиторий приватный — "
                     "что вход выполнен: gh auth login.") from None
    out = proc.stdout.decode("utf-8", "replace").strip()
    err = proc.stderr.decode("utf-8", "replace").strip()
    return proc.returncode, out, err


def _git_fail(err: str, doing: str) -> HumanError:
    """Сырой stderr наружу не показываем: приводим частые причины к понятным."""
    low = err.lower()
    if any(s in low for s in ("401", "403", "authentication", "could not read username",
                              "could not read password", "repository not found")):
        return _error(f"Сервер не пустил к репозиторию ({doing})",
                      "Если репозиторий приватный, выполни gh auth login "
                      "(и gh auth setup-git), затем повтори.")
    if any(s in low for s in ("could not resolve host", "failed to connect",
                              "connection refused", "network is unreachable")):
        return _error(f"Нет связи с сервером обновлений ({doing})",
                      "Проверь интернет и повтори позже.")
    return _error(f"Git не смог: {doing}",
                  "Проверь интернет, права на папку программы и что ветка "
                  "отслеживает сервер (git branch --set-upstream-to).")


def run_update(check_only: bool = False, root: Path | None = None) -> int:
    ensure_utf8_stdio()  # печатаем сами — кодировка наша забота, не main()
    root = Path(root) if root is not None else PROGRAM_DIR
    if shutil.which("git") is None:
        raise _error("Не найден git",
                     "Поставь git — или обновляйся архивом: " + _ARCHIVE_HINT)
    if not (root / ".git").exists():
        raise _error("Эта копия Лунапарка установлена из архива, а не через git",
                     _ARCHIVE_HINT)

    code, branch, _ = _git(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    branch = branch.strip()
    if code != 0 or not branch:
        raise _error("Программа стоит на отдельном коммите, а не на ветке",
                     "Переключись на обычную ветку клона (git checkout main) "
                     "и повтори обновление.")
    code, upstream, _ = _git(root, "rev-parse", "--abbrev-ref",
                             "--symbolic-full-name", "@{u}")
    if code != 0 or not upstream.strip():
        raise _error(f"Для ветки {branch} не настроена ветка на сервере",
                     "Обновляться можно из клона с отслеживаемой веткой: "
                     "git branch --set-upstream-to=origin/" + branch + ".")

    code, status, _ = _git(root, "status", "--porcelain", "--untracked-files=no")
    changed = [ln[3:] for ln in status.splitlines() if ln.strip()]
    if changed:
        shown = ", ".join(changed[:5])
        if len(changed) > 5:
            extra = len(changed) - 5
            shown = shown + f" и ещё {extra}"
        raise _error(f"В папке программы есть локальные правки: {shown}",
                     "Обновление ничего не тронуло. Зафиксируй правки "
                     "коммитом или перенеси их из папки программы и повтори.")

    code, _, err = _git(root, "fetch", "--quiet", timeout=180)
    if code != 0:
        raise _git_fail(err, "получить коммиты с сервера")
    _, behind_text, _ = _git(root, "rev-list", "--count", "HEAD..@{u}")
    _, ahead_text, _ = _git(root, "rev-list", "--count", "@{u}..HEAD")
    _, head_short, _ = _git(root, "rev-parse", "--short", "HEAD")
    behind = int(behind_text or 0)
    ahead = int(ahead_text or 0)

    if behind and ahead:
        raise _error("Локальная ветка разошлась с сервером",
                     "Есть и свои коммиты, и чужие. Автоматическое обновление "
                     "их не переписывает — нужен ручной разбор (git log, merge или rebase).")
    if not behind:
        if ahead:
            print(f"На сервере нового нет: ветка {branch} опережает его на {ahead} коммит(ов), "
                  f"текущий {head_short}.")
        else:
            print(f"Лунапарк актуален: ветка {branch}, коммит {head_short}.")
        return 0

    _, log, _ = _git(root, "log", "--format=%h %s", "HEAD..@{u}")
    lines = log.splitlines()
    print(f"Есть обновление ветки {branch}: новых коммитов — {behind}.")
    for ln in lines[:10]:
        print(f"  {ln}")
    if len(lines) > 10:
        rest = len(lines) - 10
        print(f"  … и ещё {rest}")
    if check_only:
        print("Поставить: luna update")
        return 0

    code, _, err = _git(root, "pull", "--ff-only", timeout=180)
    if code != 0:
        raise _git_fail(err, "поставить новые коммиты")
    _, new_short, _ = _git(root, "rev-parse", "--short", "HEAD")
    print(f"Обновлено: {head_short} → {new_short}. "
          "Если Луна сейчас запущена — перезапусти её.")
    return 0
