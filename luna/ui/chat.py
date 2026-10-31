"""Полноэкранный чат Луны (веха 1, часть Б): лента, ввод, панели.

Экран — клиент контракта docs/СОБЫТИЯ.md: всё, что он показывает, пришло
событиями источника; всё, что делает человек, уходит командами контракта.
Источник — ОДНО место подмены: create_source() ниже. Сейчас это фальшивый
агент (luna/devtools/fake_agent.py); когда ЗАДАЧА-04 ляжет в main, тимлид
подключит настоящий агент заменой тела этой одной функции.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..core.bus import EventBus
from ..core.paths import DataDir
from ..core.run_lock import RunLock
from ..core.settings import Settings
from . import color as _color
from . import moon as _moon
from . import phrases
from .anim import PhraseWheel, Scheduler, Spinner, ease_in_out
from .editor import TextEditor
from .keys import Key, Mouse, Paste, Resize
from .markdown import Span, render_markdown, wrap_spans, wrap_text
from .panels import LayoutTree, create_panel
from .screen import Buffer, Screen
from .term import Terminal
from .width import str_width

MODES = ("normal", "plan", "review", "auto", "yolo")
STATUS_BG = (0x16, 0x1B, 0x30)
PANEL_BG = (0x10, 0x13, 0x24)
GUARD_BG = (0x24, 0x1B, 0x10)
DIM_FG = (0x8B, 0x94, 0xAC)

_STYLE_MAP = {
    "text": (_color.SILVER, 0),
    "bold": (_color.SILVER, _color.BOLD),
    "italic": (_color.SILVER, _color.ITALIC),
    "code": (_color.CYAN, 0),
    "heading": (_color.VIOLET, _color.BOLD),
    "bullet": (_color.CYAN, 0),
    "dim": (DIM_FG, _color.DIM),
    "link": (_color.CYAN, _color.UNDERLINE),
    "keyword": (_color.VIOLET, 0),
    "string": (_color.AMBER, 0),
    "number": (_color.MAGENTA, 0),
    "comment": (DIM_FG, _color.ITALIC),
    "user": (_color.SILVER, 0),
    "amber": (_color.AMBER, _color.BOLD),
    "guard": (_color.AMBER, _color.BOLD),
    "cyan": (_color.CYAN, _color.BOLD),
    "tool": (_color.CYAN, _color.BOLD),
    "ok": (_color.OK, 0),
    "err": (_color.ERR, _color.BOLD),
    "notice": (_color.VIOLET, _color.DIM),
}

HELP_TEXT = """Команды и клавиши Луны
/mode [режим] — режим по кругу или по имени (normal plan review auto yolo)
/clear — очистить ленту · /help — эта справка · /exit — выход
/panels или Ctrl+T — панель плана (todo) · /layout save|load|list <имя> — раскладки
!команда — выполнить мимо модели · @файл — вложить файл (Tab — дополнить путь)
Enter — отправить · Alt+Enter — новая строка · стрелки — история и курсор
Печатать можно во время работы: сообщение встанет в очередь
Esc Esc — прервать ход · Tab / Shift+Tab — режим по кругу
Ctrl+O — раскрыть/свернуть детали (рассуждения, вывод тула, план)
F6 — фокус в панели; в панелях: ↑↓ фокус, ←→ размер, Enter — развернуть, Esc — назад
PgUp / PgDn и колесо мыши — прокрутка ленты"""


def create_source(settings=None, *, fake: bool = True):
    """Единственное место, где экран получает источник событий.

    fake=True — фальшивый агент (luna try chat, снимки, тесты);
    fake=False — настоящий агент в фоновом потоке (luna без аргументов).
    """
    if not fake:
        from ..mind.source import AgentSource

        return AgentSource(settings=settings, cwd=os.getcwd())
    from ..devtools.fake_agent import FakeAgent

    return FakeAgent(cwd=os.getcwd())


def diff_counts(summary: str, output: str):
    """Мини-дифф правок: (+N, −N) из сводки «+12 −3» или из строк вывода +/-."""
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


def compact_args(name: str, args: dict) -> str:
    """Однострочная суть вызова тула для компактной строки ленты."""
    if not isinstance(args, dict) or not args:
        return ""
    for key in ("command", "path", "file", "pattern", "query", "url"):
        if key in args:
            return str(args[key])
    try:
        return json.dumps(args, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(args)


@dataclass
class Entry:
    kind: str  # user | agent | thinking | tool | guard | ask | todo | notice | error | help | shell
    text: str = ""
    name: str = ""
    intent: str = ""
    args: dict = field(default_factory=dict)
    output: str = ""
    summary: str = ""
    ok: object = None
    entry_id: str = ""
    status: str = ""       # tool: running|done · guard/ask: pending|<решение>
    expanded: bool = False
    options: list = field(default_factory=list)
    selected: int = 0
    answer: str = ""
    items: list = field(default_factory=list)
    risk: str = ""
    reason: str = ""
    born: float = 0.0
    streaming: bool = False
    guard_of: str = ""     # guard: id тула, к которому относится подтверждение


class ChatApp:
    """Состояние чат-экрана и реакции на ввод и события источника."""

    def __init__(self, bus=None, settings=None, scheduler=None,
                 glyph_mode="emoji", source=None, cwd=None):
        self.bus = bus or EventBus()
        self.settings = settings or Settings()
        self.scheduler = scheduler or Scheduler()
        self.glyph_mode = _moon.normalize_mode(glyph_mode)
        self.cwd = cwd or os.getcwd()
        self.source = source if source is not None else create_source(self.settings)
        self.editor = TextEditor()
        self.entries: list = []
        self.todos: list = []
        self.mode = "normal"
        self.model = "—"
        self.cost = 0.0
        self.context_pct = 0
        self.busy = False
        self.done = False
        self.queue: list = []
        self.sent_commands: list = []
        self.scroll = 0
        self.layout = LayoutTree()
        self.panel_focus = False
        self._panel_cache: dict = {}
        self.guard_editing = False
        self.completions: list = []
        self.completion_index = 0
        self._clock = time.monotonic
        self._last_esc = -10.0
        self._last_change = 0.0
        self._after_interrupt = ""
        self._started = False
        self._page = 10
        self.spinner = Spinner(self.glyph_mode)
        self.wheel = PhraseWheel(phrases.spinner_phrases())

    # --- источник событий ------------------------------------------------------

    def start_source(self) -> None:
        if self._started:
            return
        self._started = True
        starter = getattr(self.source, "start", None)
        if callable(starter):
            starter()
        self.poll_source()

    def poll_source(self) -> list:
        poll = getattr(self.source, "poll", None)
        if not callable(poll):
            return []
        events = poll()
        for ev in events:
            self.on_agent_event(ev)
        return events

    def send_command(self, cmd: dict) -> None:
        self.sent_commands.append(dict(cmd))
        handler = getattr(self.source, "handle_command", None)
        if callable(handler):
            handler(cmd)
        self.bus.publish({"type": "agent.command", "command": dict(cmd)})
        self.poll_source()

    def needs_frame(self, now: float) -> bool:
        """Кадры нужны, пока идёт ход, есть события или что-то затухает."""
        if self.busy:
            return True
        pending = getattr(self.source, "has_pending", None)
        if callable(pending) and pending():
            return True
        if self.settings.get("ui.animations"):
            return now - self._last_change < 0.4
        return False

    def next_timeout(self):
        return self.scheduler.frame_interval if self.needs_frame(self._clock()) else None

    # --- события агента ----------------------------------------------------------

    def _add(self, entry: Entry) -> Entry:
        entry.born = self._clock()
        self.entries.append(entry)
        self._last_change = entry.born
        self.scroll = 0
        return entry

    def _stream_entry(self, kind: str) -> Entry:
        for entry in reversed(self.entries):
            if entry.kind == kind and entry.streaming:
                return entry
        return self._add(Entry(kind, streaming=True))

    def _find(self, kind: str, entry_id: str):
        for entry in reversed(self.entries):
            if entry.kind == kind and entry.entry_id == entry_id:
                return entry
        return None

    def on_agent_event(self, ev: dict) -> None:
        kind = ev.get("type")
        self._last_change = self._clock()
        if kind == "session.start":
            self.model = str(ev.get("model") or self.model)
            self.mode = str(ev.get("mode") or self.mode)
            if ev.get("cwd"):
                self.cwd = str(ev.get("cwd"))
        elif kind == "turn.start":
            self.busy = True
        elif kind == "agent.thinking":
            self._stream_entry("thinking").text += str(ev.get("text", ""))
        elif kind == "agent.text":
            self._stream_entry("agent").text += str(ev.get("text", ""))
        elif kind == "agent.text.done":
            for entry in self.entries:
                entry.streaming = False
        elif kind == "tool.call":
            self._add(Entry("tool", name=str(ev.get("name", "")),
                            intent=str(ev.get("intent", "")),
                            args=dict(ev.get("args") or {}),
                            entry_id=str(ev.get("id", "")), status="running"))
        elif kind == "tool.output":
            entry = self._find("tool", str(ev.get("id", "")))
            if entry is not None:
                entry.output += str(ev.get("text", ""))
        elif kind == "tool.result":
            entry = self._find("tool", str(ev.get("id", "")))
            if entry is None:
                entry = self._add(Entry("tool", entry_id=str(ev.get("id", ""))))
            entry.status = "done"
            entry.ok = bool(ev.get("ok"))
            entry.summary = str(ev.get("summary", ""))
            out = str(ev.get("output", ""))
            if out and out not in entry.output:
                entry.output += out
        elif kind == "guard.ask":
            entry = Entry("guard", name=str(ev.get("tool", "")),
                          intent=str(ev.get("intent", "")),
                          args=dict(ev.get("args") or {}),
                          entry_id=str(ev.get("id", "")), status="pending",
                          risk=str(ev.get("risk", "")),
                          reason=str(ev.get("reason", "")))
            # Подтверждение относится к вызову тула: у настоящего агента
            # guard.ask несёт тот же id, что tool.call; у фальшивого —
            # ищем идущий тул с тем же именем. Показываться блок будет
            # внутри записи тула — между вызовом и результатом (п. 3).
            tool = self._find("tool", entry.entry_id)
            if tool is None:
                for cand in reversed(self.entries):
                    if (cand.kind == "tool" and cand.status == "running"
                            and cand.name == entry.name):
                        tool = cand
                        break
            if tool is not None:
                entry.guard_of = tool.entry_id
            self._add(entry)
        elif kind == "guard.verdict":
            entry = self._find("guard", str(ev.get("id", "")))
            if entry is not None and entry.status == "pending":
                entry.status = str(ev.get("decision", ""))
        elif kind == "ask.user":
            self._add(Entry("ask", text=str(ev.get("question", "")),
                            options=list(ev.get("options") or []),
                            entry_id=str(ev.get("id", "")), status="pending"))
        elif kind == "todo":
            self.todos = list(ev.get("items") or [])
            entry = self._find("todo", "todo")
            if entry is None:
                entry = self._add(Entry("todo", entry_id="todo"))
            entry.items = list(self.todos)
            entry.born = self._clock()
        elif kind == "usage":
            self.model = str(ev.get("model") or self.model)
            self.cost = float(ev.get("cost") or 0.0)
            self.context_pct = int(round(float(ev.get("context_pct") or 0)))
        elif kind == "mode.changed":
            self.mode = str(ev.get("mode") or self.mode)
        elif kind == "notice":
            self._add(Entry("notice", text=str(ev.get("text", ""))))
        elif kind == "error":
            text = str(ev.get("message", ""))
            detail = str(ev.get("detail", ""))
            if detail:
                text = text + "\n" + detail
            self._add(Entry("error", text=text))
        elif kind == "turn.done":
            self.busy = False
            for entry in self.entries:
                entry.streaming = False
            if self._after_interrupt:
                text, self._after_interrupt = self._after_interrupt, ""
                self._send_user_message(text)
            elif self.queue:
                self._send_user_message(self.queue.pop(0))
        # незнакомые типы молча пропускаем — контракт только добавляется

    # --- отправка и команды ---------------------------------------------------------

    def _send_user_message(self, text: str) -> None:
        attachments = [tok[1:] for tok in text.split()
                       if tok.startswith("@") and len(tok) > 1]
        self._add(Entry("user", text=text))
        self.send_command({"type": "user.message", "text": text,
                           "attachments": attachments})

    def cycle_mode(self, step: int = 1) -> None:
        idx = MODES.index(self.mode) if self.mode in MODES else 0
        self.send_command({"type": "mode.set", "mode": MODES[(idx + step) % len(MODES)]})

    def submit_text(self, text: str | None = None) -> None:
        if text is None:
            text = self.editor.submit()
        text = text.strip("\n")
        if not text.strip():
            return
        if text.startswith("/") and not text.startswith("//"):
            self._slash(text)
            return
        if text.startswith("!"):
            command = text[1:].strip()
            if command:
                self._add(Entry("shell", text=command))
                self.send_command({"type": "shell", "command": command})
            return
        if self.busy:
            self.queue.append(text)
            self._add(Entry("notice", text="В очереди: " + text.split("\n")[0]))
            return
        self._send_user_message(text)

    def _slash(self, text: str) -> None:
        parts = text.split()
        cmd = parts[0]
        if cmd == "/exit":
            self.done = True
        elif cmd == "/clear":
            self.entries.clear()
            self._add(Entry("notice", text="Лента очищена."))
        elif cmd == "/help":
            self._add(Entry("help", text=HELP_TEXT))
        elif cmd == "/mode":
            if len(parts) > 1 and parts[1] in MODES:
                self.send_command({"type": "mode.set", "mode": parts[1]})
            elif len(parts) > 1:
                self._add(Entry("notice", text="Нет режима " + parts[1]))
            else:
                self.cycle_mode(1)
        elif cmd in ("/panels", "/todo"):
            self.toggle_panel("todo")
        elif cmd == "/layout":
            self._layout_command(parts[1:])
        else:
            self._add(Entry("notice", text="Не знаю команду " + cmd + " — /help подскажет."))

    def _layout_command(self, args: list) -> None:
        if not args:
            names = ", ".join(self.layout.layout_names()) or "пока нет"
            self._add(Entry("notice", text="Сохранённые раскладки: " + names))
            return
        action = args[0]
        name = args[1] if len(args) > 1 else "основная"
        if action == "save":
            self.layout.save_layout(name)
            self._add(Entry("notice", text="Раскладка сохранена: " + name))
        elif action == "load":
            ok = self.layout.load_layout(name)
            note = "Раскладка загружена: " + name if ok else "Нет раскладки " + name
            self._add(Entry("notice", text=note))
        elif action == "list":
            names = ", ".join(self.layout.layout_names()) or "пока нет"
            self._add(Entry("notice", text="Сохранённые раскладки: " + names))
        else:
            self._add(Entry("notice", text="Нужно: /layout save|load|list <имя>"))

    def toggle_panel(self, panel_id: str) -> None:
        if not self.settings.get("ui.panels"):
            self._add(Entry("notice", text="Панели выключены настройкой ui.panels."))
            return
        opened = self.layout.toggle(panel_id)
        if not opened:
            self.panel_focus = False

    # --- подтверждение и вопросы ------------------------------------------------------

    @property
    def pending_guard(self):
        for entry in reversed(self.entries):
            if entry.kind == "guard" and entry.status == "pending":
                return entry
        return None

    @property
    def pending_ask(self):
        for entry in reversed(self.entries):
            if entry.kind == "ask" and entry.status == "pending":
                return entry
        return None

    def guard_answer(self, decision: str, args: dict | None = None) -> None:
        entry = self.pending_guard
        if entry is None:
            return
        entry.status = decision
        self.guard_editing = False
        cmd = {"type": "guard.answer", "id": entry.entry_id, "decision": decision}
        if args is not None:
            cmd["args"] = args
        self.send_command(cmd)

    def _guard_finish_edit(self) -> None:
        entry = self.pending_guard
        if entry is None:
            self.guard_editing = False
            return
        text = self.editor.text.strip()
        self.editor.clear()
        args = dict(entry.args)
        parsed = None
        if text.startswith("{"):
            try:
                value = json.loads(text)
                if isinstance(value, dict):
                    parsed = value
            except ValueError:
                parsed = None
        if parsed is not None:
            args = parsed
        elif "command" in args:
            args = {"command": text}
        elif len(args) == 1:
            key = next(iter(args))
            args = {key: text}
        elif text:
            args = {"command": text}
        self.guard_answer("edit", args)

    def ask_answer(self, text: str) -> None:
        entry = self.pending_ask
        if entry is None:
            return
        entry.status = "answered"
        entry.answer = text
        self.send_command({"type": "user.answer", "id": entry.entry_id, "text": text})

    # --- ввод ---------------------------------------------------------------------------

    def _editor_key(self, key: Key) -> bool:
        """Общие клавиши редактирования; True — клавиша съедена."""
        if key.char and not key.mods:
            self.editor.insert(key.char)
        elif key.name == "backspace":
            self.editor.backspace()
        elif key.name == "delete":
            self.editor.delete()
        elif key.name == "left":
            self.editor.left()
        elif key.name == "right":
            self.editor.right()
        elif key.name == "home":
            self.editor.line_home()
        elif key.name == "end":
            self.editor.line_end()
        elif key.label == "ctrl+u":
            self.editor.clear()
        else:
            return False
        return True

    def handle_key(self, key: Key) -> None:
        if self.done:
            return
        if key.label == "ctrl+c" or key.name == "eof":
            self.done = True
            return
        guard = self.pending_guard
        if guard is not None:
            self._guard_key(key, guard)
            return
        ask = self.pending_ask
        if ask is not None:
            self._ask_key(key, ask)
            return
        if self.panel_focus:
            self._panel_key(key)
            return
        if key.name == "f6":
            if self.layout.leaves():
                self.panel_focus = True
            else:
                self.toggle_panel("todo")
                self.panel_focus = bool(self.layout.leaves())
            return
        if key.label == "ctrl+t":
            self.toggle_panel("todo")
            return
        if key.label == "ctrl+o":
            self._toggle_expand_latest()
            return
        if key.name == "esc":
            self._esc_key()
            return
        if key.name == "tab":
            if self.completions:
                if "shift" in key.mods:
                    self.completion_index = (self.completion_index - 1) % len(self.completions)
                else:
                    self._apply_completion()
                return
            self.cycle_mode(-1 if "shift" in key.mods else 1)
            return
        if key.name == "pageup":
            self.scroll = min(self.scroll + self._page, 100000)
            return
        if key.name == "pagedown":
            self.scroll = max(0, self.scroll - self._page)
            return
        if key.name == "enter":
            if "alt" in key.mods or "shift" in key.mods:
                self.editor.newline()
                self._update_completions()
            else:
                self.submit_text()
            return
        if key.name == "up":
            if self.completions:
                self.completion_index = (self.completion_index - 1) % len(self.completions)
            elif not self.editor.up():
                self.editor.history_prev()
            return
        if key.name == "down":
            if self.completions:
                self.completion_index = (self.completion_index + 1) % len(self.completions)
            elif not self.editor.down():
                self.editor.history_next()
            return
        if self._editor_key(key):
            self._update_completions()

    def _esc_key(self) -> None:
        now = self._clock()
        double = now - self._last_esc < 0.6
        self._last_esc = now
        if self.completions:
            self.completions = []
            return
        if double and self.busy:
            draft = self.editor.text
            if draft.strip():
                self._after_interrupt = draft
                self.editor.clear()
            self.send_command({"type": "interrupt"})

    def _guard_key(self, key: Key, entry: Entry) -> None:
        if self.guard_editing:
            if key.name == "enter":
                self._guard_finish_edit()
            elif key.name == "esc":
                self.guard_editing = False
                self.editor.clear()
            else:
                self._editor_key(key)
            return
        if key.name == "enter":
            self.guard_answer("yes")
        elif key.char == "n":
            self.guard_answer("no")
        elif key.char == "e":
            self.guard_editing = True
            current = entry.args.get("command")
            if current is None:
                current = json.dumps(entry.args, ensure_ascii=False)
            self.editor.set_text(str(current))
        elif key.char == "a":
            self.guard_answer("always")
        elif key.char == "q":
            self.guard_answer("stop")

    def _ask_key(self, key: Key, entry: Entry) -> None:
        if key.name == "up" and entry.options:
            entry.selected = (entry.selected - 1) % len(entry.options)
        elif key.name == "down" and entry.options:
            entry.selected = (entry.selected + 1) % len(entry.options)
        elif key.name == "enter":
            custom = self.editor.text.strip()
            if custom:
                self.editor.clear()
                self.ask_answer(custom)
            elif entry.options:
                self.ask_answer(entry.options[entry.selected])
            else:
                self.ask_answer("")
        else:
            self._editor_key(key)

    def _panel_key(self, key: Key) -> None:
        focused = self.layout.focused
        if key.name == "esc":
            self.panel_focus = False
        elif key.name == "up":
            self.layout.focus_next(-1)
        elif key.name == "down":
            self.layout.focus_next(1)
        elif key.name == "left" and focused:
            self.layout.resize(focused, -0.25)
        elif key.name == "right" and focused:
            self.layout.resize(focused, 0.25)
        elif key.name == "enter" and focused:
            self.layout.toggle_maximize(focused)
        elif key.name == "tab" and focused:
            # Tab в фокусе панелей — переставить панель среди соседей.
            self.layout.move(focused, -1 if "shift" in key.mods else 1)

    def _toggle_expand_latest(self) -> None:
        for entry in reversed(self.entries):
            if entry.kind == "thinking" and entry.text:
                entry.expanded = not entry.expanded
                return
            if entry.kind == "tool" and entry.output:
                entry.expanded = not entry.expanded
                return
            if entry.kind == "todo" and entry.items:
                entry.expanded = not entry.expanded
                return

    def handle_paste(self, paste: Paste) -> None:
        if self.pending_guard is not None and not self.guard_editing:
            return
        self.editor.insert(paste.text)
        self._update_completions()

    def handle_mouse(self, mouse: Mouse) -> None:
        if mouse.kind == "wheel_up":
            self.scroll = min(self.scroll + 3, 100000)
        elif mouse.kind == "wheel_down":
            self.scroll = max(0, self.scroll - 3)

    # --- автодополнение @файл --------------------------------------------------------------

    def _update_completions(self) -> None:
        prefix = self.editor.text[:self.editor.cursor]
        m = re.search(r"(?:^|\s)@([^\s@]*)$", prefix)
        if not m:
            self.completions = []
            return
        token = m.group(1)
        base, _, _name = token.rpartition("/")
        directory = Path(self.cwd) / base if base else Path(self.cwd)
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            self.completions = []
            return
        out = []
        for name in names:
            if name.startswith("__"):
                continue
            full = directory / name
            rel = (base + "/" + name) if base else name
            if full.is_dir():
                rel += "/"
            if rel.startswith(token) or name.startswith(_name):
                out.append(rel)
        self.completions = out[:8]
        self.completion_index = 0

    def _apply_completion(self) -> None:
        if not self.completions:
            return
        choice = self.completions[self.completion_index]
        prefix = self.editor.text[:self.editor.cursor]
        m = re.search(r"@([^\s@]*)$", prefix)
        if not m:
            return
        start = self.editor.cursor - len(m.group(1))
        self.editor.text = self.editor.text[:start] + choice + self.editor.text[self.editor.cursor:]
        self.editor.cursor = start + len(choice)
        if choice.endswith("/"):
            self._update_completions()  # вошли в каталог — показать его содержимое
        else:
            self.completions = []       # файл выбран — меню закрыто

    # --- отрисовка ---------------------------------------------------------------------------

    def _alpha(self, entry: Entry, now: float) -> float:
        if not self.settings.get("ui.animations"):
            return 1.0
        return ease_in_out((now - entry.born) / 0.3)

    def _paint(self, spans, alpha: float):
        out = []
        for text, style in spans:
            fg, attrs = _STYLE_MAP.get(style, _STYLE_MAP["text"])
            if alpha < 1.0:
                fg = _color.lerp(_color.SPACE_BG, fg, alpha)
            out.append((text, fg, attrs))
        return out

    def _entry_lines(self, entry: Entry, width: int, now: float):
        """Визуальные строки записи: список (спаны, фон строки)."""
        alpha = self._alpha(entry, now)
        raw: list = []

        def add(spans, bg=None):
            raw.append((self._paint(spans, alpha), bg))

        w = max(4, width)
        if entry.kind == "user":
            lines = wrap_spans([Span(entry.text)], w - 2)
            for i, line in enumerate(lines):
                prefix = "› " if i == 0 else "  "
                add([(prefix, "amber")] + [(s.text, "user") for s in line])
        elif entry.kind == "agent":
            md_lines = render_markdown(entry.text, w)
            for idx, line in enumerate(md_lines):
                spans = [(s.text, s.style) for s in line]
                if entry.streaming and idx == len(md_lines) - 1:
                    spans.append(("▌", "dim"))  # живой курсор стрима — в той же строке
                add(spans)
        elif entry.kind == "thinking":
            if entry.expanded:
                for line in wrap_text(entry.text, w - 2):
                    add([("◦ ", "dim"), (line, "comment")])
            else:
                count = len(entry.text)
                label = "рассуждения · символов: " + str(count) + " · Ctrl+O — показать"
                add([("◦ ", "dim"), (label, "dim")])
        elif entry.kind == "tool":
            glyph = {"emoji": "⚙", "narrow": "•", "ascii": "+"}[self.glyph_mode]
            head = [(glyph + " ", "tool"), (entry.name or "tool", "tool")]
            compact = compact_args(entry.name, entry.args)
            if compact:
                head.append(("  " + compact, "text"))
            if entry.status == "running":
                head.append(("  " + self.spinner.glyph(now), "amber"))
            add(head)
            if entry.intent:
                add([("  " + entry.intent, "dim")])
            for guard in self._guards_for(entry):
                self._guard_lines(guard, w, add)
            if entry.status == "done":
                mark = "✓ " if entry.ok else "✗ "
                style = "ok" if entry.ok else "err"
                line = [("  " + mark, style), (entry.summary, "text")]
                # Мини-дифф бейджем — только если сводка его ещё не сказала:
                # числа тогда берутся из строк +/- вывода.
                if diff_counts(entry.summary, "") is None:
                    counts = diff_counts("", entry.output)
                    if counts is not None:
                        badge = "  +" + str(counts[0]) + " −" + str(counts[1])
                        line.append((badge, "ok" if entry.ok else "err"))
                add(line)
                out_lines = entry.output.rstrip("\n").split("\n") if entry.output.strip() else []
                if out_lines:
                    if entry.expanded:
                        for out_line in out_lines:
                            for visual in wrap_text(out_line, w - 4):
                                add([("    " + visual, "dim")])
                    else:
                        label = ("  вывод: строк " + str(len(out_lines))
                                 + " · Ctrl+O — раскрыть")
                        add([(label, "dim")])
            elif entry.output:
                add([("  идёт вывод…", "dim")])
        elif entry.kind == "guard":
            self._guard_lines(entry, w, add)
        elif entry.kind == "ask":
            self._ask_lines(entry, w, add)
        elif entry.kind == "todo":
            self._todo_lines(entry, add)
        elif entry.kind == "notice":
            for line in wrap_text(entry.text, w):
                add([(line, "notice")])
        elif entry.kind == "error":
            for i, line in enumerate(wrap_text(entry.text, w - 2)):
                prefix = "✗ " if i == 0 else "  "
                style = "err" if i == 0 else "dim"
                add([(prefix, "err"), (line, style)])
        elif entry.kind == "help":
            for line in entry.text.split("\n"):
                add([(line, "dim")])
        elif entry.kind == "shell":
            add([("$ ", "cyan"), (entry.text, "text")])
        return raw

    _DECISIONS = {"yes": "да", "no": "нет", "edit": "да, с правкой",
                  "always": "да, дальше без вопросов", "stop": "стоп"}

    def _guard_lines(self, entry: Entry, width: int, add) -> None:
        if entry.status != "pending":
            # Решённый блок сворачивается в одну строку решения на месте —
            # тёплый фон не остаётся висеть в ленте (ЗАДАЧА-06, п. 3).
            denied = entry.status in ("no", "stop")
            mark = "✗ " if denied else "✓ "
            label = self._DECISIONS.get(entry.status, entry.status)
            line = "решение: " + label
            target = (entry.name + " " + compact_args(entry.name, entry.args)).strip()
            if target:
                line += " · " + target
            add([(mark, "err" if denied else "ok"), (line, "dim")])
            return
        mark = "⚠ " if self.glyph_mode == "emoji" else "! "
        head = "нужно решение"
        if entry.risk:
            head += " · риск " + entry.risk
        add([(mark, "guard"), (head, "guard")], GUARD_BG)
        if entry.intent:
            add([("  " + entry.intent, "text")], GUARD_BG)
        args_line = compact_args(entry.name, entry.args)
        if args_line:
            for line in wrap_text(args_line, width - 2):
                add([("  " + line, "code")], GUARD_BG)
        if entry.reason:
            add([("  причина: " + entry.reason, "dim")], GUARD_BG)
        add([("  Enter — да · n — нет · e — править · a — дальше без вопросов · q — стоп",
              "guard")], GUARD_BG)
        if self.guard_editing:
            add([("  правка аргументов: Enter — отправить, Esc — отмена", "dim")], GUARD_BG)

    def _ask_lines(self, entry: Entry, width: int, add) -> None:  # noqa: ARG002 — единый вид строителей строк
        add([("? ", "guard"), (entry.text, "bold")])
        for i, option in enumerate(entry.options):
            marker = "› " if i == entry.selected and entry.status == "pending" else "  "
            style = "cyan" if i == entry.selected and entry.status == "pending" else "text"
            add([(marker, "cyan"), (option, style)])
        if entry.options and entry.status == "pending":
            add([("  ↑↓ — выбор · Enter — ответить · или печатайте свой ответ", "dim")])
        if entry.status == "answered":
            add([("  ответ: " + entry.answer, "dim")])

    def _todo_lines(self, entry: Entry, add) -> None:
        items = entry.items or self.todos
        done_count = sum(1 for it in items if it.get("status") == "done")
        head = "план · сделано " + str(done_count) + " из " + str(len(items))
        add([("☑ " if self.glyph_mode == "emoji" else "• ", "cyan"), (head, "bold")])
        marks = {"done": "✓", "active": "◐", "pending": "○"}
        show = items if entry.expanded else [it for it in items
                                             if it.get("status") == "active"][:1]
        if not entry.expanded and not show and items:
            show = items[:1]
        for item in show:
            mark = marks.get(item.get("status", "pending"), "○")
            style = "dim" if item.get("status") == "done" else "text"
            add([("  " + mark + " ", "cyan"), (item.get("text", ""), style)])
        if not entry.expanded and len(items) > len(show):
            add([("  · Ctrl+O — весь план, Ctrl+T — панель", "dim")])

    def _guards_for(self, tool_entry: Entry) -> list:
        """Подтверждения, относящиеся к этому тулу, в порядке прихода."""
        return [e for e in self.entries
                if e.kind == "guard" and e.guard_of
                and e.guard_of == tool_entry.entry_id]

    def _feed_lines(self, width: int, now: float):
        lines: list = []
        for entry in self.entries:
            if (entry.kind == "guard" and entry.guard_of
                    and self._find("tool", entry.guard_of) is not None):
                continue  # рисуется внутри своего тула, между вызовом и результатом
            lines.extend(self._entry_lines(entry, width, now))
            lines.append(([], None))
        if lines:
            lines.pop()
        return lines

    def _panel_content(self, panel_id: str):
        if panel_id not in self._panel_cache:
            content = create_panel(panel_id)
            self._panel_cache[panel_id] = content
        return self._panel_cache[panel_id]

    def _draw_panels(self, buf: Buffer, x: int, y: int, w: int, h: int, now: float) -> None:
        rects = self.layout.rects(x, y, w, h)
        for panel_id, (px, py, pw, ph) in rects.items():
            if pw <= 0 or ph <= 0:
                continue
            buf.fill_rect(px, py, pw, ph, " ", bg=PANEL_BG)
            content = self._panel_content(panel_id)
            title = content.title if content is not None else panel_id
            if self.panel_focus and self.layout.focused == panel_id:
                title = "▸ " + title
            buf.text(px + 1, py, title, fg=_color.AMBER, bg=PANEL_BG, attrs=_color.BOLD)
            if content is None:
                continue
            lines = content.lines(self, max(1, pw - 2))
            row = py + 1
            for line in lines:
                if row >= py + ph:
                    break
                for visual in wrap_text(line, max(1, pw - 2)):
                    if row >= py + ph:
                        break
                    buf.text(px + 1, row, visual, fg=_color.SILVER, bg=PANEL_BG)
                    row += 1

    def _status_line(self, buf: Buffer, y: int, cols: int, now: float) -> None:
        buf.fill_rect(0, y, cols, 1, " ", bg=STATUS_BG)
        phase = _moon.context_phase(self.context_pct, self.glyph_mode)
        pct = str(self.context_pct)
        left = " " + phase + " контекст " + pct + "% · " + self.mode + " · " + self.model
        buf.text(0, y, left, fg=_color.SILVER, bg=STATUS_BG)
        x = str_width(left)
        if self.busy:
            phrase, alpha = self.wheel.text_alpha(now)
            spin = "  " + self.spinner.glyph(now) + " " + phrase + "…"
            fg = _color.lerp(STATUS_BG, _color.AMBER, alpha)
            buf.text(x, y, spin, fg=fg, bg=STATUS_BG)
            x += str_width(spin)
        cost = format(self.cost, ".4f")
        right = "$" + cost
        if self.queue:
            right += " · очередь: " + str(len(self.queue))
        rx = max(x + 1, cols - str_width(right) - 1)
        if rx < cols:
            buf.text(rx, y, right, fg=DIM_FG, bg=STATUS_BG, attrs=_color.DIM)

    def draw(self, cols: int, rows: int, now: float):
        buf = Buffer(cols, rows)
        buf.clear(bg=_color.SPACE_BG)
        panels_on = bool(self.layout.leaves())
        panel_w = max(24, cols // 3) if panels_on and cols >= 48 else 0
        chat_w = cols - panel_w
        guard = self.pending_guard
        ask = self.pending_ask
        if guard is not None and not self.guard_editing:
            input_lines, cursor_pos = ["Enter — да · n — нет · e — править · "
                                       "a — дальше без вопросов · q — стоп"], (0, 0)
            input_h = 1
        elif ask is not None and not self.editor.text:
            input_lines, cursor_pos = ["↑↓ — выбор · Enter — ответить"], (0, 0)
            input_h = 1
        else:
            full_lines, full_cursor = self.editor.visual_lines(max(4, chat_w - 2))
            input_h = min(5, len(full_lines))
            offset = len(full_lines) - input_h
            input_lines = full_lines[offset:]
            cursor_pos = (max(0, min(input_h - 1, full_cursor[0] - offset)),
                          full_cursor[1])
        completion_h = min(5, len(self.completions)) if self.completions else 0
        feed_h = max(1, rows - 1 - input_h - completion_h)
        self._page = feed_h
        lines = self._feed_lines(chat_w, now)
        max_scroll = max(0, len(lines) - feed_h)
        self.scroll = max(0, min(self.scroll, max_scroll))
        end = len(lines) - self.scroll
        start = max(0, end - feed_h)
        for row, (spans, bg) in enumerate(lines[start:end]):
            if bg is not None:
                buf.fill_rect(0, row, chat_w, 1, " ", bg=bg)
            x = 0
            for text, fg, attrs in spans:
                x += buf.text(x, row, text, fg=fg, bg=bg or _color.SPACE_BG, attrs=attrs)
        if panel_w:
            self._draw_panels(buf, chat_w, 0, panel_w, feed_h + input_h + completion_h, now)
        cy = feed_h
        for i, candidate in enumerate(self.completions[:completion_h]):
            style_fg = _color.CYAN if i == self.completion_index else DIM_FG
            attrs = _color.BOLD if i == self.completion_index else _color.DIM
            buf.text(2, cy + i, "@" + candidate, fg=style_fg, bg=_color.SPACE_BG, attrs=attrs)
        iy = feed_h + completion_h
        for i, line in enumerate(input_lines):
            prefix = "› " if i == 0 else "  "
            fg = _color.AMBER if guard is not None else _color.CYAN
            attrs = _color.BOLD if guard is not None or i == 0 else 0
            buf.text(0, iy + i, prefix, fg=fg, bg=_color.SPACE_BG, attrs=_color.BOLD)
            buf.text(2, iy + i, line, fg=_color.SILVER, bg=_color.SPACE_BG, attrs=attrs)
        self._status_line(buf, rows - 1, cols, now)
        if guard is not None and not self.guard_editing:
            return buf, None
        if ask is not None and not self.editor.text:
            return buf, None
        cursor = (min(cols - 1, 2 + cursor_pos[1]), iy + cursor_pos[0])
        return buf, cursor


def run_chat(started_at: float = 0.0, *, splash_first: bool = False, fake: bool = True) -> int:
    """Полноэкранный чат: терминал, цикл ввода, источник событий."""
    from .splash import play_splash

    settings = Settings()
    bus = EventBus()
    term = Terminal(settings=settings)
    if not term.caps.is_tty:
        print("Лунапарк: нужен живой терминал. Справка: luna --help, проверка: luna doctor")
        return 0
    data = DataDir()
    data.ensure()
    with RunLock(data.root):
        with term:
            cols, rows = term.size()
            screen = Screen(term.write, term.caps.color_level, cols, rows)
            if splash_first and play_splash(term, screen, settings, bus, started_at) == "exit":
                return 0
            app = ChatApp(bus, settings, Scheduler(), term.caps.glyph_mode,
                          source=create_source(settings, fake=fake))
            app.start_source()
            while not app.done:
                events = term.read_events(timeout=app.next_timeout())
                size = term.size()
                if size != (screen.cols, screen.rows):
                    screen.resize(*size)
                for ev in events:
                    if isinstance(ev, Key):
                        app.handle_key(ev)
                    elif isinstance(ev, Mouse):
                        app.handle_mouse(ev)
                    elif isinstance(ev, Paste):
                        app.handle_paste(ev)
                    elif isinstance(ev, Resize):
                        screen.resize(ev.cols, ev.rows)
                app.poll_source()
                buf, cursor = app.draw(size[0], size[1], time.monotonic())
                screen.present(buf, cursor=cursor)
    print(phrases.event_phrase("end") or "Орёл сел.")
    return 0
