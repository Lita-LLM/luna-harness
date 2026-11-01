"""Встроенные панели с наполнением (ЗАДАЧА-14): Луна, Лимиты, Обратная
сторона, Файлы, Ниблер.

Все данные — из событий шины (контракт docs/СОБЫТИЯ.md): PanelData —
наблюдатель, которого экран кормит каждым событием, а панели только
читают его состояние. Никаких выдумок: чего события не принесли (лимиты
провайдера), того панель честно не показывает.

Новая панель — register_panel в своём модуле, как здесь; движок
(panels/__init__.py) для этого не трогается. Так добавится и «Рой».
"""

from __future__ import annotations

import json
import re
import time

from ..disc import _smooth01
from . import PanelContent, register_panel

_MAX_ENTRIES = 120        # записей в «Обратной стороне»
_MAX_BODY = 20000         # символов результата тула храним целиком, дальше — пометка
_TAIL_DEFAULT = 24        # строк журнала, если экран не сказал высоту


def _fmt_int(n) -> str:
    return f"{int(n):,}".replace(",", " ")


def _diff_counts(summary: str, output: str):
    """Мини-дифф правок: (+N, −N) из сводки «+12 −3» или из строк вывода +/-.

    Та же логика, что у ленты чата (ui/chat.py diff_counts), — панели не
    импортируют чат: он сам импортирует панели.
    """
    m = re.search(r"\+(\d+)\s*[−-]\s*(\d+)", summary or "")
    if m:
        return int(m.group(1)), int(m.group(2))
    plus = minus = 0
    for line in (output or "").splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            plus += 1
        elif line.startswith("-") and not line.startswith("---"):
            minus += 1
    if plus or minus:
        return plus, minus
    return None


