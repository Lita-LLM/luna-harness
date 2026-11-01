"""Снимки экранов в HTML: `luna try snapshot <splash|render|main|chat|sessions|panels|help|tiles>`.

Экраны рисуются без живого терминала: часы подменяются вымышленным
временем, готовый буфер превращается в HTML-страницу — моноширинный
шрифт, каждая ячейка — span со своими цветами, тёмный фон. Так дизайн
можно смотреть глазами и прикладывать к ревью, не открывая терминал.

  luna try snapshot splash --frames 11 --every 0.12 --size 120x30 -o заставка.html
  luna try snapshot main -o main.html
"""

from __future__ import annotations

import html
import os
import sys
from pathlib import Path

from ..core.errors import HumanError
from ..core.stdio import ensure_utf8_stdio
from ..ui import color as _color
from ..ui import phrases
from ..ui.screen import Buffer

SCREENS = ("splash", "render", "main", "chat", "thinking", "sessions",
           "panels", "help", "tiles", "constellation")
_FAKE_NOW = 10000.0  # вымышленная эпоха снимка: все «сейчас» считаются от неё

_PAGE_CSS = """
body { background: #05060A; color: #C9D1E0; font-family: sans-serif; margin: 24px; }
h1 { font-size: 18px; font-weight: normal; }
.frame { margin: 0 auto 32px; width: max-content; max-width: 100%; }
.cap { color: #8B94AC; font-size: 13px; margin: 0 0 8px 4px; }
.scr { background: #0B0D17; border-radius: 14px; padding: 14px 16px;
       font-family: "DejaVu Sans Mono", "Cascadia Mono", Consolas, monospace;
       font-size: 14px; line-height: 1; white-space: pre; overflow-x: auto; }
.line { display: block; line-height: 1; min-height: 1em; white-space: pre; }
"""


class _FakeOut:
    """Вывод-«хороший терминал» для detect_caps: снимок — витрина, а не замер."""

    encoding = "utf-8"

    def isatty(self):
        return True


def _snapshot_caps(cols: int, rows: int, settings):
    """Возможности для снимка: truecolor и юникод, настройки пользователя действуют."""
    from ..ui.term import detect_caps

    env = dict(os.environ)
    if env.get("TERM") in (None, "", "dumb"):
        env["TERM"] = "xterm-256color"
    env.setdefault("COLORTERM", "truecolor")
    if sys.platform == "win32" and "WT_SESSION" not in env:
        env["WT_SESSION"] = "snapshot"  # снимок показывает вариант Windows Terminal
    caps = detect_caps(env=env, out=_FakeOut(), settings=settings)
    caps.cols, caps.rows = cols, rows
    caps.is_tty = False
    return caps


