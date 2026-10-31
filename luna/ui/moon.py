"""Фазы луны по проценту 0–100.

Процент — доля цикла: 0 — новолуние, 50 — полнолуние, 100 — снова новолуние.
В интерфейсе процент — это заполненность контекста (см. docs/ДИЗАЙН.md), поэтому
вызывающий код для строки состояния обычно передаёт pct/2: пустой контекст —
новолуние, полный — полнолуние.

Три набора глифов (режим — настройка ui.emoji и возможности терминала):
- "emoji" — эмодзи-фазы шириной 2;
- "narrow" — узкие круги шириной 1 для терминалов, где эмодзи ненадёжны
  (ConPTY-хосты пересчитывают ширину эмодзи и сдвигают экран);
- "ascii" — ASCII-«заливка» для ui.unicode=off.
"""

from __future__ import annotations

EMOJI_PHASES = ("🌑", "🌒", "🌓", "🌔", "🌕", "🌖", "🌗", "🌘")

# Узкие фазы: круги с нарастающей заливкой. У каждого east_asian_width —
# N или A (не W/F), ширина в терминале — 1 ячейка (проверено тестом).
NARROW_PHASES = ("○", "◔", "◑", "◕", "●", "◕", "◐", "◔")

_FILLS = ("....", "...#", "..##", ".###", "####", "###.", "##..", "#...")
ASCII_PHASES = tuple(f"[{f}]" for f in _FILLS)

MODES = ("emoji", "narrow", "ascii")


def normalize_mode(mode) -> str:
    """Привести режим к строке; bool — наследие вызовов «юникод есть/нет»."""
    if mode is True:
        return "emoji"
    if mode is False:
        return "ascii"
    if mode in MODES:
        return mode
    raise ValueError(f"Неизвестный режим глифов: {mode!r}")


def phase_index(pct: float) -> int:
    """Индекс фазы 0..7 по проценту цикла."""
    pct = max(0.0, min(100.0, float(pct)))
    return min(7, int(pct / 12.5))


def phase_emoji(pct: float) -> str:
    return EMOJI_PHASES[phase_index(pct)]


def phase_narrow(pct: float) -> str:
    return NARROW_PHASES[phase_index(pct)]


def phase_ascii(pct: float) -> str:
    return ASCII_PHASES[phase_index(pct)]


def phase(pct: float, mode="emoji") -> str:
    mode = normalize_mode(mode)
    if mode == "emoji":
        return phase_emoji(pct)
    if mode == "narrow":
        return phase_narrow(pct)
    return phase_ascii(pct)


def context_phase(fill_pct: float, mode="emoji") -> str:
    """Фаза для заполненности контекста: 0% — новолуние, 100% — полнолуние."""
    return phase(fill_pct / 2.0, mode)
