"""Снимки экранов в HTML: `luna try snapshot <splash|render|main>`.

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

SCREENS = ("splash", "render", "main")
_FAKE_NOW = 10000.0  # вымышленная эпоха снимка: все «сейчас» считаются от неё

_PAGE_CSS = """
body { background: #05060A; color: #C9D1E0; font-family: sans-serif; margin: 24px; }
h1 { font-size: 18px; font-weight: normal; }
.frame { margin: 0 auto 32px; width: max-content; max-width: 100%; }
.cap { color: #8B94AC; font-size: 13px; margin: 0 0 8px 4px; }
.scr { background: #0B0D17; border-radius: 14px; padding: 14px 16px;
       font-family: "DejaVu Sans Mono", "Cascadia Mono", Consolas, monospace;
       font-size: 14px; line-height: 1.18; white-space: pre; overflow-x: auto; }
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


def _render_splash(cols: int, rows: int, at: float, caps, phrase: str) -> Buffer:
    from ..ui import splash as _splash

    buf = Buffer(cols, rows)
    stars = _splash._stars(cols, rows, max(20, cols * rows // 28))
    t = max(0.0, min(1.0, at / _splash.DURATION))
    _splash.render_frame(buf, t, glyph_mode=caps.glyph_mode, phrase=phrase,
                         startup_ms=None, stars=stars, now=at)
    return buf


def _render_render(cols: int, rows: int, at: float, caps, demo) -> Buffer:
    return demo.draw(cols, rows, _FAKE_NOW + at)


def _render_main(cols: int, rows: int, at: float, caps, settings) -> Buffer:
    from ..core.bus import EventBus
    from ..ui.anim import Scheduler
    from ..ui.app import MiniApp

    app = MiniApp(EventBus(), settings, Scheduler(), caps.glyph_mode)
    buf, _cursor = app.draw(cols, rows, _FAKE_NOW + at)
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
    frames = []
    for at in times:
        if name == "splash":
            buf = _render_splash(cols, rows, at, caps, phrase)
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
        lines.append("".join(parts))
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
