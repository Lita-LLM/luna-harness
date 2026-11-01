"""Полноэкранный чат Луны (веха 1, часть Б): лента, ввод, панели.

Экран — клиент контракта docs/СОБЫТИЯ.md: всё, что он показывает, пришло
событиями источника; всё, что делает человек, уходит командами контракта.
Источник — ОДНО место подмены: create_source() ниже. Сейчас это фальшивый
агент (luna/devtools/fake_agent.py); когда ЗАДАЧА-04 ляжет в main, тимлид
подключит настоящий агент заменой тела этой одной функции.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..core.bus import EventBus
from ..core.paths import DataDir
from ..core.run_lock import RunLock
from ..core.settings import Settings
from . import color as _color
from . import layout as _layout
from . import moon as _moon
from . import phrases
from . import themes as _themes
from .anim import PhraseWheel, Scheduler, Spinner, ease_in_out, ease_out
from .editor import TextEditor
from .keys import Key, Mouse, Paste, Resize
from .markdown import Span, render_markdown, wrap_spans, wrap_text
from .panels import LayoutTree, PanelData, create_panel, find_panel, registered_panels
from .picker import PICKER_BG, SELECT_BG, Picker, PickerItem, manners_items, session_items
from .screen import Buffer, Screen
from .term import Terminal
from .width import char_width, str_width

MODES = ("normal", "plan", "review", "auto", "yolo")
_PANEL_KEYS = {"alt+m": "moon", "alt+l": "limits", "alt+b": "backside",
               "alt+f": "files", "alt+n": "nibbler",
               "alt+c": "context", "alt+g": "git", "alt+p": "processes",
               "alt+t": "tree"}
_PANEL_ANIM_SEC = 0.22   # мягкое появление/закрытие колонки панелей
_NIBBLER_SHOW_SEC = 9.0  # сколько живёт строка Ниблера над вводом
STATUS_BG = (0x16, 0x1B, 0x30)
PANEL_BG = (0x10, 0x13, 0x24)
GUARD_BG = (0x24, 0x1B, 0x10)
DIM_FG = (0x8B, 0x94, 0xAC)

# Стиль markdown → (ключ палитры, атрибуты): цвета берутся из текущей
# темы в момент отрисовки (ЗАДАЧА-18), а не зашиты при импорте.
_STYLE_MAP = {
    "text": ("silver", 0),
    "bold": ("silver", _color.BOLD),
    "italic": ("silver", _color.ITALIC),
    "code": ("cyan", 0),
    "heading": ("violet", _color.BOLD),
    "bullet": ("cyan", 0),
    "dim": ("muted", _color.DIM),
    "link": ("cyan", _color.UNDERLINE),
    "keyword": ("violet", 0),
    "string": ("amber", 0),
    "number": ("magenta", 0),
    "comment": ("muted", _color.ITALIC),
    "user": ("silver", 0),
    "amber": ("amber", _color.BOLD),
    "guard": ("amber", _color.BOLD),
    "cyan": ("cyan", _color.BOLD),
    "tool": ("cyan", _color.BOLD),
    "ok": ("ok", 0),
    "err": ("err", _color.BOLD),
    "notice": ("violet", _color.DIM),
}

# Реестр команд чата (ЗАДАЧА-16): один источник для диспетчера /-команд,
# выбиралки /help, Tab-дополнения после «/» и текстовой справки HELP_TEXT.
# Опись решений по эталонам — docs/research/КОМАНДЫ.md.
COMMANDS = [
    ("help", "выбиралка всех команд с поиском"),
    ("mode", "режим: /mode [normal|plan|review|auto|yolo], без аргумента — по кругу"),
    ("model", "модель по ролям — выбиралка, смена на лету"),
    ("cost", "расход сессии: токены и стоимость"),
    ("usage", "расход и лимиты провайдера — что отдали события"),
    ("context", "панель «Контекст»: состав контекста по долям"),
    ("status", "состояние сессии одной справкой"),
    ("doctor", "диагностика установки и настроек — в ленту"),
    ("init", "агент создаёт или обновляет AGENTS.md проекта"),
    ("memory", "открыть AGENTS.md проекта во внешнем редакторе"),
    ("review", "агент ревьюит текущий git-дифф: /review [фокус]"),
    ("export", "сохранить сессию в markdown-файл"),
    ("copy", "скопировать последний ответ; /copy err — последнюю ошибку"),
    ("sessions", "выбиралка сессий на диске"),
    ("new", "начать новую сессию"),
    ("rename", "имя текущей сессии: /rename <имя>"),
    ("manners", "выбиралка манер речи"),
    ("undo", "откатить последний ход"),
    ("rewind", "откат к ходу: /rewind [N] [files|talk|both]; без аргументов — выбиралка ходов"),
    ("redo", "вернуть откат"),
    ("compact", "сжать контекст: /compact [что сохранить]"),
    ("clear", "очистить ленту"),
    ("panels", "выбиралка панелей-тайлов"),
    ("panel", "панель по имени: /panel <имя>"),
    ("layout", "раскладки панелей: /layout save|load|list <имя>"),
    ("todo", "панель «План»"),
    ("mcp", "MCP-серверы: выбиралка статуса и включения"),
    ("agents", "экипаж: главный агент, спутники и ассистенты"),
    ("cells", "профили ячеек роя: выбиралка, Enter — карточка, e — правка в редакторе"),
    ("theme", "тема оформления: палитры luna, amoled, contrast, mono, light и свои из LUNA_HOME/themes"),
    ("editor", "сочинить сообщение во внешнем редакторе"),
    ("details", "детали ленты вкл/выкл: вывод тулов свёрнуто или скрыто"),
    ("thinking", "показ рассуждений в ленте вкл/выкл"),
    ("btw", "вопрос в сторону, не сбивая ход: /btw <вопрос>"),
    ("bug", "отчёт об ошибке файлом: версия, окружение, ошибки (без секретов)"),
    ("diff", "панель «Git»: ветка, изменения, дифф"),
    ("add-dir", "добавить каталог для @-подсказок: /add-dir <путь>"),
    ("exit", "выход (алиас /quit)"),
]
COMMAND_DESCR = dict(COMMANDS)
# Команды, которым нужен аргумент: из /help они не выполняются, а встают
# в поле ввода заготовкой «/команда ».
_NEEDS_ARG = {"rename", "panel", "layout", "add-dir", "btw", "copy",
              "mode", "compact", "review"}
# Русские имена команд (ЗАДАЧА-17): /панель = /panel, /сессии = /sessions.
# Один список рядом с реестром: русское имя → каноническое латинское.
COMMAND_RU = {
    "помощь": "help", "справка": "help",
    "режим": "mode",
    "модель": "model", "модели": "model",
    "стоимость": "cost", "расход": "usage",
    "контекст": "context",
    "статус": "status",
    "доктор": "doctor", "диагностика": "doctor",
    "инит": "init",
    "память": "memory",
    "ревью": "review",
    "экспорт": "export",
    "копировать": "copy",
    "сессии": "sessions",
    "новая": "new",
    "переименовать": "rename",
    "манеры": "manners",
    "откатить": "undo",
    "откат": "rewind",
    "вернуть": "redo",
    "сжать": "compact",
    "очистить": "clear",
    "панели": "panels",
    "панель": "panel",
    "раскладка": "layout",
    "план": "todo",
    "серверы": "mcp",
    "агенты": "agents",
    "тема": "theme",
    "редактор": "editor",
    "детали": "details",
    "рассуждения": "thinking",
    "мимо": "btw",
    "ошибка": "bug",
    "дифф": "diff",
    "каталог": "add-dir",
    "выход": "exit",
}
_RU_BY_LATIN: dict = {}
for _ru, _lat in COMMAND_RU.items():
    _RU_BY_LATIN.setdefault(_lat, _ru)


def resolve_command(word: str):
    """Имя команды как набрали → каноническое латинское или None.

    Понимает латинские имена, русские имена и любое из них, набранное
    не в той раскладке (зфтуд → panel, gfytkm → панель → panel).
    """
    w = (word or "").strip().lower()
    if not w:
        return None
    if w in COMMAND_DESCR:
        return w
    if w in COMMAND_RU:
        return COMMAND_RU[w]
    for cand in _layout.variants(w):
        c = cand.lower()
        if c in COMMAND_DESCR:
            return c
        if c in COMMAND_RU:
            return COMMAND_RU[c]
    return None
# Темы оформления (/theme, настройка ui.theme) — палитры данными:
# встроенные и свои из LUNA_HOME/themes, см. luna/ui/themes.py (ЗАДАЧА-18).

HELP_TEXT = """Команды и клавиши Луны
""" + "\n".join("/" + name + " — " + desc for name, desc in COMMANDS) + """
Выбиралки: печатать — поиск · ↑↓ — выбор · Enter — действие · Esc — закрыть
/panels — все тайлы выбиралкой · Alt+M/L/B/F/N — Луна, Лимиты, Обратная сторона, Файлы, Ниблер · Alt+C/G/P/T — Контекст, Git, Процессы, Дерево
Команды не зависят от раскладки (ui.layout_fix): /панель = /panel, а .зфтуд в ЙЦУКЕН — тоже /panel; аргументы и Ctrl/Alt-клавиши понимаются в любой раскладке
Ctrl+R — поиск по истории ввода · Ctrl+L — перерисовать экран · Ctrl+V — вставить (картинка — по пути к файлу из буфера)
Esc Esc на пустом вводе — откат к ходу выбиралкой · Esc во время хода — прервать ход одним нажатием · Ctrl+C во время хода — прервать; без хода — очистить ввод, второй подряд за секунду — выход
Отправка во время хода (chat.send_mode): inject — вставка в ход · queue — очередь · interrupt — прервать и отправить · Ctrl+S — сменить режим · Alt+Enter — разово прервать и отправить · ↑ на пустом поле — вернуть сообщение из очереди
!команда — выполнить мимо модели · @файл — вложить файл (Tab — дополнить; Tab после / — дополнить команду)
Enter — отправить · Alt+Enter — новая строка · стрелки — история и курсор · Tab / Shift+Tab — режим по кругу
Ctrl+O — раскрыть/свернуть детали записи · F6 — в режим панелей (панели открыты) или открыть «План»
В режиме панелей: ↑↓ — фокус, ←→ — размер, Tab / Shift+Tab — переставить, Enter — развернуть, x — закрыть, Esc — назад к вводу; мышь: клик по заголовку — фокус, тянуть разделитель — размер, колесо — прокрутка
PgUp / PgDn и колесо мыши — прокрутка ленты · выделение мышью: протяжка копирует текст, двойной клик — слово
Не выделяется? В большинстве терминалов Shift+протяжка выделяет в обход приложения
Ниблер — компаньон над полем ввода: изредка комментирует работу агента; серьёзное уходит агенту пометкой (настройки nibbler.*)"""


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


def _clip_line(spans, width: int):
    """Спаны (текст, стиль) → одна строка не шире width ячеек.

    Хвост обрезается по границе символа с пометкой «…»: заголовки тулов,
    вопросы и пункты плана не должны уезжать под панели (ЗАДАЧА-18)."""
    if sum(str_width(t) for t, _s in spans) <= width:
        return spans
    out = []
    room = max(1, width) - 1
    for text, style in spans:
        if room <= 0:
            break
        if str_width(text) <= room:
            out.append((text, style))
            room -= str_width(text)
            continue
        cut = ""
        for ch in text:
            cw = char_width(ch)
            if cw > room:
                break
            cut += ch
            room -= cw
        if cut:
            out.append((cut, style))
        break
    out.append(("…", "dim"))
    return out


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
    kind: str  # user | agent | thinking | tool | guard | ask | todo | notice | error | help | shell | btw
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

    @property
    def journal(self):
        return self._journal

    @journal.setter
    def journal(self, value) -> None:
        # агенту журнал нужен для своих записей (откат, сжатие)
        self._journal = value
        setter = getattr(getattr(self, "source", None), "set_journal", None)
        if callable(setter):
            setter(value)

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
        self.journal = self._journal  # теперь источник есть — отдать журнал и ему
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
        # ЗАДАЧА-17: геометрия панелей последнего кадра (для мыши) и
        # перетаскивание разделителя; отметки в выбиралке сессий.
        self._panel_rects: dict = {}
        self._panel_drag = None  # (panel_id, последний y мыши)
        self._sessions_marks: set = set()
        # Панели с наполнением и Ниблер (ЗАДАЧА-14): PanelData собирает
        # состояние из событий, Ниблер наблюдает тот же поток.
        self.panel_data = PanelData()
        self._panel_anim: tuple | None = None  # (ширина-от, ширина-до, старт)
        self._panel_anchor: tuple | None = None  # (ширина, момент) — старт анимации
        self._panel_shown_w = 0.0              # ширина колонки на прошлом кадре
        self._panel_ghost = None               # дерево закрывающейся колонки
        self._nibbler_line: tuple | None = None  # (текст, серьёзность)
        self._nibbler_until = 0.0
        self.nibbler = self._make_nibbler()
        self.guard_editing = False
        self.completions: list = []
        self.completion_index = 0
        # ЗАДАЧА-16: дополнение команд после «/», доп. каталоги @-подсказок,
        # vim-режим ввода, запрос полной перерисовки (Ctrl+L), вид правки.
        self.cmd_completions: list = []       # [(команда, описание)]
        self.extra_dirs: list = []
        self._vim_normal = False
        self._vim_pending = ""
        self._redraw = False
        self._edit_kind = ""                  # "" (манеры) | memory | compose
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
        # ЗАДАЧА-18: прерывание, режимы отправки, граница колонки панелей.
        self._ctrl_c_at = -10.0          # первый Ctrl+C на пустом вводе (момент)
        self._hint: tuple | None = None  # (текст, до какого момента) — строка над вводом
        self._applied_theme = ""         # имя применённой палитры темы
        self._col_drag = False           # мышь тянет границу лента↔панели
        self._geom_cols = 0              # ширина экрана последнего кадра
        self._started = False
        self._page = 10
        self.spinner = Spinner(self.glyph_mode)
        self.wheel = PhraseWheel(phrases.spinner_phrases())
        self._applied_theme = _themes.apply_theme(
            str(self.settings.get("ui.theme") or "luna"))

    def _make_nibbler(self):
        """Ниблер для этого экрана; фальшивый источник — только эвристики,
        настоящий — ещё и мозг роли fast. Не вышло — чат живёт без него."""
        try:
            if not self.settings.get("nibbler.enabled"):
                return None
            from ..mind.nibbler import Nibbler, make_router_brain

            brain = None
            try:
                from ..mind.source import AgentSource

                # Мозг — только при настоящем агенте: у витрин и тестовых
                # подмен он звал настоящую модель человека на фальшивых событиях.
                if isinstance(self.source, AgentSource):
                    brain = make_router_brain(self.settings)
            except Exception:  # noqa: BLE001 — мозг не обязателен
                brain = None
            return Nibbler(self.settings, emit=self._nibbler_say, brain=brain,
                           clock=time.monotonic)
        except Exception:  # noqa: BLE001 — компаньон не роняет экран
            return None

    def _nibbler_say(self, ev: dict) -> None:
        self.on_agent_event(ev)

    def _observe_panels(self, ev: dict, kind) -> None:
        """Точка подключения ЗАДАЧИ-14: каждое событие видят данные панелей
        и Ниблер; его реплики (nibbler.say) ставят строку над вводом."""
        self.panel_data.observe(ev)
        if kind == "nibbler.say":
            self._nibbler_line = (str(ev.get("text", "")),
                                   str(ev.get("severity", "minor")))
            self._nibbler_until = self._clock() + _NIBBLER_SHOW_SEC
            return
        if self.nibbler is not None and not self._restoring:
            self.nibbler.observe(ev)

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
        # Незаконченный Звездопад прошлой сессии (ЗАДАЧА-15): предложить
        # докрутить — результаты лежат на диске, рой продолжится с места.
        try:
            from ..starfall.engine import unfinished_from_records

            unfinished = unfinished_from_records(records)
        except Exception:  # noqa: BLE001 — пометка не роняет восстановление
            unfinished = None
        if unfinished:
            self._add(Entry("notice", text="Поток роя " +
                            str(unfinished.get("swarm")) + " не закончен " +
                            "(результаты — " + str(unfinished.get("dir")) + "). " +
                            "Докрутить: попроси агента «докрути поток» " +
                            "(starfall, action=resume)."))

    def _cut_feed_after_turn(self, turn: int) -> None:
        """Лента после отката разговора: ходы после turn убираются — и вживую,
        и при --continue (событие session.rewound лежит в журнале)."""
        seen = 0
        for idx, entry in enumerate(self.entries):
            if entry.kind == "user":
                seen += 1
                if seen > turn:
                    self.entries = self.entries[:idx]
                    return

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
        from ..core.sessions import build_state

        loader = getattr(self.source, "load_history", None)
        if callable(loader):
            state = build_state(records)
            try:
                loader(state["history"], turn=state["turn"], marks=state["marks"])
            except TypeError:  # подмена со старой сигнатурой — только история
                loader(state["history"])

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

    def _sessions_scope(self) -> list:
        """Сессии для выбиралки с учётом настройки chat.sessions_scope."""
        from ..core.sessions import list_sessions

        scope = str(self.settings.get("chat.sessions_scope", "all") or "all")
        sessions = list_sessions(self._data_dir())
        if scope == "cwd":
            sessions = [s for s in sessions if s["cwd"] == self.cwd]
        return sessions

    def _session_items(self) -> list:
        """Пункты выбиралки сессий: текущая — «→», отмеченные — «✓»."""
        current = str(self.journal.path) if self.journal is not None else None
        items = session_items(self._sessions_scope(), self.cwd, current)
        for item in items:
            if item.key in self._sessions_marks:
                item.mark = "✓"
        return items

    def open_sessions_picker(self) -> None:
        """/sessions и luna --resume: выбиралка сессий поверх ленты.

        Уборка (ЗАДАЧА-17): r — переименовать, d/Del — в корзину,
        пробел — отметить несколько, D — отмеченные в корзину одним
        подтверждением, u — вернуть последнее удаление из корзины.
        """
        self._sessions_marks = set()
        hint = ("Enter — переключиться · r — переименовать · d — удалить · "
                "пробел — отметить · D — отмеченные · u — вернуть из корзины · "
                "Esc — закрыть")
        self.picker = Picker("Сессии", self._session_items(),
                             hint=hint, preview_lines=0, now=self._clock(),
                             extra={"r": "rename", "d": "delete",
                                    "D": "delete_marked", "u": "undo"})
        self._picker_kind = "sessions"
        self._last_change = self._clock()

    def _refresh_sessions_picker(self) -> None:
        if self.picker is not None and self._picker_kind == "sessions":
            self.picker.set_items(self._session_items())

    def open_manners_picker(self) -> None:
        """/manners: выбиралка манер с превью, новая и редактор — клавишами."""
        hint = ("Enter — включить · n — новая · N — копия выбранной · "
                "e — открыть в редакторе · Esc — закрыть")
        self.picker = Picker("Манеры речи", manners_items(self.settings, self.cwd),
                             hint=hint, extra={"n": "new", "N": "copy", "e": "edit"},
                             now=self._clock())
        self._picker_kind = "manners"
        self._last_change = self._clock()

    # --- выбиралки ЗАДАЧИ-16: панели, справка, модели, MCP, прочее -------------

    def _open_picker(self, kind: str, title: str, items: list, *,
                     hint: str = "", extra: dict | None = None,
                     preview_lines: int = 5) -> None:
        self.picker = Picker(title, items, hint=hint, extra=extra,
                             preview_lines=preview_lines, now=self._clock())
        self._picker_kind = kind
        self._last_change = self._clock()

    def open_panels_picker(self) -> None:
        """/panels: все тайлы выбиралкой (бывшее меню цифрами — заменено)."""
        items = []
        for spec in registered_panels().values():
            opened = spec.panel_id in self.layout
            items.append(PickerItem(
                key=spec.panel_id, title=spec.title,
                detail=spec.panel_id + " · " + ("открыта" if opened else "закрыта"),
                mark="→" if opened else ""))
        self._open_picker("panels", "Панели", items,
                          hint="Enter — открыть/закрыть · Esc — закрыть",
                          preview_lines=0)

    def open_help_picker(self) -> None:
        """/help: команды выбиралкой с поиском, не простынёй текста.

        Русские имена — отдельными пунктами с тем же ключом команды:
        поиск находит оба языка, Enter выполняет каноническую команду.
        В режим панелей — F6 (панели открыты) или Alt+буква панели.
        """
        items = [PickerItem(key=name, title="/" + name, detail=desc)
                 for name, desc in COMMANDS]
        items += [PickerItem(key=lat, title="/" + ru,
                             detail=COMMAND_DESCR[lat] + " — то же, что /" + lat)
                  for ru, lat in COMMAND_RU.items()]
        self._open_picker("help", "Команды Луны", items,
                          hint="печатайте — поиск · Enter — выполнить "
                               "или вставить заготовку · Esc — закрыть · "
                               "панели: F6 или Alt+M",
                          preview_lines=0)

    def open_models_picker(self) -> None:
        """/model: роли выбиралкой; Enter — задать ссылку роли на лету."""
        from .panels.models import ROLES

        items = []
        for role, title in ROLES:
            ref = str(self.settings.get("model." + role) or "")
            detail = ref if ref else ("не задана" if role == "main" else "как главная")
            items.append(PickerItem(key=role, title=role + " — " + title,
                                    detail=detail))
        self._open_picker("models", "Модель по ролям", items,
                          hint="Enter — задать модель роли · Esc — закрыть",
                          preview_lines=0)

    def _mcp_items(self) -> list:
        from .panels.mcp import load_servers

        try:
            servers, problems = load_servers(self)
        except Exception as exc:  # noqa: BLE001 — битый конфиг не роняет экран
            return [PickerItem(key="", title="Не вышло прочитать конфиги",
                               detail=str(exc))]
        items = []
        for problem in problems:
            items.append(PickerItem(key="", title="! " + problem))
        for name in sorted(servers):
            cfg = servers[name]
            detail = cfg.transport + " · " + (cfg.source or "?") + " · " \
                + ("включён" if cfg.enabled else "выключен")
            items.append(PickerItem(key=name, title=name, detail=detail,
                                    mark="→" if cfg.enabled else "",
                                    payload=cfg))
        if not items:
            items.append(PickerItem(key="", title="MCP-серверы не настроены",
                                    detail="luna mcp add — добавить"))
        return items

    def open_mcp_picker(self) -> None:
        """/mcp: серверы выбиралкой: Enter — панель, t — вкл/выкл в конфиге."""
        self._open_picker("mcp", "MCP-серверы", self._mcp_items(),
                          hint="Enter — панель MCP · t — включить/выключить "
                               "(лунные конфиги) · Esc — закрыть",
                          extra={"t": "toggle"}, preview_lines=0)

    def open_agents_picker(self) -> None:
        """/agents: главный агент и экипаж (ЗАДАЧА-11) — Enter открывает «Экипаж»."""
        from .panels import panel_data

        items = [PickerItem(key="main", title="Главный агент — эта сессия",
                            detail="модель: " + self.model + " · режим: " + self.mode,
                            mark="→")]
        crew = panel_data(self).crew
        for agent_id in crew.order:
            row = crew.agents[agent_id]
            kind = {"assistant": "ассистент", "satellite": "спутник"}.get(row["kind"], row["kind"])
            title = row["name"] + (" — лидер" if row["leader"] else "")
            detail = " · ".join(x for x in (kind, row["role"], row["model"], row["state"],
                                            "$" + format(row["cost"], ".4f")) if x)
            items.append(PickerItem(key=agent_id, title=title, detail=detail))
        if len(items) == 1:
            items.append(PickerItem(key="crew", title="Экипажа пока нет",
                                    detail="попроси агента собрать команду или позвать ассистента"))
        self._open_picker("agents", "Агенты", items,
                          hint="Enter — панель «Экипаж» · Esc — закрыть", preview_lines=0)

    def _cells_items(self) -> list:
        """Профили ячеек роя для /cells (встроенные и свои; сессионные — у агента)."""
        from ..starfall import profiles as cell_profiles

        items = []
        for prof in cell_profiles.library(self.settings):
            detail = ("модель " + prof.model + " · тулы: " +
                      (", ".join(prof.tools) if prof.tools else "нет") +
                      " · запись " + prof.write_mode)
            preview = (prof.description + "\n\n" + prof.prompt).strip()
            items.append(PickerItem(key=prof.caste, title=prof.caste + " — " + prof.name,
                                    detail=detail, preview=preview, payload=prof))
        if not items:
            items.append(PickerItem(key="", title="Профилей ячеек нет"))
        return items

    def open_cells_picker(self) -> None:
        """/cells: профили ячеек роя выбиралкой; e — правка в редакторе."""
        self._open_picker("cells", "Профили ячеек роя", self._cells_items(),
                          hint="Enter — карточка профиля · e — открыть в "
                               "редакторе (своя копия) · Esc — закрыть",
                          extra={"e": "edit"}, preview_lines=5)

    def open_theme_picker(self) -> None:
        """/theme: тема оформления выбиралкой (настройка ui.theme)."""
        current = str(self.settings.get("ui.theme") or "luna")
        items = [PickerItem(key=key, title=title,
                            detail="своя тема" if own else "встроенная",
                            mark="→" if key == current else "")
                 for key, title, own in _themes.list_themes()]
        self._open_picker("theme", "Тема оформления", items,
                          hint="Enter — включить · Esc — закрыть",
                          preview_lines=0)

    def open_history_picker(self) -> None:
        """Ctrl+R: история ввода выбиралкой с поиском (фильтр печатанием)."""
        items = []
        for i, text in enumerate(reversed(self.editor.history)):
            items.append(PickerItem(key=str(i), title=text.split("\n")[0][:72],
                                    preview=text, payload=text))
        if not items:
            items.append(PickerItem(key="", title="История ввода пуста"))
        self._open_picker("history", "История ввода", items,
                          hint="печатайте — поиск · Enter — в поле ввода · "
                               "Esc — закрыть", preview_lines=3)

    def open_rewind_picker(self) -> None:
        """/rewind без аргументов и Esc Esc: ходы сессии выбиралкой."""
        turns = [e for e in self.entries if e.kind == "user"]
        if not turns:
            self._add(Entry("notice", text="Откатывать нечего: ходов ещё не было."))
            return
        items = []
        for n, entry in enumerate(turns, 1):
            items.append(PickerItem(
                key=str(n), title=entry.text.split("\n")[0][:64],
                detail="ход " + str(n) + " из " + str(len(turns))))
        items.reverse()
        self._open_picker("rewind", "Откат к ходу", items,
                          hint="Enter — откатить разговор и файлы к этому ходу "
                               "· Esc — закрыть", preview_lines=0)

    def open_adddir_picker(self) -> None:
        """/add-dir без пути: спросить путь строкой в выбиралке."""
        self._open_picker("adddir", "Добавить каталог", [],
                          hint="", preview_lines=0)
        if self.picker is not None:
            self.picker.ask_input("Каталог для @-подсказок: ", ("adddir", None))

    def _extra_picker_action(self, action) -> None:
        """Действия выбиралок ЗАДАЧИ-16 по виду выбиралки."""
        kind = self._picker_kind
        if action[0] == "pick" and action[1] is not None:
            item = action[1]
            if kind == "panels":
                self.picker = None
                self.toggle_panel(item.key)
            elif kind == "help":
                self.picker = None
                if item.key in _NEEDS_ARG:
                    self.editor.set_text("/" + item.key + " ")
                else:
                    self._slash("/" + item.key)
            elif kind == "models":
                current = str(self.settings.get("model." + item.key) or "")
                self.picker.ask_input(
                    "Модель роли " + item.key + " («провайдер:модель» или "
                    "модель; пусто — как главная): ", ("role", item.key),
                    initial=current)
            elif kind == "mcp":
                self.picker = None
                if item.key:
                    self.toggle_panel("mcp")
            elif kind == "agents":
                self.picker = None
                if item.key != "main" and "crew" not in self.layout:
                    self.toggle_panel("crew")
            elif kind == "cells":
                self.picker = None
                if item.key:
                    prof = item.payload
                    text = item.title + "\n" + item.detail
                    if prof is not None and getattr(prof, "prompt", ""):
                        text += "\n\n" + prof.prompt
                    self._add(Entry("notice", text=text))
            elif kind == "theme":
                self.picker = None
                self.settings.set("ui.theme", item.key)
                self._applied_theme = _themes.apply_theme(item.key)
                self._add(Entry("notice", text="Тема оформления: " + item.key))
            elif kind == "history":
                self.picker = None
                if item.payload:
                    self.editor.set_text(str(item.payload))
            elif kind == "rewind":
                self.picker = None
                self.send_command({"type": "rewind", "turn": int(item.key),
                                   "scope": "both"})
            return
        if action[0] == "action" and kind == "mcp" and action[1] == "toggle":
            item = action[2]
            if item is not None and item.key:
                self._mcp_toggle(item)
            return
        if action[0] == "action" and kind == "cells" and action[1] == "edit":
            item = action[2]
            if item is not None and item.key:
                from ..starfall import profiles as cell_profiles

                try:
                    path = cell_profiles.editable_path(item.key, self.settings)
                except ValueError as exc:
                    self._add(Entry("notice", text=str(exc)))
                    return
                self._edit_kind = "cells"
                self.edit_request = str(path)  # run_chat поставит терминал на паузу
            return
        if action[0] == "input":
            text, ctx = action[1], action[2]
            if kind == "models" and ctx is not None and ctx[0] == "role":
                role = ctx[1]
                self.settings.set("model." + role, text)
                self.picker = None
                label = text if text else "(как главная)"
                self._add(Entry("notice", text="Модель роли " + role + ": "
                                               + label + " — со следующего хода."))
            elif kind == "adddir":
                self.picker = None
                if text:
                    self._add_dir(text)

    def _mcp_toggle(self, item) -> None:
        """Вкл/выкл MCP-сервера правкой его лунного конфига (enabled).

        Чужие форматы не трогаем молча: opencode.json может быть JSONC с
        комментариями — перезапись стёрла бы их; там честно отказываем.
        После правки — request_mcp_reload(): реестр сбрасывает серверы, и
        при следующем обращении тулов они перечитают конфиги и
        перезапустятся на лету (ЗАДАЧА-18).
        """
        cfg = item.payload
        label = getattr(cfg, "source", "") or ""
        targets = {
            "LUNA_HOME/mcp.json": (self._data_dir().root / "mcp.json", "mcpServers"),
            ".mcp.json": (Path(self.cwd) / ".mcp.json", "mcpServers"),
            ".luna/mcp.json": (Path(self.cwd) / ".luna" / "mcp.json", "mcpServers"),
        }
        if label not in targets:
            self._add(Entry("notice", text="Сервер из " + (label or "чужого конфига")
                                           + " отсюда не переключаю: поправь enabled вручную."))
            return
        path, section = targets[label]
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            entry = data[section][item.key]
            entry["enabled"] = not bool(cfg.enabled)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self._add(Entry("error", text="Не вышло переключить " + item.key
                                          + ": " + str(exc)))
            return
        from ..tools.registry import request_mcp_reload

        request_mcp_reload()
        state = "выключен" if cfg.enabled else "включён"
        self._add(Entry("notice", text="MCP-сервер " + item.key + " " + state
                                       + " в " + label + " — сервер "
                                       "перезапустится при следующем вызове тулов."))
        if self.picker is not None:
            self.picker.set_items(self._mcp_items())

    def _add_dir(self, text: str) -> None:
        """/add-dir: каталог для @-подсказок этой сессии."""
        raw = text.strip()
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = Path(self.cwd) / path
        if not path.is_dir():
            self._add(Entry("notice", text="Нет такого каталога: " + raw))
            return
        value = str(path)
        if value not in self.extra_dirs:
            self.extra_dirs.append(value)
        self._add(Entry("notice", text="Каталог для @-подсказок: " + value
                                       + " (доступ тулов к нему — за ядром, "
                                       "со следующей сессии агента)"))

    def _picker_key(self, key: Key) -> None:
        picker = self.picker
        if picker is None:
            return
        self._last_change = self._clock()
        if self._picker_kind == "sessions" and picker.mode == "list" \
                and not picker.filter:
            # Пробел и Del выбиралка сама не знает — это уборка сессий.
            if key.name in ("space", " ") or key.char == " ":
                self._sessions_picker_action(
                    ("action", "toggle_mark", picker.current()))
                return
            if key.name == "delete":
                self._sessions_picker_action(
                    ("action", "delete", picker.current()))
                return
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
        elif self._picker_kind in ("panels", "help", "models", "mcp", "agents",
                                   "theme", "history", "rewind", "adddir", "cells"):
            self._extra_picker_action(action)

    def _is_current_session(self, item) -> bool:
        return self.journal is not None and item is not None \
            and str(item.payload.get("path")) == str(self.journal.path)

    def _sessions_picker_action(self, action) -> None:
        if action[0] == "confirmed":
            inner = action[1]
            if inner and inner[0] == "pick":
                self.picker = None
                self.switch_session(inner[1].payload["path"])
            elif inner and inner[0] == "trash":
                self._trash_session_items(inner[1])
            return
        if action[0] == "input":
            text, ctx = action[1], action[2]
            if ctx is not None and ctx[0] == "rename" and text:
                self._rename_session_item(ctx[1], text)
            return
        if action[0] == "action":
            self._sessions_housekeeping(action[1], action[2])
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

    def _sessions_housekeeping(self, name: str, item) -> None:
        """Клавиши уборки в выбиралке сессий: r, d, D, u, пробел."""
        picker = self.picker
        if picker is None:
            return
        if name == "undo":
            from ..core.sessions import undo_trash

            restored = undo_trash(self._data_dir())
            self._refresh_sessions_picker()
            note = ("Сессия возвращена из корзины." if restored is not None
                    else "Корзина пуста — возвращать нечего.")
            self._add(Entry("notice", text=note))
            return
        if item is None:
            return
        if name == "toggle_mark":
            if self._is_current_session(item):
                self._add(Entry("notice", text="Текущую сессию не трогаю."))
                return
            if item.key in self._sessions_marks:
                self._sessions_marks.discard(item.key)
            else:
                self._sessions_marks.add(item.key)
            self._refresh_sessions_picker()
            return
        if name == "rename":
            picker.ask_input("Новое имя сессии: ", ("rename", item),
                             initial=item.title)
            return
        if name == "delete":
            if self._is_current_session(item):
                self._add(Entry("notice", text="Текущую сессию не трогаю."))
                return
            picker.ask_confirm(
                "Удалить сессию «" + item.title + "» в корзину?",
                ("trash", [item]))
            return
        if name == "delete_marked":
            marked = [it for it in picker.items
                      if it.key in self._sessions_marks
                      and not self._is_current_session(it)]
            if not marked:
                self._add(Entry("notice", text="Отмеченных сессий нет: "
                                               "пробел отмечает выбранную."))
                return
            picker.ask_confirm(
                "Удалить " + str(len(marked)) + " отмеченных сессий в корзину?",
                ("trash", marked))
            return

    def _trash_session_items(self, items: list) -> None:
        """Выбранные сессии — в корзину вместе с чекпоинтами."""
        from ..core.sessions import trash_sessions

        safe = [it for it in items if not self._is_current_session(it)]
        moved = trash_sessions(self._data_dir(),
                               [it.payload["path"] for it in safe])
        for it in items:
            self._sessions_marks.discard(it.key)
        self._refresh_sessions_picker()
        self._add(Entry("notice", text="В корзину убрано сессий: "
                                       + str(len(moved))
                                       + " (u в /sessions — вернуть последнюю)."))

    def _rename_session_item(self, item, title: str) -> None:
        """Переименование из выбиралки: текущей — через журнал, чужой —
        записью в её файл; список сразу перечитывается."""
        from ..core.sessions import set_session_title

        if self._is_current_session(item):
            self.journal.set_title(title)
        elif not set_session_title(item.payload["path"], title):
            self._add(Entry("notice", text="Не вышло переименовать сессию."))
            return
        self._refresh_sessions_picker()
        self._add(Entry("notice", text="Сессия переименована: «" + title + "»."))

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
        kind, self._edit_kind = self._edit_kind, ""
        if kind == "cells":
            # /cells: профиль перечитается библиотекой при следующем открытии.
            if self.picker is not None and self._picker_kind == "cells":
                self.picker.set_items(self._cells_items())
            self._add(Entry("notice", text="Профиль ячеек обновлён из редактора: "
                                           + str(path)))
            self._last_change = self._clock()
            return
        if kind == "compose":
            # /editor: текст файла — черновик сообщения в поле ввода.
            try:
                text = Path(path).read_text(encoding="utf-8")
            except OSError:
                text = ""
            self.editor.set_text(text.strip("\n"))
            self._add(Entry("notice", text="Сообщение — из редактора; "
                                           "Enter отправит."))
            try:
                Path(path).unlink()
            except OSError:
                pass
            self._last_change = self._clock()
            return
        if kind == "memory":
            # /memory: проектные инструкции подхватит агент со следующего хода.
            self._add(Entry("notice", text="AGENTS.md обновлён из редактора — "
                                           "агент подхватит со следующего хода."))
            self._last_change = self._clock()
            return
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
        if self._panel_anim is not None:
            return True
        if self._nibbler_line and now < self._nibbler_until + 0.3:
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
        self._observe_panels(ev, kind)
        self._last_change = self._clock()
        if kind == "session.start":
            self.model = str(ev.get("model") or self.model)
            self.mode = str(ev.get("mode") or self.mode)
            if ev.get("cwd"):
                self.cwd = str(ev.get("cwd"))
        elif kind == "session.rewound":
            if ev.get("scope") in ("talk", "both"):
                self._cut_feed_after_turn(int(ev.get("turn") or 0))
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
            # Расход детей (agent_id, ЗАДАЧА-11) не двигает модель, стоимость
            # и контекст главного агента в статус-баре: их место — «Экипаж».
            if not ev.get("agent_id"):
                self.model = str(ev.get("model") or self.model)
                self.cost = float(ev.get("cost") or 0.0)
                self.context_pct = int(round(float(ev.get("context_pct") or 0)))
        elif kind == "mode.changed":
            self.mode = str(ev.get("mode") or self.mode)
        elif kind == "notice":
            self._add(Entry("notice", text=str(ev.get("text", ""))))
        elif kind == "btw.answer":
            # Ответ «в сторону»: отдельной записью, ход и история не тронуты.
            self._add(Entry("btw", text=str(ev.get("text", ""))))
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
                self._send_user_message(text, delivery="interrupt")
            elif self.queue:
                self._send_user_message(self.queue.pop(0), delivery="queue")
        # незнакомые типы молча пропускаем — контракт только добавляется

    # --- отправка и команды ---------------------------------------------------------

    def _compat_command(self, cmd: str, text: str) -> bool:
        """Слэш-команда из файлов (.claude/commands и родня, ЗАДАЧА-12): текст — модели."""
        from ..mind.compat import resolve_command

        try:
            body = resolve_command(Path(self.cwd), cmd[1:], text[len(cmd):].strip(), self.settings)
        except OSError:
            body = None
        if not body:
            return False
        self._send_user_message(body)
        return True

    def _reload_settings(self) -> None:
        """Настройки поправили руками — подхватить на лету (не чаще раза в 2 с)."""
        now = time.monotonic()
        if now - getattr(self, "_settings_checked", 0.0) < 2.0:
            return
        self._settings_checked = now
        reloader = getattr(self.settings, "reload_if_changed", None)
        if not callable(reloader):
            return
        try:
            changes = reloader()
        except Exception as exc:  # noqa: BLE001 — битый файл: прошлые значения остаются
            title = getattr(exc, "title", "") or str(exc)
            self._add(Entry("notice", text=title + " — работаю на прошлых значениях."))
            return
        if changes:
            keys = ", ".join(c["key"] for c in changes[:6]) + (" …" if len(changes) > 6 else "")
            self._add(Entry("notice", text="Настройки обновлены из файла: " + keys))

    def _send_user_message(self, text: str, display: str | None = None,
                           delivery: str | None = None) -> None:
        attachments = [tok[1:] for tok in text.split()
                       if tok.startswith("@") and len(tok) > 1]
        self._add(Entry("user", text=text if display is None else display))
        out_text = text
        if self.nibbler is not None:
            self.nibbler.observe({"type": "user.message", "text": text})
            # Серьёзные замечания Ниблера уходят агенту пометкой в этот ход;
            # в ленте виден только текст человека, пометка — в команде.
            notes = self.nibbler.take_notes()
            if notes:
                marks = "\n".join("[Ниблер: " + note + "]" for note in notes)
                out_text = marks + "\n\n" + text
        cmd = {"type": "user.message", "text": out_text,
               "attachments": attachments}
        if delivery:
            cmd["delivery"] = delivery  # контракт: docs/СОБЫТИЯ.md
        self.send_command(cmd)

    def cycle_mode(self, step: int = 1) -> None:
        idx = MODES.index(self.mode) if self.mode in MODES else 0
        self.send_command({"type": "mode.set", "mode": MODES[(idx + step) % len(MODES)]})

    def _match_arg(self, query: str, names: dict):
        """Аргумент команды по id/названию/названию в чужой раскладке.

        names: {значение: (имена…)}. Без ui.layout_fix — только точные
        совпадения, с ним — ещё и переводы раскладкой (kbvbns → лимиты).
        """
        q = (query or "").strip().lower()
        if not q:
            return None
        for value, aliases in names.items():
            if q in tuple(str(a).lower() for a in aliases):
                return value
        if self.settings.get("ui.layout_fix"):
            return _layout.match_name(query, names)
        return None

    def _fix_dot_command(self, text: str):
        """Ввод с точки в ЙЦУКЕН — команда, если перевод даёт известную.

        «.зфтуд Лимиты» — это «/panel Лимиты»: клавиша / в русской
        раскладке печатает точку. Обычное сообщение с точки не трогаем:
        переводим, только если первое слово стало известной командой.
        """
        stripped = text.lstrip()
        if not stripped.startswith(".") or stripped.startswith(".."):
            return None
        parts = stripped[1:].split(None, 1)
        if not parts:
            return None
        name = resolve_command(parts[0])
        if name is None:
            return None
        return "/" + name + ((" " + parts[1]) if len(parts) > 1 else "")

    def submit_text(self, text: str | None = None) -> None:
        if text is None:
            text = self.editor.submit()
        text = text.strip("\n")
        if not text.strip():
            return
        if text.startswith(".") and self.settings.get("ui.layout_fix"):
            fixed = self._fix_dot_command(text)
            if fixed is not None:
                text = fixed
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
            # Режим отправки во время хода (chat.send_mode, ЗАДАЧА-18):
            # inject — сразу агенту, подмешается перед следующим вызовом
            # модели; queue — сюда, отдельным ходом после; interrupt —
            # прервать ход и отправить этим же текстом.
            mode = self._send_mode()
            if mode == "inject":
                self._send_user_message(text, delivery="inject")
            elif mode == "interrupt":
                self._interrupt_send(text)
            else:
                self.queue.append(text)
                self._add(Entry("notice", text="В очереди: " + text.split("\n")[0]))
            return
        self._send_user_message(text)

    def _slash(self, text: str) -> None:
        parts = text.split()
        raw = parts[0]
        cmd = raw
        if self.settings.get("ui.layout_fix") and raw.startswith("/"):
            canonical = resolve_command(raw[1:])
            if canonical is not None:
                cmd = "/" + canonical
        rest = text[len(raw):].strip()
        if cmd in ("/exit", "/quit"):
            self.done = True
        elif cmd == "/clear":
            self.entries.clear()
            self._add(Entry("notice", text="Лента очищена."))
        elif cmd == "/help":
            self.open_help_picker()
        elif cmd == "/mode":
            mode = None
            if len(parts) > 1:
                mode = self._match_arg(parts[1], {m: (m,) for m in MODES})
            if len(parts) > 1 and mode is not None:
                self.send_command({"type": "mode.set", "mode": mode})
            elif len(parts) > 1:
                self._add(Entry("notice", text="Нет режима " + parts[1]))
            else:
                self.cycle_mode(1)
        elif cmd in ("/model", "/models"):
            self._model_command(parts[1:])
        elif cmd == "/cost":
            self._usage_command(usage=False)
        elif cmd == "/usage":
            self._usage_command(usage=True)
        elif cmd == "/context":
            self.toggle_panel("context")
        elif cmd == "/status":
            self._status_command()
        elif cmd in ("/doctor", "/checkup"):
            self._doctor_command()
        elif cmd == "/init":
            self._init_command()
        elif cmd == "/memory":
            self._memory_command()
        elif cmd == "/review":
            self._review_command(rest)
        elif cmd == "/export":
            self._export_command()
        elif cmd == "/bug":
            self._bug_command()
        elif cmd == "/btw":
            self._btw_command(rest)
        elif cmd == "/diff":
            self.toggle_panel("git")
        elif cmd == "/add-dir":
            if rest:
                self._add_dir(rest)
            else:
                self.open_adddir_picker()
        elif cmd == "/mcp":
            if len(parts) > 1:
                self.toggle_panel("mcp")
            else:
                self.open_mcp_picker()
        elif cmd == "/agents":
            self.open_agents_picker()
        elif cmd == "/cells":
            self.open_cells_picker()
        elif cmd == "/theme":
            self.open_theme_picker()
        elif cmd == "/editor":
            self._editor_command()
        elif cmd == "/details":
            self._flip_setting("ui.details", "Детали ленты")
        elif cmd == "/thinking":
            self._flip_setting("ui.show_thinking", "Рассуждения в ленте")
        elif cmd == "/copy":
            self._copy_command(parts[1:])
        elif cmd in ("/sessions", "/resume", "/continue"):
            self.open_sessions_picker()
        elif cmd == "/manners":
            self.open_manners_picker()
        elif cmd == "/new":
            self.start_new_session()
        elif cmd == "/rename":
            self.rename_session(text[len(raw):])
        elif cmd == "/undo":
            self.send_command({"type": "undo"})
        elif cmd == "/rewind":
            if len(parts) > 1:
                self._rewind_command(parts[1:])
            else:
                self.open_rewind_picker()
        elif cmd == "/redo":
            self.send_command({"type": "redo"})
        elif cmd in ("/compact", "/summarize"):
            hint = text[len(raw):].strip()
            self.send_command({"type": "compact", "hint": hint})
        elif cmd == "/todo":
            self.toggle_panel("todo")
        elif cmd == "/panel":
            self._panel_command(parts[1:])
        elif cmd == "/panels":
            self.open_panels_picker()
        elif cmd == "/layout":
            self._layout_command(parts[1:])
        elif not self._compat_command(cmd, text):
            self._add(Entry("notice", text="Не знаю команду " + cmd + " — /help подскажет."))

    # --- команды ЗАДАЧИ-16 ---------------------------------------------------

    def _flip_setting(self, key: str, title: str) -> None:
        """Выключатель настройкой из чата: /details, /thinking."""
        value = not bool(self.settings.get(key))
        self.settings.set(key, value)
        self._add(Entry("notice", text=title + ": " + ("вкл" if value else "выкл")))

    def _model_command(self, args: list) -> None:
        """/model: выбиралка ролей; /model <ссылка> — главной; /model <роль> <ссылка>."""
        from .panels.models import ROLES

        if not args:
            self.open_models_picker()
            return
        role_arg = self._match_arg(args[0], {r: (r, t) for r, t in ROLES}) \
            if len(args) >= 2 else None
        if len(args) >= 2 and role_arg is not None:
            role, ref = role_arg, args[1]
        elif len(args) == 1:
            role, ref = "main", args[0]
        else:
            self._add(Entry("notice", text="Нужно: /model, /model <модель> "
                                           "или /model <роль> <модель>"))
            return
        self.settings.set("model." + role, ref)
        self._add(Entry("notice", text="Модель роли " + role + ": " + ref
                                       + " — со следующего хода."))

    def _usage_command(self, *, usage: bool) -> None:
        """/cost и /usage: расход сессии из PanelData (события usage)."""
        data = self.panel_data
        turns = sum(1 for e in self.entries if e.kind == "user")
        lines = ["расход сессии",
                 "ходов: " + str(turns),
                 "токены: вход " + str(data.tokens_in) + " · выход "
                 + str(data.tokens_out)]
        if data.cache_seen:
            lines.append("кэш: " + str(data.cache))
        lines.append("стоимость: $" + format(data.cost, ".4f"))
        lines.append("контекст: " + str(data.context_pct) + "%")
        if usage:
            if turns:
                lines.append("средний ход: $" + format(data.cost / turns, ".4f"))
            if data.limits:
                lines.append("лимиты провайдера:")
                lines.extend("  " + str(k) + ": " + str(v)
                             for k, v in data.limits.items())
            else:
                lines.append("лимиты: провайдер событиями не отдал")
        self._add(Entry("help", text="\n".join(lines)))

    def _status_command(self) -> None:
        """/status: состояние сессии одной справкой."""
        from .. import __version__

        turns = sum(1 for e in self.entries if e.kind == "user")
        session = "без журнала"
        if self.journal is not None:
            session = str(getattr(self.journal, "path", "") or "журнал включён")
        panels = ", ".join(self.layout.leaves()) or "нет"
        lines = ["Луна " + str(__version__),
                 "режим: " + self.mode + " · модель: " + self.model,
                 "каталог: " + self.cwd,
                 "сессия: " + session,
                 "контекст: " + str(self.context_pct) + "% · стоимость: $"
                 + format(self.cost, ".4f") + " · ходов: " + str(turns),
                 "панели открыты: " + panels,
                 "тема: " + str(self.settings.get("ui.theme"))
                 + " · статус-строка: " + str(self.settings.get("ui.statusline"))]
        if self.extra_dirs:
            lines.append("доп. каталоги: " + ", ".join(self.extra_dirs))
        self._add(Entry("help", text="\n".join(lines)))

    def _doctor_command(self) -> None:
        """/doctor в чате: тот же run_doctor, что у luna doctor, — в ленту."""
        from ..diagnostics.doctor import run_doctor

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            run_doctor()
        text = buf.getvalue().strip()
        self._add(Entry("help", text=text or "doctor ничего не сказал"))

    def _init_command(self) -> None:
        """/init: агент создаёт/обновляет AGENTS.md проекта обычным ходом."""
        prompt = ("Изучи проект в текущем каталоге и создай или обнови файл "
                  "AGENTS.md: что за проект, стек, как собирать и тестировать, "
                  "ключевые файлы и договорённости. Если AGENTS.md уже есть — "
                  "дополни и поправь устаревшее, не переписывай с нуля.")
        if self.busy:
            self.queue.append(prompt)
            self._add(Entry("notice", text="В очереди: /init"))
            return
        self._send_user_message(prompt, display="/init — AGENTS.md проекта")

    def _review_command(self, focus: str) -> None:
        """/review: агент ревьюит текущий git-дифф обычным ходом."""
        prompt = ("Проведи ревью текущих изменений: выполни git diff и "
                  "git diff --cached, оцени корректность, риски и стиль. "
                  "Замечания — списком по серьёзности, правок не вноси.")
        if focus:
            prompt += " Фокус ревью: " + focus
        if self.busy:
            self.queue.append(prompt)
            self._add(Entry("notice", text="В очереди: /review"))
            return
        self._send_user_message(prompt, display="/review — ревью диффа")

    def _memory_command(self) -> None:
        """/memory: AGENTS.md проекта — во внешний редактор."""
        path = Path(self.cwd) / "AGENTS.md"
        if not path.exists():
            try:
                path.write_text("# AGENTS.md\n\nИнструкции проекта для Луны.\n",
                                encoding="utf-8")
            except OSError as exc:
                self._add(Entry("error", text="Не вышло создать AGENTS.md: "
                                              + str(exc)))
                return
        self._edit_kind = "memory"
        self.edit_request = str(path)

    def _editor_command(self) -> None:
        """/editor: черновик сообщения — во внешний редактор и обратно."""
        path = Path(tempfile.gettempdir()) / "luna-compose.md"
        try:
            path.write_text(self.editor.text, encoding="utf-8")
        except OSError as exc:
            self._add(Entry("error", text="Не вышло подготовить файл: " + str(exc)))
            return
        self._edit_kind = "compose"
        self.edit_request = str(path)

    def _export_command(self) -> None:
        """/export: лента сессии — в markdown-файл текущего каталога."""
        lines = ["# Сессия Луны — "
                 + time.strftime("%Y-%m-%d %H:%M", time.localtime()), ""]
        count = 0
        for entry in self.entries:
            if entry.kind == "user":
                lines.append("## Человек\n\n" + entry.text + "\n")
            elif entry.kind == "agent":
                lines.append("## Луна\n\n" + entry.text + "\n")
            elif entry.kind == "btw":
                lines.append("## В сторону\n\n" + entry.text + "\n")
            elif entry.kind == "thinking":
                lines.append("> Рассуждения: " + entry.text + "\n")
            elif entry.kind == "tool":
                head = "### Тул: " + (entry.name or "tool")
                if entry.summary:
                    head += " — " + entry.summary
                lines.append(head + "\n")
                if entry.output:
                    lines.append("```\n" + entry.output.rstrip() + "\n```\n")
            elif entry.kind == "shell":
                lines.append("`$ " + entry.text + "`\n")
            elif entry.kind == "error":
                lines.append("## Ошибка\n\n" + entry.text + "\n")
            elif entry.kind == "notice":
                lines.append("> " + entry.text + "\n")
            else:
                continue
            count += 1
        name = "luna-session-" + time.strftime("%Y%m%d-%H%M%S") + ".md"
        path = Path(self.cwd) / name
        try:
            path.write_text("\n".join(lines), encoding="utf-8")
        except OSError as exc:
            self._add(Entry("error", text="Не вышло записать экспорт: " + str(exc)))
            return
        self._add(Entry("notice", text="Сессия сохранена: " + str(path)
                                       + " (записей: " + str(count) + ")"))

    def _bug_command(self) -> None:
        """/bug: отчёт об ошибке локальным файлом. Секретов в нём нет:
        настройки — без ключей/токенов, секреты живут отдельным файлом
        и сюда не читаются вовсе."""
        import platform
        import sys

        from .. import __version__

        lines = ["# Отчёт об ошибке Луны",
                 "",
                 "версия: " + str(__version__),
                 "дата: " + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
                 "платформа: " + platform.platform(),
                 "python: " + sys.version.split()[0],
                 "каталог: " + self.cwd,
                 "модель: " + self.model + " · режим: " + self.mode,
                 "",
                 "## Настройки (без секретов)"]
        try:
            for item in self.settings.describe():
                key = str(item.get("key", ""))
                if any(word in key for word in ("secret", "token", "key", "password")):
                    continue
                lines.append(key + " = " + str(item.get("value", "")))
        except Exception:  # noqa: BLE001 — отчёт важнее полноты
            lines.append("(не вышло прочитать настройки)")
        errors = [e for e in self.panel_data.backside if e.get("kind") == "error"]
        lines.append("")
        lines.append("## Последние ошибки (журнал событий)")
        if errors:
            for entry in errors[-5:]:
                lines.append("### " + str(entry.get("title", "")))
                lines.append(str(entry.get("body", "")))
        else:
            lines.append("ошибок в журнале нет")
        feed_errors = [e.text for e in self.entries if e.kind == "error"]
        if feed_errors:
            lines.append("")
            lines.append("## Ошибки в ленте")
            lines.extend("- " + text.split("\n")[0] for text in feed_errors[-5:])
        name = "bug-" + time.strftime("%Y%m%d-%H%M%S") + ".md"
        path = self._data_dir().logs / name
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError as exc:
            self._add(Entry("error", text="Не вышло записать отчёт: " + str(exc)))
            return
        self._add(Entry("notice", text="Отчёт об ошибке: " + str(path)
                                       + " — секретов в нём нет, приложи к обращению."))

    def _btw_command(self, question: str) -> None:
        """/btw: вопрос в сторону — ядро отвечает по истории, не трогая её
        и не занимая ход (команда btw контракта, ЗАДАЧА-18)."""
        if not question:
            self._add(Entry("notice", text="Нужно: /btw <вопрос>"))
            return
        self._add(Entry("btw", text="в сторону: " + question))
        self.send_command({"type": "btw", "text": question})

    def _panel_command(self, args: list) -> None:
        """/panel <имя> — панель по id, названию или алиасу; без имени — выбиралка.

        Имя понимается и в чужой раскладке (kbvbns — «Лимиты»), пока
        включён ui.layout_fix; выключен — только точные имена.
        """
        if not args:
            self.open_panels_picker()
            return
        query = " ".join(args)
        spec = find_panel(query)
        if spec is not None and not self.settings.get("ui.layout_fix"):
            q = query.strip().lower()
            names = (spec.panel_id, spec.title.lower(), *spec.aliases)
            if q not in names:
                spec = None  # нашлась только переводом раскладки — а он выключен
        if spec is None:
            self._add(Entry("notice", text="Нет панели «" + query
                                           + "» — /panels покажет все."))
            return
        self.toggle_panel(spec.panel_id)

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
            self._panel_anchor = (self._panel_shown_w, self._clock())
            ok = self.layout.load_layout(name)
            note = "Раскладка загружена: " + name if ok else "Нет раскладки " + name
            self._add(Entry("notice", text=note))
        elif action == "list":
            names = ", ".join(self.layout.layout_names()) or "пока нет"
            self._add(Entry("notice", text="Сохранённые раскладки: " + names))
        else:
            self._add(Entry("notice", text="Нужно: /layout save|load|list <имя>"))

    def _rewind_command(self, args: list) -> None:
        """/rewind [N] [files|talk|both] — список ходов или откат к ходу N."""
        scopes = {"files": ("files", "файлы"), "talk": ("talk", "разговор"),
                  "both": ("both", "оба", "всё")}
        command: dict = {"type": "rewind"}
        for arg in args:
            if arg.isdigit():
                command["turn"] = int(arg)
            else:
                scope = self._match_arg(arg, scopes)
                if scope is not None:
                    command["scope"] = scope
        self.send_command(command)

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
        self._panel_anchor = (self._panel_shown_w, self._clock())
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
        if self.settings.get("ui.layout_fix"):
            # Ctrl/Alt — по физической клавише: Alt+д — это Alt+L.
            key = _layout.physical_key(key)
        self._reload_settings()
        if key.name == "eof":
            self.done = True
            return
        if key.label == "ctrl+c":
            self._ctrl_c_key()
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
        if key.label in _PANEL_KEYS:
            self.toggle_panel(_PANEL_KEYS[key.label])
            return
        if key.label == "ctrl+s":
            self._cycle_send_mode()
            return
        if key.label == "ctrl+r":
            self.open_history_picker()
            return
        if key.label == "ctrl+l":
            self._redraw = True  # run_chat перерисует кадр целиком
            return
        if key.label == "ctrl+v":
            self._paste_clipboard()
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
            # Vim-режим ввода: Esc в режиме ввода — переход в нормальный,
            # а не прерывание и не откат (настройка ui.vim_mode).
            if self.settings.get("ui.vim_mode") and not self._vim_normal:
                if self.completions or self.cmd_completions:
                    self.completions = []
                    self.cmd_completions = []
                    return
                self._vim_normal = True
                return
            self._esc_key()
            return
        if self._vim_normal and self.settings.get("ui.vim_mode"):
            self._vim_key(key)
            return
        if key.name == "tab":
            if self.cmd_completions:
                if "shift" in key.mods:
                    self.completion_index = (self.completion_index - 1) % len(self.cmd_completions)
                else:
                    self._apply_cmd_completion()
                return
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
            if "alt" in key.mods and self.busy and self.editor.text.strip():
                # Alt+Enter во время хода — разово: прервать и отправить.
                self._interrupt_send(self.editor.submit())
            elif "alt" in key.mods or "shift" in key.mods:
                self.editor.newline()
                self._update_completions()
            else:
                self.submit_text()
            return
        if key.name == "up":
            if self.cmd_completions:
                self.completion_index = (self.completion_index - 1) % len(self.cmd_completions)
            elif self.completions:
                self.completion_index = (self.completion_index - 1) % len(self.completions)
            elif not self.editor.text and self.queue:
                # Отозвать последнее сообщение из очереди обратно в редактор.
                self.editor.set_text(self.queue.pop())
            elif not self.editor.up():
                self.editor.history_prev()
            return
        if key.name == "down":
            if self.cmd_completions:
                self.completion_index = (self.completion_index + 1) % len(self.cmd_completions)
            elif self.completions:
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
        if self.completions or self.cmd_completions:
            self.completions = []
            self.cmd_completions = []
            return
        if self.busy:
            # Esc во время хода — прервать ОДНИМ нажатием (ЗАДАЧА-18);
            # набросок из поля ввода уезжает следующим ходом.
            draft = self.editor.text
            if draft.strip():
                self._after_interrupt = draft
                self.editor.clear()
            self.send_command({"type": "interrupt"})
            return
        if double and not self.editor.text.strip():
            # Esc Esc на пустом вводе — к выбору хода для отката (как у эталонов).
            self.open_rewind_picker()

    # --- прерывание и режимы отправки (ЗАДАЧА-18) ---------------------------

    def _flash_hint(self, text: str) -> None:
        """Короткая подсказка строкой над полем ввода (не запись ленты)."""
        self._hint = (text, self._clock() + 4.0)

    def _ctrl_c_key(self) -> None:
        """Ctrl+C: во время хода — прервать; с текстом в поле — очистить
        поле; на пустом поле второй подряд за секунду — выход, после
        первого — подсказка. Выход по первому нажатию остался только
        у конца ввода (EOF)."""
        now = self._clock()
        if self.busy:
            self.send_command({"type": "interrupt"})
            self._ctrl_c_at = -10.0
            return
        if self.editor.text.strip():
            self.editor.clear()
            self._ctrl_c_at = -10.0
            return
        if now - self._ctrl_c_at <= 1.0:
            self.done = True
            return
        self._ctrl_c_at = now
        self._flash_hint("Ещё раз Ctrl+C в течение секунды — выход из Луны.")

    def _send_mode(self) -> str:
        mode = str(self.settings.get("chat.send_mode") or "inject")
        return mode if mode in ("inject", "queue", "interrupt") else "inject"

    _SEND_MODE_TITLES = {"inject": "вставка в текущий ход",
                         "queue": "очередь отдельным ходом",
                         "interrupt": "прервать и отправить сразу"}

    def _cycle_send_mode(self) -> None:
        """Ctrl+S: режим отправки во время хода — по кругу, на лету."""
        order = ("inject", "queue", "interrupt")
        nxt = order[(order.index(self._send_mode()) + 1) % len(order)]
        self.settings.set("chat.send_mode", nxt)
        self._flash_hint("Отправка во время хода: " + self._SEND_MODE_TITLES[nxt] +
                         " (Ctrl+S — сменить)")

    def _interrupt_send(self, text: str) -> None:
        """Прервать текущий ход и сразу отправить текст новым (Alt+Enter
        и режим interrupt): текст уйдёт, как только придёт turn.done."""
        text = (text or "").strip("\n")
        if not text.strip():
            return
        self._after_interrupt = text
        self.editor.clear()
        self.send_command({"type": "interrupt"})
        self._add(Entry("notice", text="Прерываю ход — дальше: " +
                                       text.split("\n")[0]))

    def _meta_lines(self, now: float) -> list:
        """Строки над полем ввода: подсказка, очередь, режим отправки."""
        out: list = []
        if self._hint is not None and now < self._hint[1]:
            out.append(self._hint[0])
        for text in self.queue[:3]:
            out.append("в очереди: " + str(text).split("\n")[0])
        if len(self.queue) > 3:
            out.append("в очереди ещё " + str(len(self.queue) - 3) +
                       " (↑ на пустом поле — вернуть в редактор)")
        if self.busy:
            out.append("ход идёт · Enter: " + self._SEND_MODE_TITLES[self._send_mode()] +
                       " · Ctrl+S — сменить · Alt+Enter — прервать и отправить")
        return out[:4]

    # --- vim-режим ввода и вставка (ЗАДАЧА-16) --------------------------------

    def _vim_key(self, key: Key) -> None:
        """Нормальный режим vim-ввода: h l 0 $ w b — движение, x — символ,
        dd — строка, i/a/A — обратно в ввод. Остальное глотаем молча."""
        ed = self.editor
        if self._vim_pending == "d":
            self._vim_pending = ""
            if key.char == "d":
                self._vim_delete_line()
            return
        if key.name == "up":
            ed.up() or ed.history_prev()
            return
        if key.name == "down":
            ed.down() or ed.history_next()
            return
        if not key.char or key.mods:
            return
        ch = key.char
        if ch == "h":
            ed.left()
        elif ch == "l":
            ed.right()
        elif ch == "0":
            ed.line_home()
        elif ch == "$":
            ed.line_end()
        elif ch == "w":
            self._vim_word(1)
        elif ch == "b":
            self._vim_word(-1)
        elif ch == "x":
            ed.delete()
        elif ch == "d":
            self._vim_pending = "d"
        elif ch == "i":
            self._vim_normal = False
        elif ch == "a":
            ed.right()
            self._vim_normal = False
        elif ch == "A":
            ed.line_end()
            self._vim_normal = False

    def _vim_word(self, step: int) -> None:
        ed = self.editor
        starts = [m.start() for m in re.finditer(r"\S+", ed.text)]
        if step > 0:
            target = next((s for s in starts if s > ed.cursor), None)
            if target is not None:
                ed.cursor = target
            else:
                ed.cursor = len(ed.text)
        else:
            target = next((s for s in reversed(starts) if s < ed.cursor), None)
            if target is not None:
                ed.cursor = target
            else:
                ed.cursor = 0

    def _vim_delete_line(self) -> None:
        ed = self.editor
        text, cur = ed.text, ed.cursor
        start = text.rfind("\n", 0, cur) + 1
        end = text.find("\n", cur)
        if end == -1:
            ed.text = text[:start].rstrip("\n") if start else ""
            ed.cursor = len(ed.text)
        else:
            ed.text = text[:start] + text[end + 1:]
            ed.cursor = min(start, len(ed.text))

    def _paste_clipboard(self) -> None:
        """Ctrl+V: текст из буфера — в ввод; путь к файлу картинки — @вложением.

        Саму картинку терминал приложению не отдаёт (вставка несёт только
        текст), поэтому честный максимум — путь к файлу из буфера.
        """
        text = _clipboard_text()
        stripped = text.strip()
        if stripped and "\n" not in stripped:
            path = Path(stripped.strip('"'))
            if path.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif",
                                       ".webp", ".bmp") and path.is_file():
                self.editor.insert("@" + stripped.strip('"') + " ")
                self._add(Entry("notice", text="Картинка из буфера вложена "
                                               "файлом: " + str(path)))
                return
        if text:
            self.editor.insert(text)
            self._update_completions()
            return
        self._add(Entry("notice", text="Буфер пуст или недоступен; картинку "
                                       "терминал напрямую не передаёт — "
                                       "вставьте путь к файлу картинки."))

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

    def _resize_column(self, delta: int) -> None:
        """Ширина колонки панелей: запоминается в раскладке (layout.column_w)."""
        cols = self._geom_cols or 80
        base = self.layout.column_w or max(24, cols // 3)
        self.layout.column_w = min(max(16, base + delta), max(24, cols - 24))
        self._panel_anchor = (self._panel_shown_w, self._clock())

    def _panel_key(self, key: Key) -> None:
        focused = self.layout.focused
        if key.name == "esc":
            self.panel_focus = False
            return
        if key.name == "f6":
            # F6 повторно — выход из режима панелей (раньше не работал:
            # handle_key уводил все клавиши сюда, а f6 здесь не знали).
            self.panel_focus = False
            return
        if key.name in ("left", "right") and "shift" in key.mods:
            # Shift+←/→ — граница лента↔панели: стрелка — куда едет граница.
            self._resize_column(-2 if key.name == "right" else 2)
            return
        if focused:
            # Свои клавиши панели (прокрутка, выбор, дифф) — первыми.
            content = self._panel_content(focused)
            if content is not None and content.handle_key(self, key):
                return
        if key.name == "up":
            self.layout.focus_next(-1)
        elif key.name == "down":
            self.layout.focus_next(1)
        elif key.name == "left" and focused:
            self.layout.resize(focused, -0.25)
        elif key.name == "right" and focused:
            self.layout.resize(focused, 0.25)
        elif key.name == "enter" and focused:
            self.layout.toggle_maximize(focused)
        elif key.char == "x" and focused:
            # x — закрыть сфокусированную панель, режим выключаем при пустом дереве.
            self.layout.close(focused)
            if not self.layout.leaves():
                self.panel_focus = False
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

    def _panel_at(self, mouse: Mouse):
        """Панель под мышью по геометрии последнего кадра или None."""
        mx, my = mouse.x - 1, mouse.y - 1
        for panel_id, (px, py, pw, ph) in self._panel_rects.items():
            if px <= mx < px + pw and py <= my < py + ph:
                return panel_id, (px, py, pw, ph), mx, my
        return None

    def _panel_mouse(self, mouse: Mouse) -> bool:
        """Мышь по панелям (ЗАДАЧА-17): True — событие съели панели.

        Клик по заголовку — фокус и режим панелей; перетаскивание
        разделителя — размер; колесо над панелью — её прокрутка.
        """
        if self._panel_drag is not None:
            panel_id, last_y = self._panel_drag
            if mouse.kind == "move":
                dy = (mouse.y - 1) - last_y
                if dy:
                    self.layout.resize(panel_id, dy * 0.25)
                    self._panel_drag = (panel_id, mouse.y - 1)
                return True
            if mouse.kind == "release":
                self._panel_drag = None
                return True
        found = self._panel_at(mouse)
        if found is None:
            return False
        panel_id, (px, py, pw, ph), mx, my = found
        if mouse.kind in ("wheel_up", "wheel_down"):
            content = self._panel_content(panel_id)
            key = Key("up" if mouse.kind == "wheel_up" else "down")
            if content is not None:
                content.handle_key(self, key)
            return True
        if mouse.kind == "press" and mouse.button == 0:
            below = any(oy == py + ph for _id, (ox, oy, ow, oh)
                        in self._panel_rects.items() if _id != panel_id)
            if below and my == py + ph - 1:
                # Взялись за разделитель — дальше move меняет размер.
                self._panel_drag = (panel_id, my)
                self.layout.focused = panel_id
                return True
            self.layout.focused = panel_id
            if my == py:
                self.panel_focus = True  # клик по заголовку — фокус и режим
            return True
        if self._sel_anchor is not None:
            return False  # идёт выделение ленты — отпуск обработает лента
        return mouse.kind in ("move", "release")

    def handle_mouse(self, mouse: Mouse) -> None:
        if self.picker is not None:
            return  # под выбиралкой лента мышью не выделяется
        if self._col_drag:
            # Тянем границу лента↔панели: ширина колонки — от позиции мыши.
            if mouse.kind == "move":
                cols = self._geom_cols
                if cols:
                    self.layout.column_w = min(max(16, cols - (mouse.x - 1)),
                                               max(24, cols - 24))
                return
            if mouse.kind == "release":
                self._col_drag = False
                return
        if (mouse.kind == "press" and mouse.button == 0 and self.layout.leaves()
                and self._feed_geom[0]
                and (mouse.x - 1) - self._feed_geom[0] in (-1, 0)):
            # Захват — только сама граница и последняя колонка ленты:
            # заголовок панели начинается на колонку правее.
            self._col_drag = True
            self.panel_focus = False
            return
        if self._panel_mouse(mouse):
            return
        if mouse.kind == "wheel_up":
            self.scroll = min(self.scroll + 3, 100000)
        elif mouse.kind == "wheel_down":
            self.scroll = max(0, self.scroll - 3)
        elif mouse.kind == "press" and mouse.button == 0:
            # Клик по ленте или вводу снимает фокус с панели (ЗАДАЧА-18).
            self.panel_focus = False
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
        cmd_match = re.fullmatch(r"/([a-zа-яё-]*)", prefix, re.IGNORECASE)
        if cmd_match is not None and self.editor.cursor == len(self.editor.text):
            # Tab после «/»: команды реестра с описаниями (ЗАДАЧА-16) —
            # латинские и русские имена, игла — в любой раскладке.
            needle = cmd_match.group(1).lower()
            needles = {needle}
            if self.settings.get("ui.layout_fix"):
                needles = {v.lower() for v in _layout.variants(needle)}
            all_names = list(COMMANDS) + [
                (ru, COMMAND_DESCR[lat] + " (/" + lat + ")")
                for ru, lat in COMMAND_RU.items()]
            self.cmd_completions = [
                (name, desc) for name, desc in all_names
                if any(name.startswith(n) for n in needles)][:8]
            self.completions = []
            self.completion_index = 0
            return
        self.cmd_completions = []
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
        if not base:
            # Доп. каталоги (/add-dir): их файлы подсказываем полным путём.
            for extra in self.extra_dirs:
                try:
                    extra_names = sorted(os.listdir(extra))
                except OSError:
                    continue
                for name in extra_names:
                    if name.startswith("__") or not name.startswith(token):
                        continue
                    full = Path(extra) / name
                    out.append(str(full) + ("/" if full.is_dir() else ""))
        self.completions = out[:8]
        self.completion_index = 0

    def _apply_cmd_completion(self) -> None:
        """Tab по команде: «/model » в поле ввода, дальше — Enter или аргумент."""
        if not self.cmd_completions:
            return
        name = self.cmd_completions[self.completion_index][0]
        self.editor.set_text("/" + name + " ")
        self.cmd_completions = []

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
        # Цвета — из текущей палитры темы (применяется в draw из ui.theme).
        out = []
        for text, style in spans:
            key, attrs = _STYLE_MAP.get(style, _STYLE_MAP["text"])
            fg = _color.palette_color(key)
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
            add(_clip_line(head, w))
            if entry.intent:
                for line in wrap_text(entry.intent, w - 2):
                    add([("  " + line, "dim")])
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
                if out_lines and (entry.expanded or self.settings.get("ui.details")):
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
            self._todo_lines(entry, add, w)
        elif entry.kind == "notice":
            for line in wrap_text(entry.text, w):
                add([(line, "notice")])
        elif entry.kind == "error":
            for i, line in enumerate(wrap_text(entry.text, w - 2)):
                prefix = "✗ " if i == 0 else "  "
                style = "err" if i == 0 else "dim"
                add([(prefix, "err"), (line, style)])
        elif entry.kind == "help":
            for raw_line in entry.text.split("\n"):
                for line in wrap_text(raw_line, w):
                    add([(line, "dim")])
        elif entry.kind == "btw":
            for line in wrap_text(entry.text, w - 2):
                add([("· ", "dim"), (line, "comment")])
        elif entry.kind == "shell":
            add(_clip_line([("$ ", "cyan"), (entry.text, "text")], w))
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
            add(_clip_line([(mark, "err" if denied else "ok"), (line, "dim")], width))
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

    def _ask_lines(self, entry: Entry, width: int, add) -> None:
        for j, line in enumerate(wrap_text(entry.text, max(4, width - 2))):
            add([("? ", "guard"), (line, "bold")] if j == 0
                else [("  ", "guard"), (line, "bold")])
        for i, option in enumerate(entry.options):
            marker = "› " if i == entry.selected and entry.status == "pending" else "  "
            style = "cyan" if i == entry.selected and entry.status == "pending" else "text"
            add(_clip_line([(marker, "cyan"), (option, style)], width))
        if entry.options and entry.status == "pending":
            add([("  ↑↓ — выбор · Enter — ответить · или печатайте свой ответ", "dim")])
        if entry.status == "answered":
            add([("  ответ: " + entry.answer, "dim")])

    def _todo_lines(self, entry: Entry, add, width: int = 80) -> None:
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
            add(_clip_line([("  " + mark + " ", "cyan"),
                            (item.get("text", ""), style)], width))
        if not entry.expanded and len(items) > len(show):
            add([("  · Ctrl+O — весь план, Ctrl+T — панель", "dim")])

    def _guards_for(self, tool_entry: Entry) -> list:
        """Подтверждения, относящиеся к этому тулу, в порядке прихода."""
        return [e for e in self.entries
                if e.kind == "guard" and e.guard_of
                and e.guard_of == tool_entry.entry_id]

    def _feed_lines(self, width: int, now: float):
        lines: list = []
        show_thinking = bool(self.settings.get("ui.show_thinking"))
        for entry in self.entries:
            if (entry.kind == "guard" and entry.guard_of
                    and self._find("tool", entry.guard_of) is not None):
                continue  # рисуется внутри своего тула, между вызовом и результатом
            if entry.kind == "thinking" and not entry.expanded and not show_thinking:
                continue  # /thinking выключил показ рассуждений
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

    # Подсказка режима панелей (ЗАДАЧА-17): видна внизу колонки, пока
    # фокус в панелях, — как управлять, не вспоминая справку.
    _PANEL_HINT = ("↑↓ — фокус · ←→ — размер · Shift+←/→ — ширина колонки · "
                   "Tab — переставить · Enter — развернуть · x — закрыть · "
                   "Esc/F6 — назад к вводу")

    def _panel_hints(self, tree, w: int) -> dict:
        """Желаемые высоты видимых панелей; ручной размер побеждает."""
        hints = {}
        for panel_id in tree.visible_leaves():
            if panel_id in tree.manual:
                continue
            content = self._panel_content(panel_id)
            if content is not None:
                hints[panel_id] = content.height_hint_for(self, max(1, w - 2))
        return hints

    def _draw_panel_header(self, buf: Buffer, px: int, py: int, pw: int,
                           title: str, focused: bool) -> None:
        """Заголовок-полоса: мягкий градиент к фону, без жёсткой рамки.

        В фокусе полоса теплее и заголовок — янтарём с пометкой ▸,
        обычные — серебром по приглушённому градиенту (docs/ДИЗАЙН.md).
        """
        start = _color.HEADER_FOCUS_BG if focused else _color.HEADER_BG
        stops = [start, PANEL_BG]
        grad = _color.gradient(stops, max(1, pw))
        for i in range(pw):
            buf.set(px + i, py, " ", bg=grad[i])
        fg = _color.AMBER if focused else _color.SILVER
        label = ("▸ " if focused else "") + title
        col = px + 1
        for ch in label:
            if col >= px + pw:
                break
            bg = grad[col - px] if col - px < len(grad) else PANEL_BG
            col += buf.text(col, py, ch, fg=fg, bg=bg, attrs=_color.BOLD)

    def _draw_panels(self, buf: Buffer, x: int, y: int, w: int, h: int, now: float,
                     tree=None) -> None:
        tree = tree if tree is not None else self.layout
        rects = tree.rects(x, y, w, h, hints=self._panel_hints(tree, w))
        self._panel_rects = dict(rects)
        for panel_id, (px, py, pw, ph) in rects.items():
            if pw <= 0 or ph <= 0:
                continue
            buf.fill_rect(px, py, pw, ph, " ", bg=PANEL_BG)
            content = self._panel_content(panel_id)
            title = content.title if content is not None else panel_id
            focused = bool(self.panel_focus and tree.focused == panel_id)
            self._draw_panel_header(buf, px, py, pw, title, focused)
            if content is None:
                continue
            content.height_hint = max(0, ph - 1)
            if content.draw_into(buf, self, px, py + 1, pw, max(0, ph - 1)):
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
        self._draw_panel_dividers(buf, rects)

    def _draw_panel_dividers(self, buf: Buffer, rects: dict) -> None:
        """Тонкие разделители на стыках панелей: ─ по горизонтали, │ по вертикали."""
        items = list(rects.items())
        for panel_id, (px, py, pw, ph) in items:
            below = any(oy == py + ph and ox < px + pw and px < ox + ow
                        for _id, (ox, oy, ow, oh) in items if _id != panel_id)
            if below and ph >= 2:
                for i in range(pw):
                    buf.set(px + i, py + ph - 1, "─", fg=DIM_FG, bg=PANEL_BG)
            right = any(ox == px + pw and oy < py + ph and py < oy + oh
                        for _id, (ox, oy, ow, oh) in items if _id != panel_id)
            if right and pw >= 2:
                for j in range(ph):
                    buf.set(px + pw - 1, py + j, "│", fg=DIM_FG, bg=PANEL_BG)

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
        variant = str(self.settings.get("ui.statusline") or "full")
        if variant == "off":
            return  # пустая строка: только фон (настройка ui.statusline)
        phase = _moon.context_phase(self.context_pct, self.glyph_mode)
        pct = str(self.context_pct)
        if variant == "compact":
            left = " " + phase + " контекст " + pct + "% · " + self.model
            buf.text(0, y, left, fg=_color.SILVER, bg=STATUS_BG)
            right = "$" + format(self.cost, ".4f")
            rx = max(str_width(left) + 1, cols - str_width(right) - 1)
            if rx < cols:
                buf.text(rx, y, right, fg=DIM_FG, bg=STATUS_BG, attrs=_color.DIM)
            return
        left = " " + phase + " контекст " + pct + "% · " + self.mode + " · " + self.model
        if self.layout.leaves() and not self.panel_focus:
            left += " · F6 — панели"
        if self.settings.get("ui.vim_mode"):
            left += " · vim:" + ("normal" if self._vim_normal else "insert")
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

    def _panel_width(self, cols: int, now: float) -> int:
        """Ширина колонки панелей; появление и закрытие — мягко (ui.animations).

        Анимация стартует от момента переключения (якорь ставит
        toggle_panel), а не от первого кадра: разовый рендер без истории
        кадров (тест, снимок, чужие часы) показывает сразу конечную ширину.
        Закрывающуюся колонку рисуем по «призраку» — снимку дерева до
        закрытия: само дерево уже пусто, а ширина ещё едет к нулю.
        """
        target = 0
        if self.layout.leaves() and cols >= 48:
            # Ручная ширина колонки (перетаскивание, Shift+←/→) побеждает
            # долю по умолчанию и живёт в раскладке (ЗАДАЧА-18).
            if self.layout.column_w:
                target = min(max(16, self.layout.column_w), max(24, cols - 24))
            else:
                target = max(24, cols // 3)
        if self.layout.leaves():
            self._panel_ghost = self.layout.clone()
        if not self.settings.get("ui.animations"):
            self._panel_anim = None
            self._panel_shown_w = float(target)
            return target
        if self._panel_anchor is not None:
            from_w, t0 = self._panel_anchor
            self._panel_anchor = None
            if float(target) != from_w:
                self._panel_anim = (from_w, float(target), t0)
        if self._panel_anim is None:
            self._panel_shown_w = float(target)
            return target
        from_w, to_w, t0 = self._panel_anim
        raw = (now - t0) / _PANEL_ANIM_SEC
        if raw < -0.05 or raw >= 1.0:
            self._panel_anim = None
            if to_w == 0:
                self._panel_ghost = None
            self._panel_shown_w = to_w
            return int(to_w)
        value = from_w + (to_w - from_w) * ease_out(max(0.0, raw))
        self._panel_shown_w = value
        return int(round(value))

    def draw(self, cols: int, rows: int, now: float):
        # Тема — на лету: настройку ui.theme могли сменить /theme, тулом
        # luna или правкой файла; палитра применяется до отрисовки кадра.
        theme_name = str(self.settings.get("ui.theme") or "luna")
        if theme_name != self._applied_theme:
            _themes.apply_theme(theme_name)
            self._applied_theme = theme_name
        self._geom_cols = cols
        buf = Buffer(cols, rows)
        buf.clear(bg=_color.SPACE_BG)
        panel_w = self._panel_width(cols, now)
        nib_h = 1 if (self._nibbler_line and now < self._nibbler_until) else 0
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
        completion_h = 0
        if self.cmd_completions:
            completion_h = min(5, len(self.cmd_completions))
        elif self.completions:
            completion_h = min(5, len(self.completions))
        meta_lines = self._meta_lines(now)
        meta_h = len(meta_lines)
        feed_h = max(1, rows - 1 - input_h - completion_h - nib_h - meta_h)
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
            tree = self.layout if self.layout.leaves() else (self._panel_ghost or self.layout)
            total_h = feed_h + input_h + completion_h + nib_h + meta_h
            hint_lines: list = []
            if self.panel_focus and self.layout.leaves():
                # В режиме панелей внизу колонки — подсказка управления.
                hint_lines = wrap_text(self._PANEL_HINT, max(4, panel_w - 2))[:5]
            self._draw_panels(buf, chat_w, 0, panel_w,
                              total_h - len(hint_lines), now, tree=tree)
            for i, line in enumerate(hint_lines):
                buf.text(chat_w + 1, total_h - len(hint_lines) + i, line,
                         fg=DIM_FG, bg=PANEL_BG, attrs=_color.DIM)
        else:
            self._panel_rects = {}
        cy = feed_h
        if self.cmd_completions:
            for i, (name, desc) in enumerate(self.cmd_completions[:completion_h]):
                style_fg = _color.CYAN if i == self.completion_index else DIM_FG
                attrs = _color.BOLD if i == self.completion_index else _color.DIM
                buf.text(2, cy + i, "/" + name + " — " + desc,
                         fg=style_fg, bg=_color.SPACE_BG, attrs=attrs)
        else:
            for i, candidate in enumerate(self.completions[:completion_h]):
                style_fg = _color.CYAN if i == self.completion_index else DIM_FG
                attrs = _color.BOLD if i == self.completion_index else _color.DIM
                buf.text(2, cy + i, "@" + candidate, fg=style_fg, bg=_color.SPACE_BG, attrs=attrs)
        if nib_h:
            # Строка Ниблера — между лентой и вводом; в конце жизни затухает.
            text, severity = self._nibbler_line
            fg = _color.ERR if severity == "serious" else _color.VIOLET
            left = self._nibbler_until - now
            if left < 1.5:
                fg = _color.lerp(_color.SPACE_BG, fg, max(0.0, left / 1.5))
            label = "🐾 Ниблер: " if self.glyph_mode == "emoji" else "Ниблер: "
            buf.text(2, feed_h + completion_h, label + text, fg=fg,
                     bg=_color.SPACE_BG, attrs=_color.ITALIC)
        my = feed_h + completion_h + nib_h
        for i, line in enumerate(meta_lines):
            # Подсказка, очередь и режим отправки — строкой над вводом.
            buf.text(2, my + i, _cells_slice(line, 0, max(3, chat_w - 3)),
                     fg=DIM_FG, bg=_color.SPACE_BG, attrs=_color.DIM)
        iy = my + meta_h
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


def _clipboard_text() -> str:
    """Текст системного буфера обмена, если платформа его отдаёт без библиотек.

    Только по явному Ctrl+V (не опрос): Windows — PowerShell Get-Clipboard,
    macOS — pbpaste, Linux — wl-paste/xclip, если стоят. Не вышло — "".
    """
    import shutil
    import subprocess
    import sys

    commands = []
    if sys.platform == "win32":
        commands.append(["powershell", "-NoProfile", "-Command",
                         "Get-Clipboard -Raw"])
    elif sys.platform == "darwin":
        commands.append(["pbpaste"])
    else:
        for tool, args in (("wl-paste", ["-n"]), ("xclip", ["-selection",
                                                           "clipboard", "-o"])):
            if shutil.which(tool):
                commands.append([tool, *args])
    for cmd in commands:
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=3)
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0 and proc.stdout:
            return proc.stdout.decode("utf-8", "replace")
    return ""


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
    # Уборка сессий (ЗАДАЧА-17): корзина самоочищается по сроку, пустые
    # сессии (ни одной реплики человека) уходят сами — при старте все,
    # при выходе ещё и текущая проверка не нужна: текущую не трогаем.
    try:
        from ..core.sessions import drop_empty_sessions, purge_trash

        purge_trash(data, int(settings.get("chat.sessions_trash_days") or 7))
        if settings.get("chat.sessions_drop_empty"):
            drop_empty_sessions(data)
    except Exception:  # noqa: BLE001 — уборка не имеет права ронять старт
        pass
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
                    if app._redraw:
                        # Ctrl+L: следующий кадр — целиком, не диффом.
                        app._redraw = False
                        screen.resize(*term.size())
                    if app.edit_request is not None:
                        _run_external_editor(app, term, screen, trap)
                    buf, cursor = app.draw(size[0], size[1], time.monotonic())
                    screen.present(buf, cursor=cursor)
            finally:
                trap.uninstall()
                current = app.journal if app is not None else journal
                if current is not None:
                    current.close()
                # При выходе пустые сессии уходят сами; текущую (только
                # что закрытую) не трогаем: keep — её путь.
                try:
                    if settings.get("chat.sessions_drop_empty"):
                        from ..core.sessions import drop_empty_sessions

                        keep = str(current.path) if current is not None else None
                        drop_empty_sessions(data, keep_path=keep)
                except Exception:  # noqa: BLE001 — выход важнее уборки
                    pass
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
