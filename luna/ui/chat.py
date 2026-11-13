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
from . import thinking_view as _think
from .anim import PhraseWheel, Scheduler, Spinner, ease_in_out, ease_out
from .editor import TextEditor
from .keyhints import find_key_hints
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
_TIDE_SHINE_SEC = 1.6    # блик по разделителю прилива после сжатия
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
    ("pult", "пульт ИИ: провайдеры, скан сети, ключи, модели и роли"),
    ("newmoon", "Новолуние: тёмный режим без следов, каждая модель — с твоего подтверждения"),
    ("update", "обновить Луну с GitHub и применить на лету, сессия не теряется; /update check — только проверить"),
    ("reload", "горячая перезагрузка: свежий код с диска, сессия и настройки остаются (и в Новолунии)"),
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
    ("copy", "скопировать последний ответ; /copy code — последний блок кода из ответа; /copy err — последнюю ошибку"),
    ("sessions", "выбиралка сессий на диске"),
    ("new", "начать новую сессию"),
    ("rename", "имя текущей сессии: /rename <имя>"),
    ("manners", "выбиралка манер речи"),
    ("undo", "откатить последний ход"),
    ("rewind", "откат к ходу: /rewind [N] [files|talk|both]; без аргументов — выбиралка ходов"),
    ("redo", "вернуть откат"),
    ("compact", "сжать контекст: /compact [что сохранить]"),
    ("clear", "очистить ленту"),
    ("panels", "выбиралка панелей-тайлов (пробел — открыть/закрыть, не закрывая список); /panels reset — забыть запомненную раскладку"),
    ("send", "режим отправки во время хода — выбиралка: inject/queue/interrupt (на лету — Alt+S)"),
    ("panel", "панель по имени: /panel <имя>"),
    ("layout", "раскладки панелей: /layout save|load|list <имя>"),
    ("todo", "панель «План»"),
    ("mcp", "MCP-серверы: выбиралка статуса и включения"),
    ("agents", "экипаж: главный агент, спутники и ассистенты"),
    ("cells", "профили ячеек роя: выбиралка, Enter — карточка, e — правка в редакторе"),
    ("theme", "тема оформления: палитры luna, amoled, contrast, mono, light и свои из LUNA_HOME/themes"),
    ("editor", "сочинить сообщение во внешнем редакторе"),
    ("details", "детали ленты вкл/выкл: вывод тулов свёрнуто или скрыто"),
    ("thinking", "показ рассуждений: /thinking [summary|full|hidden] — по кругу summary → hidden → full, на лету — Alt+O"),
    ("effort", "усилие рассуждений: /effort [роль] [уровень] — выбиралка; шкала auto/none/minimal/low/medium/high/xhigh/max, на лету — Alt+E"),
    ("delete", "текущую сессию — в корзину и начать новую (повтор команды — подтверждение; вернуть — u в /sessions)"),
    ("btw", "вопрос в сторону, не сбивая ход: /btw <вопрос>"),
    ("bug", "отчёт об ошибке файлом: версия, окружение, ошибки (без секретов)"),
    ("diff", "панель «Git»: ветка, изменения, дифф"),
    ("add-dir", "добавить каталог для @-подсказок: /add-dir <путь>"),
    ("exit", "выход"),
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
    "пульт": "pult", "провайдеры": "pult",
    "обновить": "update", "обновление": "update", "перезагрузка": "reload",
    "новолуние": "newmoon",
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
    "отправка": "send",
    "панель": "panel",
    "раскладка": "layout",
    "план": "todo",
    "серверы": "mcp",
    "агенты": "agents",
    "тема": "theme",
    "редактор": "editor",
    "детали": "details",
    "рассуждения": "thinking",
    "усилие": "effort",
    "удалить": "delete",
    "мимо": "btw",
    "ошибка": "bug",
    "дифф": "diff",
    "каталог": "add-dir",
    "выход": "exit",
}
_RU_BY_LATIN: dict = {}
for _ru, _lat in COMMAND_RU.items():
    _RU_BY_LATIN.setdefault(_lat, _ru)

# Латинские алиасы — привычки других агентов: /resume у Claude Code и
# Codex — это наши /sessions. Свои алиасы — настройка ui.command_aliases.
COMMAND_ALIASES = {
    "resume": "sessions", "history": "sessions",
    "providers": "pult", "provider": "pult", "ai": "pult", "setup": "pult",
    "dark": "newmoon", "incognito": "newmoon", "nm": "newmoon",
    "quit": "exit", "q": "exit",
    "models": "model", "h": "help", "?": "help",
    "cls": "clear", "reset": "new",
    "upgrade": "update", "hot": "reload", "restart": "reload",
}
_USER_ALIASES: dict = {}


def set_user_aliases(spec) -> dict:
    """Свои алиасы из настройки: «resume=sessions, мод=model».

    Имя слева — новое, справа — существующая команда (или её алиас).
    Плохие пары молча пропускаем: настройка не должна ронять чат.
    """
    _USER_ALIASES.clear()
    pairs = spec.items() if isinstance(spec, dict) else         (part.split("=", 1) for part in str(spec or "").split(",") if "=" in part)
    for alias, target in pairs:
        alias = str(alias).strip().lstrip("/").lower()
        target = resolve_command(str(target).strip().lstrip("/"))
        if alias and target and alias not in COMMAND_DESCR:
            _USER_ALIASES[alias] = target
    return dict(_USER_ALIASES)


def all_aliases(ru: bool = True) -> dict:
    """Все алиасы: русские имена (если ru), латинские и свои — имя → команда."""
    return {**(COMMAND_RU if ru else {}), **COMMAND_ALIASES, **_USER_ALIASES}


def resolve_command(word: str, layouts: bool = True):
    """Имя команды как набрали → каноническое латинское или None.

    Понимает латинские имена, русские имена и любое из них, набранное
    не в той раскладке (зфтуд → panel, gfytkm → панель → panel).
    """
    w = (word or "").strip().lower()
    if not w:
        return None
    if w in COMMAND_DESCR:
        return w
    # Без layouts (ui.layout_fix выключен) — ни раскладки, ни русских имён:
    # так решено в ЗАДАЧЕ-17; латинские и свои алиасы работают всегда.
    aliases = all_aliases(ru=layouts)
    if w in aliases:
        return aliases[w]
    if not layouts:
        return None
    for cand in _layout.variants(w):
        c = cand.lower()
        if c in COMMAND_DESCR:
            return c
        if c in aliases:
            return aliases[c]
    return None
# Темы оформления (/theme, настройка ui.theme) — палитры данными:
# встроенные и свои из LUNA_HOME/themes, см. luna/ui/themes.py (ЗАДАЧА-18).

