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
from .picker import PICKER_BG, SELECT_BG, Picker, manners_items, session_items
from .screen import Buffer, Screen
from .term import Terminal
from .width import char_width, str_width

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
/copy — скопировать последний ответ · /copy err — последнюю ошибку целиком
/sessions — выбиралка сессий на диске: Enter — переключиться на сессию
/new — начать новую сессию · /rename <имя> — имя текущей сессии
/manners — выбиралка манер речи: Enter — включить, n — новая, e — открыть в редакторе
Выделение мышью: протяжка по ленте копирует текст, двойной клик — слово
Не выделяется? В большинстве терминалов Shift+протяжка выделяет в обход приложения
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


def _wordy(ch: str) -> bool:
    """Символ «слова» для двойного клика: буквы, цифры и знаки путей/команд."""
    return ch.isalnum() or ch in "_-./:\\@#~"


def _cells_slice(text: str, a: int, b) -> str:
    """Срез строки по колонкам ячеек [a..b] включительно; b=None — до конца."""
    out = []
    pos = 0
    for ch in text:
        w = char_width(ch)
        if pos >= a and (b is None or pos <= b):
            out.append(ch)
        pos += w
    return "".join(out)


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
                 glyph_mode="emoji", source=None, cwd=None, trap=None,
                 copy_hook=None, journal=None, data=None):
        self.bus = bus or EventBus()
        self.settings = settings or Settings()
        self.scheduler = scheduler or Scheduler()
        self.glyph_mode = _moon.normalize_mode(glyph_mode)
        self.cwd = cwd or os.getcwd()
        self.trap = trap  # ErrorTrap полноэкранного сеанса: ошибки потоков — в ленту
        self.journal = journal  # SessionJournal: события и команды на диск (п. 5)
        self._data = data  # DataDir каталога данных (по умолчанию — настоящий)
        # Выбиралка поверх ленты (/sessions, /manners) и её дела (ЗАДАЧА-10):
        self.picker: Picker | None = None
        self._picker_kind = ""
        self.edit_request = None      # путь файла, который run_chat откроет в редакторе
        self._edit_manner_id = ""
        self._pending_switch = None   # путь сессии: переключиться по концу хода
        self._pending_new = False     # новая сессия по концу хода
        self._want_journal = False    # журнал создать при закрытии выбиралки (--resume)
        self._restoring = False
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
        # Копирование (п. 4): хук в буфер обмена ставит run_chat; выделение —
        # пара ((строка, колонка), (строка, колонка)) визуальных линий ленты.
        self.copy_hook = copy_hook
        self.selection: tuple | None = None
        self._sel_anchor: tuple | None = None
        self._last_press: tuple | None = None
        self._feed_texts: list = []
        self._feed_first = 0
        self._feed_geom = (0, 0)
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
        events = []
        if self.trap is not None:
            # Ошибки фоновых потоков приходят событиями error — как от источника.
            events.extend(self.trap.drain())
        poll = getattr(self.source, "poll", None)
        if callable(poll):
            events.extend(poll())
        for ev in events:
            self.on_agent_event(ev)
        return events

    def restore(self, records: list) -> None:
        """Воспроизвести журнал прошлой сессии в ленту (luna --continue).

        События идут обычным путём on_agent_event, команды применяют только
        свой видимый эффект: источнику ничего не отправляется, в журнал
        ничего не дописывается. Висячие подтверждения и вопросы прошлой
        сессии помечаются «прервано» — отвечать на них некому.
        """
        self._restoring = True
        try:
            for rec in records:
                data = rec.get("data") or {}
                if rec.get("kind") == "event":
                    self.on_agent_event(data)
                elif rec.get("kind") == "command":
                    self._restore_command(data)
        finally:
            self._restoring = False
        self.busy = False
        for entry in self.entries:
            entry.streaming = False
            if entry.kind in ("guard", "ask") and entry.status == "pending":
                entry.status = "прервано"

    def _restore_command(self, cmd: dict) -> None:
        kind = cmd.get("type")
        if kind == "user.message":
            self._add(Entry("user", text=str(cmd.get("text", ""))))
        elif kind == "shell":
            self._add(Entry("shell", text=str(cmd.get("command", ""))))
        elif kind == "guard.answer":
            entry = self._find("guard", str(cmd.get("id", "")))
            if entry is not None:
                entry.status = str(cmd.get("decision", ""))
        elif kind == "user.answer":
            entry = self._find("ask", str(cmd.get("id", "")))
            if entry is not None:
                entry.status = "answered"
                entry.answer = str(cmd.get("text", ""))
        # mode.set восстановится событием mode.changed из того же журнала

    def send_command(self, cmd: dict) -> None:
        if self.journal is not None:
            self.journal.append_command(cmd)
        self.sent_commands.append(dict(cmd))
        handler = getattr(self.source, "handle_command", None)
        if callable(handler):
            handler(cmd)
        self.bus.publish({"type": "agent.command", "command": dict(cmd)})
        self.poll_source()

    # --- сессии: память, переключение, новая (ЗАДАЧА-10) -------------------------

    def _data_dir(self) -> DataDir:
        if self._data is None:
            self._data = DataDir()
        return self._data

    def attach_history(self, records: list) -> None:
        """Отдать источнику историю прошлой сессии из журнала.

        Память модели, а не только лента: build_history собирает реплики,
        ответы, вызовы и результаты тулов; источник без load_history
        (старые подмены) просто пропускается.
        """
        from ..core.sessions import build_history

        loader = getattr(self.source, "load_history", None)
        if callable(loader):
            loader(build_history(records))

    def switch_session(self, path) -> bool:
        """Переключиться на сессию с диска: лента, память и журнал — её.

        Идущий ход перед этим прерывается обычной командой interrupt
        (текущая сессия уже на диске). Дальше пишем в файл выбранной.
        """
        if self.journal is not None and str(self.journal.path) == str(path):
            self._add(Entry("notice", text="Это текущая сессия."))
            return False
        if self.busy:
            # Флаг — до interrupt: turn.done у фальшивого источника приходит
            # синхронно внутри send_command и сразу выполняет переключение.
            self._pending_switch = str(path)
            self.send_command({"type": "interrupt"})
            if self._pending_switch is not None:
                self._add(Entry("notice", text="Прерываю ход — и переключаюсь на выбранную сессию."))
            return True
        return self._do_switch(path)

    def _do_switch(self, path) -> bool:
        from ..core.sessions import SessionJournal, load_records

        records = load_records(path)
        if not records:
            self._add(Entry("notice", text="Не выходит прочитать сессию: " + str(path)))
            return False
        old = self.journal
        journal, _records = SessionJournal.resume(self._data_dir(), path)
        self.journal = journal
        self._want_journal = False
        if old is not None:
            old.close()
        self.entries.clear()
        self.queue.clear()
        self.todos = []
        self.context_pct = 0
        self.cost = 0.0
        self.restore(records)
        self.attach_history(records)
        # Режим восстановленной сессии — и агенту (mode.changed из журнала
        # обновил только экран).
        self.send_command({"type": "mode.set", "mode": self.mode})
        title = ""
        started = 0.0
        for rec in records:
            if rec.get("kind") == "title":
                title = str(rec.get("title") or title)
            elif rec.get("kind") == "meta":
                started = float(rec.get("started") or 0)
        if title:
            label = "«" + title + "»"
        elif started:
            label = "от " + time.strftime("%d.%m %H:%M", time.localtime(started))
        else:
            label = ""
        self._add(Entry("notice", text="Сессия переключена " + label
                                       + " — модель помнит разговор."))
        return True

    def start_new_session(self) -> bool:
        """Новая сессия, не выходя из чата (/new)."""
        if not self.settings.get("chat.session_log"):
            self._add(Entry("notice", text="Журнал сессий выключен настройкой "
                                           "chat.session_log — новая сессия неотличима."))
            return False
        if self.busy:
            self._pending_new = True
            self.send_command({"type": "interrupt"})
            if self._pending_new:
                self._add(Entry("notice", text="Прерываю ход — и начинаю новую сессию."))
            return True
        return self._do_new_session()

    def _do_new_session(self) -> bool:
        from ..core.sessions import SessionJournal

        old = self.journal
        self.journal = SessionJournal.create(self._data_dir(), self.cwd)
        if old is not None:
            old.close()
        self.entries.clear()
        self.queue.clear()
        self.todos = []
        self.context_pct = 0
        self.cost = 0.0
        self.busy = False
        loader = getattr(self.source, "load_history", None)
        if callable(loader):
            loader([])  # память модели — с чистого листа
        self._add(Entry("notice", text="Новая сессия. Прошлая осталась на диске — "
                                       "/sessions вернёт к ней."))
        return True

    def rename_session(self, title: str) -> None:
        """/rename <имя> — имя текущей сессии, в журнал и в выбиралку."""
        title = title.strip()
        if not title:
            self._add(Entry("notice", text="Нужно имя: /rename <имя>"))
            return
        if self.journal is None:
            self._add(Entry("notice", text="Журнал выключен — имя сессии хранить негде."))
            return
        self.journal.set_title(title)
        self._add(Entry("notice", text="Сессия теперь называется «" + title + "»."))

    # --- выбиралка: сессии и манеры (ЗАДАЧА-10) --------------------------------

    def open_sessions_picker(self) -> None:
        """/sessions и luna --resume: выбиралка сессий поверх ленты."""
        from ..core.sessions import list_sessions

        scope = str(self.settings.get("chat.sessions_scope", "all") or "all")
        sessions = list_sessions(self._data_dir())
        if scope == "cwd":
            sessions = [s for s in sessions if s["cwd"] == self.cwd]
        current = str(self.journal.path) if self.journal is not None else None
        hint = ("Enter — переключиться · Esc — закрыть · каталог: "
                + str(self._data_dir().sessions))
        self.picker = Picker("Сессии", session_items(sessions, self.cwd, current),
                             hint=hint, preview_lines=0, now=self._clock())
        self._picker_kind = "sessions"
        self._last_change = self._clock()

    def open_manners_picker(self) -> None:
        """/manners: выбиралка манер с превью, новая и редактор — клавишами."""
        hint = ("Enter — включить · n — новая · N — копия выбранной · "
                "e — открыть в редакторе · Esc — закрыть")
        self.picker = Picker("Манеры речи", manners_items(self.settings, self.cwd),
                             hint=hint, extra={"n": "new", "N": "copy", "e": "edit"},
                             now=self._clock())
        self._picker_kind = "manners"
        self._last_change = self._clock()

    def _picker_key(self, key: Key) -> None:
        picker = self.picker
        if picker is None:
            return
        self._last_change = self._clock()
        action = picker.handle_key(key)
        if action is None:
            return
        kind = action[0]
        if kind == "close":
            self.picker = None
            if self.journal is None and self._want_journal:
                # --resume без выбора: обычная новая сессия с журналом.
                from ..core.sessions import SessionJournal

                self.journal = SessionJournal.create(self._data_dir(), self.cwd)
                self._want_journal = False
            return
        if self._picker_kind == "sessions":
            self._sessions_picker_action(action)
        elif self._picker_kind == "manners":
            self._manners_picker_action(action)

    def _sessions_picker_action(self, action) -> None:
        if action[0] == "confirmed":
            inner = action[1]
            if inner and inner[0] == "pick":
                self.picker = None
                self.switch_session(inner[1].payload["path"])
            return
        if action[0] != "pick" or action[1] is None:
            return
        item = action[1]
        if self.busy:
            self.picker.ask_confirm(
                "Идёт ход. Прервать его и переключиться на эту сессию?",
                ("pick", item))
            return
        self.picker = None
        self.switch_session(item.payload["path"])

    def _manners_picker_action(self, action) -> None:
        from ..core.errors import HumanError
        from ..mind import manners

        picker = self.picker
        try:
            if action[0] == "pick" and action[1] is not None:
                entry = manners.choose(self.settings, action[1].key, self.cwd)
                self.picker = None
                name = entry["name"] if entry else ""
                self._add(Entry("notice", text="Манера включена: «" + name
                                               + "» — со следующего хода."))
            elif action[0] == "action" and action[1] == "new":
                picker.ask_input("Новая манера, имя латиницей: ", ("new", None))
            elif action[0] == "action" and action[1] == "copy":
                item = action[2]
                if item is not None:
                    picker.ask_input("Копия манеры " + item.key + ", имя латиницей: ",
                                     ("copy", item.key))
            elif action[0] == "input":
                text, ctx = action[1], action[2]
                if text and ctx is not None:
                    entry = manners.create_from_template(
                        self.settings, text, ctx[1] or "")
                    picker.set_items(manners_items(self.settings, self.cwd))
                    self._add(Entry("notice", text="Манера «" + entry["name"]
                                                   + "» создана: " + str(entry["path"])
                                                   + " — e откроет её в редакторе."))
            elif action[0] == "action" and action[1] == "edit":
                item = action[2]
                if item is not None:
                    path = manners.editable_path(self.settings, item.key, self.cwd)
                    self._edit_manner_id = item.key
                    self.edit_request = path  # run_chat поставит терминал на паузу
        except HumanError as err:
            text = err.title
            if err.hint:
                text += " " + err.hint
            self._add(Entry("notice", text=text))

    def after_edit(self, path) -> None:
        """run_chat вернулся из внешнего редактора: подхватить правки."""
        if self._picker_kind == "manners" and self.picker is not None:
            self.picker.set_items(manners_items(self.settings, self.cwd))
        self._add(Entry("notice", text="Файл обновлён из редактора: " + str(path)
                                       + " — манера подхватится со следующего хода."))
        self._edit_manner_id = ""
        self._last_change = self._clock()

    def needs_frame(self, now: float) -> bool:
        """Кадры нужны, пока идёт ход, есть события или что-то затухает."""
        if self.busy:
            return True
        if self.trap is not None and self.trap.pending:
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
        if self.journal is not None and not self._restoring:
            self.journal.append_event(ev)
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
            if self._pending_switch is not None:
                path, self._pending_switch = self._pending_switch, None
                self._do_switch(path)
            elif self._pending_new:
                self._pending_new = False
                self._do_new_session()
            elif self._after_interrupt:
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
        elif cmd == "/copy":
            self._copy_command(parts[1:])
        elif cmd == "/sessions":
            self.open_sessions_picker()
        elif cmd == "/manners":
            self.open_manners_picker()
        elif cmd == "/new":
            self.start_new_session()
        elif cmd == "/rename":
            self.rename_session(text[len("/rename"):])
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

    def _copy_command(self, args: list) -> None:
        """/copy — последний ответ агента; /copy err — последняя ошибка целиком."""
        if args and args[0] == "err":
            entry = next((e for e in reversed(self.entries)
                          if e.kind == "error" and e.text), None)
            what, empty = "ошибку", "Ошибок в ленте пока нет."
        else:
            entry = next((e for e in reversed(self.entries)
                          if e.kind == "agent" and e.text), None)
            what, empty = "ответ", "Ответов агента в ленте пока нет."
        if entry is None:
            self._add(Entry("notice", text=empty))
            return
        if self.copy_hook is None:
            self._add(Entry("notice", text="Буфер обмена недоступен в этом режиме."))
            return
        self.copy_hook(entry.text)
        count = str(len(entry.text))
        self._add(Entry("notice", text="Скопировано в буфер обмена (" + what
                                       + "): " + count + " символов."))

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
        if key.name not in ("pageup", "pagedown"):
            self.selection = None  # любая работа с клавиатуры снимает выделение
        guard = self.pending_guard
        if guard is not None:
            self._guard_key(key, guard)
            return
        ask = self.pending_ask
        if ask is not None:
            self._ask_key(key, ask)
            return
        if self.picker is not None:
            self._picker_key(key)
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
        if self.picker is not None:
            return  # под выбиралкой лента мышью не выделяется
        if mouse.kind == "wheel_up":
            self.scroll = min(self.scroll + 3, 100000)
        elif mouse.kind == "wheel_down":
            self.scroll = max(0, self.scroll - 3)
        elif mouse.kind == "press" and mouse.button == 0:
            self._mouse_press(mouse)
        elif mouse.kind == "move" and self._sel_anchor is not None:
            pos = self._feed_pos(mouse)
            if pos is not None:
                self.selection = (self._sel_anchor, pos)
        elif mouse.kind == "release" and mouse.button == 0:
            anchor, self._sel_anchor = self._sel_anchor, None
            if anchor is None or self.selection is None:
                return
            (l0, c0), (l1, c1) = self._sel_range()
            if (l0, c0) == (l1, c1):
                return  # простой клик без протяжки — не выделение
            text = self.selection_text()
            if text.strip() and self.copy_hook is not None:
                self.copy_hook(text)

    # --- выделение мышью и копирование ------------------------------------------------

    def _feed_pos(self, mouse: Mouse):
        """Мышь → (индекс визуальной строки ленты, колонка ячеек) или None."""
        chat_w, feed_h = self._feed_geom
        x, y = mouse.x - 1, mouse.y - 1
        if not (0 <= y < feed_h and 0 <= x < chat_w):
            return None
        idx = self._feed_first + y
        if idx >= len(self._feed_texts):
            return None
        return idx, x

    def _mouse_press(self, mouse: Mouse) -> None:
        pos = self._feed_pos(mouse)
        if pos is None:
            self.selection = None
            self._sel_anchor = None
            return
        now = self._clock()
        last = self._last_press
        self._last_press = (now, mouse.x, mouse.y)
        if (last is not None and now - last[0] < 0.45
                and abs(mouse.x - last[1]) <= 1 and abs(mouse.y - last[2]) <= 1):
            self._select_word(pos)  # двойной клик — слово целиком, сразу в буфер
            self._sel_anchor = None
            text = self.selection_text()
            if text.strip() and self.copy_hook is not None:
                self.copy_hook(text)
            return
        self._sel_anchor = pos
        self.selection = (pos, pos)

    def _select_word(self, pos) -> None:
        idx, col = pos
        text = self._feed_texts[idx]
        starts = []
        p = 0
        for ch in text:
            starts.append(p)
            p += char_width(ch)
        ci = None
        for i, start in enumerate(starts):
            if start <= col < start + char_width(text[i]):
                ci = i
                break
        if ci is None or not _wordy(text[ci]):
            self.selection = (pos, pos)
            return
        a = ci
        while a > 0 and _wordy(text[a - 1]):
            a -= 1
        b = ci
        while b + 1 < len(text) and _wordy(text[b + 1]):
            b += 1
        end_col = starts[b] + char_width(text[b]) - 1
        self.selection = ((idx, starts[a]), (idx, end_col))

    def _sel_range(self):
        (l0, c0), (l1, c1) = self.selection
        if (l1, c1) < (l0, c0):
            return (l1, c1), (l0, c0)
        return (l0, c0), (l1, c1)

    def selection_text(self) -> str:
        """Текст выделения по визуальным строкам последнего кадра."""
        if self.selection is None:
            return ""
        (l0, c0), (l1, c1) = self._sel_range()
        out = []
        for idx in range(l0, l1 + 1):
            if idx >= len(self._feed_texts):
                break
            text = self._feed_texts[idx]
            a = c0 if idx == l0 else 0
            b = c1 if idx == l1 else None
            out.append(_cells_slice(text, a, b))
        return "\n".join(out)

    def _draw_selection(self, buf: Buffer, first: int, feed_h: int) -> None:
        """Подсветка выделения: REVERSE на ячейках видимой части диапазона."""
        if self.selection is None:
            return
        (l0, c0), (l1, c1) = self._sel_range()
        for idx in range(l0, l1 + 1):
            row = idx - first
            if not (0 <= row < feed_h) or idx >= len(self._feed_texts):
                continue
            text = self._feed_texts[idx]
            line_w = str_width(text)
            a = c0 if idx == l0 else 0
            b = c1 if idx == l1 else max(0, line_w - 1)
            for x in range(a, min(b, buf.cols - 1) + 1):
                ch, fg, bg, attrs = buf.cells[row][x]
                buf.cells[row][x] = (ch, fg, bg, attrs | _color.REVERSE)

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
            denied = entry.status in ("no", "stop", "прервано")
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

    _PICKER_STYLES = {"title": "bold", "text": "text", "dim": "dim",
                      "accent": "cyan", "mark": "amber", "hint": "dim"}

    def _draw_picker(self, buf: Buffer, width: int, height: int, now: float) -> None:
        """Выбиралка поверх ленты: мягкий блок без рамок, появление затуханием."""
        picker = self.picker
        if picker is None:
            return
        alpha = 1.0
        if self.settings.get("ui.animations"):
            alpha = ease_in_out((now - picker.born) / 0.25)
        lines, sel_row = picker.render_block(max(10, width - 2), max(3, height - 1))
        for row, spans in enumerate(lines[:height]):
            bg = SELECT_BG if row == sel_row else PICKER_BG
            buf.fill_rect(0, row, width, 1, " ", bg=bg)
            painted = self._paint([(text, self._PICKER_STYLES.get(role, "text"))
                                   for text, role in spans], alpha)
            x = 1
            for text, fg, attrs in painted:
                x += buf.text(x, row, text, fg=fg, bg=bg, attrs=attrs)

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
        # Геометрия и тексты ленты — для выделения мышью и копирования (п. 4):
        # строки хранятся все, first — индекс первой видимой на этом кадре.
        self._feed_texts = ["".join(t for t, _f, _a in spans)
                            for spans, _bg in lines]
        self._feed_first = start
        self._feed_geom = (chat_w, feed_h)
        for row, (spans, bg) in enumerate(lines[start:end]):
            if bg is not None:
                buf.fill_rect(0, row, chat_w, 1, " ", bg=bg)
            x = 0
            for text, fg, attrs in spans:
                x += buf.text(x, row, text, fg=fg, bg=bg or _color.SPACE_BG, attrs=attrs)
        self._draw_selection(buf, start, feed_h)
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
        if self.picker is not None:
            self._draw_picker(buf, chat_w, feed_h, now)
            return buf, None
        if guard is not None and not self.guard_editing:
            return buf, None
        if ask is not None and not self.editor.text:
            return buf, None
        cursor = (min(cols - 1, 2 + cursor_pos[1]), iy + cursor_pos[0])
        return buf, cursor


