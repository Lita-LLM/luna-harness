# Перенесено из BENDER: bender/tools/shell.py (коммит 6552ed660bfabf5202569f9905212b77dbf6d31a), сокращено.
"""Тул bash: таймаут, фоновые команды, обрезка большого вывода в файл."""

from __future__ import annotations

import subprocess

from .registry import Tool, ToolResult, truncate_output


def _bash(ctx, args) -> ToolResult:
    command = args.get("command", "")
    if not command.strip():
        return ToolResult(False, "Пустая команда.")
    timeout = int(args.get("timeout") or 0)
    if not timeout:
        timeout = int(ctx.settings.get("tools.bash_timeout", 120)) if ctx.settings else 120
    limit = int(ctx.settings.get("tools.output_limit", 8000)) if ctx.settings else 8000
    if args.get("background"):
        proc = subprocess.Popen(command, shell=True, cwd=str(ctx.cwd),
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        ctx.background[proc.pid] = proc
        return ToolResult(True, "Фоновая команда запущена, pid " + str(proc.pid) +
                          ". Вывод соберёт её завершение; pid держим в этой сессии.")
    try:
        done = subprocess.run(command, shell=True, cwd=str(ctx.cwd), timeout=timeout,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        out = done.stdout or ""
        code = done.returncode
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        shown, _ = truncate_output(out, limit, ctx.output_dir, "bash")
        return ToolResult(False, "Команда не уложилась в " + str(timeout) +
                          " с и убита.\n" + shown)
    shown, _ = truncate_output(out, limit, ctx.output_dir, "bash")
    head = "Код выхода: " + str(code) + "\n"
    return ToolResult(code == 0, head + shown if shown else head + "(вывода нет)")


def bash_tool() -> Tool:
    return Tool("bash", "Выполнить команду оболочки в рабочем каталоге. "
                        "background=true — запустить в фоне и сразу вернуть pid.",
                {"type": "object", "properties": {
                    "command": {"type": "string"},
                    "timeout": {"type": "integer", "description": "секунд"},
                    "background": {"type": "boolean"}},
                 "required": ["command"]}, _bash, mutating=True)
