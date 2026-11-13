"""Терминал: возможности, raw-режим, альтернативный экран, ожидание ввода.

В стандартной библиотеке Windows нет curses — он и не нужен: вывод — ANSI,
ввод — сырые байты (POSIX) или ReadConsoleInputW (Windows, ctypes).
Терминал восстанавливается всегда: выход, исключение, Ctrl+C, SIGTERM —
восстановление идемпотентно и висит ещё и на atexit.

В простое read_events спит в блокирующем select / WaitForSingleObject:
никаких циклов опроса.
"""

from __future__ import annotations

import atexit
import locale
import os
import shutil
import signal
import sys
from dataclasses import dataclass

from . import keys as _keys

IS_WINDOWS = sys.platform == "win32"
ESC_FLUSH_TIMEOUT = 0.05  # одиночный Esc не должен ждать дольше

ENTER_SEQ = "\x1b[?1049h\x1b[?25l\x1b[?1000h\x1b[?1002h\x1b[?1003h\x1b[?1006h\x1b[?2004h\x1b[H"
EXIT_SEQ = "\x1b[?2004l\x1b[?1006l\x1b[?1003l\x1b[?1002l\x1b[?1000l\x1b[0m\x1b[?25h\x1b[?1049l"


@dataclass
class Caps:
    color_level: str  # truecolor | 256 | 16 | mono
    unicode: bool
    is_tty: bool
    cols: int
    rows: int
    term: str = ""
    reason: str = ""  # как определили цвет — для doctor
    emoji: bool = True
    emoji_reason: str = ""  # почему эмодзи включены или выключены — для doctor

    @property
    def glyph_mode(self) -> str:
        """Режим глифов фаз и значков: emoji | narrow | ascii (см. ui/moon.py)."""
        if self.emoji:
            return "emoji"
        return "narrow" if self.unicode else "ascii"


def _env_unicode(env) -> bool:
    for name in ("LC_ALL", "LC_CTYPE", "LANG"):
        v = env.get(name, "")
        if "utf" in v.lower().replace("-", ""):
            return True
    return False


def detect_caps(env=None, out=None, settings=None) -> Caps:
    """Определить возможности терминала; настройки перекрывают автоопределение."""
    env = os.environ if env is None else env
    out = sys.stdout if out is None else out
    is_tty = bool(getattr(out, "isatty", lambda: False)())
    size = shutil.get_terminal_size((80, 24))
    term_name = env.get("TERM", "")
    level = "mono"
    reason = "не терминал"
    if is_tty:
        if "NO_COLOR" in env:
            level, reason = "mono", "NO_COLOR задан"
        elif env.get("WT_SESSION"):
            level, reason = "truecolor", "Windows Terminal (WT_SESSION)"
        elif env.get("COLORTERM", "").lower() in ("truecolor", "24bit"):
            level, reason = "truecolor", "COLORTERM=" + env.get("COLORTERM", "")
        elif "256" in term_name or env.get("TERM_PROGRAM") in ("iTerm.app", "WezTerm", "ghostty"):
            level, reason = "256", f"TERM={term_name}"
        elif term_name in ("", "dumb"):
            shown_term = term_name or "не задан"
            level, reason = "mono", f"TERM={shown_term}"
        else:
            level, reason = "16", f"TERM={term_name}"
    unicode_ok = False
    emoji_ok = False
    emoji_reason = "вывод не в терминал"
    if is_tty:
        encoding = (getattr(out, "encoding", None) or locale.getpreferredencoding(False) or "")
        unicode_ok = "utf" in encoding.lower().replace("-", "") or _env_unicode(env)
        if IS_WINDOWS and not unicode_ok:
            unicode_ok = _windows_cp() == 65001
        if term_name == "linux":
            # Последним: специфичный запрет сильнее общего разрешения.
            # Консоль Linux: юникод урезанный, эмодзи нет — и кодовая
            # страница Windows 65001 не должна это перебивать (РЕВЬЮ-01, п. 2).
            unicode_ok = False
        # Эмодзи — отдельная возможность, а не следствие юникода: ConPTY-хосты
        # пересобирают поток и считают эмодзи шириной 1, поэтому на Windows
        # без WT_SESSION они в auto выключены (РЕВЬЮ-01, раунд 2, п. 3).
        if IS_WINDOWS:
            if env.get("WT_SESSION"):
                emoji_ok, emoji_reason = True, "Windows Terminal (WT_SESSION)"
            else:
                emoji_ok, emoji_reason = False, "Windows без WT_SESSION: ConPTY-хост"
        elif unicode_ok:
            emoji_ok, emoji_reason = True, "POSIX-терминал с юникодом"
        else:
            emoji_reason = "терминал без юникода"
    unicode_setting = None
    if settings is not None:
        color_setting = settings.get("ui.color")
        if color_setting and color_setting != "auto":
            level, reason = color_setting, "настройка ui.color"
        unicode_setting = settings.get("ui.unicode")
        if unicode_setting == "on":
            unicode_ok = True
        elif unicode_setting == "off":
            unicode_ok = False
        emoji_setting = settings.get("ui.emoji")
        if emoji_setting == "on":
            emoji_ok, emoji_reason = True, "настройка ui.emoji"
        elif emoji_setting == "off":
            emoji_ok, emoji_reason = False, "настройка ui.emoji"
        if unicode_setting == "off":
            # ASCII-режим сильнее: при ui.unicode=off глифы — только ASCII.
            emoji_ok, emoji_reason = False, "настройка ui.unicode=off: ASCII-режим"
    return Caps(level, unicode_ok, is_tty, size.columns, size.lines, term_name, reason,
                emoji_ok, emoji_reason)