def _copy_to_clipboard(text: str, term: Terminal) -> str:
    """Хук копирования для ChatApp: OSC 52 в терминал (+ clip.exe на Windows)."""
    from .clipboard import copy_text

    return copy_text(text, write=term.write)


def _open_session(settings, data, continue_last: bool, fake: bool,
                  resume: str | None = None):
    """(журнал, записи, открыть_выбиралку, пометка) сеанса чата.

    Журнал ведётся, если включён настройкой chat.session_log; витрина
    `luna try chat` (фальшивый агент) историю не пишет. При --continue
    берём последнюю сессию этого каталога и дописываем в тот же файл;
    resume — сессия по id/префиксу (--resume <id>) или выбиралка до чата
    (resume == ""). Не нашлась — новая сессия и пометка об этом.
    """
    if fake and not continue_last and resume is None:
        return None, [], False, None
    if not settings.get("chat.session_log"):
        return None, [], False, None
    from ..core.sessions import SessionJournal, find_by_prefix, find_last

    cwd = os.getcwd()
    if resume:
        found = find_by_prefix(data, resume)
        if found is not None:
            journal, records = SessionJournal.resume(data, found["path"])
            return journal, records, False, None
        return (SessionJournal.create(data, cwd), [], False,
                "Сессии с id «" + resume + "» на диске нет — начинаю новую.")
    if resume == "":
        # Выбиралка до чата: журнала пока нет (пустой файл не плодим);
        # ChatApp создаст его, когда выбиралку закроют без выбора.
        return None, [], True, None
    if continue_last:
        path = find_last(data, cwd)
        if path is not None:
            journal, records = SessionJournal.resume(data, path)
            return journal, records, False, None
    return SessionJournal.create(data, cwd), [], False, None