HELP_TEXT = """Команды и клавиши Луны
""" + "\n".join("/" + name + " — " + desc for name, desc in COMMANDS) + """
Выбиралки: печатать — поиск · ↑↓ — выбор · Enter — действие · Esc — закрыть · мышь: клик выбирает, колесо крутит, наведение подсвечивает
/panels — все тайлы выбиралкой: пробел открывает/закрывает панель сразу, список не закрывается · Alt+M/L/B/F/N — Луна, Лимиты, Обратная сторона, Файлы, Ниблер · Alt+C/G/P/T — Контекст, Git, Процессы, Дерево
Команды не зависят от раскладки (ui.layout_fix): /панель = /panel, а .зфтуд в ЙЦУКЕН — тоже /panel; аргументы и Ctrl/Alt-клавиши понимаются в любой раскладке
Ctrl+R — поиск по истории ввода · Ctrl+L — перерисовать экран · Ctrl+V — вставить (картинка — по пути к файлу из буфера) · Ctrl+G — промпт во внешнем редакторе · ? на пустом вводе — эта справка
Правка как в readline: Ctrl+A/E — начало/конец строки · Ctrl+U/K — вырезать до начала/конца строки · Ctrl+W — путь/слово до пробела назад · Ctrl+Backspace / Alt+Backspace — слово назад · Ctrl+Delete / Alt+D — слово вперёд · Ctrl+Y — вернуть вырезанное, Alt+Y следом — по кольцу дальше · Ctrl+←/→ — по словам (до знаков препинания) · Alt+←/→ — по словам до пробела · Ctrl+P/N — история назад/вперёд · Ctrl+_ (Ctrl+Shift+-) — отмена правки ввода · Ctrl+Z — свернуть в фон (Unix; fg — вернуться)
Esc Esc на пустом вводе — откат к ходу выбиралкой, с текстом — убрать набросок в историю (↑ вернёт; Ctrl+C тоже не теряет) · Esc во время хода — прервать ход одним нажатием · Ctrl+C во время хода — прервать; без хода — очистить ввод, второй подряд за секунду — выход
Отправка во время хода (chat.send_mode, видна в строке состояния): inject — вставка в ход · queue — очередь · interrupt — прервать и отправить · Alt+S — сменить режим по кругу · /send — выбиралка · Alt+Enter — разово прервать и отправить · ↑ на пустом поле — вернуть сообщение из очереди
Ctrl+S — спрятать промпт в заначку (как в Claude Code): на пустом поле — вернуть с курсором, на непустом — поменять местами
Мышление: /thinking — показ рассуждений по кругу summary (шапка «думает» и пересказ быстрой роли) → hidden → full, Alt+O — тот же круг на лету; /effort — глубина рассуждений, Alt+E — уровень по кругу на лету
!команда — выполнить самому, без вопросов Предохранителя: вывод — в контекст, ход модели — по настройке chat.bang_triggers · #текст — дописать в AGENTS.md проекта · @файл — вложить файл (нечёткий поиск; Tab — дополнить; Tab после / — дополнить команду)
Enter — отправить · Alt+Enter, Shift+Enter и \\+Enter — новая строка · стрелки — история и курсор · Tab / Shift+Tab — режим по кругу
Ctrl+O — развернуть/Свернуть ВСЕ блоки ленты (настройка ui.expand_all) · Alt+↑/↓ — курсор по блокам, Enter/пробел на пустом вводе — раскрыть блок · клик по заголовку блока (▸/▾) — только его, мысли — клик по любой их строке · PageUp/PageDown — лента, Ctrl+Home/End — к началу/к свежему; прокрученная лента стоит, пока Луна пишет (ui.scroll_hold)
Строка состояния кликабельна: режим, модель, effort, мышление, отправка и тема открывают свои выбиралки · клик по ссылке/пути в ленте — по настройке ui.click_link · мышь — ui.mouse (full/scroll/off)
Раскладка панелей запоминается (ui.remember_layout): /panels reset — забыть · F6 — в режим панелей (панели открыты) или открыть «План»
В режиме панелей: ↑↓ — фокус, ←→ — размер, Tab / Shift+Tab — переставить, Enter — развернуть, x — закрыть, Esc — назад к вводу; мышь: клик по заголовку — фокус, тянуть заголовок — переставить, разделитель — размер, ✕ — закрыть, колесо — прокрутка
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


def _split_painted(spans: list, width: int) -> list:
    """Раскрашенную строку [(текст, fg, attrs)] — в куски не шире width;
    продолжения с отступом в два пробела, цвета сохраняются."""
    # Широкий символ — не больше двух ячеек: короткой строке мерить нечего.
    if sum(len(t) for t, _f, _a in spans) * 2 <= width \
            or sum(str_width(t) for t, _f, _a in spans) <= width:
        return [spans]
    out, cur, used = [], [], 0
    for text, fg, attrs in spans:
        piece = ""
        for ch in text:
            cw = char_width(ch)
            if used + cw > width:
                if piece:
                    cur.append((piece, fg, attrs))
                out.append(cur)
                cur, used, piece = [("  ", fg, 0)], 2, ""
                if ch == " ":
                    continue
            piece += ch
            used += cw
        if piece:
            cur.append((piece, fg, attrs))
    if cur and any(t.strip() for t, _f, _a in cur):
        out.append(cur)
    return out


def _tide_spans(entry, width: int, now: float, anim: bool, glyph_mode: str) -> list:
    """Строка прилива (/compact): пока идёт сводка — бегущая волна,
    потом — разделитель «прилив · было → стало» с одним бликом."""
    import math

    waves = ".~=" if glyph_mode == "ascii" else "·~≈"
    if entry.streaming:
        label = " прилив: сводка старых ходов… "
    else:
        label = " прилив · " + entry.text + \
            (" · ходов в сводке: " + entry.summary if entry.summary else "") + " "
    width = max(4, width)
    label = label[:max(0, width - 4)]
    lw = str_width(label)
    left = max(0, (width - lw) // 2)
    shine = None
    if not entry.streaming and anim and entry.finished_at:
        t = now - entry.finished_at
        if 0 <= t < _TIDE_SHINE_SEC:
            shine = t / _TIDE_SHINE_SEC * (width + 12) - 6
    cells = []
    for x in range(width):
        if left <= x < left + lw:
            continue
        if entry.streaming:
            phase = now * 5.0 if anim else 0.0
            v = math.sin(x * 0.45 - phase) + 0.5 * math.sin(x * 0.17 + phase * 0.45)
            ch = waves[2] if v > 0.8 else (waves[1] if v > -0.3 else waves[0])
            style = "cyan" if v > 0.8 else ("comment" if v > -0.3 else "dim")
        else:
            ch = waves[2]
            style = "cyan" if shine is not None and abs(x - shine) < 3 else "dim"
        cells.append((x, ch, style))
    spans, cur, cur_style = [], "", None
    lab_style = "cyan" if entry.streaming or shine is not None else "notice"

    def push(text, style):
        nonlocal cur, cur_style
        if style != cur_style and cur:
            spans.append((cur, cur_style))
            cur = ""
        cur_style = style
        cur += text

    it = iter(cells)
    nxt = next(it, None)
    x = 0
    while x < width:
        if x == left and lw:
            push(label, lab_style)
            x += lw
            continue
        if nxt is not None and nxt[0] == x:
            push(nxt[1], nxt[2])
            nxt = next(it, None)
        x += 1
    if cur:
        spans.append((cur, cur_style))
    return spans


_INC_MIN = 2000  # короче — рендерим целиком, дешевле самой бухгалтерии
_PARA = "\n\n"


def _safe_cut(text: str) -> int:
    """Последняя граница абзаца вне блока кода (-1 — нет такой)."""
    cut = text.rfind(_PARA)
    while cut > 0 and text.count("```", 0, cut) % 2:
        cut = text.rfind(_PARA, 0, cut)
    return cut


def _incremental(entry, tag: str, text: str, width: int, render, gap) -> list:
    """Строки растущего текста: готовые абзацы — из памяти, заново — хвост.

    Стрим растёт на каждом кадре; полный перерендер десятков тысяч
    символов на кадр вешал экран, когда модель (или несколько) писала
    быстро. Абзацы до последней пустой строки уже не меняются.
    gap — как приклеить хвост: None — хвост рендерится с ведущей пустой
    строкой (markdown), иначе — строка-разделитель (wrap_text).
    """
    if not entry.streaming or len(text) < _INC_MIN:
        return render(text)
    cut = _safe_cut(text)
    if cut <= 0:
        return render(text)
    inc = getattr(entry, "_inc", None)
    if inc is not None and inc[0] == (tag, width, cut) and text.startswith(inc[1]):
        head = inc[2]
    else:
        head = render(text[:cut])
        entry._inc = ((tag, width, cut), text[:cut], head)
    if gap is None:
        return head + render(text[cut:])
    return head + [gap] + render(text[cut + 2:])


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


_LINK_RE = re.compile(
    r"https?://\S+|(?:[\w@~.-]+/)+[\w.-]+"
    r"|[\w.-]+\.(?:py|md|txt|json|toml|html|js|ts|sh|yaml|yml|log)\b")


def _find_links(text: str) -> list:
    """Ссылки и пути файлов в строке ленты: (колонка_от, колонка_до, токен)
    в ячейках — для кликабельных зон (клик — по ui.click_link)."""
    out = []
    for m in _LINK_RE.finditer(text or ""):
        token = m.group(0).rstrip(").,;»\"'")
        if not token:
            continue
        start = str_width(text[:m.start()])
        out.append((start, start + str_width(token) - 1, token))
    return out


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
    # Мышление (ЗАДАЧА-19): строки пересказа быстрой ролью — пары
    # (текст, момент рождения) для градиента появления; finished_at —
    # когда рассуждение кончилось; chunk_at — последний кусок (вспышка звёзд).
    summary_lines: list = field(default_factory=list)
    finished_at: float = 0.0
    chunk_at: float = 0.0
    # Стрим с проявлением (ЗАДАЧА-20): отметки кусков текста ответа —
    # (длина текста после куска, момент прихода); fade красит хвост по ним.
    chunk_marks: list = field(default_factory=list)
    # Кэш отрисовки (ЗАДАЧА-22): готовые визуальные строки неизменной
    # записи и их плоский текст; ключ — в ChatApp._entry_cache_key.
    _cache_key: object = field(default=None, repr=False, compare=False)
    _cache_lines: object = field(default=None, repr=False, compare=False)
    _cache_texts: object = field(default=None, repr=False, compare=False)


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
        self._pult = None  # пульт ИИ (ui/pult.py) — создаётся первым /pult
        from .updater import Updater

        self.updater = Updater(self)
        self.relaunch = ""  # "newmoon" — перезапуск в Новолунии, "reload" — горячая перезагрузка
        self.handoff = None  # снимок сессии для горячей перезагрузки (ui/updater.py)
        # Записи сессии в памяти, когда журнала нет (Новолуние, журнал
        # выключен): из них горячая перезагрузка поднимет ленту и память.
        self.tape: list = []
        self._nm_stash = None  # сообщение, ждущее подтверждения модели
        self._nm_extra: list = []  # адреса вне ролей (запасные, пулы), просившие подтверждения
        self.edit_request = None      # путь файла, который run_chat откроет в редакторе
        self._edit_manner_id = ""
        self._pending_switch = None   # путь сессии: переключиться по концу хода
        self._pending_new = False     # новая сессия по концу хода
        # Удаление текущей сессии (ЗАДАЧА-19): по концу хода / подтверждение.
        self._pending_delete_current = False
        self._last_deleted_current = None  # путь: u в /sessions вернёт и переключит
        self._delete_confirm_until = 0.0
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
        # ЗАДАЧА-20: мышь везде, блоки ленты, память раскладки, заначка,
        # свёрнутые вставки, уведомления и заголовок окна.
        self.expand_all = bool(self.settings.get("ui.expand_all"))
        self.zones: list = []            # карта кликабельных зон последнего кадра
        self._hover_pos = None           # где мышь: подсветка подсказки клавиши
        self._line_map: list = []        # визуальная строка ленты → (запись, первая?)
        self._block_sel = None           # запись под курсором блоков (Alt+↑/↓)
        self._stash: tuple | None = None  # (текст, курсор) заначки Ctrl+S
        self._guard_replace = False      # Предохранитель: «нет, и вот что вместо»
        self._pastes: dict = {}          # «[вставлено N строк]» → полный текст
        self._send_hint_shown = False    # подсказка о режиме отправки — раз за сессию
        self._saved_places: dict = {}    # панель → (индекс, вес) из памяти раскладки
        self._saved_cols = 0             # ширина терминала при сохранении раскладки
        self._panel_head_drag = None     # перетаскивание заголовка панели
        self.session_title = ""          # тема сессии для заголовка окна
        self._session_titled = False     # имя задано (руками или автоматом)
        self.color_level = "truecolor"   # ставит run_chat из возможностей терминала
        # ЗАДАЧА-22: полоса прокрутки и жесты ленты (состояние жеста и
        # геометрия полосы последнего кадра), замеры кадров для ui.perf.
        self._sb_drag = None             # (смещение захвата в ползунке)
        self._sb_geom = None             # (всего строк, высота ленты, макс. прокрутка, верх ползунка, высота ползунка)
        self._feed_drag = None           # жест перетаскивания ленты / долгого нажатия
        self._frame_times: list = []     # длительности последних кадров, с
        self._auto_low = False           # слабый режим, включённый автоматически
        self._perf_warned = False        # предупреждение о слабом режиме — раз за сессию
        self.notify_hook = None          # run_chat: строка в терминал (звонок/OSC)
        self.title_hook = None           # run_chat: заголовок окна (OSC 2)
        self.open_hook = None            # run_chat: открыть ссылку/файл снаружи
        self.suspend_hook = None         # run_chat (POSIX): Ctrl+Z — фон/fg
        self._page = 10
        self.spinner = Spinner(self.glyph_mode)
        self.wheel = PhraseWheel(phrases.spinner_phrases())
        self._applied_theme = _themes.apply_theme(
            str(self.settings.get("ui.theme") or "luna"))
        # Память раскладки (ЗАДАЧА-20): только в настоящем сеансе (журнал
        # или явно переданный каталог данных) — витрины и снимки не трогаем.
        if self._journal is not None or data is not None:
            self._restore_saved_layout()

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
        self.updater.tick()
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
                if rec.get("kind") == "title":
                    # Имя сессии уже задано — автоназвание не перезапишет.
                    self.session_title = str(rec.get("title") or self.session_title)
                    self._session_titled = True
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

    def _restore_prompt(self, text: str) -> None:
        """/undo: промпт откатанного хода — обратно в поле ввода (ЗАДАЧА-20).

        Если в поле уже что-то набрано, оно не затирается молча, а уходит
        в историю ввода (стрелка вверх вернёт), как черновики у эталонов.
        """
        if not text:
            return
        if self.editor.text.strip():
            self.editor.push_history(self.editor.text)
        self.editor.set_text(text)
        self._flash_hint("Промпт откатанного хода — в поле ввода "
                         "(прежний набросок — в истории, ↑).")

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
        elif not self._restoring:
            self.tape.append({"kind": "command", "data": dict(cmd)})
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

    def delete_current_session(self) -> None:
        """/delete: текущую сессию — в корзину, сразу новая пустая.

        Подтверждение — повторной командой в течение 10 секунд (как
        двойной Ctrl+C): первое /delete спрашивает, второе выполняет.
        Идущий ход сначала прерывается, удаление — по его концу.
        """
        if self.journal is None:
            self._add(Entry("notice", text="Журнал выключен — удалять нечего."))
            return
        if self.busy:
            self._pending_delete_current = True
            self.send_command({"type": "interrupt"})
            if self._pending_delete_current:
                self._add(Entry("notice", text="Прерываю ход — и удаляю "
                                               "текущую сессию."))
            return
        now = self._clock()
        if now > self._delete_confirm_until:
            self._delete_confirm_until = now + 10.0
            self._add(Entry("notice", text="Удалить текущую сессию в корзину "
                                           "и начать новую? Повтори /delete "
                                           "в течение 10 секунд."))
            return
        self._delete_confirm_until = 0.0
        self._do_delete_current()

    def _do_delete_current(self) -> None:
        """Само удаление текущей: журнал закрыт, файл с чекпоинтами — в
        корзину ЗАДАЧИ-17 (sessions/.trash), дальше — как /new."""
        from ..core.sessions import trash_session

        journal = self.journal
        if journal is None:
            return
        path = journal.path
        self.journal = None
        try:
            journal.close()
        except Exception:
            pass  # журнал уже закрыт — не страшно, файл на месте
        moved = None
        try:
            moved = trash_session(self._data_dir(), path)
        except OSError:
            moved = None
        self._last_deleted_current = str(path)
        self._do_new_session()
        if moved is not None:
            self._add(Entry("notice", text="Прежняя сессия — в корзине. "
                                           "Вернуть её: /sessions, клавиша u "
                                           "— и чат переключится обратно."))
        else:
            self._add(Entry("notice", text="Файл прежней сессии в корзину не "
                                           "уехал (его уже не было), но новая "
                                           "сессия начата."))

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
        self.session_title = title
        self._session_titled = True
        self._update_title()
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

    def _panels_items(self) -> list:
        items = []
        for spec in registered_panels().values():
            opened = spec.panel_id in self.layout
            items.append(PickerItem(
                key=spec.panel_id, title=spec.title,
                detail=spec.panel_id + " · " + ("открыта" if opened else "закрыта"),
                mark="→" if opened else ""))
        return items

    def open_panels_picker(self) -> None:
        """/panels: переключатель тайлов, НЕ закрывается после действия
        (ЗАДАЧА-20): пробел/клик открывают и закрывают панели сразу,
        раскладка за списком перестраивается вживую."""
        self._open_picker("panels", "Панели", self._panels_items(),
                          hint="пробел/клик — открыть/закрыть сразу · "
                               "↑↓/Tab — ходить · Enter/Esc — выйти",
                          preview_lines=0)

    def _toggle_panel_from_picker(self, item) -> None:
        """Пробел или клик в /panels: панель — сразу, список остаётся."""
        if item is None or not item.key:
            return
        self.toggle_panel(item.key)
        if self.picker is not None:
            self.picker.set_items(self._panels_items())

    def open_mode_picker(self) -> None:
        """Режим агента выбиралкой: тот же путь, что Tab и /mode —
        команда mode.set источнику; human_only не обходится."""
        items = [PickerItem(key=m, title=m,
                            detail="режим Предохранителя",
                            mark="→" if m == self.mode else "")
                 for m in MODES]
        self._open_picker("mode", "Режим агента", items,
                          hint="Enter — включить · Esc — закрыть",
                          preview_lines=0)

    def open_send_picker(self) -> None:
        """/send: режим отправки во время хода выбиралкой."""
        current = self._send_mode()
        items = [PickerItem(key=m, title=m + " — " + self._SEND_MODE_TITLES[m],
                            mark="→" if m == current else "")
                 for m in ("inject", "queue", "interrupt")]
        self._open_picker("send", "Отправка во время хода", items,
                          hint="Enter — включить · Esc — закрыть · "
                               "на лету — Alt+S",
                          preview_lines=0)

    def open_thinking_picker(self) -> None:
        """Показ рассуждений выбиралкой (клик по строке состояния)."""
        current = self._thinking_mode()
        items = [PickerItem(key=m, title=m,
                            detail=self._THINKING_TITLES[m],
                            mark="→" if m == current else "")
                 for m in self._THINKING_ORDER]
        self._open_picker("thinking", "Показ рассуждений", items,
                          hint="Enter — включить · Esc — закрыть",
                          preview_lines=0)

    def open_help_picker(self) -> None:
        """/help: команды выбиралкой с поиском, не простынёй текста.

        Русские имена — отдельными пунктами с тем же ключом команды:
        поиск находит оба языка, Enter выполняет каноническую команду.
        В режим панелей — F6 (панели открыты) или Alt+буква панели.
        """
        items = [PickerItem(key=name, title="/" + name, detail=desc)
                 for name, desc in COMMANDS]
        set_user_aliases(self.settings.get("ui.command_aliases") or "")
        items += [PickerItem(key=lat, title="/" + ru,
                             detail=COMMAND_DESCR[lat] + " — то же, что /" + lat)
                  for ru, lat in all_aliases().items() if lat in COMMAND_DESCR]
        self._open_picker("help", "Команды Луны", items,
                          hint="печатайте — поиск · Enter — выполнить "
                               "или вставить заготовку · Esc — закрыть · "
                               "панели: F6 или Alt+M",
                          preview_lines=0)

    # --- Новолуние (core/newmoon.py) ------------------------------------------

    def newmoon_command(self, arg: str = "") -> None:
        """/newmoon: вне режима — перезапуск в Новолунии (эта сессия
        остаётся как есть); в режиме — подтверждения адресов."""
        from ..core import newmoon as _nm

        if _nm.active():
            self.open_newmoon_confirm()
            return
        if self.busy:
            self._add(Entry("notice", text="Идёт ход — дождись конца или прерви (Esc), "
                                           "потом /newmoon."))
            return
        self.relaunch = "newmoon"
        self.done = True

    def _nm_targets(self) -> list:
        """[(адрес, модель, [роли])] — куда пойдут запросы ролей и просившие сами."""
        from ..core.secrets import Secrets
        from ..router.core import Router

        try:
            router = Router(self.settings, Secrets(self._data_dir().secrets))
        except Exception:  # noqa: BLE001
            return []
        found: dict = {}
        for role in ("main", "fast", "guard", "vision"):
            ref = router.role_ref(role)
            if not ref:
                continue
            try:
                cfg, model = router.resolve(ref)
            except Exception:  # noqa: BLE001 — роль без модели: пропускаем
                continue
            if cfg.kind == "fake":
                continue
            found.setdefault((cfg.base_url, model), []).append(role)
        for base, model in self._nm_extra:
            found.setdefault((base, model), []).append("по запросу")
        return [(b, m, roles) for (b, m), roles in found.items()]

    def open_newmoon_confirm(self, extra=None) -> None:
        """Подтверждение адресов: Enter по строке — разрешить туда запросы."""
        from ..core import newmoon as _nm

        if extra and extra[0] and extra not in self._nm_extra:
            self._nm_extra.append(extra)
        items = []
        for base, model, roles in self._nm_targets():
            ok = _nm.is_confirmed(base, model)
            items.append(PickerItem(
                key=_nm.target_key(base, model), payload=(base, model),
                title=("✓ " if ok else "○ ") + model + " → " + (base or "?"),
                detail="роли: " + ", ".join(roles) + (" · разрешено" if ok
                                                      else " · Enter — разрешаю отправлять туда")))
        items.append(PickerItem(key="__all__", title="Разрешить все из списка"))
        items.append(PickerItem(key="__pult__", title="Сменить модели — пульт ИИ"))
        self._open_picker("newmoon", "🌑 Новолуние — куда можно слать запросы", items,
                          hint="Без подтверждения ни один запрос не уйдёт · "
                               "Enter — разрешить · Esc — готово",
                          preview_lines=0)

    def _newmoon_picker_action(self, action) -> None:
        from ..core import newmoon as _nm

        if action[0] == "close":
            self.picker = None
            self._nm_release()
            return
        if action[0] != "pick" or action[1] is None:
            return
        item = action[1]
        if item.key == "__pult__":
            self.picker = None
            self.open_pult()
            return
        targets = [it.payload for it in self.picker.items if it.payload]             if item.key == "__all__" else [item.payload]
        for base, model in targets:
            _nm.confirm(base, model)
        self.open_newmoon_confirm()
        if self._nm_main_ok():
            self._nm_release()

    def _nm_main_ok(self) -> bool:
        from ..core import newmoon as _nm

        for base, model, roles in self._nm_targets():
            if "main" in roles and not _nm.is_confirmed(base, model):
                return False
        return True

    def _nm_release(self) -> None:
        """Отпустить сообщение, ждавшее подтверждения главной модели."""
        stash, self._nm_stash = self._nm_stash, None
        if stash is not None:
            if self._nm_main_ok():
                self.picker = None
                self._send_user_message(*stash)
            else:
                self._add(Entry("notice", text="Главная модель не подтверждена — "
                                               "сообщение не отправлено (оно в истории, ↑)."))

    def open_pult(self, arg: str = "") -> None:
        """/pult: провайдеры, скан сети, ключи и модели (ui/pult.py);
        /pult scan — сразу скан."""
        from .pult import Pult

        if self._pult is None:
            self._pult = Pult(self)
        if arg.strip() in ("scan", "скан"):
            self._pult.start_scan("fast")
        else:
            self._pult.open()

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
                          hint="Enter — выбрать модель роли · Esc — закрыть",
                          preview_lines=0)

    def _model_choice_items(self, role: str, remote: list) -> list:
        """Пункты выбора модели роли: свои действия, текущая, затем список API."""
        current = str(self.settings.get("model." + role) or "")
        items = [PickerItem(key="__manual__", title="✎ вписать ссылку вручную",
                            detail="«провайдер:модель» — для чего нет в списке")]
        if role != "main":
            items.append(PickerItem(key="__inherit__", title="как главная",
                                    detail="своей модели у роли нет",
                                    mark="" if current else "→"))
        seen = set()
        for ref in [current] + list(remote):
            if not ref or ref in seen:
                continue
            seen.add(ref)
            items.append(PickerItem(key=ref, title=ref,
                                    detail="сейчас" if ref == current else "",
                                    mark="→" if ref == current else ""))
        return items

    def open_model_choice(self, role: str) -> None:
        """Второй экран /model: модели провайдера списком с поиском и кликом.

        Список берём из API в фоне, чтобы экран открылся сразу, а сеть не
        вешала интерфейс; пока он едет, можно выбрать текущую или вписать."""
        import threading

        self._model_role = role
        self._open_picker(
            "modelpick", "Модель роли " + role, self._model_choice_items(role, []),
            hint="печатайте — поиск · Enter — выбрать · Esc — закрыть",
            preview_lines=0)
        picker = self.picker

        def fetch() -> None:
            try:
                from ..core.secrets import Secrets
                from ..router.core import Router

                router = Router(self.settings, Secrets(self._data_dir().secrets))
                models = router.fetch_models()
            except Exception:  # noqa: BLE001 — нет сети или ключа не роняет экран
                return
            if self.picker is picker:
                picker.set_items(self._model_choice_items(role, models))
                picker.hint = ("печатайте — поиск · Enter — выбрать · Esc — закрыть"
                               " · моделей в списке: " + str(len(models)))
                self._last_change = self._clock()

        threading.Thread(target=fetch, daemon=True).start()

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
                # Enter в /panels — тоже переключатель без закрытия (ЗАДАЧА-20).
                self._toggle_panel_from_picker(item)
            elif kind == "mode":
                self.picker = None
                self.send_command({"type": "mode.set", "mode": item.key})
            elif kind == "send":
                self.picker = None
                self.settings.set("chat.send_mode", item.key)
                self._add(Entry("notice", text="Отправка во время хода: "
                                               + self._SEND_MODE_TITLES[item.key]))
            elif kind == "thinking":
                self.picker = None
                self._set_thinking_mode(item.key)
                self._add(Entry("notice", text="Рассуждения: "
                                               + self._THINKING_TITLES[item.key]))
            elif kind == "help":
                self.picker = None
                if item.key in _NEEDS_ARG:
                    self.editor.set_text("/" + item.key + " ")
                else:
                    self._slash("/" + item.key)
            elif kind == "models":
                self.open_model_choice(item.key)
            elif kind == "modelpick":
                role = self._model_role
                if item.key == "__manual__":
                    current = str(self.settings.get("model." + role) or "")
                    self.picker.ask_input(
                        "Модель роли " + role + " («провайдер:модель» или "
                        "модель; пусто — как главная): ", ("role", role),
                        initial=current)
                else:
                    self.picker = None
                    ref = "" if item.key == "__inherit__" else item.key
                    self.settings.set("model." + role, ref)
                    self._add(Entry("notice", text="Модель роли " + role + ": "
                                    + (ref or "(как главная)") + " — со следующего хода."))
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
            elif kind == "effort":
                self.picker = None
                key = str(item.payload or "model.effort")
                self.settings.set(key, item.key)
                self._add(Entry("notice", text="Усилие рассуждений (" + key + "): "
                                               + item.key + " — со следующего "
                                               "вызова модели."))
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
            if kind in ("models", "modelpick") and ctx is not None and ctx[0] == "role":
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
        if self._picker_kind == "panels" and picker.mode == "list" \
                and not picker.filter:
            # /panels-переключатель (ЗАДАЧА-20): пробел — панель сразу,
            # Tab/Shift+Tab — ходить по списку; список не закрывается.
            if key.name in ("space", " ") or key.char == " ":
                self._toggle_panel_from_picker(picker.current())
                return
            if key.name == "tab":
                picker._move(-1 if "shift" in key.mods else 1)
                return
        action = picker.handle_key(key)
        if action is not None:
            self._route_picker_action(action)

    def _route_picker_action(self, action) -> None:
        """Действие выбиралки — общий путь для клавиш и кликов мыши."""
        kind = action[0]
        if self._picker_kind == "newmoon":
            self._newmoon_picker_action(action)
            return
        if self._picker_kind == "pult" and self._pult is not None:
            # Пульт ведёт свои экраны сам: Esc у него — шаг назад.
            self._pult.on_action(action)
            return
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
                                   "theme", "history", "rewind", "adddir", "cells",
                                   "effort", "mode", "send", "thinking", "modelpick"):
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
            elif inner and inner[0] == "trash_current":
                # Текущую — в корзину и сразу новую (ЗАДАЧА-19).
                self.picker = None
                if self.busy:
                    self._pending_delete_current = True
                    self.send_command({"type": "interrupt"})
                    if self._pending_delete_current:
                        self._add(Entry("notice", text="Прерываю ход — и удаляю "
                                                       "текущую сессию."))
                else:
                    self._do_delete_current()
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
            if (restored is not None and self._last_deleted_current
                    and str(restored) == self._last_deleted_current):
                # Возвращаем именно удалённую текущую — и переключаемся
                # обратно на неё (ЗАДАЧА-19).
                self._last_deleted_current = None
                self.picker = None
                self.switch_session(restored)
                return
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
                # Текущую можно (ЗАДАЧА-19): подтверждение — корзина —
                # сразу новая пустая сессия, как /new.
                picker.ask_confirm(
                    "Текущую сессию — в корзину и начать новую?",
                    ("trash_current", item))
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

    # --- производительность (ЗАДАЧА-22) --------------------------------------
    # Кадр на слабой машине не должен стоить дороже самой работы: готовые
    # строки неизменных записей кэшируются (_entry_lines), а если кадр
    # стабильно долгий, ui.perf=auto сам включает слабый режим.

    _PERF_FRAME_LIMIT = 0.045   # медиана кадра, с: выше — машина не тянет
    _PERF_FRAMES = 30           # сколько кадров смотреть для решения
    _LOW_INTERVAL = 0.12        # интервал кадра в слабом режиме, с
    _LONG_PRESS_SEC = 0.5       # долгое нажатие на ленту → выделение, с

    def _perf_setting(self) -> str:
        mode = str(self.settings.get("ui.perf") or "auto")
        return mode if mode in ("auto", "low", "full") else "auto"

    def _low_perf(self) -> bool:
        """Слабый режим действует: явно (ui.perf=low) или сам включился."""
        mode = self._perf_setting()
        if mode == "low":
            return True
        return mode == "auto" and self._auto_low

    def _anim_on(self) -> bool:
        """Анимации действуют: настройка включена и не слабый режим."""
        return bool(self.settings.get("ui.animations")) and not self._low_perf()

    def _note_frame_time(self, seconds: float) -> None:
        """Запомнить длительность кадра; стабильно долгие кадры при
        ui.perf=auto включают слабый режим (предупреждение — строкой
        в ленту один раз, её ставит draw в начале следующего кадра)."""
        self._frame_times.append(seconds)
        if len(self._frame_times) > 60:
            del self._frame_times[:-60]
        if self._perf_setting() != "auto" or self._auto_low:
            return
        if len(self._frame_times) < self._PERF_FRAMES:
            return
        recent = sorted(self._frame_times[-self._PERF_FRAMES:])
        median = recent[len(recent) // 2]
        if median > self._PERF_FRAME_LIMIT:
            self._auto_low = True

    def needs_frame(self, now: float) -> bool:
        """Кадры нужны, пока идёт ход, есть события или что-то затухает."""
        if self.busy:
            return True
        if self._feed_drag is not None and not self._feed_drag["moved"] \
                and not self._feed_drag["select"]:
            return True  # держим палец: ждём долгое нажатие → выделение
        if self._fade_busy(now):
            return True
        if self._panel_anim is not None:
            return True
        for entry in self.entries[-8:]:
            if entry.kind == "tide" and self._anim_on() and (
                    entry.streaming or now - (entry.finished_at or 0.0) < _TIDE_SHINE_SEC):
                return True
        if self._nibbler_line and now < self._nibbler_until + 0.3:
            return True
        if self.trap is not None and self.trap.pending:
            return True
        pending = getattr(self.source, "has_pending", None)
        if callable(pending) and pending():
            return True
        if self._anim_on():
            return now - self._last_change < 0.4
        return False

    def next_timeout(self):
        if not self.needs_frame(self._clock()):
            return self.updater.timeout()
        if self._low_perf():
            return max(self.scheduler.frame_interval, self._LOW_INTERVAL)
        return self.scheduler.frame_interval

    # --- события агента ----------------------------------------------------------

    def _add(self, entry: Entry) -> Entry:
        entry.born = self._clock()
        self.entries.append(entry)
        self._last_change = entry.born
        # Прокрутил вверх и читает — лента не утаскивает вниз сама; вниз
        # возвращает только своё сообщение (или выключенный ui.scroll_hold).
        if entry.kind == "user" or not self.settings.get("ui.scroll_hold", True):
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
        elif not self._restoring:
            self.tape.append({"kind": "event", "data": ev})
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
                turn = int(ev.get("turn") or 0)
                # /undo возвращает промпт откатанного хода в поле ввода
                # (ЗАДАЧА-20): текст берём из ленты ДО её обрезки.
                prompt = ""
                if not self._restoring:
                    users = [e for e in self.entries if e.kind == "user"]
                    if len(users) > turn:
                        prompt = users[turn].text
                self._cut_feed_after_turn(turn)
                if prompt:
                    self._restore_prompt(prompt)
        elif kind == "turn.start":
            self.busy = True
            self._update_title()
        elif kind == "agent.thinking":
            entry = self._stream_entry("thinking")
            entry.text += str(ev.get("text", ""))
            entry.chunk_at = self._clock()
        elif kind == "thinking.summary":
            # Пересказ быстрой роли: строкой в запись «думает» (ЗАДАЧА-19).
            # Событие может прийти позже конца шага — крепится к последней
            # записи рассуждений, поток хода оно не задерживает.
            target = None
            for cand in reversed(self.entries):
                if cand.kind == "thinking":
                    target = cand
                    break
            if target is not None:
                target.summary_lines.append(
                    (str(ev.get("text", "")), self._clock()))
        elif kind == "agent.text":
            entry = self._stream_entry("agent")
            entry.text += str(ev.get("text", ""))
            entry.chunk_marks.append((len(entry.text), self._clock()))
        elif kind == "agent.text.done":
            now_clock = self._clock()
            for entry in self.entries:
                if entry.streaming and entry.kind == "thinking":
                    entry.finished_at = now_clock
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
            self._notify("Луна ждёт решения по действию")
            self._update_title()
        elif kind == "guard.verdict":
            entry = self._find("guard", str(ev.get("id", "")))
            if entry is not None and entry.status == "pending":
                entry.status = str(ev.get("decision", ""))
        elif kind == "ask.user":
            self._add(Entry("ask", text=str(ev.get("question", "")),
                            options=list(ev.get("options") or []),
                            entry_id=str(ev.get("id", "")), status="pending"))
            self._notify("Луна задала вопрос")
            self._update_title()
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
        elif kind == "context.compacting":
            self._add(Entry("tide", streaming=True))
        elif kind in ("context.compacted", "context.compact_end"):
            tide = next((e for e in reversed(self.entries)
                         if e.kind == "tide" and e.streaming), None)
            if kind == "context.compact_end":
                if tide is not None:
                    self.entries.remove(tide)
            else:
                if tide is None:  # журнал старых сессий: начала не было
                    tide = self._add(Entry("tide"))
                tide.streaming = False
                # Из журнала (--continue) — без блика: он про «сейчас».
                tide.finished_at = 0.0 if self._restoring else self._clock()
                def pct(v):
                    return format(v, "g") if isinstance(v, (int, float)) else "?"

                tide.text = pct(ev.get("before_pct")) + "% → " + pct(ev.get("after_pct")) + "%"
                turns = ev.get("summarized_turns")
                tide.summary = str(len(turns)) if isinstance(turns, list) else ""
                self._tide_notice = True  # notice «Прилив: …» — уже в разделителе
        elif kind == "notice":
            text = str(ev.get("text", ""))
            if getattr(self, "_tide_notice", False) and text.startswith("Прилив: контекст сжат"):
                self._tide_notice = False
                return
            self._add(Entry("notice", text=text))
        elif kind == "btw.answer":
            # Ответ «в сторону»: отдельной записью, ход и история не тронуты.
            self._add(Entry("btw", text=str(ev.get("text", ""))))
        elif kind == "error":
            text = str(ev.get("message", ""))
            detail = str(ev.get("detail", ""))
            if detail:
                text = text + "\n" + detail
            self._add(Entry("error", text=text))
            if ev.get("code") == "newmoon_unconfirmed":
                data = ev.get("data") or {}
                self.open_newmoon_confirm(extra=(str(data.get("base_url") or ""),
                                                 str(data.get("model") or "")))
        elif kind == "turn.done":
            self.busy = False
            self._notify("Ход Луны завершён")
            self._update_title()
            now_clock = self._clock()
            for entry in self.entries:
                if entry.streaming and entry.kind == "thinking":
                    entry.finished_at = now_clock
                entry.streaming = False
            if self._pending_switch is not None:
                path, self._pending_switch = self._pending_switch, None
                self._do_switch(path)
            elif self._pending_new:
                self._pending_new = False
                self._do_new_session()
            elif self._pending_delete_current:
                self._pending_delete_current = False
                self._do_delete_current()
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
        from ..core import newmoon as _nm

        if _nm.active() and not self._nm_main_ok():
            # Новолуние: сначала человек разрешает адрес главной модели,
            # потом сообщение уходит само.
            self._nm_stash = (text, display, delivery)
            self.open_newmoon_confirm()
            return
        attachments = [tok[1:] for tok in text.split()
                       if tok.startswith("@") and len(tok) > 1]
        self._add(Entry("user", text=text if display is None else display))
        # Автоназвание сессии по первому сообщению (ЗАДАЧА-20).
        if (not self._session_titled and self.journal is not None
                and self.settings.get("chat.auto_title")):
            title = " ".join(text.split())[:60].strip()
            if title:
                self.session_title = title
                self._session_titled = True
                try:
                    self.journal.set_title(title)
                except Exception:  # noqa: BLE001 — имя не важнее хода
                    pass
                self._update_title()
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
        # Свёрнутые вставки разворачиваются обратно в полный текст.
        for placeholder, full in list(self._pastes.items()):
            if placeholder in text:
                text = text.replace(placeholder, full)
                del self._pastes[placeholder]
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
        if text.startswith("#"):
            note = text[1:].strip()
            from ..core import newmoon as _nm

            if note and _nm.active():
                self._add(Entry("notice", text="Новолуние: в память проекта не пишу — "
                                               "это след. Скажи словами, если правда надо."))
            elif note:
                self._memory_note(note)
            return
        if self.busy:
            if not self._send_hint_shown:
                # Подсказка о режиме отправки — один раз за сессию (ЗАДАЧА-20).
                self._send_hint_shown = True
                self._flash_hint("Ход идёт: Enter — "
                                 + self._SEND_MODE_TITLES[self._send_mode()]
                                 + ". Сменить: /send или Alt+S.")
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

    def _memory_note(self, note: str) -> None:
        """#текст: дописать в память проекта — AGENTS.md (ЗАДАЧА-20)."""
        path = Path(self.cwd) / "AGENTS.md"
        try:
            if not path.exists():
                path.write_text("# AGENTS.md\n\nИнструкции проекта для Луны.\n",
                                encoding="utf-8")
            with path.open("a", encoding="utf-8") as fh:
                fh.write("\n- " + note + "\n")
        except OSError as exc:
            self._add(Entry("error", text="Не вышло дописать в AGENTS.md: "
                                          + str(exc)))
            return
        self._add(Entry("notice", text="В память проекта (AGENTS.md): " + note))

    def _slash(self, text: str) -> None:
        parts = text.split()
        raw = parts[0]
        cmd = raw
        if raw.startswith("/"):
            # Алиасы и русские имена — всегда; угадывание раскладки
            # (зфтуд → panel) — только с ui.layout_fix.
            set_user_aliases(self.settings.get("ui.command_aliases") or "")
            canonical = resolve_command(raw[1:], layouts=bool(
                self.settings.get("ui.layout_fix")))
            if canonical is not None:
                cmd = "/" + canonical
        rest = text[len(raw):].strip()
        if cmd in ("/exit", "/quit"):
            self.done = True
        elif cmd == "/pult":
            self.open_pult(rest)
        elif cmd == "/newmoon":
            self.newmoon_command(rest)
        elif cmd == "/reload":
            self.updater.reload_command()
        elif cmd == "/update":
            self.updater.update_command(rest)
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
            self._thinking_command(parts[1:])
        elif cmd == "/effort":
            self._effort_command(parts[1:])
        elif cmd == "/delete":
            self.delete_current_session()
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
        elif cmd == "/send":
            if len(parts) > 1 and parts[1] in self._SEND_MODE_TITLES:
                self.settings.set("chat.send_mode", parts[1])
                self._add(Entry("notice", text="Отправка во время хода: "
                                               + self._SEND_MODE_TITLES[parts[1]]))
            elif len(parts) > 1:
                self._add(Entry("notice", text="Режимы отправки: inject, "
                                               "queue, interrupt."))
            else:
                self.open_send_picker()
        elif cmd == "/panel":
            self._panel_command(parts[1:])
        elif cmd == "/panels":
            if len(parts) > 1 and parts[1] == "reset":
                self._reset_saved_layout()
            else:
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

    # --- мышление и усилие (ЗАДАЧА-19) ------------------------------------

    _THINKING_ORDER = ("summary", "hidden", "full")
    _THINKING_TITLES = {
        "summary": "summary — шапка «думает» и пересказ одной строкой; "
                   "полный текст — Ctrl+O",
        "full": "full — полный текст рассуждений сразу",
        "hidden": "hidden — рассуждения не показываются (Ctrl+O развернёт)",
    }

    def _set_thinking_mode(self, mode: str) -> None:
        # ui.show_thinking старше и не переименовывается: держим оба ключа
        # согласованными, чтобы старые читатели не врали.
        self.settings.set("ui.thinking", mode)
        self.settings.set("ui.show_thinking", mode != "hidden")

    def _thinking_command(self, args: list) -> None:
        """/thinking: по кругу summary → hidden → full; аргумент — сразу."""
        arg = args[0].strip().lower() if args else ""
        if arg in ("off", "нет"):
            mode = "hidden"
        elif arg in ("on",):
            mode = "full"
        elif arg in self._THINKING_ORDER:
            mode = arg
        elif not arg:
            mode = self._next_thinking_mode()
        else:
            self._add(Entry("notice", text="Режимы рассуждений: summary, "
                                           "full, hidden. Сейчас: "
                                           + self._thinking_mode()))
            return
        self._set_thinking_mode(mode)
        self._add(Entry("notice", text="Рассуждения: "
                                       + self._THINKING_TITLES[mode]))

    def _next_thinking_mode(self) -> str:
        current = self._thinking_mode()
        idx = self._THINKING_ORDER.index(current)
        return self._THINKING_ORDER[(idx + 1) % len(self._THINKING_ORDER)]

    def _cycle_thinking(self) -> None:
        """Alt+O: режим показа рассуждений — по кругу, на лету."""
        mode = self._next_thinking_mode()
        self._set_thinking_mode(mode)
        self._flash_hint("Рассуждения: " + self._THINKING_TITLES[mode])

    _EFFORT_LEVELS = ("auto", "none", "minimal", "low", "medium", "high",
                      "xhigh", "max")
    _EFFORT_ROLES = ("main", "fast", "guard", "vision")

    def open_effort_picker(self, setting_key: str) -> None:
        """/effort: уровень шкалы выбиралкой для общего ключа или роли."""
        current = str(self.settings.get(setting_key) or "auto")
        items = []
        for level in self._EFFORT_LEVELS:
            items.append(PickerItem(
                key=level, title=level + " — " + _think.EFFORT_TITLES[level],
                mark="→" if level == current else "", payload=setting_key))
        self._open_picker("effort", "Усилие рассуждений (" + setting_key + ")",
                          items,
                          hint="Enter — задать · Esc — закрыть",
                          preview_lines=0)

    def _effort_command(self, args: list) -> None:
        """/effort [роль] [уровень]: без аргументов — выбиралка общего."""
        args = [a.strip().lower() for a in args if a.strip()]
        if not args:
            self.open_effort_picker("model.effort")
            return
        if len(args) == 1 and args[0] in self._EFFORT_ROLES:
            self.open_effort_picker("model.effort_" + args[0])
            return
        if len(args) == 1 and args[0] in self._EFFORT_LEVELS:
            self.settings.set("model.effort", args[0])
            self._add(Entry("notice", text="Усилие рассуждений: " + args[0]
                                           + " — со следующего вызова модели."))
            return
        if (len(args) == 2 and args[0] in self._EFFORT_ROLES
                and args[1] in self._EFFORT_LEVELS):
            self.settings.set("model.effort_" + args[0], args[1])
            self._add(Entry("notice", text="Усилие роли " + args[0] + ": "
                                           + args[1] + " — со следующего "
                                           "вызова модели."))
            return
        self._add(Entry("notice", text="Нужно: /effort, /effort <уровень>, "
                                       "/effort <роль> или /effort <роль> "
                                       "<уровень>; роли: main, fast, guard, "
                                       "vision; шкала: auto, none, minimal, "
                                       "low, medium, high, xhigh, max."))

    def _cycle_effort(self) -> None:
        """Alt+E: общее усилие рассуждений — по кругу шкалы, на лету."""
        current = str(self.settings.get("model.effort") or "auto")
        if current not in self._EFFORT_LEVELS:
            current = "auto"
        nxt = self._EFFORT_LEVELS[
            (self._EFFORT_LEVELS.index(current) + 1) % len(self._EFFORT_LEVELS)]
        self.settings.set("model.effort", nxt)
        self._flash_hint("Усилие рассуждений: " + nxt + " ("
                         + _think.EFFORT_TITLES[nxt] + ")")

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
                 + " · статус-строка: " + str(self.settings.get("ui.statusline")),
                 "мышление: " + self._thinking_mode()
                 + " · усилие: " + str(self.settings.get("model.effort") or "auto")]
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
        """/copy — последний ответ агента; /copy code — последний блок кода
        из ответа; /copy err — последняя ошибка целиком."""
        if args and args[0] == "code":
            block = None
            for entry in reversed(self.entries):
                if entry.kind != "agent" or not entry.text:
                    continue
                found = re.findall(r"```[^\n]*\n(.*?)```", entry.text, re.DOTALL)
                if found:
                    block = found[-1].strip("\n")
                    break
            if block is None:
                self._add(Entry("notice", text="Блоков кода в ответах пока нет."))
                return
            if self.copy_hook is None:
                self._add(Entry("notice", text="Буфер обмена недоступен в этом режиме."))
                return
            self.copy_hook(block)
            self._add(Entry("notice", text="Скопировано в буфер обмена "
                                           "(блок кода): " + str(len(block))
                                           + " символов."))
            return
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
        if panel_id in self.layout:
            self.layout.close(panel_id)
            opened = False
        else:
            # Повторное открытие — на запомненное место и размер (ЗАДАЧА-20).
            place = self._saved_places.get(panel_id)
            if place is not None:
                opened = self.layout.open_at(panel_id, place[0], place[1])
            else:
                opened = self.layout.open(panel_id)
        if not opened:
            self.panel_focus = False
        self._save_layout()

    # --- память раскладки панелей (ЗАДАЧА-20) --------------------------------

    def _save_layout(self) -> None:
        """Запомнить раскладку (ручное действие человека, не каждый кадр)."""
        if not self.settings.get("ui.remember_layout"):
            return
        try:
            from . import layout_store

            snapshot = self.layout.snapshot()
            order = snapshot.get("order") or []
            weights = snapshot.get("weights") or {}
            # Места закрытых панелей не забываем: повторное открытие
            # ставит панель туда, где она жила (в снимок — полем places).
            places = dict(self._saved_places)
            for i, pid in enumerate(order):
                places[pid] = (i, weights.get(pid, 1.0))
            self._saved_places = places
            snapshot["places"] = {pid: [i, w] for pid, (i, w) in places.items()}
            layout_store.put_layout(self._data_dir(), self.cwd, snapshot,
                                    self._geom_cols or 80)
            self._saved_cols = self._geom_cols or self._saved_cols
        except Exception:  # noqa: BLE001 — память раскладки не роняет экран
            pass

    def _restore_saved_layout(self) -> None:
        """При старте: раскладка проекта или общая — из LUNA_HOME."""
        if not self.settings.get("ui.remember_layout"):
            return
        if not self.settings.get("ui.panels"):
            return
        try:
            from . import layout_store

            entry = layout_store.get_layout(self._data_dir(), self.cwd)
        except Exception:  # noqa: BLE001
            entry = None
        if not entry:
            return
        snapshot = entry.get("snapshot") or {}
        self.layout.restore(snapshot)
        self._saved_cols = int(entry.get("cols") or 0)
        raw_places = snapshot.get("places")
        if isinstance(raw_places, dict) and raw_places:
            self._saved_places = {pid: (int(v[0]), float(v[1]))
                                  for pid, v in raw_places.items()
                                  if isinstance(v, (list, tuple)) and len(v) == 2}
        else:
            order = snapshot.get("order") or []
            weights = snapshot.get("weights") or {}
            self._saved_places = {pid: (i, weights.get(pid, 1.0))
                                  for i, pid in enumerate(order)}

    def _reset_saved_layout(self) -> None:
        """/panels reset: забыть сохранённую раскладку и закрыть панели."""
        try:
            from . import layout_store

            layout_store.clear_layout(self._data_dir(), self.cwd)
        except Exception:  # noqa: BLE001
            pass
        self._saved_places = {}
        self._saved_cols = 0
        self.layout = LayoutTree()
        self.panel_focus = False
        self._add(Entry("notice", text="Раскладка панелей забыта: "
                                       "сохранённая в LUNA_HOME очищена, "
                                       "панели закрыты."))

    # --- блоки ленты: раскрыть всё / один блок (ЗАДАЧА-20) ---------------------

    def _exp(self, entry: Entry) -> bool:
        """Блок развёрнут: индивидуально или общим ui.expand_all."""
        return bool(self.expand_all or entry.expanded)

    def _expandable(self, entry: Entry) -> bool:
        if entry.kind == "thinking":
            return bool(entry.text)
        if entry.kind == "tool":
            return bool(entry.output) or bool(self._guards_for(entry))
        if entry.kind == "todo":
            return bool(entry.items)
        return False

    def _toggle_expand_all(self) -> None:
        """Ctrl+O: развернуть все блоки ленты / свернуть все обратно."""
        self.expand_all = not self.expand_all
        self.settings.set("ui.expand_all", self.expand_all)
        if not self.expand_all:
            for entry in self.entries:
                entry.expanded = False
        self._flash_hint("Блоки ленты: " + ("все развёрнуты" if self.expand_all
                                            else "все свёрнуты")
                         + " (Ctrl+O)")

    def _toggle_entry(self, entry: Entry) -> None:
        """Клик по заголовку / Enter на блоке: только этот блок."""
        if self.expand_all:
            # Из общего «всё открыто» клик сворачивает один блок: остальным
            # раздаём индивидуальное «открыто» и гасим общий флаг.
            self.expand_all = False
            self.settings.set("ui.expand_all", False)
            for other in self.entries:
                if self._expandable(other):
                    other.expanded = other is not entry
            return
        entry.expanded = not entry.expanded

    def _move_block_cursor(self, step: int) -> None:
        """Alt+↑/↓: курсор по раскрываемым блокам ленты."""
        blocks = [e for e in self.entries if self._expandable(e)]
        if not blocks:
            self._flash_hint("Раскрываемых блоков в ленте пока нет.")
            return
        if self._block_sel not in blocks:
            self._block_sel = blocks[-1] if step < 0 else blocks[0]
        else:
            idx = blocks.index(self._block_sel)
            self._block_sel = blocks[max(0, min(len(blocks) - 1, idx + step))]
        self.scroll = 0

    # --- уведомления и заголовок окна (ЗАДАЧА-20) ------------------------------

    def _notify(self, text: str) -> None:
        """Звонок/уведомление человеку по настройке ui.notify."""
        mode = str(self.settings.get("ui.notify") or "off")
        if mode == "off" or self.notify_hook is None or self._restoring:
            return
        seq = ""
        if mode in ("bell", "both"):
            seq += "\a"
        if mode in ("osc", "both"):
            seq += "\x1b]9;" + text + "\a"
        if seq:
            try:
                self.notify_hook(seq)
            except Exception:  # noqa: BLE001 — сигнал не роняет экран
                pass

    def _update_title(self) -> None:
        """Заголовок окна: тема сессии и состояние (настройка ui.title)."""
        if self.title_hook is None or not self.settings.get("ui.title"):
            return
        topic = self.session_title or Path(self.cwd).name or "сессия"
        if self.pending_guard is not None or self.pending_ask is not None:
            state = "нужен ответ"
        elif self.busy:
            state = "идёт ход"
        else:
            state = "ждёт"
        try:
            self.title_hook("Луна · " + topic + " · " + state)
        except Exception:  # noqa: BLE001
            pass

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

    def guard_answer(self, decision: str, args: dict | None = None,
                     text: str = "") -> None:
        entry = self.pending_guard
        if entry is None:
            return
        entry.status = decision
        if text:
            entry.answer = text
        self.guard_editing = False
        self._guard_replace = False
        cmd = {"type": "guard.answer", "id": entry.entry_id, "decision": decision}
        if args is not None:
            cmd["args"] = args
        if text:
            cmd["text"] = text
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
        elif key.name == "backspace" and ("ctrl" in key.mods or "alt" in key.mods):
            self.editor.kill_word_back_fine()
        elif key.name == "delete" and ("ctrl" in key.mods or "alt" in key.mods):
            self.editor.kill_word_forward()
        elif key.label == "alt+d":
            self.editor.kill_word_forward()
        elif key.label == "alt+y":
            self.editor.yank_pop()
        elif key.name == "backspace":
            self.editor.backspace()
        elif key.name == "delete":
            self.editor.delete()
        elif key.name == "left" and "ctrl" in key.mods:
            self.editor.word_left_fine()
        elif key.name == "right" and "ctrl" in key.mods:
            self.editor.word_right_fine()
        elif key.name == "left" and "alt" in key.mods:
            self.editor.word_left()
        elif key.name == "right" and "alt" in key.mods:
            self.editor.word_right()
        elif key.name == "left":
            self.editor.left()
        elif key.name == "right":
            self.editor.right()
        elif key.name == "home":
            self.editor.line_home()
        elif key.name == "end":
            self.editor.line_end()
        elif key.label == "ctrl+a":
            self.editor.line_home()
        elif key.label == "ctrl+e":
            self.editor.line_end()
        elif key.label == "ctrl+u":
            # Readline: вырезать до начала строки в кольцо (Ctrl+Y вернёт).
            self.editor.kill_to_start()
        elif key.label == "ctrl+k":
            self.editor.kill_to_end()
        elif key.label == "ctrl+w":
            self.editor.kill_word_back()
        elif key.label == "ctrl+y":
            self.editor.yank()
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
        if key.label == "ctrl+s":
            # Ctrl+S — только заначка промпта (ЗАДАЧА-20), в том числе при
            # фокусе панели: режим отправки переехал на Alt+S.
            self._stash_key()
            return
        if self.panel_focus:
            self._panel_key(key)
            return
        if key.label in _PANEL_KEYS:
            self.toggle_panel(_PANEL_KEYS[key.label])
            return
        if key.label == "alt+s":
            self._cycle_send_mode()
            return
        if key.label in ("alt+up", "alt+down"):
            self._move_block_cursor(-1 if key.label == "alt+up" else 1)
            return
        if key.label == "ctrl+g":
            self._editor_command()
            return
        if key.label == "ctrl+_":
            self.editor.undo_edit()
            return
        if key.label == "ctrl+z":
            # Unix: свернуть в фон (fg вернёт); без хука запуска — отмена правки.
            if self.suspend_hook is not None:
                self.suspend_hook()
            else:
                self.editor.undo_edit()
            return
        if key.label == "alt+e":
            self._cycle_effort()
            return
        if key.label == "alt+o":
            self._cycle_thinking()
            return
        if key.label in ("ctrl+p", "ctrl+n"):
            # Readline-история без стрелок: Ctrl+P — назад, Ctrl+N — вперёд.
            if key.label == "ctrl+p":
                self.editor.history_prev()
            else:
                self.editor.history_next()
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
            self._toggle_expand_all()
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
        if key.name == "end" and "ctrl" in key.mods:
            self.scroll = 0  # к свежему — скролл больше не держит
            return
        if key.name == "home" and "ctrl" in key.mods:
            self.scroll = 100000
            return
        if key.name == "enter":
            if "alt" in key.mods and self.busy and self.editor.text.strip():
                # Alt+Enter во время хода — разово: прервать и отправить.
                self._interrupt_send(self.editor.submit())
            elif "alt" in key.mods or "shift" in key.mods:
                self.editor.newline()
                self._update_completions()
            elif self._backslash_before_cursor():
                # \ + Enter — перевод строки: слеш съедается (ЗАДАЧА-20).
                self.editor.backspace()
                self.editor.newline()
                self._update_completions()
            elif self._block_sel is not None and not self.editor.text:
                # Курсор блоков (Alt+↑/↓): Enter на пустом вводе — блок.
                self._toggle_entry(self._block_sel)
            elif self.cmd_completions and self.editor.text.startswith("/") \
                    and " " not in self.editor.text.strip():
                # Enter по открытому списку команд — выбрать подсвеченную.
                self._apply_cmd_completion()
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
        if key.char == "?" and not self.editor.text \
                and not self.completions and not self.cmd_completions:
            # ? на пустом вводе — справка клавиш, как у эталонов.
            self._add(Entry("help", text=HELP_TEXT))
            return
        if key.char == " " and not self.editor.text \
                and self._block_sel is not None:
            # Пробел на пустом вводе при курсоре блоков — раскрыть блок.
            self._toggle_entry(self._block_sel)
            return
        if self._editor_key(key):
            self._update_completions()

    def _backslash_before_cursor(self) -> bool:
        ed = self.editor
        return ed.cursor > 0 and ed.text[ed.cursor - 1] == "\\"

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
        elif double:
            # Esc Esc с текстом — очистить, но не потерять: набросок в
            # истории ввода, стрелка вверх вернёт (как у Клод Кода).
            self.editor.push_history(self.editor.text)
            self.editor.clear()
            self._last_esc = -10.0
            self._flash_hint("Набросок убран в историю ввода — ↑ вернёт.")

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
            self.editor.push_history(self.editor.text)  # ↑ вернёт очищенное
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
        """Alt+S: режим отправки во время хода — по кругу, на лету."""
        order = ("inject", "queue", "interrupt")
        nxt = order[(order.index(self._send_mode()) + 1) % len(order)]
        self.settings.set("chat.send_mode", nxt)
        self._flash_hint("Отправка во время хода: " + self._SEND_MODE_TITLES[nxt] +
                         " (Alt+S — сменить, /send — выбиралка)")

    def _stash_key(self) -> None:
        """Ctrl+S — заначка промпта, как в Claude Code (ЗАДАЧА-20).

        Поле не пусто — убрать набранное в заначку (или поменять с ней
        местами); поле пусто и заначка есть — вернуть текст с курсором.
        """
        ed = self.editor
        if ed.text:
            current = (ed.text, ed.cursor)
            if self._stash is not None:
                old, self._stash = self._stash, current
                ed.set_text(old[0])
                ed.cursor = min(old[1], len(ed.text))
                self._flash_hint("Заначка и поле поменяны местами (Ctrl+S).")
            else:
                self._stash = current
                ed.clear()
                self._flash_hint("Промпт — в заначке. Ctrl+S на пустом поле "
                                 "вернёт его с курсором.")
        elif self._stash is not None:
            text, cursor = self._stash
            self._stash = None
            ed.set_text(text)
            ed.cursor = min(cursor, len(ed.text))
            self._update_completions()
        else:
            self._flash_hint("Заначка пуста: набери текст и нажми Ctrl+S.")

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
        """Строки над полем ввода: подсказка, заначка, очередь, режим."""
        out: list = []
        if self._hint is not None and now < self._hint[1]:
            out.append(self._hint[0])
        if self._stash is not None:
            first = self._stash[0].split("\n")[0]
            out.append("в заначке: " + first + " (Ctrl+S — вернуть или обменять)")
        for text in self.queue[:3]:
            out.append("в очереди: " + str(text).split("\n")[0])
        if len(self.queue) > 3:
            out.append("в очереди ещё " + str(len(self.queue) - 3) +
                       " (↑ на пустом поле — вернуть в редактор)")
        if self.busy:
            out.append("ход идёт · Enter: " + self._SEND_MODE_TITLES[self._send_mode()] +
                       " · Alt+S — сменить · Alt+Enter — прервать и отправить")
        return out[:5]

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
            self._insert_paste(text)
            self._update_completions()
            return
        self._add(Entry("notice", text="Буфер пуст или недоступен; картинку "
                                       "терминал напрямую не передаёт — "
                                       "вставьте путь к файлу картинки."))

    def _guard_key(self, key: Key, entry: Entry) -> None:
        if self._guard_replace:
            # «Нет, и вот что вместо»: текст отказа-инструкции — в поле ввода.
            if key.name == "enter":
                text = self.editor.text.strip()
                self.editor.clear()
                self._guard_replace = False
                self.guard_answer("no_replace", text=text)
            elif key.name == "esc":
                self._guard_replace = False
                self.editor.clear()
            else:
                self._editor_key(key)
            return
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
        elif key.char == "p":
            self.guard_answer("always_project")
        elif key.char == "r":
            self._guard_replace = True
            self.editor.clear()
            self._flash_hint("Что сделать вместо? Печатай и Enter — "
                             "отказ уйдёт модели с этим текстом.")
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
        # Любое ручное изменение раскладки клавишами — в память (ЗАДАЧА-20).
        if key.name in ("left", "right", "tab", "enter") or key.char == "x":
            self._save_layout()

    def _toggle_expand_latest(self) -> None:
        """Устаревший путь ЗАДАЧИ-18 — теперь это общий Ctrl+O (ЗАДАЧА-20)."""
        self._toggle_expand_all()

    def handle_paste(self, paste: Paste) -> None:
        if self.pending_guard is not None and not self.guard_editing:
            return
        if self.picker is not None and self.picker.mode == "input":
            # Вставка — в поле выбиралки (ключ в пульте, путь, имя), а не
            # в поле чата под ней.
            self.picker.paste(paste.text)
            self._last_change = self._clock()
            return
        self._insert_paste(paste.text)
        self._update_completions()

    def _insert_paste(self, text: str) -> None:
        """Вставка в поле ввода (ЗАДАЧА-20): перетащенный файл — @путём,
        большая вставка — свёрнутым маркером [вставлено N строк]."""
        stripped = text.strip().strip('"').strip("'")
        if "\n" not in stripped and stripped:
            candidate = stripped
            if candidate.startswith("file://"):
                from urllib.parse import unquote, urlparse

                candidate = unquote(urlparse(candidate).path)
            path = Path(candidate).expanduser()
            if path.is_file():
                self.editor.insert("@" + str(path) + " ")
                return
        lines = text.count("\n") + 1 if text else 0
        threshold = int(self.settings.get("ui.paste_collapse_lines") or 0)
        if threshold and lines > threshold:
            placeholder = "[вставлено " + str(lines) + " строк]"
            base = placeholder
            n = 2
            while placeholder in self._pastes and self._pastes[placeholder] != text:
                placeholder = base[:-1] + " · " + str(n) + "]"
                n += 1
            self._pastes[placeholder] = text
            self.editor.insert(placeholder)
            return
        self.editor.insert(text)

    def _expand_paste_in_editor(self) -> None:
        """Клик по свёрнутой вставке в поле ввода — развернуть обратно."""
        for placeholder, full in list(self._pastes.items()):
            if placeholder in self.editor.text:
                self.editor.set_text(
                    self.editor.text.replace(placeholder, full, 1))
                del self._pastes[placeholder]
                return

    def _panel_at(self, mouse: Mouse):
        """Панель под мышью по геометрии последнего кадра или None."""
        mx, my = mouse.x - 1, mouse.y - 1
        for panel_id, (px, py, pw, ph) in self._panel_rects.items():
            if px <= mx < px + pw and py <= my < py + ph:
                return panel_id, (px, py, pw, ph), mx, my
        return None

    def _panel_mouse(self, mouse: Mouse, clicks: bool = True) -> bool:
        """Мышь по панелям (ЗАДАЧА-17, крестик и перетаскивание — ЗАДАЧА-20).

        Клик по заголовку — фокус и режим панелей; заголовок можно тащить
        для перестановки; крестик в заголовке закрывает; перетаскивание
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
                self._save_layout()
                return True
        if self._panel_head_drag is not None:
            drag_id, sx, sy, moved = self._panel_head_drag
            if mouse.kind == "move":
                if abs(mouse.x - sx) > 1 or abs(mouse.y - sy) > 1:
                    moved = True
                self._panel_head_drag = (drag_id, sx, sy, moved)
                return True
            if mouse.kind == "release":
                self._panel_head_drag = None
                if moved:
                    found = self._panel_at(mouse)
                    if found is not None and found[0] != drag_id:
                        self._move_panel_towards(drag_id, found[0])
                    self._save_layout()
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
        if mouse.kind == "press" and mouse.button == 0 and clicks:
            if my == py and pw >= 6 and mx == px + pw - 1:
                # Крестик в заголовке — закрыть панель.
                self.layout.close(panel_id)
                if not self.layout.leaves():
                    self.panel_focus = False
                self._save_layout()
                return True
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
                self._panel_head_drag = (panel_id, mouse.x, mouse.y, False)
            return True
        if self._sel_anchor is not None:
            return False  # идёт выделение ленты — отпуск обработает лента
        return mouse.kind in ("move", "release")

    def _move_panel_towards(self, panel_id: str, target_id: str) -> None:
        """Переставить панель по дереву на место другой (перетаскивание)."""
        leaves = self.layout.leaves()
        if panel_id not in leaves or target_id not in leaves:
            return
        step = 1 if leaves.index(panel_id) < leaves.index(target_id) else -1
        for _ in range(len(leaves)):
            cur = self.layout.leaves()
            if cur.index(panel_id) == cur.index(target_id):
                break
            if not self.layout.move(panel_id, step):
                break

    # --- мышь везде: карта зон кадра (ЗАДАЧА-20) ------------------------------

    def _zone_at(self, mx: int, my: int):
        """Зона последнего кадра по точке: (x0, y0, x1, y1, действие)."""
        for zone in reversed(self.zones):
            x0, y0, x1, y1, _action = zone
            if x0 <= mx < x1 and y0 <= my < y1:
                return zone
        return None

    def _click_link(self, token: str) -> None:
        """Клик по ссылке/пути в ленте: поведение — ui.click_link."""
        mode = str(self.settings.get("ui.click_link") or "copy")
        if mode == "off":
            return
        if mode == "open" and self.open_hook is not None:
            try:
                self.open_hook(token)
                self._flash_hint("Открываю: " + token)
                return
            except Exception:  # noqa: BLE001 — не вышло открыть: копируем
                pass
        if self.copy_hook is not None:
            self.copy_hook(token)
            self._flash_hint("Скопировано: " + token)

    def _zone_action(self, action) -> None:
        """Действие кликабельной зоны кадра."""
        kind = action[0]
        if kind == "status":
            name = action[1]
            if name == "mode":
                self.open_mode_picker()
            elif name == "model":
                self.open_models_picker()
            elif name == "effort":
                self.open_effort_picker("model.effort")
            elif name == "thinking":
                self.open_thinking_picker()
            elif name == "send":
                self.open_send_picker()
            elif name == "theme":
                self.open_theme_picker()
            elif name == "newmoon":
                self.open_newmoon_confirm()
            elif name == "update":
                self.updater.update_command()
        elif kind == "block":
            self.panel_focus = False
            self._block_sel = action[1]
            self._toggle_entry(action[1])
        elif kind == "link":
            self._click_link(action[1])
        elif kind == "paste":
            self._expand_paste_in_editor()
        elif kind == "key":
            # Клик по подсказке «Esc — закрыть» — та же клавиша, что с
            # клавиатуры: без неё (нетбук, сенсор) доступно всё обещанное.
            self.handle_key(action[1])

    def _key_zones(self, buf: Buffer, text: str, x: int, y: int,
                   right: int) -> None:
        """Зоны «Клавиша — действие» строки text, нарисованной с колонки x;
        под мышью кусочек подсвечивается — видно, что это кнопка."""
        if not self.settings.get("ui.click_hints"):
            return
        hints = find_key_hints(text)
        if not hints:
            return
        hover = None
        hp = self._hover_pos
        if hp is not None and hp[1] == y:
            for i, (a, b, _key) in enumerate(hints):
                if x + a <= hp[0] < x + b:
                    hover = i  # последняя накрывшая — как у _zone_at
        for i, (a, b, key) in enumerate(hints):
            x0, x1 = x + a, min(x + b, right, buf.cols)
            if x1 <= x0:
                continue
            self.zones.append((x0, y, x1, y + 1, ("key", key)))
            if i == hover and 0 <= y < buf.rows:
                for cx in range(x0, x1):
                    ch, _fg, bg, attrs = buf.cell(cx, y)
                    buf.cells[y][cx] = (ch, _color.CYAN, bg,
                                        (attrs & ~_color.DIM) | _color.UNDERLINE)

    def _picker_mouse(self, mouse: Mouse) -> None:
        """Мышь над выбиралкой: наведение подсвечивает, клик выбирает,
        колесо крутит список (ЗАДАЧА-20)."""
        picker = self.picker
        if picker is None:
            return
        mx, my = mouse.x - 1, mouse.y - 1
        if mouse.kind in ("wheel_up", "wheel_down"):
            picker._move(-3 if mouse.kind == "wheel_up" else 3)
            return
        zone = self._zone_at(mx, my)
        if zone is not None and zone[4][0] == "key":
            if mouse.kind == "press" and mouse.button == 0:
                self._zone_action(zone[4])
            return
        item = zone[4][1] if zone is not None and zone[4][0] == "picker" else None
        if item is None:
            return
        if mouse.kind == "move":
            picker.hover_to(item)
        elif mouse.kind == "press" and mouse.button == 0:
            if self._picker_kind == "panels":
                picker.hover_to(item)
                self._toggle_panel_from_picker(item)
                return
            action = picker.pick_item(item)
            if action is not None:
                self._route_picker_action(action)

    def _mouse_mode(self) -> str:
        mode = str(self.settings.get("ui.mouse") or "full")
        return mode if mode in ("full", "scroll", "off") else "full"

    def handle_mouse(self, mouse: Mouse) -> None:
        mode = self._mouse_mode()
        if mode == "off":
            return  # мышь не трогаем вовсе: выделение — за терминалом
        if mouse.kind == "move":
            self._hover_pos = (mouse.x - 1, mouse.y - 1)
        if "shift" in mouse.mods and mouse.kind in ("press", "move", "release"):
            return  # Shift+протяжка — обход: выделение средствами терминала
        if self.picker is not None:
            self._picker_mouse(mouse)
            return  # под выбиралкой лента мышью не выделяется
        if mouse.button == 0 and mouse.kind in ("move", "release"):
            # Идущие жесты ЗАДАЧИ-22: тяга ползунка и тяга ленты. Съедают
            # события первыми — иначе панельный обработчик или выделение
            # утащат движение не туда.
            if self._sb_drag is not None:
                if mouse.kind == "move":
                    self._scrollbar_move(mouse)
                else:
                    self._sb_drag = None
                return
            if self._feed_drag_event(mouse):
                return
        if mouse.kind == "press" and mouse.button == 0 and mode == "full" \
                and self._scrollbar_hit(mouse):
            # Полоса прокрутки — у правого края ленты; нажатие на неё
            # важнее зон под ней и границы колонок (она и рисуется там).
            self.panel_focus = False
            self._scrollbar_press(mouse)
            return
        if mouse.kind == "press" and mouse.button == 0 and mode == "full":
            zone = self._zone_at(mouse.x - 1, mouse.y - 1)
            if zone is not None and zone[4][0] in ("status", "block", "link",
                                                   "paste", "key"):
                self._zone_action(zone[4])
                return
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
                self._save_layout()
                return
        clicks = mode == "full"
        if (clicks and mouse.kind == "press" and mouse.button == 0
                and self.layout.leaves()
                and self._feed_geom[0]
                and (mouse.x - 1) - self._feed_geom[0] in (-1, 0)):
            # Захват — только сама граница и последняя колонка ленты:
            # заголовок панели начинается на колонку правее.
            self._col_drag = True
            self.panel_focus = False
            return
        if self._panel_mouse(mouse, clicks=clicks):
            return
        if not clicks and mouse.kind in ("press", "release"):
            return  # ui.mouse=scroll: клики не наши, только колесо ниже
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
            if self._sel_anchor is not None and self.selection is not None \
                    and self.selection[0] == self.selection[1]:
                self._soft_click(mouse)
            self._finish_selection_copy()

    def _soft_click(self, mouse: Mouse) -> None:
        """Простой клик (без протяжки) по телу мыслей — свернуть/развернуть.
        На отпускании, а не на нажатии: протяжка по мыслям — выделение."""
        zone = self._zone_at(mouse.x - 1, mouse.y - 1)
        if zone is not None and zone[4][0] == "soft_block":
            self._toggle_entry(zone[4][1])

    def _finish_selection_copy(self) -> None:
        """Отпустили мышь после протяжки: выделение — в буфер обмена."""
        anchor, self._sel_anchor = self._sel_anchor, None
        if anchor is None or self.selection is None:
            return
        (l0, c0), (l1, c1) = self._sel_range()
        if (l0, c0) == (l1, c1):
            return  # простой клик без протяжки — не выделение
        text = self.selection_text()
        if text.strip() and self.copy_hook is not None:
            self.copy_hook(text)

    def _feed_drag_event(self, mouse: Mouse) -> bool:
        """Идущий жест по ленте (ui.drag_scroll). Тяга ВДОЛЬ строк (сдвиг
        на 2+ строки) — прокрутка за пальцем; тяга почти горизонтальная —
        выделение, как раньше; после долгого нажатия (промоушн делает
        draw) тяга выделяет в любую сторону. True — событие у жеста."""
        drag = self._feed_drag
        if drag is None:
            return False
        if mouse.kind == "move":
            if drag["select"]:
                pos = self._feed_pos(mouse)
                if pos is not None and self._sel_anchor is not None:
                    self.selection = (self._sel_anchor, pos)
                return True
            dy = (mouse.y - 1) - drag["y"]
            if not drag["moved"] and abs(dy) >= 2:
                drag["moved"] = True  # жест определился: это прокрутка
                self.selection = None
                self._sel_anchor = None
            if drag["moved"]:
                # Контент — за пальцем: палец вниз — лента вниз, к старому.
                self.scroll = max(0, min(100000, drag["scroll"] + dy))
                return True
            pos = self._feed_pos(mouse)
            if pos is not None:
                if self._sel_anchor is None:
                    self._sel_anchor = drag["pos"]
                self.selection = (self._sel_anchor, pos)
            return True
        if mouse.kind == "release":
            was_select = bool(drag["select"])
            had_anchor = self._sel_anchor is not None
            self._feed_drag = None
            if was_select or had_anchor:
                self._finish_selection_copy()
            elif not drag["moved"]:
                self._soft_click(mouse)
            return True
        return False

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
            self._feed_drag = None
            return
        now = self._clock()
        last = self._last_press
        self._last_press = (now, mouse.x, mouse.y)
        if (last is not None and now - last[0] < 0.45
                and abs(mouse.x - last[1]) <= 1 and abs(mouse.y - last[2]) <= 1):
            self._select_word(pos)  # двойной клик — слово целиком, сразу в буфер
            self._sel_anchor = None
            self._feed_drag = None
            text = self.selection_text()
            if text.strip() and self.copy_hook is not None:
                self.copy_hook(text)
            return
        if self.settings.get("ui.drag_scroll"):
            # Нажал и держит (ЗАДАЧА-22): тяга — прокрутка за пальцем,
            # долгое нажатие — выделение (срок смотрит draw). Двойной
            # клик выше уже обработан; Shift+протяжка — обход терминала.
            self._feed_drag = {"pos": pos, "x": mouse.x - 1,
                               "y": mouse.y - 1, "t": now,
                               "scroll": self.scroll,
                               "moved": False, "select": False}
            self._sel_anchor = None
            self.selection = None
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

    def _all_command_names(self) -> list:
        """Все имена для списка на «/»: встроенные, русские алиасы рядом,
        команды из файлов совместимости и скиллы (ЗАДАЧА-20, раздел 5)."""
        names = list(COMMANDS) + [
            (ru, COMMAND_DESCR[lat] + " (/" + lat + ")")
            for ru, lat in all_aliases().items() if lat in COMMAND_DESCR]
        seen = {name for name, _desc in names}
        try:
            from ..mind.compat import list_commands, list_skills

            for item in list_commands(Path(self.cwd), self.settings):
                if item["name"] not in seen:
                    seen.add(item["name"])
                    names.append((item["name"],
                                  (item["description"] or "команда из файлов")
                                  + " · файл"))
            for item in list_skills(Path(self.cwd), self.settings):
                if item["name"] not in seen:
                    seen.add(item["name"])
                    names.append((item["name"],
                                  (item["description"] or "скилл") + " · скилл"))
        except Exception:  # noqa: BLE001 — совместимость не роняет подсказки
            pass
        return names

    def _update_completions(self) -> None:
        prefix = self.editor.text[:self.editor.cursor]
        cmd_match = re.fullmatch(r"/([a-zа-яё0-9-]*)", prefix, re.IGNORECASE)
        if cmd_match is not None and self.editor.cursor == len(self.editor.text):
            # «/» — ПОЛНЫЙ список команд с описаниями (ЗАДАЧА-20): без
            # обрезки по количеству; показ — окном с прокруткой в draw().
            needle = cmd_match.group(1).lower()
            needles = {needle}
            if self.settings.get("ui.layout_fix"):
                # Сама игла обязана остаться в наборе: variants("") пуст,
                # и одиночный «/» раньше из-за этого не показывал ничего.
                needles |= {v.lower() for v in _layout.variants(needle)}
            self.cmd_completions = [
                (name, desc) for name, desc in self._all_command_names()
                if any(name.startswith(n) for n in needles)]
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
        if not base and token:
            # Нечёткий поиск по проекту (ЗАДАЧА-20): подпоследовательность
            # символов токена в относительном пути, лучшие — первыми.
            self.completions = self._fuzzy_files(token)[:8]
            self.completion_index = 0
            return
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

    def _fuzzy_files(self, token: str) -> list:
        """Файлы проекта нечётким поиском: токен — подпоследовательность
        пути; сортировка — короче путь и раньше совпадение лучше."""
        if not token:
            return []
        needle = token.lower()
        scored = []
        root = Path(self.cwd)
        count = 0
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if not d.startswith(".") and d != "__pycache__"]
            for name in filenames:
                count += 1
                if count > 3000:
                    break
                # Путь — всегда через «/»: на Windows os.walk даёт «\»,
                # а подсказка и модель живут в POSIX-записи (красный CI).
                rel = (Path(dirpath) / name).relative_to(root).as_posix()
                low = rel.lower()
                pos = 0
                first = None
                for ch in needle:
                    found = low.find(ch, pos)
                    if found < 0:
                        break
                    if first is None:
                        first = found
                    pos = found + 1
                else:
                    scored.append(((first or 0), len(rel), rel))
            if count > 3000:
                break
        scored.sort()
        out = [rel for _f, _l, rel in scored]
        # Доп. каталоги (/add-dir): их файлы подсказываем полным путём.
        for extra in self.extra_dirs:
            try:
                extra_names = sorted(os.listdir(extra))
            except OSError:
                continue
            for name in extra_names:
                if name.startswith("__"):
                    continue
                low = name.lower()
                pos = 0
                ok = True
                for ch in needle:
                    found = low.find(ch, pos)
                    if found < 0:
                        ok = False
                        break
                    pos = found + 1
                if ok:
                    full = Path(extra) / name
                    out.append(full.as_posix() + ("/" if full.is_dir() else ""))
        return out

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
        if not self._anim_on():
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

    def _entry_cache_key(self, entry: Entry, width: int, now: float):
        """Ключ кэша строк записи или None — запись живая, не кэшируем.

        Живые: стрим, идущий тул, незаконченные рассуждения, свежий
        градиент пересказа, проявляющийся хвост стрима и только что
        родившиеся записи (пока идёт их собственное появление). Всё
        остальное на длинной ленте неизменно — а пересчитывать перенос
        и раскраску всей ленты на каждый кадр слабая машина не тянет
        (замер до правки: ~275 мс кадр на ленте в 3600 строк).
        """
        if entry.streaming:
            return None
        if entry.kind == "tide" and self._anim_on() \
                and now - (entry.finished_at or 0.0) < _TIDE_SHINE_SEC:
            return None  # по разделителю ещё бежит блик
        if entry.kind == "tool" and entry.status == "running":
            return None
        if entry.kind == "thinking":
            if not entry.finished_at:
                return None
            if entry.summary_lines and self._anim_on() \
                    and now - entry.summary_lines[-1][1] < 0.9:
                return None  # пересказ ещё проявляется градиентом
        if entry.chunk_marks and self._anim_on() \
                and str(self.settings.get("ui.stream_fade") or "off") != "off":
            fade_s = max(0.05, float(self.settings.get("ui.stream_fade_ms")
                                     or 220) / 1000.0)
            if now - entry.chunk_marks[-1][1] < fade_s:
                return None  # хвост стрима ещё проявляется
        if self._anim_on() and entry.born and now - entry.born < 0.35:
            return None  # собственное появление записи ещё идёт
        stamp = [entry.kind, len(entry.text), entry.status, entry.ok,
                 entry.summary, len(entry.output), len(entry.summary_lines),
                 entry.summary_lines[-1][1] if entry.summary_lines else 0.0,
                 entry.selected, entry.answer, entry.intent, entry.name,
                 entry.risk, entry.reason]
        if entry.kind in ("tool", "guard"):
            stamp.append(tuple(sorted((k, str(v)[:40])
                                      for k, v in (entry.args or {}).items())))
        if entry.kind == "todo":
            items = entry.items or self.todos
            stamp.append(tuple((str(it.get("text", "")), str(it.get("status", "")))
                               for it in items))
        if entry.kind == "tool":
            stamp.append(tuple((g.entry_id, g.status,
                                tuple(sorted((k, str(v)[:40])
                                             for k, v in (g.args or {}).items())))
                               for g in self._guards_for(entry)))
        return (width, self._exp(entry), self.expand_all,
                self._thinking_mode(), bool(self.settings.get("ui.details")),
                int(self.settings.get("ui.diff_lines") or 12),
                self.glyph_mode, self._applied_theme, self._anim_on(),
                self.guard_editing, self._guard_replace, tuple(stamp))

    def _entry_lines(self, entry: Entry, width: int, now: float):
        """Визуальные строки записи: из кэша, если запись не изменилась."""
        key = self._entry_cache_key(entry, width, now)
        if key is not None and entry._cache_key == key \
                and entry._cache_lines is not None:
            return entry._cache_lines
        # Стрим: запись меняется каждый кадр. Перерисовываем не чаще, чем
        # позволяет её цена (≈20% времени), — огромный текст от быстрой
        # модели (или нескольких сразу) больше не вешает экран.
        cost = getattr(entry, "_render_cost", 0.0)
        stale = getattr(entry, "_render_lines", None)
        if entry.streaming and stale is not None and cost > 0.008 \
                and getattr(entry, "_render_w", None) == width \
                and time.monotonic() - getattr(entry, "_render_at", 0.0) < cost * 4:
            return stale
        started = time.monotonic()
        lines = self._entry_lines_fresh(entry, width, now)
        entry._render_at = time.monotonic()
        entry._render_cost = entry._render_at - started
        entry._render_w = width
        entry._render_lines = lines
        if key is not None:
            entry._cache_key = key
            entry._cache_lines = lines
            entry._cache_texts = ["".join(t for t, _f, _a in spans)
                                  for spans, _bg in lines]
        return lines

    def _entry_lines_fresh(self, entry: Entry, width: int, now: float):
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
            md_lines = _incremental(entry, "md", entry.text, w,
                                    lambda s: render_markdown(s, w), None)
            faded = self._fade_last_line(entry, md_lines, now)
            for idx, line in enumerate(md_lines):
                if faded is not None and idx == len(md_lines) - 1:
                    raw.append((faded, None))
                    continue
                spans = [(s.text, s.style) for s in line]
                if entry.streaming and idx == len(md_lines) - 1:
                    spans.append(("▌", "dim"))  # живой курсор стрима — в той же строке
                add(spans)
        elif entry.kind == "tide":
            add(_tide_spans(entry, w, now, self._anim_on(), self.glyph_mode))
        elif entry.kind == "thinking":
            mode = self._thinking_mode()
            elapsed = max(0.0, (entry.finished_at or now) - entry.born) \
                if entry.born else 0.0
            secs = int(elapsed)
            moon = _think.moon_glyph(elapsed, self.glyph_mode)
            effort = str(self.settings.get("model.effort") or "auto")
            stars = ""
            if entry.streaming and self._anim_on():
                # Звёзды вспыхивают на свежих кусках; глубина effort —
                # числом звёзд (орбит) в шапке.
                stars = " " + _think.star_sparkle(
                    now, entry.chunk_at, 2 + _think.orbit_count(effort),
                    self.glyph_mode)
            verb = "думает" if entry.streaming else "думала"
            arrow = "▾ " if self._exp(entry) else "▸ "
            head = [(arrow, "cyan"), (moon + " ", "amber"),
                    (verb + " · " + str(secs) + " с", "dim")]
            if stars:
                head.append((stars, "cyan"))
            add(_clip_line(head, w))

            def add_grad(spans, born_at):
                line_alpha = alpha
                if self._anim_on():
                    line_alpha = alpha * _think.summary_alpha(born_at, now)
                raw.append((self._paint(spans, line_alpha), None))

            if self._exp(entry) or mode == "full":
                for line in _incremental(entry, "wt", entry.text, w - 2,
                                         lambda s: wrap_text(s, w - 2), ""):
                    add([("◦ ", "dim"), (line, "comment")])
            elif mode == "summary":
                for text_line, line_born in entry.summary_lines:
                    for visual in wrap_text(text_line, w - 4):
                        add_grad([("  ↳ ", "cyan"), (visual, "text")], line_born)
                count = len(entry.text)
                if entry.summary_lines:
                    label = "Ctrl+O или клик — полный текст рассуждений"
                elif not entry.streaming:
                    # Пересказа не случилось: быстрая роль не настроена
                    # или провайдер молчал — говорим честно.
                    label = ("рассуждения · символов: " + str(count)
                             + " · без пересказа · Ctrl+O — показать")
                else:
                    label = ""
                if label:
                    add([("◦ ", "dim"), (label, "dim")])
            else:  # hidden, но запись раскрыта (Ctrl+O/клик)
                for line in wrap_text(entry.text, w - 2):
                    add([("◦ ", "dim"), (line, "comment")])
        elif entry.kind == "tool":
            glyph = {"emoji": "⚙", "narrow": "•", "ascii": "+"}[self.glyph_mode]
            arrow = ""
            if entry.output or self._guards_for(entry):
                arrow = "▾ " if self._exp(entry) else "▸ "
            head = [(arrow, "cyan"), (glyph + " ", "tool"),
                    (entry.name or "tool", "tool")]
            compact = compact_args(entry.name, entry.args)
            if compact:
                head.append(("  " + compact, "text"))
            if entry.status == "running":
                head.append(("  " + self.spinner.glyph(now), "amber"))
                if self._anim_on():
                    # Метеор вызова (ЗАДАЧА-19): звёздный след у идущего тула.
                    head.append((" " + _think.star_sparkle(
                        now, entry.born, 3, self.glyph_mode), "cyan"))
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
                if out_lines and (self._exp(entry) or self.settings.get("ui.details")):
                    if self._exp(entry):
                        for out_line in out_lines:
                            for visual in wrap_text(out_line, w - 4):
                                add([("    " + visual, "dim")])
                    elif entry.name in ("edit", "write"):
                        # Дифф правки — свёрнуто до ui.diff_lines строк
                        # (ЗАДАЧА-20); остальное — по Ctrl+O или клику.
                        shown = 0
                        cap = max(1, int(self.settings.get("ui.diff_lines") or 12))
                        for out_line in out_lines:
                            if shown >= cap:
                                break
                            for visual in wrap_text(out_line, w - 4):
                                if shown >= cap:
                                    break
                                add([("    " + visual, "dim")])
                                shown += 1
                        rest = len(out_lines) - shown
                        label = "  вывод: строк " + str(len(out_lines))
                        if rest > 0:
                            label += " · ещё " + str(rest)
                        add([(label + " · Ctrl+O — весь вывод", "dim")])
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
        # Страховка ширины: строка любого вида не шире ленты, иначе она
        # уезжает под полосу прокрутки и панели. Лишнее — переносом.
        fitted: list = []
        for spans, bg in raw:
            fitted.extend((part, bg) for part in _split_painted(spans, w))
        return fitted

    _DECISIONS = {"yes": "да", "no": "нет", "edit": "да, с правкой",
                  "always": "да, дальше без вопросов",
                  "always_project": "да, в этом проекте без вопросов",
                  "no_replace": "нет, со своей инструкцией вместо",
                  "stop": "стоп"}

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
        add([("  Enter — да · n — нет · e — править · a — без вопросов (сессия)",
              "guard")], GUARD_BG)
        add([("  p — без вопросов в этом проекте · r — нет, и вот что вместо · q — стоп",
              "guard")], GUARD_BG)
        if self.guard_editing:
            add([("  правка аргументов: Enter — отправить, Esc — отмена", "dim")], GUARD_BG)
        if self._guard_replace:
            add([("  что сделать вместо: печатай в поле ввода, Enter — отправить отказ с этим текстом, Esc — отмена", "dim")], GUARD_BG)

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
        arrow = ("▾ " if self._exp(entry) else "▸ ") if items else ""
        add([(arrow, "cyan"),
             ("☑ " if self.glyph_mode == "emoji" else "• ", "cyan"), (head, "bold")])
        marks = {"done": "✓", "active": "◐", "pending": "○"}
        show = items if self._exp(entry) else [it for it in items
                                                if it.get("status") == "active"][:1]
        if not self._exp(entry) and not show and items:
            show = items[:1]
        for item in show:
            mark = marks.get(item.get("status", "pending"), "○")
            style = "dim" if item.get("status") == "done" else "text"
            add(_clip_line([("  " + mark + " ", "cyan"),
                            (item.get("text", ""), style)], width))
        if not self._exp(entry) and len(items) > len(show):
            add([("  · Ctrl+O — весь план, Ctrl+T — панель", "dim")])

    def _guards_for(self, tool_entry: Entry) -> list:
        """Подтверждения, относящиеся к этому тулу, в порядке прихода."""
        return [e for e in self.entries
                if e.kind == "guard" and e.guard_of
                and e.guard_of == tool_entry.entry_id]

    def _thinking_mode(self) -> str:
        """Режим показа рассуждений (ЗАДАЧА-19): hidden | summary | full.

        ui.show_thinking старше ui.thinking и не переименовывается:
        выключенный ui.show_thinking всегда значит hidden.
        """
        mode = str(self.settings.get("ui.thinking") or "summary")
        if mode == "hidden" or not self.settings.get("ui.show_thinking"):
            return "hidden"
        return mode if mode in ("summary", "full") else "summary"

    def _fade_last_line(self, entry: Entry, md_lines, now: float):
        """Проявление стрима (ЗАДАЧА-20): спаны последней строки ответа,
        где свежие символы ещё тусклые; None — проявления нет.

        token — весь последний кусок проявляется разом, char — каждый
        символ по своему возрасту из chunk_marks. Ступеней — 4, от
        приглушённого цвета темы к обычному; даты кусков ставит
        on_agent_event, часы здесь — только аргумент now.
        """
        mode = str(self.settings.get("ui.stream_fade") or "off")
        if mode == "off" or not self._anim_on():
            return None
        if self.color_level in ("16", "mono") or not entry.chunk_marks:
            return None
        if not md_lines:
            return None
        fade_s = max(0.05, float(self.settings.get("ui.stream_fade_ms") or 220) / 1000.0)
        last = md_lines[-1]
        tail_len = sum(len(s.text) for s in last)
        if not tail_len:
            return None
        marks = entry.chunk_marks

        def char_time(gi: int) -> float:
            for end, t in marks:
                if gi < end:
                    return t
            return marks[-1][1]

        entry_alpha = self._alpha(entry, now)
        muted = _color.palette_color("muted")
        out = []
        pos = len(entry.text) - tail_len
        changed = False
        for span in last:
            key, attrs = _STYLE_MAP.get(span.style, _STYLE_MAP["text"])
            fg = _color.palette_color(key)
            for ch in span.text:
                t = marks[-1][1] if mode == "token" else char_time(pos)
                pos += 1
                age = (now - t) / fade_s
                if age >= 1.0:
                    out.append((ch, fg, attrs))
                    continue
                changed = True
                step = max(0.0, int(max(0.0, age) * 4) / 4.0)
                col = _color.lerp(muted, fg, step)
                if entry_alpha < 1.0:
                    col = _color.lerp(_color.SPACE_BG, col, entry_alpha)
                out.append((ch, col, attrs))
        if not changed:
            return None
        if entry.streaming:
            out.append(("▌", muted, _color.DIM))
        return out

    def _fade_busy(self, now: float) -> bool:
        """Что-то ещё проявляется — нужен кадр; в простое — тишина."""
        if str(self.settings.get("ui.stream_fade") or "off") == "off":
            return False
        if not self._anim_on():
            return False
        fade_s = max(0.05, float(self.settings.get("ui.stream_fade_ms") or 220) / 1000.0)
        for entry in reversed(self.entries[-3:]):
            if entry.chunk_marks and now - entry.chunk_marks[-1][1] < fade_s:
                return True
        return False

    def _feed_lines(self, width: int, now: float):
        lines: list = []
        texts: list = []
        self._line_map = []
        thinking_mode = self._thinking_mode()
        for entry in self.entries:
            if (entry.kind == "guard" and entry.guard_of
                    and self._find("tool", entry.guard_of) is not None):
                continue  # рисуется внутри своего тула, между вызовом и результатом
            if entry.kind == "thinking" and not self._exp(entry) \
                    and thinking_mode == "hidden":
                continue  # /thinking скрыл показ рассуждений
            entry_lines = self._entry_lines(entry, width, now)
            if entry._cache_lines is entry_lines \
                    and entry._cache_texts is not None \
                    and len(entry._cache_texts) == len(entry_lines):
                entry_texts = entry._cache_texts
            else:
                entry_texts = ["".join(t for t, _f, _a in spans)
                               for spans, _bg in entry_lines]
            for i, line in enumerate(entry_lines):
                lines.append(line)
                texts.append(entry_texts[i])
                self._line_map.append((entry, i == 0))
            lines.append(([], None))
            texts.append("")
            self._line_map.append(None)
        if lines:
            lines.pop()
            texts.pop()
            self._line_map.pop()
        self._feed_texts = texts
        return lines

    # --- полоса прокрутки ленты (ЗАДАЧА-22) --------------------------------

    def _scrollbar_visible(self, total: int, feed_h: int) -> bool:
        mode = str(self.settings.get("ui.scrollbar") or "auto")
        if mode == "off":
            return False
        if mode == "on":
            return True
        return total > feed_h  # auto: только когда есть что листать

    def _draw_scrollbar(self, buf: Buffer, chat_w: int, feed_h: int,
                        total: int, max_scroll: int) -> None:
        """Полоса у правого края ЛЕНТЫ (при открытых панелях — не у края
        экрана): тонкая дорожка и ползунок. Геометрию последнего кадра
        запоминаем: по ней мышь считает попадание и тягу ползунка."""
        self._sb_geom = None
        if chat_w < 8 or feed_h < 2 \
                or not self._scrollbar_visible(total, feed_h):
            return
        if max_scroll > 0:
            thumb_h = min(feed_h, max(1, feed_h * feed_h // max(1, total)))
            span = feed_h - thumb_h
            thumb_y = int(round((max_scroll - self.scroll) * span
                                / max_scroll)) if span > 0 else 0
        else:
            thumb_h, thumb_y = feed_h, 0
        thumb_ch, track_ch = ("#", "|") if self.glyph_mode == "ascii" \
            else ("█", "│")
        x = chat_w - 1
        for row in range(feed_h):
            if thumb_y <= row < thumb_y + thumb_h:
                buf.set(x, row, thumb_ch, fg=_color.SILVER, bg=_color.SPACE_BG)
            else:
                buf.set(x, row, track_ch, fg=DIM_FG, bg=_color.SPACE_BG)
        self._sb_geom = (total, feed_h, max_scroll, thumb_y, thumb_h)

    def _scrollbar_hit(self, mouse: Mouse) -> bool:
        """Клик попал в колонку полосы прокрутки последнего кадра."""
        if self._sb_geom is None:
            return False
        chat_w, feed_h = self._feed_geom
        return (mouse.x - 1) == chat_w - 1 and 0 <= (mouse.y - 1) < feed_h

    def _scrollbar_press(self, mouse: Mouse) -> None:
        """Нажатие на полосу: по ползунку — захват тяги, по дорожке —
        страница в ту сторону."""
        _total, _feed_h, _max_scroll, thumb_y, thumb_h = self._sb_geom
        y = mouse.y - 1
        if thumb_y <= y < thumb_y + thumb_h:
            self._sb_drag = y - thumb_y
        elif y < thumb_y:
            self.scroll = min(self.scroll + self._page, 100000)
        else:
            self.scroll = max(0, self.scroll - self._page)

    def _scrollbar_move(self, mouse: Mouse) -> None:
        """Тяга ползунка: его верх едет за мышью, прокрутка — обратно
        пропорционально (та же формула, что у отрисовки, наоборот)."""
        _total, feed_h, max_scroll, _thumb_y, thumb_h = self._sb_geom
        span = feed_h - thumb_h
        if span <= 0 or max_scroll <= 0:
            return
        new_y = min(max(0, (mouse.y - 1) - (self._sb_drag or 0)), span)
        self.scroll = max_scroll - int(round(new_y * max_scroll / span))

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
        if pw >= 6:
            # Крестик закрытия — в правом конце заголовка (ЗАДАЧА-20);
            # попадание по нему считает _panel_mouse по геометрии панели.
            bg = grad[pw - 1] if pw - 1 < len(grad) else PANEL_BG
            buf.text(px + pw - 1, py, "✕", fg=DIM_FG, bg=bg)

    def _draw_panels(self, buf: Buffer, x: int, y: int, w: int, h: int, now: float,
                     tree=None) -> None:
        tree = tree if tree is not None else self.layout
        rects = tree.rects(x, y, w, h, hints=self._panel_hints(tree, w))
        self._panel_rects = dict(rects)
        for panel_id, (px, py, pw, ph) in rects.items():
            if pw <= 0 or ph <= 0:
                continue
            buf.fill_rect(px, py, pw, ph, " ", bg=PANEL_BG)
            with buf.clip(px, py, pw, ph, "панель " + panel_id):
                self._draw_one_panel(buf, tree, panel_id, px, py, pw, ph)
        self._draw_panel_dividers(buf, rects)

    def _draw_one_panel(self, buf: Buffer, tree, panel_id: str,
                        px: int, py: int, pw: int, ph: int) -> None:
        content = self._panel_content(panel_id)
        title = content.title if content is not None else panel_id
        focused = bool(self.panel_focus and tree.focused == panel_id)
        self._draw_panel_header(buf, px, py, pw, title, focused)
        if content is None:
            return
        content.height_hint = max(0, ph - 1)
        if content.draw_into(buf, self, px, py + 1, pw, max(0, ph - 1)):
            return
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
        if self._anim_on():
            alpha = ease_in_out((now - picker.born) / 0.25)
        lines, sel_row = picker.render_block(max(10, width - 2), max(3, height - 1))
        # Оверлей закрывает свою зону целиком: раньше под коротким списком
        # просвечивала лента и мешалась с пунктами.
        buf.fill_rect(0, 0, width, height, " ", bg=PICKER_BG)
        for row, spans in enumerate(lines[:height]):
            bg = SELECT_BG if row == sel_row else PICKER_BG
            buf.fill_rect(0, row, width, 1, " ", bg=bg)
            painted = self._paint([(text, self._PICKER_STYLES.get(role, "text"))
                                   for text, role in spans], alpha)
            x = 1
            for text, fg, attrs in painted:
                x += buf.text(x, row, text, fg=fg, bg=bg, attrs=attrs)
            # Кликабельная зона строки пункта — из карты строк выбиралки.
            item = picker._row_map.get(row)
            if item is not None:
                self.zones.append((0, row, width, row + 1, ("picker", item)))
            elif any(role == "hint" for _t, role in spans):
                self._key_zones(buf, "".join(t for t, _r in spans), 1, row, width)

    def _status_line(self, buf: Buffer, y: int, cols: int, now: float) -> None:
        buf.fill_rect(0, y, cols, 1, " ", bg=STATUS_BG)
        variant = str(self.settings.get("ui.statusline") or "full")
        if variant == "off":
            return  # пустая строка: только фон (настройка ui.statusline)
        phase = _moon.context_phase(self.context_pct, self.glyph_mode)
        pct = str(self.context_pct)
        if variant == "compact":
            left = " " + phase + " контекст " + pct + "% · "
            buf.text(0, y, left, fg=_color.SILVER, bg=STATUS_BG)
            mx = str_width(left)
            buf.text(mx, y, self.model, fg=_color.SILVER, bg=STATUS_BG)
            self.zones.append((mx, y, mx + str_width(self.model), y + 1,
                               ("status", "model")))
            left += self.model
            right = "$" + format(self.cost, ".4f")
            rx = max(str_width(left) + 1, cols - str_width(right) - 1)
            if rx < cols:
                buf.text(rx, y, right, fg=DIM_FG, bg=STATUS_BG, attrs=_color.DIM)
            return
        # Полный вариант — кликабельными сегментами (ЗАДАЧА-20): каждый
        # сегмент открывает ту же выбиралку, что и его команда. На узком
        # экране первыми уходят тема, мышление и effort — режим, модель,
        # отправка и стоимость остаются всегда.
        effort = str(self.settings.get("model.effort") or "auto")
        from ..core import newmoon as _nm

        segments = [
            (" " + phase + " контекст " + pct + "%", None, "core"),
            (" · ", None, "core"),
            (self.mode, ("status", "mode"), "core"),
            (" · ", None, "core"),
            (self.model, ("status", "model"), "core"),
            (" · effort ", None, "effort"),
            (effort, ("status", "effort"), "effort"),
            (" · мышление ", None, "thinking"),
            (self._thinking_mode(), ("status", "thinking"), "thinking"),
            (" · отправка ", None, "core"),
            (self._send_mode(), ("status", "send"), "core"),
            (" · тема ", None, "theme"),
            (str(self.settings.get("ui.theme") or "luna"), ("status", "theme"), "theme"),
        ]
        if _nm.active():
            segments.insert(0, (" 🌑 новолуние ·", ("status", "newmoon"), "core"))
        if self.updater.available:
            segments.insert(0, (" ↻ обновление ·", ("status", "update"), "core"))
        if self.layout.leaves() and not self.panel_focus:
            segments.append((" · ", None, "extra"))
            segments.append(("F6 — панели", ("key", Key("f6")), "extra"))
        if self.settings.get("ui.vim_mode"):
            segments.append((" · vim:" + ("normal" if self._vim_normal else "insert"), None, "extra"))
        right_w = str_width("$" + format(self.cost, ".4f")) + 1
        if self.queue:
            right_w += str_width(" · очередь: " + str(len(self.queue)))
        for drop in ("extra", "theme", "thinking", "effort"):
            total = sum(str_width(t) for t, _a, _tag in segments)
            if total + 1 + right_w <= cols:
                break
            segments = [s for s in segments if s[2] != drop]
        x = 0
        avail = max(0, cols - right_w - 1)
        for text, action, _tag in segments:
            w = str_width(text)
            if x + w > avail:
                # Даже без лишних сегментов не влезло (узкий терминал) —
                # режем с многоточием: стоимость справа видна всегда.
                room = avail - x - 1
                if room > 0:
                    cut = _cells_slice(text, 0, room - 1) + "…"
                    buf.text(x, y, cut, fg=_color.SILVER, bg=STATUS_BG)
                    if action is not None:
                        self.zones.append((x, y, x + str_width(cut), y + 1, action))
                    x += str_width(cut)
                break
            buf.text(x, y, text, fg=_color.SILVER, bg=STATUS_BG)
            if action is not None and w:
                self.zones.append((x, y, x + w, y + 1, action))
            x += w
        cost = format(self.cost, ".4f")
        right = "$" + cost
        if self.queue:
            right += " · очередь: " + str(len(self.queue))
        if self.busy:
            # Декоративная фраза — только если влезает целиком рядом со
            # стоимостью; иначе один глиф крутилки, иначе ничего.
            phrase, alpha = self.wheel.text_alpha(now)
            glyph = self.spinner.glyph(now)
            room = cols - x - str_width(right) - 2
            for spin in ("  " + glyph + " " + phrase + "…", "  " + glyph):
                if str_width(spin) <= room:
                    fg = _color.lerp(STATUS_BG, _color.AMBER, alpha) \
                        if len(spin) > 3 else _color.AMBER
                    buf.text(x, y, spin, fg=fg, bg=STATUS_BG)
                    x += str_width(spin)
                    break
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
                manual = self.layout.column_w
                if self._saved_cols and cols < self._saved_cols:
                    # Терминал уже сохранённого: ужать пропорционально,
                    # само сохранённое значение не трогаем (ЗАДАЧА-20).
                    manual = max(16, manual * cols // self._saved_cols)
                target = min(max(16, manual), max(24, cols - 24))
            else:
                target = max(24, cols // 3)
        if self.layout.leaves():
            self._panel_ghost = self.layout.clone()
        if not self._anim_on():
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
        """Кадр целиком; длительность замеряем — ui.perf=auto по ней
        решает, не пора ли в слабый режим (см. _note_frame_time)."""
        started = time.perf_counter()
        try:
            return self._draw_frame(cols, rows, now)
        finally:
            self._note_frame_time(time.perf_counter() - started)

    def _draw_frame(self, cols: int, rows: int, now: float):
        # Слабый режим включился сам на прошлых кадрах — предупредить
        # строкой в ленту, один раз за сессию.
        if self._auto_low and not self._perf_warned:
            self._perf_warned = True
            self._add(Entry("notice", text="Кадры даются этой машине тяжело — "
                                           "включила слабый режим интерфейса: "
                                           "реже кадры, без проявления стрима "
                                           "и звёзд. Насовсем: luna set ui.perf "
                                           "low; вернуть полный: luna set "
                                           "ui.perf full"))
        # Долгое нажатие на ленту — выделение (жест ui.drag_scroll):
        # палец держат на месте дольше порога — дальше тяга выделяет.
        drag = self._feed_drag
        if drag is not None and not drag["moved"] and not drag["select"] \
                and now - drag["t"] >= self._LONG_PRESS_SEC:
            drag["select"] = True
            if self._sel_anchor is None:
                self._sel_anchor = drag["pos"]
                self.selection = (drag["pos"], drag["pos"])
        # Тема — на лету: настройку ui.theme могли сменить /theme, тулом
        # luna или правкой файла; палитра применяется до отрисовки кадра.
        theme_name = str(self.settings.get("ui.theme") or "luna")
        if theme_name != self._applied_theme:
            _themes.apply_theme(theme_name)
            self._applied_theme = theme_name
        self._geom_cols = cols
        self.zones = []  # карта кликабельных зон строится заново на кадр
        buf = Buffer(cols, rows)
        buf.clear(bg=_color.SPACE_BG)
        panel_w = self._panel_width(cols, now)
        nib_h = 1 if (self._nibbler_line and now < self._nibbler_until) else 0
        chat_w = cols - panel_w
        guard = self.pending_guard
        ask = self.pending_ask
        # Вместо ввода — строка подсказок ответа: она же ряд кнопок.
        guard_hint = (guard is not None and not self.guard_editing
                      and not self._guard_replace) or (
            guard is None and ask is not None and not self.editor.text)
        if guard is not None and not self.guard_editing and not self._guard_replace:
            # Подсказка — кнопки ответа: переносом в ширину ленты, иначе
            # хвост с «q — стоп» уезжал за край и терялся.
            input_lines = wrap_text("Enter — да · n — нет · e — править · "
                                    "a — без вопросов · p — в проекте без вопросов · "
                                    "r — нет, и вот что вместо · q — стоп",
                                    max(4, chat_w - 3))[:4]
            cursor_pos = (0, 0)
            input_h = len(input_lines)
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
            completion_h = min(10, len(self.cmd_completions))
        elif self.completions:
            completion_h = min(5, len(self.completions))
        meta_lines = self._meta_lines(now)
        meta_h = len(meta_lines)
        feed_h = max(1, rows - 1 - input_h - completion_h - nib_h - meta_h)
        self._page = feed_h
        # Под полосу прокрутки — постоянный зазор: полоса не садится на
        # последний символ строк, а лента не перетекает, когда она появилась.
        sb_gap = 0 if str(self.settings.get("ui.scrollbar") or "auto") == "off" else 1
        feed_w = max(4, chat_w - sb_gap)
        lines = self._feed_lines(feed_w, now)
        # Скролл держится на месте, пока Луна пишет: scroll считается от
        # низа, поэтому выросшую ленту добавляем к нему — вид не едет.
        prev = getattr(self, "_feed_prev", None)
        if self.scroll > 0 and prev is not None and prev[0] == feed_w \
                and self.settings.get("ui.scroll_hold", True):
            self.scroll += len(lines) - prev[1]
        self._feed_prev = (feed_w, len(lines))
        max_scroll = max(0, len(lines) - feed_h)
        self.scroll = max(0, min(self.scroll, max_scroll))
        end = len(lines) - self.scroll
        start = max(0, end - feed_h)
        # Геометрия и тексты ленты — для выделения мышью и копирования (п. 4):
        # строки хранятся все, first — индекс первой видимой на этом кадре.
        # Сами тексты уже собрал _feed_lines (неизменные — из кэша записей).
        self._feed_first = start
        self._feed_geom = (chat_w, feed_h)
        for row, (spans, bg) in enumerate(lines[start:end]):
            if bg is not None:
                buf.fill_rect(0, row, chat_w, 1, " ", bg=bg)
            x = 0
            with buf.clip(0, 0, feed_w, feed_h, "лента"):
                for text, fg, attrs in spans:
                    x += buf.text(x, row, text, fg=fg, bg=bg or _color.SPACE_BG, attrs=attrs)
            # Зоны ленты: заголовок раскрываемого блока и ссылки/пути.
            mapped = self._line_map[start + row] if start + row < len(self._line_map) else None
            if mapped is not None and mapped[1] and self._expandable(mapped[0]):
                self.zones.append((0, row, chat_w, row + 1, ("block", mapped[0])))
            elif mapped is not None and mapped[0].kind == "thinking" \
                    and self._expandable(mapped[0]):
                # Тело мыслей — тоже кнопка, но мягкая: по отпусканию без
                # протяжки (_soft_click), чтобы выделение мышью работало.
                self.zones.append((0, row, chat_w, row + 1, ("soft_block", mapped[0])))
            line_text = self._feed_texts[start + row] if start + row < len(self._feed_texts) else ""
            for a, b, token in _find_links(line_text):
                self.zones.append((a, row, b + 1, row + 1, ("link", token)))
            if mapped is not None and mapped[0].status == "pending" \
                    and mapped[0].kind in ("guard", "ask"):
                # Подсказки ожидающего вопроса — кнопки; у отвеченного
                # они уже ничего не значат, а модельный текст не трогаем.
                self._key_zones(buf, line_text, 0, row, chat_w)
        self._draw_selection(buf, start, feed_h)
        self._draw_scrollbar(buf, chat_w, feed_h, len(lines), max_scroll)
        if panel_w:
            tree = self.layout if self.layout.leaves() else (self._panel_ghost or self.layout)
            total_h = feed_h + input_h + completion_h + nib_h + meta_h
            hint_lines: list = []
            if self.panel_focus and self.layout.leaves():
                # В режиме панелей внизу колонки — подсказка управления.
                hint_lines = wrap_text(self._PANEL_HINT, max(4, panel_w - 2))[:5]
            with buf.clip(chat_w, 0, panel_w, total_h, "панели"):
                self._draw_panels(buf, chat_w, 0, panel_w,
                                  total_h - len(hint_lines), now, tree=tree)
            for i, line in enumerate(hint_lines):
                hy = total_h - len(hint_lines) + i
                with buf.clip(chat_w, hy, panel_w, 1, "подсказка панелей"):
                        buf.text(chat_w + 1, hy, line, fg=DIM_FG, bg=PANEL_BG,
                             attrs=_color.DIM)
                self._key_zones(buf, line, chat_w + 1, hy, cols)
        else:
            self._panel_rects = {}
        cy = feed_h
        bottom = buf.clip(0, feed_h, chat_w, rows - 1 - feed_h, "низ чата")
        bottom.__enter__()
        if self.cmd_completions:
            # Окно списка команд вокруг выбранной: список полный (раздел 5
            # ЗАДАЧИ-20), показ прокручивается за completion_index.
            total = len(self.cmd_completions)
            win_start = max(0, min(self.completion_index - completion_h + 1,
                                   total - completion_h))
            for j in range(completion_h):
                i = win_start + j
                if i >= total:
                    break
                name, desc = self.cmd_completions[i]
                style_fg = _color.CYAN if i == self.completion_index else DIM_FG
                attrs = _color.BOLD if i == self.completion_index else _color.DIM
                buf.text(2, cy + j,
                         _cells_slice("/" + name + " — " + desc, 0,
                                      max(0, chat_w - 3)),
                         fg=style_fg, bg=_color.SPACE_BG, attrs=attrs)
        else:
            for i, candidate in enumerate(self.completions[:completion_h]):
                style_fg = _color.CYAN if i == self.completion_index else DIM_FG
                attrs = _color.BOLD if i == self.completion_index else _color.DIM
                buf.text(2, cy + i,
                         _cells_slice("@" + candidate, 0, max(0, chat_w - 3)),
                         fg=style_fg, bg=_color.SPACE_BG, attrs=attrs)
        if nib_h:
            # Строка Ниблера — между лентой и вводом; в конце жизни затухает.
            text, severity = self._nibbler_line
            fg = _color.ERR if severity == "serious" else _color.VIOLET
            left = self._nibbler_until - now
            if left < 1.5:
                fg = _color.lerp(_color.SPACE_BG, fg, max(0.0, left / 1.5))
            label = "🐾 Ниблер: " if self.glyph_mode == "emoji" else "Ниблер: "
            # Режем по ширине ЛЕНТЫ, а не экрана: раньше длинная реплика
            # уезжала поверх колонки панелей — Ниблер писал вне раскладки
            # (ЗАДАЧА-22). Весь вывод — только внутри своего места кадра.
            buf.text(2, feed_h + completion_h,
                     _cells_slice(label + text, 0, max(0, chat_w - 3)), fg=fg,
                     bg=_color.SPACE_BG, attrs=_color.ITALIC)
        my = feed_h + completion_h + nib_h
        for i, line in enumerate(meta_lines):
            # Подсказка, очередь и режим отправки — строкой над вводом.
            buf.text(2, my + i, _cells_slice(line, 0, max(3, chat_w - 3)),
                     fg=DIM_FG, bg=_color.SPACE_BG, attrs=_color.DIM)
            self._key_zones(buf, line, 2, my + i, chat_w - 1)
        iy = my + meta_h
        for i, line in enumerate(input_lines):
            prefix = "› " if i == 0 else "  "
            fg = _color.AMBER if guard is not None else _color.CYAN
            attrs = _color.BOLD if guard is not None or i == 0 else 0
            buf.text(0, iy + i, prefix, fg=fg, bg=_color.SPACE_BG, attrs=_color.BOLD)
            buf.text(2, iy + i, line, fg=_color.SILVER, bg=_color.SPACE_BG, attrs=attrs)
            if guard_hint:
                self._key_zones(buf, line, 2, iy + i, chat_w)
        bottom.__exit__(None, None, None)
        if self._pastes and any(ph in self.editor.text for ph in self._pastes):
            # Клик по полю ввода со свёрнутой вставкой — развернуть её.
            self.zones.append((0, iy, chat_w, iy + input_h, ("paste",)))
        with buf.clip(0, rows - 1, cols, 1, "статус"):
            self._status_line(buf, rows - 1, cols, now)
        if self.picker is not None:
            with buf.clip(0, 0, chat_w, feed_h, "выбиралка"):
                self._draw_picker(buf, chat_w, feed_h, now)
            return buf, None
        if guard is not None and not self.guard_editing and not self._guard_replace:
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


def _open_external(target: str) -> None:
    """Открыть ссылку/файл снаружи (клик по ленте, ui.click_link=open)."""
    import subprocess
    import sys
    import webbrowser

    if target.startswith(("http://", "https://")):
        webbrowser.open(target)
        return
    if sys.platform == "win32":
        os.startfile(target)  # noqa: S606 — путь из ленты, действие человека
    elif sys.platform == "darwin":
        subprocess.Popen(["open", target])
    else:
        subprocess.Popen(["xdg-open", target])


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


RELAUNCH_NEWMOON = 77  # код run_chat: перезапуститься в Новолунии
RELAUNCH_RELOAD = 78   # код run_chat: горячая перезагрузка, снимок — в HANDOFF
HANDOFF = None         # последний снимок сессии (ui/updater.py: Updater.snapshot)


def run_chat(started_at: float = 0.0, *, splash_first: bool = False, fake: bool = True,
             continue_last: bool = False, resume: str | None = None,
             open_pult: bool = False, newmoon: bool = False,
             handoff: dict | None = None) -> int:
    """Полноэкранный чат: терминал, цикл ввода, источник событий.

    handoff — снимок сессии от горячей перезагрузки: без заставки и без
    выбора сессии, лента и память — из него (ui/updater.py).
    """
    global HANDOFF
    import sys

    from ..core import newmoon as _nm
    from .errtrap import ErrorTrap
    from .splash import play_splash

    if newmoon:
        # До DataDir и журнала: пути уже должны смотреть во временную папку.
        _nm.enable()
        continue_last, resume = False, None
    settings = Settings()
    if _nm.active():
        settings.ephemeral = True  # настройки режима — только в памяти
        settings.set("chat.session_log", False)
        settings.set("ui.theme", "newmoon")
    if handoff:
        splash_first = False
        settings._session.update(handoff.get("settings_session") or {})
        _nm.restore_confirmed(handoff.get("confirmed") or ())
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
    if handoff:
        journal, restored, picker_first, open_note = None, list(handoff.get("tape") or []), False, None
        if handoff.get("journal_path"):
            from ..core.sessions import SessionJournal

            journal, restored = SessionJournal.resume(data, handoff["journal_path"])
    else:
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
                # Хуки живого терминала (ЗАДАЧА-20): уровень цвета для fade,
                # звонок/уведомления, заголовок окна, открытие ссылок, фон.
                app.color_level = term.caps.color_level
                app.notify_hook = term.write
                app.title_hook = lambda t: term.write("\x1b]2;" + t + "\a")
                app.open_hook = _open_external
                if os.name == "posix":
                    def _suspend() -> None:
                        term.suspend()
                        try:
                            import signal as _signal

                            os.kill(os.getpid(), _signal.SIGTSTP)
                        finally:
                            term.resume()
                            app._redraw = True

                    app.suspend_hook = _suspend
                app._update_title()
                if restored:
                    app.restore(restored)
                    app.attach_history(restored)
                if handoff:
                    if journal is None:
                        app.tape = list(restored)
                    if handoff.get("titled"):
                        app.session_title = str(handoff.get("title") or "")
                        app._session_titled = True
                    if handoff.get("draft"):
                        app.editor.set_text(str(handoff["draft"]))
                    app._add(Entry("notice", text=handoff.get("note") or
                                   "↻ Луна перезагрузилась на лету: свежий код, сессия на месте."))
                elif restored:
                    pass
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
                # luna setup и первый запуск без модели — сразу в пульт ИИ.
                if handoff:
                    pass
                elif open_pult or (not fake and not settings.get("model.main")):
                    app.open_pult()
                elif _nm.active():
                    app._add(Entry("notice", text=phrases.event_phrase("newmoon")
                                   or "Новолуние."))
                    app.open_newmoon_confirm()
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
                if app is not None:
                    # Снимок — всегда: упадёт новый код после горячей
                    # перезагрузки — прежний поднимется с последнего места.
                    try:
                        HANDOFF = app.handoff or app.updater.snapshot()
                    except Exception:  # noqa: BLE001
                        pass
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
    if app is not None and getattr(app, "relaunch", "") == "reload":
        return RELAUNCH_RELOAD
    if _nm.active():
        # Экран и прокрутка терминала — чисто; временная папка — прочь.
        sys.stdout.write("[2J[3J[H")
        sys.stdout.flush()
        _nm.wipe()
        print("🌑 Новолуние закончилось. Следов нет.")
        return 0
    if app is not None and getattr(app, "relaunch", "") == "newmoon":
        return RELAUNCH_NEWMOON
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
