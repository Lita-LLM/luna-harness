# Перенесено из BENDER: bender/tools/shell.py (коммит 6552ed660bfabf5202569f9905212b77dbf6d31a), сокращено.
"""Тул bash: таймаут, фоновые команды, обрезка большого вывода в файл."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .registry import Tool, ToolResult

IS_WINDOWS = sys.platform == "win32"


def shell_name() -> str:
    """Какая оболочка реально исполняет команды — модель должна это знать."""
    if IS_WINDOWS:
        return "cmd.exe (Windows): dir, type, findstr, where; PowerShell — через powershell -NoProfile -Command \"...\""
    return os.path.basename(os.environ.get("SHELL", "") or "/bin/sh")


def _decode(raw: bytes) -> str:
    # Вывод команд на Windows бывает в UTF-8, в OEM-кодировке консоли (cp866 у dir)
    # или в ANSI (cp1251); text=True берёт только одну и падает на чужих байтах.
    if not raw:
        return ""
    raw = raw.replace(b"\r\n", b"\n")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    if IS_WINDOWS:
        try:
            import ctypes

            for cp in (ctypes.windll.kernel32.GetOEMCP(), ctypes.windll.kernel32.GetACP()):
                try:
                    return raw.decode("cp" + str(cp))
                except (UnicodeDecodeError, LookupError):
                    continue
        except Exception:  # noqa: BLE001 — нет ctypes/kernel32: ниже мягкий вариант
            pass
    return raw.decode("utf-8", errors="replace")


def _group_kwargs() -> dict:
    # Своя группа процессов: прерывание убивает всё дерево одной группой
    # (POSIX — killpg по группе, Windows — taskkill /T по корню), а не
    # только оболочку, иначе дети переживут прерванную команду.
    if IS_WINDOWS:
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


def kill_tree(proc) -> None:
    """Убить процесс со всем деревом детей; уже умерший — не ошибка."""
    if proc.poll() is not None:
        return
    if IS_WINDOWS:
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, timeout=10)
        except Exception:  # noqa: BLE001 — taskkill мог не успеть/не найтись
            pass
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    if proc.poll() is None:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def _sandbox_preexec(spec, timeout: int):
    """rlimit для процесса ячейки (POSIX): память и число процессов —
    жёстко, время — ещё и RLIMIT_CPU поверх опроса по таймауту тула.
    Контейнерам не нужно: у них свои --memory/--pids-limit."""
    memory = int(getattr(spec, "memory_mb", 0) or 0)
    procs = int(getattr(spec, "max_processes", 0) or 0)
    cpu = max(1, int(timeout) + 5)

    def apply() -> None:
        import resource

        if memory:
            limit = memory * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        if procs:
            resource.setrlimit(resource.RLIMIT_NPROC, (procs, procs))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 1))

    return apply


# Поверх того, что прячет песочница роя: чаты и ключи других харнессов.
# Работник с bash не должен вычитать их через «/ только для чтения».
_CAGE_HIDE_DIRS = (".claude", ".codex", ".gemini", ".config/opencode")
_CONTAINER_CLIENT_ENV = ("HOME", "XDG_RUNTIME_DIR", "DOCKER_HOST", "CONTAINER_HOST",
                         "DOCKER_CONFIG")


def _cage_workdir(ctx, cage) -> Path:
    if cage.fs_mode == "rw" and cage.fs_roots:
        return Path(cage.fs_roots[0])
    scratch = getattr(ctx, "scratch_dir", None)
    if scratch is None:
        import tempfile

        scratch = Path(tempfile.mkdtemp(prefix="luna-cage-sh-"))
        ctx.scratch_dir = scratch  # уберёт Agent.cleanup()
    return Path(scratch)


def _cage_spec(ctx, cage):
    """(SandboxSpec, None) или (None, причина отказа). Без проверенной
    изоляции bash клетки не запускается никогда — ни на Windows, ни при
    starfall.sandbox=none."""
    if cage.bash != "sandbox":
        return None, "клетка: bash не выдан (bash: off)."
    from ..starfall.sandbox import BACKEND_ORDER, resolve_spec

    workdir = _cage_workdir(ctx, cage)
    spec = resolve_spec(ctx.settings, workdir, net=False)
    if spec.unavailable or spec.backend not in BACKEND_ORDER or not spec.verified:
        reason = str(getattr(spec, "reason", "") or "бэкенд не найден")
        return None, ("клетка: изоляция bash недоступна (" + reason + "). "
                      "Команда НЕ выполнена: без песочницы клетка bash не запускает.")
    spec.net = False  # сеть работнику — только через web_fetch с его списком хостов
    return spec, None


def _cage_hide_args(workdir: Path) -> list:
    home = Path(os.path.expanduser("~"))
    args: list = []
    for rel in _CAGE_HIDE_DIRS:
        d = home / rel
        if d.is_dir() and d != workdir and d not in workdir.parents:
            args += ["--tmpfs", str(d)]
    return args


def _spawn_cage(ctx, cage, command: str, kwargs: dict):
    from ..worker.cage import env_for_bash

    spec, error = _cage_spec(ctx, cage)
    if spec is None:
        return None, error
    argv, _env = spec.wrap(command)
    workdir = Path(spec.workdir)
    if spec.backend == "bwrap":
        # Прячем до --bind рабочей папки: поздний mount сильнее раннего.
        hide = _cage_hide_args(workdir)
        try:
            at = argv.index("--bind")
        except ValueError:
            at = len(argv) - 3
        argv = argv[:at] + hide + argv[at:]
        env = env_for_bash()
    else:
        env = env_for_bash()
        # клиенту контейнеров нужны свои конфиги и сокет; внутрь контейнера
        # это не попадает — туда идут только явные --env рецепта
        env.update({k: os.environ[k] for k in _CONTAINER_CLIENT_ENV if k in os.environ})
    kwargs["env"] = env
    proc = subprocess.Popen(argv, shell=False, cwd=str(workdir),
                            stdin=subprocess.DEVNULL, **kwargs)
    return proc, None


def _spawn(ctx, command: str, timeout: int, **popen_kw):
    """Запуск команды с учётом ctx.sandbox (изоляция ячеек, ЗАДАЧА-21).

    Возвращает (Popen, None) или (None, текст отказа): явно выбранный,
    но недоступный бэкенд не подменяется обычным запуском молча.
    """
    spec = getattr(ctx, "sandbox", None)
    kwargs = dict(popen_kw)
    kwargs.update(_group_kwargs())
    cage = getattr(ctx, "cage", None)
    if cage is not None:
        return _spawn_cage(ctx, cage, command, kwargs)
    if spec is not None and getattr(spec, "unavailable", False):
        return None, ("Изоляция bash недоступна: " +
                      str(getattr(spec, "reason", "") or "бэкенд не найден") +
                      ". Команда НЕ выполнена без изоляции.")
    if spec is not None and getattr(spec, "backend", "none") in ("bwrap", "podman", "docker"):
        argv, env = spec.wrap(command)
        if env is not None:
            kwargs["env"] = env
        proc = subprocess.Popen(argv, shell=False, cwd=str(ctx.cwd),
                                stdin=subprocess.DEVNULL, **kwargs)
        return proc, None
    if spec is not None and not IS_WINDOWS and getattr(spec, "limits", False):
        kwargs["preexec_fn"] = _sandbox_preexec(spec, timeout)
    proc = subprocess.Popen(command, shell=True, cwd=str(ctx.cwd),
                            stdin=subprocess.DEVNULL, **kwargs)
    return proc, None


def _bash(ctx, args) -> ToolResult:
    command = args.get("command", "")
    if not command.strip():
        return ToolResult(False, "Пустая команда.")
    cage = getattr(ctx, "cage", None)
    if cage is not None and cage.bash != "sandbox":
        return ToolResult(False, "клетка: bash не выдан (bash: off).")
    timeout = int(args.get("timeout") or 0)
    if not timeout:
        timeout = int(ctx.settings.get("tools.bash_timeout", 120)) if ctx.settings else 120
    if args.get("background") and cage is not None:
        # Фон пережил бы запуск: контейнер podman/docker не умирает вместе
        # с клиентом, а клетка обещает «после себя ничего».
        return ToolResult(False, "клетка: фоновые команды не выдаются — только обычные.")
    if args.get("background"):
        # Без output_dir — во временную папку системы, не в cwd: иначе
        # логи фона сыплются прямо в проект.
        log_dir = Path(getattr(ctx, "output_dir", "") or tempfile.gettempdir())
        log_dir.mkdir(parents=True, exist_ok=True)
        log = log_dir / ("bg-" + time.strftime("%Y%m%d-%H%M%S") + ".log")
        # Вывод фона — в файл: непрочитанный PIPE рано или поздно повесит процесс.
        with open(log, "wb") as fh:
            try:
                proc, error = _spawn(ctx, command, timeout, stdout=fh,
                                     stderr=subprocess.STDOUT)
            except OSError as exc:
                return ToolResult(False, "Не удалось запустить команду: " + str(exc))
        if proc is None:
            return ToolResult(False, error or "Не удалось запустить команду.")
        ctx.background[proc.pid] = proc
        return ToolResult(True, "Фоновая команда запущена, pid " + str(proc.pid) +
                          ". Вывод пишется в " + str(log) + " (читай тулом read).")
    # Обрезку большого вывода делает цикл агента один раз (с сохранением в файл):
    # если резать и здесь, и там — модель получит двойной маркер и два файла.
    # Цикл ожидания — опросом по 0,05 с: прерывание хода (ctx.cancel_event)
    # и таймаут убивают всё дерево сразу, а не ждут конца команды.
    try:
        proc, error = _spawn(ctx, command, timeout, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT)
    except OSError as exc:
        return ToolResult(False, "Не удалось запустить команду: " + str(exc))
    if proc is None:
        return ToolResult(False, error or "Не удалось запустить команду.")
    cancel = getattr(ctx, "cancel_event", None)
    deadline = time.monotonic() + timeout
    out_b = b""
    while True:
        try:
            out_b, _ = proc.communicate(timeout=0.05)
            break
        except subprocess.TimeoutExpired:
            if cancel is not None and cancel.is_set():
                kill_tree(proc)
                out_b, _ = proc.communicate()
                return ToolResult(False, "Команда прервана человеком.\n" +
                                  _decode(out_b or b""))
            if time.monotonic() >= deadline:
                kill_tree(proc)
                out_b, _ = proc.communicate()
                return ToolResult(False, "Команда не уложилась в " + str(timeout) +
                                  " с и убита.\n" + _decode(out_b or b""))
    out = _decode(out_b or b"")
    code = proc.returncode
    head = "Код выхода: " + str(code) + "\n"
    return ToolResult(code == 0, head + out if out else head + "(вывода нет)")


def bash_tool() -> Tool:
    return Tool("bash", "Выполнить команду оболочки в рабочем каталоге. Оболочка: " + shell_name() +
                ". background=true — запустить в фоне и сразу вернуть pid.",
                {"type": "object", "properties": {
                    "command": {"type": "string"},
                    "timeout": {"type": "integer", "description": "секунд"},
                    "background": {"type": "boolean"}},
                 "required": ["command"]}, _bash, mutating=True)