def run_chat(started_at: float = 0.0, *, splash_first: bool = False, fake: bool = True,
             continue_last: bool = False, resume: str | None = None) -> int:
    """Полноэкранный чат: терминал, цикл ввода, источник событий."""
    from .errtrap import ErrorTrap
    from .splash import play_splash

    settings = Settings()
    bus = EventBus()
    term = Terminal(settings=settings)
    if not term.caps.is_tty:
        print("Лунапарк: нужен живой терминал. Справка: luna --help, проверка: luna doctor")
        return 0
    data = DataDir()
    data.ensure()
    # Пока экран наш, ошибки потоков и stderr уходят в лог, а не поверх
    # ленты (ЗАДАЧА-06, п. 2): лог — LUNA_HOME/logs/chat-<время>.log.
    trap = ErrorTrap(data.logs / ("chat-" + time.strftime("%Y%m%d-%H%M%S") + ".log"))
    journal, restored, picker_first, open_note = _open_session(
        settings, data, continue_last, fake, resume)
    with RunLock(data.root):
        with term:
            trap.install()
            app = None
            try:
                cols, rows = term.size()
                screen = Screen(term.write, term.caps.color_level, cols, rows)
                if splash_first and play_splash(term, screen, settings, bus, started_at) == "exit":
                    return 0
                app = ChatApp(bus, settings, Scheduler(), term.caps.glyph_mode,
                              source=create_source(settings, fake=fake), trap=trap,
                              copy_hook=lambda text: _copy_to_clipboard(text, term),
                              journal=journal, data=data)
                if restored:
                    app.restore(restored)
                    app.attach_history(restored)
                elif picker_first:
                    app._want_journal = True
                    app.open_sessions_picker()
                elif open_note:
                    app._add(Entry("notice", text=open_note))
                elif continue_last or resume:
                    if journal is None:
                        note = "Журнал сессий выключен настройкой chat.session_log."
                    else:
                        note = "Продолжить нечего: прошлых сессий в этом каталоге нет."
                    app._add(Entry("notice", text=note))
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
                    if app.edit_request is not None:
                        _run_external_editor(app, term, screen, trap)
                    buf, cursor = app.draw(size[0], size[1], time.monotonic())
                    screen.present(buf, cursor=cursor)
            finally:
                trap.uninstall()
                current = app.journal if app is not None else journal
                if current is not None:
                    current.close()
    print(phrases.event_phrase("end") or "Орёл сел.")
    return 0


def _run_external_editor(app: ChatApp, term: Terminal, screen: Screen,
                         trap) -> None:
    """Пауза чата ради внешнего редактора (манера по клавише e в выбиралке).

    Терминал и ловушка ошибок отдаются редактору как обычному миру;
    после — полноэкранный режим заново, кадр перерисовывается целиком,
    правки подхватывает app.after_edit.
    """
    from .extedit import run_editor

    path = app.edit_request
    app.edit_request = None
    error = None
    trap.uninstall()
    term.suspend()
    try:
        run_editor(path)
    except OSError as exc:
        error = "Не вышло открыть редактор: " + str(exc)
    finally:
        term.resume()
        trap.install()
        screen.resize(*term.size())  # _prev сброшен — кадр целиком
    app.after_edit(path)
    if error:
        app._add(Entry("error", text=error))