def _windows_cp() -> int:
    try:
        import ctypes

        return int(ctypes.windll.kernel32.GetConsoleOutputCP())
    except Exception:  # noqa: BLE001
        return 0


class Terminal:
    def __init__(self, settings=None, instream=None, outstream=None):
        self.settings = settings
        self.instream = sys.stdin if instream is None else instream
        self.out = sys.stdout if outstream is None else outstream
        for s in (self.out,):
            try:
                s.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass
        self.caps = detect_caps(out=self.out, settings=settings)
        self.parser = _keys.KeyParser()
        self._in_session = False
        self._restore = None
        self._wake_r = None
        self._wake_w = None
        self._old_handlers = {}
        atexit.register(self._cleanup)

    # --- вывод -------------------------------------------------------------

    def write(self, text: str) -> None:
        try:
            self.out.write(text)
            self.out.flush()
        except UnicodeEncodeError:
            # Одиночная половинка UTF-16 (суррогат) в тексте: склеить пары,
            # одиночки — заменить. Раньше это ловилось как «пайп закрыли»
            # и экран навсегда уходил в /dev/null.
            self.out.write(fix_surrogates(text))
            self.out.flush()
        except (BrokenPipeError, OSError, ValueError):
            try:  # вывод обрезали (пайп закрыли) — дальше молчим в /dev/null
                os.dup2(os.open(os.devnull, os.O_WRONLY), self.out.fileno())
            except (OSError, ValueError, AttributeError):
                pass

    def size(self) -> tuple[int, int]:
        s = shutil.get_terminal_size((self.caps.cols, self.caps.rows))
        return s.columns, s.lines

    # --- сессия полноэкранного режима ---------------------------------------

    def __enter__(self) -> "Terminal":
        if self._in_session:
            return self
        self._in_session = True
        if IS_WINDOWS:
            self._restore = _windows_raw_mode()
        else:
            self._restore = _posix_raw_mode(self.instream)
            self._open_wake_pipe()
        self._install_signals()
        self.write(ENTER_SEQ)
        return self

    def __exit__(self, *exc) -> bool:
        self._cleanup()
        return False

    def suspend(self) -> None:
        """Пауза полноэкранного режима: вернуть терминал обычному миру
        (внешний редактор из чата). После — resume()."""
        self._cleanup()

    def resume(self) -> None:
        """Вернуться в полноэкранный режим после suspend()."""
        if not self._in_session:
            self.__enter__()

    def _cleanup(self) -> None:
        if not self._in_session:
            return
        self._in_session = False
        try:
            self.write(EXIT_SEQ)
        finally:
            self._restore_signals()
            if self._restore is not None:
                try:
                    self._restore()
                except Exception:  # noqa: BLE001
                    pass
                self._restore = None
            self._close_wake_pipe()

    def _install_signals(self) -> None:
        def handler(signum, frame):  # noqa: ARG001 — сигнатура обработчика
            self._cleanup()
            raise SystemExit(128 + signum)

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                self._old_handlers[sig] = signal.signal(sig, handler)
            except (ValueError, OSError):
                pass
        if not IS_WINDOWS and hasattr(signal, "SIGWINCH"):
            def on_winch(signum, frame):  # noqa: ARG001
                if self._wake_w is not None:
                    try:
                        os.write(self._wake_w, b"R")
                    except OSError:
                        pass

            try:
                self._old_handlers[signal.SIGWINCH] = signal.signal(signal.SIGWINCH, on_winch)
            except (ValueError, OSError):
                pass

    def _restore_signals(self) -> None:
        for sig, old in self._old_handlers.items():
            try:
                signal.signal(sig, old)
            except (ValueError, OSError):
                pass
        self._old_handlers = {}

    def _open_wake_pipe(self) -> None:
        try:
            self._wake_r, self._wake_w = os.pipe()
            os.set_blocking(self._wake_r, False)
            os.set_blocking(self._wake_w, False)
        except OSError:
            self._wake_r = self._wake_w = None

    def _close_wake_pipe(self) -> None:
        for fd in (self._wake_r, self._wake_w):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._wake_r = self._wake_w = None

    # --- ввод ----------------------------------------------------------------

    def read_events(self, timeout: float | None) -> list:
        """Ждать ввода до timeout секунд (None — без срока) и вернуть события."""
        if IS_WINDOWS:
            return self._read_events_windows(timeout)
        return self._read_events_posix(timeout)

    def _read_events_posix(self, timeout) -> list:
        import select

        in_fd = self.instream.fileno()
        wait = timeout
        if self.parser.pending and (wait is None or wait > ESC_FLUSH_TIMEOUT):
            wait = ESC_FLUSH_TIMEOUT  # висящий Esc разрешаем быстро
        fds = [in_fd]
        if self._wake_r is not None:
            fds.append(self._wake_r)
        try:
            ready, _, _ = select.select(fds, [], [], wait)
        except (OSError, ValueError):
            return []
        events: list = []
        if self._wake_r is not None and self._wake_r in ready:
            try:
                os.read(self._wake_r, 64)
            except OSError:
                pass
            cols, rows = self.size()
            if (cols, rows) != (self.caps.cols, self.caps.rows):
                self.caps.cols, self.caps.rows = cols, rows
                events.append(_keys.Resize(cols, rows))
        if in_fd in ready:
            try:
                data = os.read(in_fd, 65536)
            except OSError:
                data = b""
            if not data:
                events.append(_keys.Key("eof"))
            else:
                events.extend(self.parser.feed(data))
        if not ready:
            events.extend(self.parser.flush())
        return events

    def _read_events_windows(self, timeout) -> list:
        events = _windows_read(timeout, self.parser)
        cols, rows = self.size()
        if (cols, rows) != (self.caps.cols, self.caps.rows):
            self.caps.cols, self.caps.rows = cols, rows
            events.append(_keys.Resize(cols, rows))
        return events