class PanelData:
    """Состояние панелей из потока событий: расход, журнал, файлы, лимиты."""

    def __init__(self):
        self.model = ""
        self.context_pct = 0
        self.cost = 0.0
        self.tokens_in = 0
        self.tokens_out = 0
        self.cache = 0
        self.cache_seen = False
        self.limits: dict | None = None
        self.backside: list[dict] = []   # {kind, title, body, ok}
        self.files: dict = {}            # путь → {reads, writes, plus, minus, diff}
        self.nibbler: list[dict] = []    # {severity, text}
        self._calls: dict = {}           # id вызова → (имя, args, запись журнала)
        self._thinking = ""
        # ЗАДАЧА-16: фоновые bash, диагностика, MCP-активность, время хода.
        self.jobs: list[dict] = []       # {pid, command, log, ts} фоновых bash
        self._bg_calls: dict = {}        # id вызова → команда (ждём pid из результата)
        self.diag: dict | None = None    # {title, body, ok, ts} последнего линтера/тестов
        self.mcp_stats: dict = {}        # сервер → {calls, errors}
        self.turn_started: float | None = None
        self.last_turn_sec: float | None = None

    # --- вход событий ---------------------------------------------------------

    def observe(self, ev: dict) -> None:
        kind = ev.get("type")
        if kind == "session.start":
            if ev.get("model"):
                self.model = str(ev.get("model"))
        elif kind == "usage":
            self._on_usage(ev)
        elif kind == "tool.call":
            self._on_tool_call(ev)
        elif kind == "tool.output":
            entry = self._entry_of(ev.get("id"))
            if entry is not None:
                entry["body"] += str(ev.get("text", ""))
        elif kind == "tool.result":
            self._on_tool_result(ev)
        elif kind == "guard.verdict":
            self._push("guard",
                       "Предохранитель: " + str(ev.get("decision", "")) +
                       " (" + str(ev.get("by", "")) + ")",
                       str(ev.get("reason", "")))
        elif kind == "error":
            text = str(ev.get("message", ""))
            detail = str(ev.get("detail", ""))
            self._push("error", "Ошибка: " + text, detail, ok=False)
        elif kind == "agent.thinking":
            self._thinking += str(ev.get("text", ""))
        elif kind == "turn.done":
            if self.turn_started is not None:
                self.last_turn_sec = max(0.0, time.time() - self.turn_started)
                self.turn_started = None
            if self._thinking.strip():
                self._push("thinking", "Рассуждения", self._thinking.strip())
            self._thinking = ""
        elif kind == "nibbler.say":
            self.nibbler.append({"severity": str(ev.get("severity", "minor")),
                                 "text": str(ev.get("text", ""))})
        elif kind == "turn.start":
            self.turn_started = time.time()
        limits = ev.get("limits")
        if isinstance(limits, dict) and limits:
            self.limits = dict(limits)

    # --- частности -------------------------------------------------------------

    def _on_usage(self, ev: dict) -> None:
        if ev.get("model"):
            self.model = str(ev.get("model"))
        self.tokens_in += int(ev.get("input") or 0)
        self.tokens_out += int(ev.get("output") or 0)
        for key in ("cache", "cache_read", "cached"):
            if ev.get(key) is not None:
                self.cache += int(ev.get(key) or 0)
                self.cache_seen = True
        if ev.get("cost") is not None:
            self.cost = float(ev.get("cost") or 0.0)
        if ev.get("context_pct") is not None:
            self.context_pct = int(round(float(ev.get("context_pct") or 0)))

    def _on_tool_call(self, ev: dict) -> None:
        name = str(ev.get("name", ""))
        args = dict(ev.get("args") or {})
        title = "⚙ " + name
        intent = str(ev.get("intent") or "")
        if intent:
            title += " — " + intent
        body = ""
        if args:
            body = json.dumps(args, ensure_ascii=False, indent=1, default=str)
        entry = self._push("tool", title, body, ok=None)
        self._calls[str(ev.get("id", ""))] = (name, args, entry)
        if name.startswith("mcp__"):
            # Активность MCP-серверов для панели «MCP»: имя тула — mcp__сервер__тул.
            parts = name.split("__")
            if len(parts) > 1 and parts[1]:
                stat = self.mcp_stats.setdefault(parts[1], {"calls": 0, "errors": 0})
                stat["calls"] += 1
        if name == "bash" and args.get("background"):
            # Фоновый bash: pid и файл лога придут текстом результата.
            self._bg_calls[str(ev.get("id", ""))] = str(args.get("command") or "")
        path = str(args.get("path") or "")
        if name == "read" and path:
            rec = self._file(path)
            rec["reads"] += 1

    def _on_tool_result(self, ev: dict) -> None:
        entry = self._entry_of(ev.get("id"))
        call = self._calls.pop(str(ev.get("id", "")), None)
        out = str(ev.get("output", ""))
        summary = str(ev.get("summary", ""))
        if entry is not None:
            entry["ok"] = bool(ev.get("ok"))
            tail = ("итог: " + summary) if summary else ""
            if out and out not in entry["body"]:
                tail = (tail + "\n" + out) if tail else out
            if tail:
                entry["body"] = (entry["body"] + "\n" + tail) if entry["body"] else tail
            if len(entry["body"]) > _MAX_BODY:
                entry["body"] = entry["body"][:_MAX_BODY] + "\n…[дальше обрезано в панели]"
        if call is None:
            return
        name, args, _entry = call
        self._track_job(ev, name)
        self._track_diag(name, args, ev)
        if name.startswith("mcp__") and not ev.get("ok"):
            parts = name.split("__")
            if len(parts) > 1 and parts[1] in self.mcp_stats:
                self.mcp_stats[parts[1]]["errors"] += 1
        path = str(args.get("path") or "")
        if name in ("edit", "write") and path:
            rec = self._file(path)
            rec["writes"] += 1
            counts = _diff_counts(summary, out)
            if counts:
                rec["plus"] += counts[0]
                rec["minus"] += counts[1]
            if out:
                rec["diff"] = out

    # Команды, чей вывод — «диагностика» проекта: тесты, линтеры, компиляторы.
    _DIAG_HINTS = ("pytest", "unittest", "ruff", "mypy", "flake8", "eslint",
                   "tsc", "cargo", "go build", "go vet", "npm run", "make",
                   "gcc", "clang", "compileall", "pyright")

    def _track_job(self, ev: dict, name: str) -> None:
        """Фоновый bash завершился запуском: вытащить pid и лог из текста."""
        call_id = str(ev.get("id", ""))
        command = self._bg_calls.pop(call_id, None)
        if command is None or name != "bash":
            return
        text = str(ev.get("output", "")) + " " + str(ev.get("summary", ""))
        pid_match = re.search(r"pid\D{0,4}(\d+)", text)
        log_match = re.search(r"(\S+\.log)\b", text)
        self.jobs.append({
            "pid": int(pid_match.group(1)) if pid_match else 0,
            "command": command,
            "log": log_match.group(1) if log_match else "",
            "ts": time.time(),
        })

    def _track_diag(self, name: str, args: dict, ev: dict) -> None:
        """Последний вывод линтера/компилятора/тестов — для панели «Диагностика»."""
        if name != "bash":
            return
        command = str(args.get("command") or "")
        if not any(hint in command for hint in self._DIAG_HINTS):
            return
        body = str(ev.get("output", "") or ev.get("summary", ""))
        self.diag = {"title": command, "body": body[-6000:],
                     "ok": bool(ev.get("ok")), "ts": time.time()}

    def _entry_of(self, call_id):
        call = self._calls.get(str(call_id))
        return call[2] if call else None

    def _file(self, path: str) -> dict:
        return self.files.setdefault(
            path, {"reads": 0, "writes": 0, "plus": 0, "minus": 0, "diff": ""})

    def _push(self, kind: str, title: str, body: str, ok=None) -> dict:
        entry = {"kind": kind, "title": title, "body": body, "ok": ok}
        self.backside.append(entry)
        if len(self.backside) > _MAX_ENTRIES:
            del self.backside[:len(self.backside) - _MAX_ENTRIES]
        return entry


