# Перенесено из BENDER: bender/tools/shell.py (коммит 6552ed660bfabf5202569f9905212b77dbf6d31a), сокращено.
"""Тул bash: таймаут, фоновые команды, обрезка большого вывода в файл."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
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


def _bash(ctx, args) -> ToolResult:
    command = args.get("command", "")
    if not command.strip():
        return ToolResult(False, "Пустая команда.")
    timeout = int(args.get("timeout") or 0)
    if not timeout:
        timeout = int(ctx.settings.get("tools.bash_timeout", 120)) if ctx.settings else 120
    if args.get("background"):
        log_dir = Path(getattr(ctx, "output_dir", "") or ".")
        log_dir.mkdir(parents=True, exist_ok=True)
        log = log_dir / ("bg-" + time.strftime("%Y%m%d-%H%M%S") + ".log")
        # Вывод фона — в файл: непрочитанный PIPE рано или поздно повесит процесс.
        with open(log, "wb") as fh:
            proc = subprocess.Popen(command, shell=True, cwd=str(ctx.cwd), stdin=subprocess.DEVNULL,
                                    stdout=fh, stderr=subprocess.STDOUT, **_group_kwargs())
        ctx.background[proc.pid] = proc
        return ToolResult(True, "Фоновая команда запущена, pid " + str(proc.pid) +
                          ". Вывод пишется в " + str(log) + " (читай тулом read).")
    # Обрезку большого вывода делает цикл агента один раз (с сохранением в файл):
    # если резать и здесь, и там — модель получит двойной маркер и два файла.
    # Цикл ожидания — опросом по 0,05 с: прерывание хода (ctx.cancel_event)
    # и таймаут убивают всё дерево сразу, а не ждут конца команды.
    try:
        proc = subprocess.Popen(command, shell=True, cwd=str(ctx.cwd),
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, **_group_kwargs())
    except OSError as exc:
        return ToolResult(False, "Не удалось запустить команду: " + str(exc))
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