# --- POSIX raw -------------------------------------------------------------


def _posix_raw_mode(stream):
    """Включить raw-режим termios; вернуть функцию восстановления."""
    try:
        import termios
        import tty

        fd = stream.fileno()
        old = termios.tcgetattr(fd)
        tty.setraw(fd)
        # setraw гасит и ONLCR у вывода? Нет: setraw трогает и c_oflag (OPOST).
        # Возвращаем постобработку вывода, чтобы \n в обычном выводе жил как обычно;
        # рендерер пишет \x1b-последовательности без \n, ему всё равно.
        new = termios.tcgetattr(fd)
        new[1] = new[1] | termios.OPOST
        termios.tcsetattr(fd, termios.TCSANOW, new)

        def restore() -> None:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

        return restore
    except Exception:  # noqa: BLE001 — не tty (пайп): raw не нужен
        return None


# --- Windows: raw-режим и ввод через ctypes ---------------------------------

_ENABLE_VT_PROCESSING = 0x0004
_ENABLE_VT_INPUT = 0x0200
# PROCESSED|LINE|ECHO гасятся маской. PROCESSED (0x0001) — критично:
# без него Ctrl+C не превращается в сигнал SIGINT, а приходит обычным
# событием клавиши в обработчик чата (ЗАДАЧА-18: Ctrl+C — это клавиша,
# а не выход). На POSIX то же даёт tty.setraw (ISIG выключен, байт 0x03).
_DISABLE_LINE_ECHO_PROCESSED = ~(0x0001 | 0x0002 | 0x0004)  # PROCESSED|LINE|ECHO