def panel_data(app) -> PanelData:
    """PanelData экрана: живёт на приложении (app.panel_data), создаётся лениво."""
    data = getattr(app, "panel_data", None)
    if not isinstance(data, PanelData):
        data = PanelData()
        try:
            app.panel_data = data
        except Exception:  # noqa: BLE001 — приложение-объект мог запретить запись
            pass
    return data


# --- луна полублоками ----------------------------------------------------------


def moon_art(pct: float, width: int, mode: str = "emoji") -> list:
    """Фаза луны крупно текстовыми подпикселями (идея — ui/disc.py).

    Доля освещённости — заполнение контекста: 0% — новолуние (пепельный
    свет), 100% — полнолуние. Свет справа, терминатор плавный (та же
    формула, что у диска заставки). В ascii-режиме подпиксели — строки
    из #, + и . — без полублоков.
    """
    phase = max(0.0, min(1.0, float(pct) / 100.0))
    cols = max(5, min(13, int(width)))
    rows = max(3, (cols + 1) // 2)
    subrows = rows * 2
    cx, cy, r = cols / 2.0, subrows / 2.0, min(cols, subrows) / 2.0
    term = 1.0 - 2.0 * phase

    def level(sx: int, sy: int):
        u = (sx + 0.5 - cx) / r
        v = (sy + 0.5 - cy) / r
        if u * u + v * v > 1.0:
            return None
        return _smooth01((u - term) / (2.0 * 0.09) + 0.5)

    if mode == "ascii":
        out = []
        for sy in range(subrows):
            line = ""
            for sx in range(cols):
                lv = level(sx, sy)
                line += " " if lv is None else ("#" if lv >= 0.6 else ("+" if lv >= 0.35 else "."))
            out.append(line.rstrip())
        return out
    out = []
    for row in range(rows):
        line = ""
        for sx in range(cols):
            top = level(sx, 2 * row)
            bottom = level(sx, 2 * row + 1)
            line += _cell_char(top, bottom)
        out.append(line.rstrip())
    return out


def _cell_char(top, bottom) -> str:
    """Ячейка из двух подпикселей: полублоки, ▒ на терминаторе, ░ — пепельный свет."""
    if top is None and bottom is None:
        return " "
    if top is None:
        return "▄" if bottom >= 0.45 else "░"
    if bottom is None:
        return "▀" if top >= 0.45 else "░"
    avg = (top + bottom) / 2.0
    if avg >= 0.66:
        return "█"
    if avg <= 0.3:
        return "░"
    if top - bottom > 0.18:
        return "▀"
    if bottom - top > 0.18:
        return "▄"
    return "▒"


# --- панели ---------------------------------------------------------------------


class MoonPanel(PanelContent):
    """«Луна»: фаза по контексту, токены, стоимость, модель."""

    def __init__(self):
        super().__init__("Луна")

    def lines(self, app, width: int) -> list:
        data = panel_data(app)
        mode = getattr(app, "glyph_mode", "emoji")
        out = moon_art(data.context_pct, width, mode)
        out.append("")
        out.append("контекст: " + str(data.context_pct) + "%")
        if data.model:
            out.append("модель: " + data.model)
        tokens = "токены: вход " + _fmt_int(data.tokens_in) + " · выход " + _fmt_int(data.tokens_out)
        if data.cache_seen:
            tokens += " · кэш " + _fmt_int(data.cache)
        out.append(tokens)
        out.append("стоимость сессии: $" + format(data.cost, ".4f"))
        return out


class LimitsPanel(PanelContent):
    """«Лимиты»: что провайдер отдал в событиях; не отдал — честное «нет данных»."""

    def __init__(self):
        super().__init__("Лимиты")

    def lines(self, app, width: int) -> list:
        data = panel_data(app)
        if not data.limits:
            out = ["нет данных",
                   "провайдер не отдаёт заголовки лимитов,",
                   "а события сессии их не несут"]
            if data.model:
                out.append("")
                out.append("модель: " + data.model)
            return out
        return [str(k) + ": " + str(v) for k, v in data.limits.items()]


class _ScrollablePanel(PanelContent):
    """База журнальных панелей: хвост записей, прокрутка стрелками в фокусе."""

    def __init__(self, title: str):
        super().__init__(title)
        self.scroll = 0  # строк пропустить от хвоста

    def _window(self, all_lines: list) -> list:
        height = self.height_hint or _TAIL_DEFAULT
        if self.scroll:
            all_lines = all_lines[:max(0, len(all_lines) - self.scroll)] or all_lines[:0]
        return all_lines[-height:]

    def handle_key(self, app, key) -> bool:
        if key.name == "up":
            self.scroll += 1
            return True
        if key.name == "down":
            self.scroll = max(0, self.scroll - 1)
            return True
        if key.name == "pageup":
            self.scroll += 10
            return True
        if key.name == "pagedown":
            self.scroll = max(0, self.scroll - 10)
            return True
        return False


class BacksidePanel(_ScrollablePanel):
    """«Обратная сторона»: тулы с аргументами и полными результатами, ошибки, лог."""

    def __init__(self):
        super().__init__("Обратная сторона")

    def lines(self, app, width: int) -> list:
        data = panel_data(app)
        if not data.backside:
            return ["пока пусто: агент ещё ничего не делал"]
        all_lines: list = []
        for entry in data.backside:
            mark = {True: "✓", False: "✗", None: "…"}.get(entry.get("ok"), "…")
            all_lines.append(mark + " " + entry["title"])
            body = entry.get("body") or ""
            for line in body.splitlines():
                all_lines.append("  " + line)
        return self._window(all_lines)


class NibblerPanel(_ScrollablePanel):
    """«Ниблер»: история его замечаний за сессию."""

    def __init__(self):
        super().__init__("Ниблер")

    def lines(self, app, width: int) -> list:
        data = panel_data(app)
        if not data.nibbler:
            return ["Ниблер пока молчит"]
        all_lines = []
        for item in data.nibbler:
            mark = "! " if item.get("severity") == "serious" else "· "
            all_lines.append(mark + item.get("text", ""))
        return self._window(all_lines)


class FilesPanel(PanelContent):
    """«Файлы»: что сессия читала и меняла; Enter в фокусе — дифф выбранного."""

    def __init__(self):
        super().__init__("Файлы")
        self.selected = 0
        self.diff_for: str | None = None

    def _paths(self, app) -> list:
        return sorted(panel_data(app).files)

    def lines(self, app, width: int) -> list:
        data = panel_data(app)
        if self.diff_for is not None:
            rec = data.files.get(self.diff_for) or {}
            out = ["дифф: " + self.diff_for, "(Enter — назад к списку)"]
            diff = rec.get("diff") or ""
            if diff:
                out.extend(diff.splitlines())
            else:
                out.append("диффа нет: файл только читали")
            height = self.height_hint or _TAIL_DEFAULT
            return out[:height + 2]
        paths = self._paths(app)
        if not paths:
            return ["сессия пока не трогала файлы"]
        self.selected = max(0, min(self.selected, len(paths) - 1))
        out = []
        for i, path in enumerate(paths):
            rec = data.files[path]
            parts = []
            if rec["writes"]:
                parts.append("+" + str(rec["plus"]) + " −" + str(rec["minus"]))
                parts.append("правок: " + str(rec["writes"]))
            if rec["reads"]:
                parts.append("читал: " + str(rec["reads"]))
            mark = "▸ " if i == self.selected else "  "
            out.append(mark + path + (" · " + " · ".join(parts) if parts else ""))
        return out

    def handle_key(self, app, key) -> bool:
        paths = self._paths(app)
        if key.name == "enter":
            if self.diff_for is not None:
                self.diff_for = None
                return True
            if paths:
                self.diff_for = paths[max(0, min(self.selected, len(paths) - 1))]
                return True
            return False
        if self.diff_for is not None:
            return False
        if key.name == "up" and paths:
            self.selected = max(0, self.selected - 1)
            return True
        if key.name == "down" and paths:
            self.selected = min(len(paths) - 1, self.selected + 1)
            return True
        return False


register_panel("moon", "Луна", MoonPanel, aliases=("moon", "контекст", "context"))
register_panel("limits", "Лимиты", LimitsPanel, aliases=("limits", "приливы"))
register_panel("backside", "Обратная сторона", BacksidePanel,
               aliases=("backside", "лог", "debug"))
register_panel("files", "Файлы", FilesPanel, aliases=("files",))
register_panel("nibbler", "Ниблер", NibblerPanel, aliases=("nibbler",))