def _render_splash(cols: int, rows: int, at: float, caps, phrase: str,
                   settings=None) -> Buffer:
    from ..ui import splash as _splash
    from ..ui.astro import moon_sky

    buf = Buffer(cols, rows)
    stars = _splash._stars(cols, rows, max(20, cols * rows // 28))
    t = max(0.0, min(1.0, at / _splash.DURATION))
    hemisphere = "north"
    if settings is not None:
        hemisphere = str(settings.get("ui.hemisphere") or "north")
    sky = moon_sky(hemisphere=hemisphere)
    _splash.render_frame(buf, t, glyph_mode=caps.glyph_mode, phrase=phrase,
                         startup_ms=None, stars=stars, now=at, sky=sky)
    return buf


def _render_render(cols: int, rows: int, at: float, caps, demo) -> Buffer:
    return demo.draw(cols, rows, _FAKE_NOW + at)


def _render_main(cols: int, rows: int, at: float, caps, settings) -> Buffer:
    from ..core.bus import EventBus
    from ..ui.anim import Scheduler
    from ..ui.chat import ChatApp

    app = ChatApp(EventBus(), settings, Scheduler(), caps.glyph_mode)
    app._clock = lambda: _FAKE_NOW  # проявление ленты — от вымышленного «сейчас», не от аптайма машины
    app.start_source()
    buf, _cursor = app.draw(cols, rows, _FAKE_NOW + at)
    return buf


_CHAT_STATES = ("стрим", "тул", "подтверждение", "вопрос")


def _render_chat_state(state: str, cols: int, rows: int, caps, settings) -> Buffer:
    """Одно именованное состояние чата для снимка: события — по контракту."""
    from ..core.bus import EventBus
    from ..ui.anim import Scheduler
    from ..ui.chat import ChatApp

    app = ChatApp(EventBus(), settings, Scheduler(), caps.glyph_mode)
    app._clock = lambda: _FAKE_NOW  # проявление ленты — от вымышленного «сейчас», не от аптайма машины
    app.on_agent_event({"type": "session.start", "cwd": "/home/user/project",
                        "model": "fake-agent", "mode": "normal"})
    app.submit_text("Почини сборку и поставь зависимости")
    app.on_agent_event({"type": "turn.start", "turn": 1})
    app.on_agent_event({"type": "agent.thinking",
                        "text": "Сверяюсь со звёздами и смотрю логи сборки. "})
    if state == "стрим":
        app.on_agent_event({"type": "agent.text",
                            "text": "## Готово\n\nСделал **по шагам** и печатаю дальше"})
        app.on_agent_event({"type": "usage", "model": "fake-agent", "input": 900,
                            "output": 120, "cost": 0.0031, "context_pct": 34})
    elif state == "тул":
        app.on_agent_event({"type": "agent.text", "text": "Ставлю зависимость."})
        app.on_agent_event({"type": "agent.text.done"})
        app.on_agent_event({"type": "tool.call", "id": "tool-1", "name": "bash",
                            "args": {"command": "pip install lxml"},
                            "intent": "Ставлю зависимость для разбора разметки"})
        app.on_agent_event({"type": "tool.output", "id": "tool-1",
                            "text": "Collecting lxml\nDownloading lxml-5.3.0.whl\n"})
        app.on_agent_event({"type": "usage", "model": "fake-agent", "input": 900,
                            "output": 120, "cost": 0.0031, "context_pct": 41})
    elif state == "подтверждение":
        app.on_agent_event({"type": "tool.call", "id": "tool-g", "name": "bash",
                            "args": {"command": "rm -rf build/"},
                            "intent": "Чищу каталог сборки перед пересборкой"})
        app.on_agent_event({"type": "guard.ask", "id": "guard-1", "tool": "bash",
                            "args": {"command": "rm -rf build/"},
                            "intent": "Чищу каталог сборки перед пересборкой",
                            "risk": "high", "reason": "необратимое удаление",
                            "choices": ["yes", "no", "edit", "always", "stop"]})
    else:  # вопрос
        app.on_agent_event({"type": "agent.text", "text": "Уточню один момент."})
        app.on_agent_event({"type": "agent.text.done"})
        app.on_agent_event({"type": "ask.user", "id": "ask-1",
                            "question": "Какой формат отчёта нужен?",
                            "options": ["Короткий", "Подробный", "Только цифры"]})
    buf, _cursor = app.draw(cols, rows, _FAKE_NOW + 1.0)
    return buf


def _render_thinking(cols: int, rows: int, at: float, caps, settings) -> Buffer:
    """Экран мышления (ЗАДАЧА-19): модель думает, луна наливается светом,
    пересказы быстрой роли проявляются строками под шапкой «думает».

    Время сцены поставлено вручную от _FAKE_NOW: начало раздумья — за
    20 секунд до эпохи снимка, пересказы — на 7-й и 14-й секундах;
    кадр рисуется в _FAKE_NOW + at, поэтому по кадрам видно и налив
    луны, и вспышки звёзд, и градиент свежих строк.
    """
    import tempfile

    from ..core.bus import EventBus
    from ..core.settings import Settings
    from ..ui.anim import Scheduler
    from ..ui.chat import ChatApp

    # Настройки сцены — во временной папке: снимок не должен писать
    # в настоящий settings.json человека.
    tmp_settings = Settings(home=Path(tempfile.mkdtemp(prefix="luna-snap-")),
                            cwd=Path.cwd())
    tmp_settings.set("ui.thinking", "summary")
    tmp_settings.set("ui.show_thinking", True)
    tmp_settings.set("model.effort", "high")
    app = ChatApp(EventBus(), tmp_settings, Scheduler(), caps.glyph_mode)
    base = _FAKE_NOW - 20.0
    app._clock = lambda: base
    app.on_agent_event({"type": "session.start", "cwd": "/home/user/project",
                        "model": "fake-agent", "mode": "normal"})
    app.submit_text("Разберись, почему падает сборка на Windows")
    app.on_agent_event({"type": "turn.start", "turn": 1})
    app.on_agent_event({"type": "agent.thinking",
                        "text": "Смотрю логи сборки и сравниваю с прошлой "
                                "неделей. Падение только на Windows, значит "
                                "ищу различия в путях и кодировках. "})
    app._clock = lambda: base + 7.0
    app.on_agent_event({"type": "thinking.summary",
                        "text": "Смотрит логи сборки и ищет, чем Windows "
                                "отличается от остальных платформ",
                        "seconds": 7})
    app.on_agent_event({"type": "agent.thinking",
                        "text": "Нашёл подозрительное место в чтении "
                                "конфига: путь собирается через os.path, "
                                "а дальше режется по прямому слешу. "})
    app._clock = lambda: base + 14.0
    app.on_agent_event({"type": "thinking.summary",
                        "text": "Проверяет сборку путей в чтении конфига — "
                                "похоже, слеши режут путь не на той платформе",
                        "seconds": 14})
    app.on_agent_event({"type": "agent.thinking",
                        "text": "Сверяю с тестами на разделители путей…"})
    buf, _cursor = app.draw(cols, rows, _FAKE_NOW + at)
    return buf


def _render_sessions(cols: int, rows: int, caps, settings) -> Buffer:
    """Выбиралка сессий поверх ленты: вымышленные сессии, без диска."""
    import time as _time

    from ..core.bus import EventBus
    from ..ui.anim import Scheduler
    from ..ui.chat import ChatApp
    from ..ui.picker import Picker, session_items

    app = ChatApp(EventBus(), settings, Scheduler(), caps.glyph_mode)
    app._clock = lambda: _FAKE_NOW  # проявление ленты — от вымышленного «сейчас», не от аптайма машины
    app.on_agent_event({"type": "session.start", "cwd": "/home/user/project",
                        "model": "fake-agent", "mode": "normal"})
    app.submit_text("Почини сборку и поставь зависимости")
    now = _time.time()
    fake = [
        {"id": "a1b2c3d4", "cwd": "/home/user/project", "started": now - 3600,
         "records": 42, "path": "a1b2c3d4.jsonl", "title": "",
         "first": "Почини сборку и поставь зависимости", "turns": 3},
        {"id": "d4e5f6a7", "cwd": "/home/user/project", "started": now - 86400,
         "records": 17, "path": "d4e5f6a7.jsonl", "title": "Разбор логов",
         "first": "Глянь логи за вчера", "turns": 2},
        {"id": "a7b8c9d0", "cwd": "/home/user/other", "started": now - 2 * 86400,
         "records": 9, "path": "a7b8c9d0.jsonl", "title": "",
         "first": "Напиши скрипт бэкапа", "turns": 1},
    ]
    app.picker = Picker("Сессии", session_items(fake, "/home/user/project"),
                        hint="Enter — переключиться · r — переименовать · "
                             "d — удалить · пробел — отметить · D — отмеченные · "
                             "u — вернуть из корзины · Esc — закрыть",
                        preview_lines=0, now=_FAKE_NOW)
    app._picker_kind = "sessions"
    buf, _cursor = app.draw(cols, rows, _FAKE_NOW + 1.0)
    return buf


def _render_panels(cols: int, rows: int, caps, settings) -> Buffer:
    """Экран чата с открытыми панелями (ЗАДАЧА-14): данные — событиями."""
    from ..core.bus import EventBus
    from ..ui.anim import Scheduler
    from ..ui.chat import ChatApp

    app = ChatApp(EventBus(), settings, Scheduler(), caps.glyph_mode)
    app._clock = lambda: _FAKE_NOW  # проявление ленты — от вымышленного «сейчас», не от аптайма машины
    app.on_agent_event({"type": "session.start", "cwd": "/home/user/project",
                        "model": "fake-agent", "mode": "normal"})
    app.submit_text("Почини сборку и поправь обработчик")
    app.on_agent_event({"type": "turn.start", "turn": 1})
    app.on_agent_event({"type": "todo", "items": [
        {"text": "Понять задачу", "status": "done"},
        {"text": "Выполнить работу", "status": "active"},
        {"text": "Проверить результат", "status": "pending"}]})
    app.on_agent_event({"type": "agent.thinking",
                        "text": "Сверяюсь со звёздами и смотрю, что уже есть под рукой. "})
    app.on_agent_event({"type": "tool.call", "id": "t1", "name": "read",
                        "args": {"path": "app.py"}, "intent": "Читаю обработчик"})
    app.on_agent_event({"type": "tool.result", "id": "t1", "ok": True,
                        "summary": "120 строк", "output": "def handle(): ...\n"})
    app.on_agent_event({"type": "tool.call", "id": "t2", "name": "edit",
                        "args": {"path": "app.py"}, "intent": "Правлю обработчик"})
    app.on_agent_event({"type": "tool.result", "id": "t2", "ok": True,
                        "summary": "app.py: +12 −3",
                        "output": "+ добавлен стрим\n- старый вызов\n"})
    app.on_agent_event({"type": "tool.call", "id": "t3", "name": "bash",
                        "args": {"command": "python -m unittest -q"},
                        "intent": "Прогоняю тесты"})
    app.on_agent_event({"type": "tool.output", "id": "t3", "text": "Ran 246 tests\n"})
    app.on_agent_event({"type": "tool.result", "id": "t3", "ok": True,
                        "summary": "зелёные", "output": "OK\n"})
    app.on_agent_event({"type": "guard.verdict", "id": "t3", "decision": "allow",
                        "by": "rules", "reason": "тесты — безопасная команда"})
    app.on_agent_event({"type": "error", "message": "Хьюстон, у нас проблема",
                        "detail": "фальшивая ошибка для обратной стороны"})
    app.on_agent_event({"type": "usage", "model": "fake-agent", "input": 4200,
                        "output": 860, "cost": 0.0061, "context_pct": 62})
    app.on_agent_event({"type": "agent.text", "text": "Готово, сборка зелёная."})
    app.on_agent_event({"type": "agent.text.done"})
    app.on_agent_event({"type": "nibbler.say", "severity": "minor", "to": "human",
                        "text": "Тесты гоняет сам, без напоминаний. Редкость."})
    app.on_agent_event({"type": "nibbler.say", "severity": "serious", "to": "agent",
                        "text": "app.py правится уже третий раз за сессию — пахнет петлёй."})
    app.on_agent_event({"type": "turn.done", "turn": 1, "reason": "end"})
    # Команда детей (ЗАДАЧА-11): спутники, письмо и кошелёк — панель «Экипаж».
    app.on_agent_event({"type": "agent.spawn", "agent_id": "snap-crew-1",
                        "parent_id": "main", "kind": "satellite", "name": "Кодер1",
                        "role": "пишет код", "model": "fake:echo", "leader": False})
    app.on_agent_event({"type": "agent.state", "agent_id": "snap-crew-1",
                        "parent_id": "main", "kind": "satellite", "name": "Кодер1",
                        "state": "действует"})
    app.on_agent_event({"type": "usage", "agent_id": "snap-crew-1",
                        "parent_id": "main", "kind": "satellite", "name": "Кодер1",
                        "model": "fake:echo", "input": 1200, "output": 300,
                        "cost": 0.0015, "context_pct": 1.0})
    app.on_agent_event({"type": "agent.message", "agent_id": "snap-crew-1",
                        "parent_id": "main", "kind": "satellite", "name": "Кодер1",
                        "from": "Луна", "to": "Кодер1",
                        "from_id": "main", "to_id": "snap-crew-1",
                        "text": "Почини сборку"})
    app.on_agent_event({"type": "budget.update", "agent_id": "snap-crew-1",
                        "parent_id": "main", "kind": "satellite", "name": "Кодер1",
                        "spent_usd": 0.0015, "limit_usd": 5.0, "remaining_usd": 4.9985,
                        "tokens": 1500, "rate_per_min_usd": 0.09, "eta_sec": 3330.0,
                        "forecast_min_usd": 0.09, "exhausted": False})
    app.layout.open("moon")
    app.layout.open("files")
    app.layout.open("backside")
    app.layout.open("crew")
    app.layout.open("nibbler")
    # Режим панелей: фокус подсвечен, внизу колонки — подсказка клавиш.
    app.panel_focus = True
    app.layout.focused = "moon"
    # Снимок живёт в вымышленном времени (_FAKE_NOW), а срок строки Ниблера
    # приложение ставит по своим часам — для кадра продлеваем её жизнь.
    app._nibbler_until = _FAKE_NOW + 60.0
    buf, _cursor = app.draw(cols, rows, _FAKE_NOW + 1.0)
    return buf


def _render_help(cols: int, rows: int, caps, settings) -> Buffer:
    """Выбиралка команд /help поверх ленты (ЗАДАЧА-16)."""
    from ..core.bus import EventBus
    from ..ui.anim import Scheduler
    from ..ui.chat import ChatApp

    app = ChatApp(EventBus(), settings, Scheduler(), caps.glyph_mode)
    app._clock = lambda: _FAKE_NOW  # проявление — от вымышленного «сейчас»
    app.on_agent_event({"type": "session.start", "cwd": "/home/user/project",
                        "model": "fake-agent", "mode": "normal"})
    app.submit_text("Почини сборку и поставь зависимости")
    app.open_help_picker()
    buf, _cursor = app.draw(cols, rows, _FAKE_NOW + 1.0)
    return buf


def _render_tiles(cols: int, rows: int, caps, settings) -> Buffer:
    """Новые тайлы ЗАДАЧИ-16: Контекст, MCP, Процессы, Диагностика."""
    from ..core.bus import EventBus
    from ..ui.anim import Scheduler
    from ..ui.chat import ChatApp

    app = ChatApp(EventBus(), settings, Scheduler(), caps.glyph_mode)
    app._clock = lambda: _FAKE_NOW  # проявление — от вымышленного «сейчас»
    app.on_agent_event({"type": "session.start", "cwd": "/home/user/project",
                        "model": "fake-agent", "mode": "normal"})
    app.submit_text("Почини сборку и запусти тесты в фоне")
    app.on_agent_event({"type": "turn.start", "turn": 1})
    app.on_agent_event({"type": "agent.thinking",
                        "text": "Сверяюсь со звёздами и смотрю, что уже есть под рукой. "})
    app.on_agent_event({"type": "agent.text",
                        "text": "Ставлю зависимость и гоняю тесты."})
    app.on_agent_event({"type": "tool.call", "id": "t1", "name": "bash",
                        "args": {"command": "python -m unittest -q"},
                        "intent": "Прогоняю тесты"})
    app.on_agent_event({"type": "tool.result", "id": "t1", "ok": True,
                        "summary": "зелёные",
                        "output": "Ran 312 tests in 4.1s\nOK\n"})
    app.on_agent_event({"type": "tool.call", "id": "t2", "name": "bash",
                        "args": {"command": "python server.py", "background": True},
                        "intent": "Сервер в фоне"})
    app.on_agent_event({"type": "tool.result", "id": "t2", "ok": True,
                        "summary": "Фоновая команда запущена, pid 4242. "
                                   "Вывод пишется в bg-4242.log",
                        "output": "Фоновая команда запущена, pid 4242. "
                                  "Вывод пишется в bg-4242.log"})
    app.on_agent_event({"type": "tool.call", "id": "t3",
                        "name": "mcp__files__read",
                        "args": {"path": "notes.md"}, "intent": "Читаю через MCP"})
    app.on_agent_event({"type": "tool.result", "id": "t3", "ok": True,
                        "summary": "прочитано", "output": "заметки\n"})
    app.on_agent_event({"type": "usage", "model": "fake-agent", "input": 4200,
                        "output": 860, "cost": 0.0061, "context_pct": 47})
    app.on_agent_event({"type": "agent.text.done"})
    app.on_agent_event({"type": "turn.done", "turn": 1, "reason": "end"})
    app.layout.open("context")
    app.layout.open("mcp")
    app.layout.open("processes")
    app.layout.open("diag")
    buf, _cursor = app.draw(cols, rows, _FAKE_NOW + 1.0)
    return buf


def _render_constellation(cols: int, rows: int, caps, settings) -> Buffer:
    """Панель «Созвездие» (ЗАДАЧА-15): рой событиями swarm.*, часы — _FAKE_NOW."""
    from ..core.bus import EventBus
    from ..ui.anim import Scheduler
    from ..ui.chat import ChatApp

    app = ChatApp(EventBus(), settings, Scheduler(), caps.glyph_mode)
    app._clock = lambda: _FAKE_NOW  # мерцание — от вымышленного «сейчас»
    app.on_agent_event({"type": "session.start", "cwd": "/home/user/project",
                        "model": "fake-agent", "mode": "normal"})
    app.submit_text("Запусти звездопад: классифицируй все отзывы")
    app.on_agent_event({"type": "swarm.start", "swarm": "leo-snap", "stream": "leo",
                        "total": 48, "links": 6, "profiles": ["cls"],
                        "budget_usd": 1.0, "mail": "tree", "host": "leo/0"})
    cells = []
    states = (["run"] * 10 + ["wait"] * 14 + ["done"] * 18 + ["failed"] * 2 +
              ["skipped"] * 4)
    for i, state in enumerate(states):
        link, idx = divmod(i, 8)
        cells.append(["leo/cls." + str(link) + "." + str(idx), state])
    app.on_agent_event({"type": "swarm.cells", "swarm": "leo-snap",
                        "cells": cells, "links": False})
    app.on_agent_event({"type": "swarm.progress", "swarm": "leo-snap", "done": 18,
                        "failed": 2, "skipped": 0, "total": 48,
                        "spent_usd": 0.0123, "eta_sec": 42.0})
    app.on_agent_event({"type": "agent.message", "agent_id": "leo/cls.2.0",
                        "parent_id": "leo/cls.2.3", "kind": "cell",
                        "name": "leo/cls.2.0", "from": "leo/cls.2.3",
                        "to": "leo/cls.2.0", "from_id": "leo/cls.2.3",
                        "to_id": "leo/cls.2.0", "text": "разметил, сдаю"})
    app.layout.open("constellation")
    buf, _cursor = app.draw(cols, rows, _FAKE_NOW + 1.0)
    return buf


def render_frames(name: str, cols: int, rows: int, times: list, settings=None) -> list:
    """Список (подпись, буфер) для экрана name в моменты times (секунды от старта)."""
    from ..core.settings import Settings

    settings = Settings() if settings is None else settings
    caps = _snapshot_caps(cols, rows, settings)
    phrase = phrases.random_splash()
    demo = None
    if name == "render":
        from ..ui.render_try import _Demo

        demo = _Demo(caps.glyph_mode)
        demo.spinner.started = _FAKE_NOW
        demo.wheel.started = _FAKE_NOW
        demo._fps_at = _FAKE_NOW
    if name == "chat":
        # У чата кадры — не моменты времени, а именованные состояния ленты.
        return [(state, _render_chat_state(state, cols, rows, caps, settings))
                for state in _CHAT_STATES]
    if name == "thinking":
        # Мышление — наоборот, чистая функция времени кадра.
        return [(str(at) + " с", _render_thinking(cols, rows, at, caps, settings))
                for at in times]
    if name == "sessions":
        return [("выбиралка сессий", _render_sessions(cols, rows, caps, settings))]
    if name == "panels":
        return [("панели открыты", _render_panels(cols, rows, caps, settings))]
    if name == "help":
        return [("выбиралка команд", _render_help(cols, rows, caps, settings))]
    if name == "tiles":
        return [("тайлы: контекст, mcp, процессы, диагностика",
                 _render_tiles(cols, rows, caps, settings))]
    if name == "constellation":
        return [("созвездие роя", _render_constellation(cols, rows, caps, settings))]
    frames = []
    for at in times:
        if name == "splash":
            buf = _render_splash(cols, rows, at, caps, phrase, settings)
        elif name == "render":
            buf = _render_render(cols, rows, at, caps, demo)
        else:
            buf = _render_main(cols, rows, at, caps, settings)
        label = f"{at:.2f} с"
        frames.append((label, buf))
    return frames


# --- буфер → HTML ----------------------------------------------------------


def _run_css(fg, bg, attrs: int) -> str:
    if attrs & _color.REVERSE:
        fg, bg = bg, fg
    parts = []
    if fg is not None:
        parts.append("color:" + _color.hex_of(fg))
    if bg is not None:
        parts.append("background:" + _color.hex_of(bg))
    if attrs & _color.BOLD:
        parts.append("font-weight:700")
    if attrs & _color.DIM:
        parts.append("opacity:.55")
    if attrs & _color.ITALIC:
        parts.append("font-style:italic")
    if attrs & _color.UNDERLINE:
        parts.append("text-decoration:underline")
    return ";".join(parts)


def buffer_to_html(buf: Buffer) -> str:
    """Один буфер — HTML-фрагмент экрана: строки из span-ов, соседние ячейки
    с одинаковым стилем сливаются в один span (файл не пухнет зря)."""
    lines = []
    for y in range(buf.rows):
        runs = []  # (css, текст)
        for x in range(buf.cols):
            ch, fg, bg, attrs = buf.cells[y][x]
            if ch == "":
                continue  # продолжение широкого символа: места не занимает
            css = _run_css(fg, bg, attrs)
            if runs and runs[-1][0] == css:
                runs[-1][1] += ch
            else:
                runs.append([css, ch])
        parts = []
        for css, text in runs:
            esc = html.escape(text)
            if css:
                parts.append(f'<span style="{css}">{esc}</span>')
            else:
                parts.append(esc)
        # Каждая строка — отдельный блок с line-height: 1: между строками
        # нет щелей от строчного интервала шрифта (ЗАДАЧА-05).
        lines.append('<div class="line">' + "".join(parts) + "</div>")
    return '<div class="scr">' + "\n".join(lines) + "</div>"


def frames_to_html(frames: list, title: str) -> str:
    """Страница раскадровки: кадры подряд, у каждого подпись с моментом."""
    parts = ["<!DOCTYPE html>", '<html lang="ru">', "<head>", '<meta charset="utf-8">',
             f"<title>{html.escape(title)}</title>", f"<style>{_PAGE_CSS}</style>",
             "</head>", "<body>", f"<h1>{html.escape(title)}</h1>"]
    total = len(frames)
    for i, (label, buf) in enumerate(frames):
        caption = f"кадр {i + 1} из {total} · {label}"
        parts.append('<div class="frame">')
        parts.append(f'<p class="cap">{html.escape(caption)}</p>')
        parts.append(buffer_to_html(buf))
        parts.append("</div>")
    parts.append("</body>")
    parts.append("</html>")
    return "\n".join(parts) + "\n"


# --- команда -----------------------------------------------------------------


def parse_args(argv: list) -> dict:
    """Разобрать аргументы snapshot; ошибки — HumanError с подсказкой."""
    if not argv or argv[0] not in SCREENS:
        names = " | ".join(SCREENS)
        raise HumanError("usage", f"Нужно: luna try snapshot <{names}> -o файл.html",
                         "Например: luna try snapshot splash --frames 11 --every 0.12 -o заставка.html")
    opts = {"name": argv[0], "at": 0.0, "frames": 1, "every": 0.12,
            "cols": 80, "rows": 24, "out": None}
    rest = argv[1:]
    i = 0
    while i < len(rest):
        arg = rest[i]
        if arg in ("--at", "--every"):
            i += 1
            if i >= len(rest):
                raise HumanError("usage", f"После {arg} нужно число секунд", "")
            try:
                opts["at" if arg == "--at" else "every"] = float(rest[i])
            except ValueError:
                raise HumanError("usage", f"Не число секунд: {rest[i]}", "") from None
        elif arg == "--frames":
            i += 1
            if i >= len(rest):
                raise HumanError("usage", "После --frames нужно целое число кадров", "")
            try:
                opts["frames"] = int(rest[i])
            except ValueError:
                raise HumanError("usage", f"Не целое число кадров: {rest[i]}", "") from None
        elif arg == "--size":
            i += 1
            if i >= len(rest) or "x" not in rest[i]:
                raise HumanError("usage", "Размер — как 120x30 (колонки x строки)", "")
            cols, _, rows = rest[i].partition("x")
            try:
                opts["cols"], opts["rows"] = int(cols), int(rows)
            except ValueError:
                raise HumanError("usage", f"Не размер: {rest[i]}", "") from None
        elif arg == "-o":
            i += 1
            if i >= len(rest):
                raise HumanError("usage", "После -o нужен путь файла", "")
            opts["out"] = rest[i]
        else:
            raise HumanError("usage", f"Неизвестный аргумент: {arg}",
                             "Есть: --at, --frames, --every, --size, -o.")
        i += 1
    if not opts["out"]:
        raise HumanError("usage", "Нужен -o файл.html — куда писать снимок", "")
    if opts["frames"] < 1 or opts["every"] < 0 or opts["at"] < 0:
        raise HumanError("usage", "--frames ≥ 1, --every и --at — не отрицательные", "")
    if not (10 <= opts["cols"] <= 500 and 5 <= opts["rows"] <= 200):
        raise HumanError("usage", "Размер снимка — от 10x5 до 500x200", "")
    return opts


def run_snapshot(argv: list) -> int:
    # Печатаем сами — сами и обеспечиваем кодировку: run_snapshot зовут
    # и в обход main() (тесты), а cp1252 пайпа роняет «Снимок» (CI zadacha-03).
    ensure_utf8_stdio()
    opts = parse_args(argv)
    times = [opts["at"] + i * opts["every"] for i in range(opts["frames"])]
    frames = render_frames(opts["name"], opts["cols"], opts["rows"], times)
    name = opts["name"]
    title = f"Луна — снимок экрана {name}"
    page = frames_to_html(frames, title)
    out = Path(opts["out"])
    if out.parent and not out.parent.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    frames_count = len(frames)
    cols, rows = opts["cols"], opts["rows"]
    size = f"{cols}x{rows}"
    print(f"Снимок: {out} — экран {name}, {size}, кадров: {frames_count}")
    return 0