def _windows_raw_mode():
    """Включить VT-вывод и VT-ввод; вернуть восстановление. НЕ ПРОВЕРЕНО на живой Windows."""
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        saved = []
        for handle_id, is_input in ((-11, False), (-10, True)):
            handle = kernel32.GetStdHandle(handle_id)
            mode = ctypes.c_uint32()
            if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                continue
            saved.append((handle, mode.value))
            new = mode.value
            if is_input:
                new = (new | _ENABLE_VT_INPUT) & _DISABLE_LINE_ECHO_PROCESSED
            else:
                new = new | _ENABLE_VT_PROCESSING
            kernel32.SetConsoleMode(handle, new)

        def restore() -> None:
            for handle, old in saved:
                kernel32.SetConsoleMode(handle, old)

        return restore
    except Exception:  # noqa: BLE001
        return None


def fix_surrogates(text: str) -> str:
    """Пары половинок UTF-16 — в настоящий символ, одиночки — в «�»."""
    return text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")


_VK = {0x25: "left", 0x26: "up", 0x27: "right", 0x28: "down", 0x24: "home",
       0x23: "end", 0x21: "pageup", 0x22: "pagedown", 0x2D: "insert", 0x2E: "delete"}
_VK.update({0x70 + i: f"f{i + 1}" for i in range(12)})


