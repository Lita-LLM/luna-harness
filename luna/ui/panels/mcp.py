"""Панель «MCP» (ЗАДАЧА-16): серверы из конфигов и их жизнь в сессии.

Состав серверов — из тех же файлов, что читает ядро (mcp.manager.
load_configs): LUNA_HOME/mcp.json, .mcp.json, opencode.json, .luna/mcp.json.
Статус — честный: настроен/выключен по конфигу, а «живость» — только по
событиям сессии (вызовы тулов mcp__сервер__тул и их ошибки в PanelData).
Отдельных подключений панель не открывает и сеть не дёргает.
"""

from __future__ import annotations

from pathlib import Path

from . import PanelContent, register_panel
from .builtin import panel_data


def _home_of(app) -> Path:
    getter = getattr(app, "_data_dir", None)
    if callable(getter):
        try:
            return Path(getter().root)
        except Exception:  # noqa: BLE001 — панель не роняет экран
            pass
    from ...core.paths import DataDir

    return Path(DataDir().root)


def load_servers(app) -> tuple:
    """(серверы по имени, проблемы разбора) — та же функция, что у ядра."""
    from ...mcp.manager import load_configs

    cwd = Path(str(getattr(app, "cwd", "") or "."))
    return load_configs(cwd, _home_of(app))


class McpPanel(PanelContent):
    """MCP-серверы: конфиг, транспорт, активность и ошибки сессии."""

    def __init__(self):
        super().__init__("MCP")

    def lines(self, app, width: int) -> list:
        try:
            servers, problems = load_servers(app)
        except Exception as exc:  # noqa: BLE001 — битый конфиг не роняет экран
            return ["не вышло прочитать конфиги MCP: " + str(exc)]
        out = []
        for problem in problems:
            out.append("! " + problem)
        if not servers:
            out.append("MCP-серверы не настроены")
            out.append("конфиги: ~/.luna/mcp.json, .mcp.json,")
            out.append(".luna/mcp.json, раздел mcp в opencode.json")
            out.append("добавить: luna mcp add · список: /mcp")
            return out
        stats = panel_data(app).mcp_stats
        for name in sorted(servers):
            cfg = servers[name]
            state = "вкл" if cfg.enabled else "выкл"
            if cfg.transport == "unsupported":
                state += " · транспорт не поддержан"
            out.append(name + " · " + state)
            if cfg.transport == "http":
                detail = cfg.url
            elif cfg.command:
                detail = " ".join(cfg.command)
            else:
                detail = cfg.transport
            if cfg.note:
                detail += " · " + cfg.note
            out.append("  " + detail)
            out.append("  источник: " + (cfg.source or "?"))
            stat = stats.get(name)
            if stat and stat["calls"]:
                line = "  вызовов в сессии: " + str(stat["calls"])
                if stat["errors"]:
                    line += " · ошибок: " + str(stat["errors"])
                out.append(line)
            else:
                out.append("  в этой сессии ещё не вызывался")
        return out


register_panel("mcp", "MCP", McpPanel, aliases=("mcp",))
