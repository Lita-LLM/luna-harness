"""Темы оформления: палитры как данные (ЗАДАЧА-18).

Тема — набор цветов по ключам палитры (ui/color.py: PALETTE). Встроенные
живут здесь словарями; свои человек кладёт JSON-файлами в LUNA_HOME/themes/
(«имя.json»: {"title": "...", "colors": {"space_bg": "#000000", ...}}) —
недостающие ключи наследуются от встроенной «luna». Рендер цветов из
литералов не берёт: apply_theme() раскладывает палитру по модулям.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..core.paths import luna_home
from . import color as _color

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

_LUNA = {
    "space_bg": (0x0B, 0x0D, 0x17),
    "panel_bg": (0x10, 0x13, 0x24),
    "status_bg": (0x16, 0x1B, 0x30),
    "guard_bg": (0x24, 0x1B, 0x10),
    "header_bg": (0x1D, 0x22, 0x3D),
    "header_focus_bg": (0x2E, 0x2A, 0x4A),
    "silver": (0xC9, 0xD1, 0xE0),
    "muted": (0x8B, 0x94, 0xAC),
    "cyan": (0x00, 0xE5, 0xFF),
    "violet": (0x7B, 0x5C, 0xFF),
    "magenta": (0xFF, 0x2B, 0xD6),
    "amber": (0xFF, 0xB3, 0x47),
    "ok": (0x4D, 0xFF, 0xB0),
    "err": (0xFF, 0x4D, 0x6D),
}

_SILVER = _LUNA["silver"]

BUILTIN: dict = {
    "luna": ("Луна", dict(_LUNA)),
    "amoled": ("AMOLED: чистый чёрный", {
        **_LUNA,
        "space_bg": (0x00, 0x00, 0x00),
        "panel_bg": (0x05, 0x05, 0x05),
        "status_bg": (0x0A, 0x0A, 0x0A),
        "guard_bg": (0x17, 0x11, 0x06),
        "header_bg": (0x10, 0x10, 0x10),
        "header_focus_bg": (0x1E, 0x1E, 0x1E),
        "silver": (0xC9, 0xC9, 0xC9),
        "muted": (0x6E, 0x6E, 0x6E),
    }),
    "contrast": ("Контраст", {
        **_LUNA,
        "silver": (0xE8, 0xED, 0xF8),
        "muted": (0xB8, 0xC0, 0xD4),
    }),
    "mono": ("Монохром", {
        **_LUNA,
        "cyan": _SILVER, "violet": _SILVER, "magenta": _SILVER,
        "amber": _SILVER, "ok": _SILVER, "err": _SILVER,
    }),
    "light": ("Светлая", {
        **_LUNA,
        "space_bg": (0xF2, 0xF4, 0xF8),
        "panel_bg": (0xE7, 0xEA, 0xF1),
        "status_bg": (0xDC, 0xE1, 0xEC),
        "guard_bg": (0xF3, 0xE7, 0xCE),
        "header_bg": (0xD3, 0xD9, 0xE6),
        "header_focus_bg": (0xC2, 0xCB, 0xE0),
        "silver": (0x1B, 0x22, 0x33),
        "muted": (0x5C, 0x66, 0x7A),
        "cyan": (0x00, 0x7A, 0x99),
        "violet": (0x5B, 0x3D, 0xD6),
        "magenta": (0xC2, 0x1D, 0xA0),
        "amber": (0xA8, 0x64, 0x00),
        "ok": (0x0A, 0x7A, 0x4A),
        "err": (0xC2, 0x1E, 0x3A),
    }),
}

_applied = "luna"


def themes_dir(home=None) -> Path:
    return Path(home or luna_home()) / "themes"


def _user_theme_files(home=None) -> list:
    folder = themes_dir(home)
    if not folder.is_dir():
        return []
    return sorted(folder.glob("*.json"))


def _read_user_theme(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    colors = {}
    raw = data.get("colors")
    if isinstance(raw, dict):
        for key, value in raw.items():
            rgb = _color.parse_hex(value)
            if key in _LUNA and rgb is not None:
                colors[key] = rgb
    return {"title": str(data.get("title") or ""), "colors": colors}


def list_themes(home=None) -> list:
    """[(имя, подпись, своя ли)] — встроенные, затем свои из LUNA_HOME/themes."""
    out = [(name, title, False) for name, (title, _pal) in BUILTIN.items()]
    for path in _user_theme_files(home):
        data = _read_user_theme(path)
        if data is None:
            continue
        title = data["title"] or path.stem
        out.append((path.stem, title + " (своя)", True))
    return out


def load_palette(name: str, home=None) -> tuple:
    """(палитра RGB-кортежей, фактически применённое имя).

    Неизвестное имя и битый файл честно откатываются на «luna»."""
    palette = dict(_LUNA)
    key = str(name or "luna")
    if key in BUILTIN:
        palette.update(BUILTIN[key][1])
        return palette, key
    path = themes_dir(home) / (key + ".json")
    if path.is_file():
        data = _read_user_theme(path)
        if data is not None:
            palette.update(data["colors"])
            return palette, key
    return palette, "luna"


def applied_name() -> str:
    return _applied


def apply_theme(name: str, home=None) -> str:
    """Применить тему к процессу: палитра расходится по модулям рендера."""
    global _applied
    palette, actual = load_palette(name, home)
    _color.apply_palette(palette)
    # Модули со своими константами-зеркалами получают те же значения.
    try:
        from . import chat as _chat

        _chat.STATUS_BG = palette["status_bg"]
        _chat.PANEL_BG = palette["panel_bg"]
        _chat.GUARD_BG = palette["guard_bg"]
        _chat.DIM_FG = palette["muted"]
        _chat.PICKER_BG = palette["panel_bg"]
        _chat.SELECT_BG = palette["status_bg"]
    except Exception:  # noqa: BLE001 — чат мог быть не импортирован
        pass
    try:
        from . import picker as _picker

        _picker.PICKER_BG = palette["panel_bg"]
        _picker.SELECT_BG = palette["status_bg"]
        _picker.DIM_FG = palette["muted"]
    except Exception:  # noqa: BLE001
        pass
    _applied = actual
    return actual


def save_theme(home, name: str, colors: dict, title: str = "") -> Path:
    """Записать свою тему JSON-ом в LUNA_HOME/themes. Возвращает путь.

    Имя — латиница/цифры/дефис/подчёркивание, встроенные не перекрываем;
    цвета — только известные ключи палитры и только честные #RRGGBB."""
    key = str(name or "").strip().lower()
    if not _NAME_RE.match(key):
        raise ValueError("имя темы: латиница, цифры, дефис, подчёркивание")
    if key in BUILTIN:
        raise ValueError("имя занято встроенной темой: " + key)
    clean = {}
    for k, v in (colors or {}).items():
        rgb = _color.parse_hex(v)
        if k in _LUNA and rgb is not None:
            clean[k] = _color.hex_of(rgb)
    if not clean:
        raise ValueError("не передано ни одного цвета (ключи палитры: " +
                         ", ".join(sorted(_LUNA)) + ")")
    folder = themes_dir(home)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (key + ".json")
    payload = {"title": str(title or key), "colors": clean}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    return path
