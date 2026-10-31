# Перенесено из BENDER: bender/tools/shell.py (коммит 6552ed660bfabf5202569f9905212b77dbf6d31a), сокращено.
"""Тул bash: таймаут, фоновые команды, обрезка большого вывода в файл."""

from __future__ import annotations

import os
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
                                    stdout=fh, stderr=subprocess.STDOUT)
        ctx.background[proc.pid] = proc
        return ToolResult(True, "Фоновая команда запущена, pid " + str(proc.pid) +
                          ". Вывод пишется в " + str(log) + " (читай тулом read).")
    # Обрезку большого вывода делает цикл агента один раз (с сохранением в файл):
    # если резать и здесь, и там — модель получит двойной маркер и два файла.
    try:
        done = subprocess.run(command, shell=True, cwd=str(ctx.cwd), timeout=timeout,
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT)
        out = _decode(done.stdout or b"")
        code = done.returncode
    except subprocess.TimeoutExpired as exc:
        out = _decode(exc.stdout or b"") if isinstance(exc.stdout, bytes) else ""
        return ToolResult(False, "Команда не уложилась в " + str(timeout) +
                          " с и убита.\n" + out)
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