def _windows_read(timeout, parser) -> list:
    """Ожидание консольного ввода без опроса: WaitForSingleObject + ReadConsoleInputW.

    С VT-вводом мышь и клавиши приходят как поток символов (их разбирает parser);
    записи с нулевым UnicodeChar — это служебные клавиши по виртуальным кодам.
    НЕ ПРОВЕРЕНО на живой Windows.
    """
    import ctypes
    from ctypes import wintypes

    class KEY_EVENT_RECORD(ctypes.Structure):
        _fields_ = [("bKeyDown", wintypes.BOOL), ("wRepeatCount", wintypes.WORD),
                    ("wVirtualKeyCode", wintypes.WORD), ("wVirtualScanCode", wintypes.WORD),
                    ("UnicodeChar", wintypes.WCHAR), ("dwControlKeyState", wintypes.DWORD)]

    class MOUSE_EVENT_RECORD(ctypes.Structure):
        _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT),
                    ("dwButtonState", wintypes.DWORD), ("dwControlKeyState", wintypes.DWORD),
                    ("dwEventFlags", wintypes.DWORD)]

    class UNION(ctypes.Union):
        _fields_ = [("KeyEvent", KEY_EVENT_RECORD), ("MouseEvent", MOUSE_EVENT_RECORD),
                    ("_pad", ctypes.c_byte * 16)]

    class INPUT_RECORD(ctypes.Structure):
        _fields_ = [("EventType", wintypes.WORD), ("Event", UNION)]

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.GetStdHandle(-10)
    wait_ms = 0xFFFFFFFF if timeout is None else max(0, int(timeout * 1000))
    if parser.pending:
        wait_ms = min(wait_ms, int(ESC_FLUSH_TIMEOUT * 1000))
    if kernel32.WaitForSingleObject(handle, wait_ms) != 0:
        return parser.flush()
    records = (INPUT_RECORD * 32)()
    read = wintypes.DWORD()
    if not kernel32.ReadConsoleInputW(handle, records, 32, ctypes.byref(read)):
        return []
    events: list = []
    for i in range(read.value):
        rec = records[i]
        if rec.EventType == 1:  # KEY_EVENT
            kev = rec.Event.KeyEvent
            if not kev.bKeyDown:
                continue
            state = kev.dwControlKeyState
            vk = kev.wVirtualKeyCode
            if (state & 0x000F) and 0x41 <= vk <= 0x5A:
                # Ctrl/Alt + буква: сочетание — по виртуальному коду, то есть
                # по физической клавише, а не по раскладке (ЗАДАЧА-17):
                # Alt+д русской раскладки приходит как Alt+L.
                mods = set()
                if state & 0x0010:
                    mods.add("shift")
                if state & 0x000C:
                    mods.add("ctrl")
                if state & 0x0003:
                    mods.add("alt")
                events.append(_keys.Key(chr(ord("a") + vk - 0x41),
                                        mods=frozenset(mods)))
                continue
            ch = kev.UnicodeChar
            if vk == 0x1B:
                # Настоящая клавиша Esc (VK_ESCAPE) — сразу событие. Через
                # разбор она ждала продолжения escape-последовательности до
                # следующей клавиши, и двойной Esc превращался в одиночный.
                # Последовательности VT-ввода приходят с vk=0 — их не задевает.
                events.extend(parser.flush())
                events.append(_keys.Key("esc"))
                continue
            if vk == 0x08 and state & 0x000F:
                # Ctrl/Alt+Backspace: символом приходит тот же 0x7F/0x08,
                # что и у голого Backspace, — сочетание видно только по vk.
                mods = {"ctrl"} if state & 0x000C else set()
                if state & 0x0003:
                    mods.add("alt")
                events.extend(parser.flush())
                events.append(_keys.Key("backspace", mods=frozenset(mods)))
                continue
            if ch and "\ud800" <= ch <= "\udbff":
                # Смайлик и всё вне BMP консоль отдаёт двумя записями —
                # половинками UTF-16; первую держим до второй.
                parser._luna_high = ch
                continue
            if ch and "\udc00" <= ch <= "\udfff":
                high, parser._luna_high = getattr(parser, "_luna_high", ""), ""
                ch = fix_surrogates(high + ch) if high else ""
            if ch and ch != "\x00":
                events.extend(parser.feed(ch.encode("utf-8", "replace")))
            else:
                name = _VK.get(kev.wVirtualKeyCode)
                if name:
                    mods = set()
                    state = kev.dwControlKeyState
                    if state & 0x0010:
                        mods.add("shift")
                    if state & 0x000C:
                        mods.add("ctrl")
                    if state & 0x0003:
                        mods.add("alt")
                    events.append(_keys.Key(name, mods=frozenset(mods)))
        elif rec.EventType == 2:  # MOUSE_EVENT (если VT-ввод недоступен)
            mev = rec.Event.MouseEvent
            x, y = mev.X + 1, mev.Y + 1
            if mev.dwEventFlags == 0x0004:  # MOUSE_WHEELED
                up = (mev.dwButtonState >> 16) < 0x8000
                events.append(_keys.Mouse("wheel_up" if up else "wheel_down", x, y))
            elif mev.dwEventFlags == 0:
                kind = "press" if mev.dwButtonState else "release"
                events.append(_keys.Mouse(kind, x, y, button=0))
    return events
