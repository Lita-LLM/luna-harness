"""Фазы луны по проценту 0–100.

Процент — доля цикла: 0 — новолуние, 50 — полнолуние, 100 — снова новолуние.
В интерфейсе процент — это заполненность контекста (см. docs/ДИЗАЙН.md), поэтому
вызывающий код для строки состояния обычно передаёт pct/2: пустой контекст — 🌑,
полный — 🌕. Эмодзи — восемь фаз; запасной набор без эмодзи — ASCII-«заливка».
"""

from __future__ import annotations

EMOJI_PHASES = ("🌑", "🌒", "🌓", "🌔", "🌕", "🌖", "🌗", "🌘")

_FILLS = ("....", "...#", "..##", ".###", "####", "###.", "##..", "#...")
ASCII_PHASES = tuple(f"[{f}]" for f in _FILLS)


def phase_index(pct: float) -> int:
    """Индекс фазы 0..7 по проценту цикла."""
    pct = max(0.0, min(100.0, float(pct)))
    return min(7, int(pct / 12.5))


def phase_emoji(pct: float) -> str:
    return EMOJI_PHASES[phase_index(pct)]


def phase_ascii(pct: float) -> str:
    return ASCII_PHASES[phase_index(pct)]


def phase(pct: float, unicode_ok: bool = True) -> str:
    return phase_emoji(pct) if unicode_ok else phase_ascii(pct)


def context_phase(fill_pct: float, unicode_ok: bool = True) -> str:
    """Фаза для заполненности контекста: 0% — новолуние, 100% — полнолуние."""
    return phase(fill_pct / 2.0, unicode_ok)
