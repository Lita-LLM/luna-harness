"""Настоящее небо: астрономическая фаза луны без сети.

Расчёт — линейная модель синодического месяца из docs/ДИЗАЙН.md:
новолуние 2000-01-06 18:14 UTC, период 29,530588853 суток. Для заставки
и подписи фазы точности хватает (доли суток); астродинамику с возмущениями
сюда не тащим — Луна лёгкая.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

SYNODIC_DAYS = 29.530588853
_NEW_MOON = datetime(2000, 1, 6, 18, 14, tzinfo=timezone.utc)

_NAMES = (
    (0.033, "Новолуние"),
    (0.217, "Растущий серп"),
    (0.283, "Первая четверть"),
    (0.467, "Растущая луна"),
    (0.533, "Полнолуние"),
    (0.717, "Убывающая луна"),
    (0.783, "Последняя четверть"),
    (0.967, "Убывающий серп"),
)


@dataclass(frozen=True)
class MoonSky:
    age_days: float          # возраст луны в сутках от ближайшего новолуния назад
    cycle: float             # доля синодического месяца 0..1
    illumination: float      # освещённая доля диска 0..1
    waxing: bool             # растёт (первая половина цикла)
    mirror: bool             # терминатор зеркален относительно северного растущего вида
    hemisphere: str          # north | south
    name: str                # русское имя фазы

    @property
    def is_new(self) -> bool:
        return self.illumination < 0.04

    @property
    def is_full(self) -> bool:
        return self.illumination > 0.96


def _phase_name(cycle: float) -> str:
    if cycle < 0.033 or cycle >= 0.967:
        return "Новолуние"
    for limit, name in _NAMES[1:]:
        if cycle < limit:
            return name
    return "Убывающий серп"


def moon_sky(moment: datetime | None = None, hemisphere: str = "north") -> MoonSky:
    """Фаза луны на момент (aware datetime; naive трактуем как UTC)."""
    if moment is None:
        moment = datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    delta_days = (moment.astimezone(timezone.utc) - _NEW_MOON).total_seconds() / 86400.0
    cycle = (delta_days / SYNODIC_DAYS) % 1.0
    age = cycle * SYNODIC_DAYS
    illumination = (1.0 - math.cos(2.0 * math.pi * cycle)) / 2.0
    waxing = cycle < 0.5
    south = hemisphere == "south"
    # Север: растущая луна светится справа, убывающая — слева.
    # Южное полушарие видит луну перевёрнутой: стороны меняются местами.
    mirror = (not waxing) != south
    return MoonSky(age_days=age, cycle=cycle, illumination=illumination,
                   waxing=waxing, mirror=mirror,
                   hemisphere="south" if south else "north",
                   name=_phase_name(cycle))
