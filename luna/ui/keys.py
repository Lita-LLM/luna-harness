"""Разбор ввода терминала в события.

Обычные клавиши, стрелки и Esc-последовательности, Ctrl/Alt/Shift-сочетания,
мышь SGR 1006 (клик, отпускание, колесо), вставка bracketed paste, фокус.
Парсер потоковый: недописанная последовательность ждёт продолжения, а flush()
по тайм-ауту превращает одиночный Esc в клавишу Esc.
"""

from __future__ import annotations

import codecs
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Key:
    name: str  # "a", "enter", "up", "tab", "backspace", "f5", "esc", "space"…
    char: str = ""  # печатный символ, если есть
    mods: frozenset = frozenset()  # подмножество {"ctrl", "alt", "shift"}

    @property
    def label(self) -> str:
        parts = [m for m in ("ctrl", "alt", "shift") if m in self.mods]
        parts.append(self.name)
        return "+".join(parts)


@dataclass(frozen=True)
class Mouse:
    kind: str  # press | release | wheel_up | wheel_down | move
    x: int
    y: int
    button: int = 0
    mods: frozenset = frozenset()


@dataclass(frozen=True)
class Paste:
    text: str


@dataclass(frozen=True)
class Resize:
    cols: int
    rows: int


@dataclass(frozen=True)
class Unknown:
    seq: str


_MOD_BITS = {2: ("shift",), 3: ("alt",), 4: ("shift", "alt"), 5: ("ctrl",),
             6: ("shift", "ctrl"), 7: ("alt", "ctrl"), 8: ("shift", "alt", "ctrl")}

_CSI_LETTERS = {"A": "up", "B": "down", "C": "right", "D": "left",
                "H": "home", "F": "end", "P": "f1", "Q": "f2", "R": "f3", "S": "f4"}

_TILDE = {1: "home", 2: "insert", 3: "delete", 4: "end", 5: "pageup", 6: "pagedown",
          7: "home", 8: "end", 11: "f1", 12: "f2", 13: "f3", 14: "f4", 15: "f5",
          17: "f6", 18: "f7", 19: "f8", 20: "f9", 21: "f10", 23: "f11", 24: "f12"}

_PASTE_END = "\x1b[201~"


def _mods_from(param: int) -> frozenset:
    return frozenset(_MOD_BITS.get(param, ()))


class KeyParser:
    def __init__(self):
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._buf = ""
        self._paste: str | None = None

    @property
    def pending(self) -> bool:
        """Есть недописанная последовательность (ждём продолжения или flush)."""
        return bool(self._buf) or self._paste is not None

    def feed(self, data: bytes) -> list:
        self._buf += self._decoder.decode(data)
        return self._drain(final=False)

    def flush(self) -> list:
        """Тайм-аут ожидания: разрешить висящий Esc и прочее как есть."""
        return self._drain(final=True)

    # --- внутренности -----------------------------------------------------

    def _drain(self, final: bool) -> list:
        events = []
        while True:
            if self._paste is not None:
                end = self._buf.find(_PASTE_END)
                if end < 0:
                    if len(self._buf) > len(_PASTE_END):
                        keep = len(_PASTE_END) - 1
                        self._paste += self._buf[:-keep]
                        self._buf = self._buf[-keep:]
                    break
                self._paste += self._buf[:end]
                self._buf = self._buf[end + len(_PASTE_END):]
                events.append(Paste(self._paste))
                self._paste = None
                continue
            if not self._buf:
                break
            ev, consumed = self._parse_one(final)
            if consumed == 0:
                break
            self._buf = self._buf[consumed:]
            if ev is not None:
                events.append(ev)
        return events

    def _parse_one(self, final: bool):
        s = self._buf
        c = s[0]
        if c == "\x1b":
            return self._parse_escape(final)
        if c in "\r\n":
            return Key("enter"), 1
        if c in ("\x7f", "\x08"):
            return Key("backspace"), 1
        if c == "\t":
            return Key("tab"), 1
        if c == "\x00":
            return Key("space", mods=frozenset({"ctrl"})), 1
        if "\x01" <= c <= "\x1a":
            letter = chr(ord("a") + ord(c) - 1)
            return Key(letter, mods=frozenset({"ctrl"})), 1
        if ord(c) < 0x20 or ord(c) == 0x7f:
            return Key(f"ctrl+{c!r}"), 1
        return Key(c, char=c), 1

    def _parse_escape(self, final: bool):
        s = self._buf
        if len(s) == 1:
            if final:
                return Key("esc"), 1
            return None, 0  # ждём: вдруг это начало последовательности
        second = s[1]
        if second == "\x1b":
            return Key("esc"), 1
        if second == "[":
            return self._parse_csi(final)
        if second == "O":
            if len(s) < 3:
                return (None, 0) if not final else (Unknown(s), len(s))
            name = _CSI_LETTERS.get(s[2])
            if name:
                return Key(name), 3
            return Unknown(s[:3]), 3
        # Alt + клавиша
        sub = KeyParser()
        sub._buf = s[1:]
        ev, consumed = sub._parse_one(final)
        if isinstance(ev, Key):
            return Key(ev.name, char=ev.char, mods=ev.mods | {"alt"}), 1 + consumed
        return Unknown(s[:1 + max(consumed, 1)]), 1 + max(consumed, 1)

    def _parse_csi(self, final: bool):
        s = self._buf
        i = 2
        while i < len(s) and not ("@" <= s[i] <= "~"):
            i += 1
        if i >= len(s):
            return (Unknown(s), len(s)) if final else (None, 0)
        body, final_ch = s[2:i], s[i]
        consumed = i + 1
        seq = s[:consumed]
        if body.startswith("<"):
            return self._mouse(body[1:], final_ch, seq), consumed
        if final_ch == "~":
            return self._tilde(body, seq), consumed
        params = body.split(";") if body else []
        mod = _mods_from(int(params[1])) if len(params) > 1 and params[1].isdigit() else frozenset()
        if final_ch == "Z":
            return Key("tab", mods=frozenset({"shift"})), consumed
        if final_ch in ("I", "O"):
            return Key("focus_in" if final_ch == "I" else "focus_out"), consumed
        name = _CSI_LETTERS.get(final_ch)
        if name:
            return Key(name, mods=mod), consumed
        return Unknown(seq), consumed

    def _tilde(self, body: str, seq: str):
        params = body.split(";") if body else []
        code = int(params[0]) if params and params[0].isdigit() else -1
        if code == 200:
            self._paste = ""
            return None
        if code == 201:  # конец вставки без начала — мусор
            return Unknown(seq)
        mod = _mods_from(int(params[1])) if len(params) > 1 and params[1].isdigit() else frozenset()
        name = _TILDE.get(code)
        if name:
            return Key(name, mods=mod)
        return Unknown(seq)

    def _mouse(self, body: str, final_ch: str, seq: str):
        try:
            b, x, y = (int(p) for p in body.split(";"))
        except ValueError:
            return Unknown(seq)
        mods = frozenset(
            ({"shift"} if b & 4 else set()) | ({"alt"} if b & 8 else set()) | ({"ctrl"} if b & 16 else set()))
        if b & 64:
            kind = "wheel_up" if (b & 1) == 0 else "wheel_down"
            return Mouse(kind, x, y, button=b & 3, mods=mods)
        if b & 32:
            return Mouse("move", x, y, button=b & 3, mods=mods)
        return Mouse("press" if final_ch == "M" else "release", x, y, button=b & 3, mods=mods)
