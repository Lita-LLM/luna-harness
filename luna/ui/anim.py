"""Планировщик анимаций: кадры только пока что-то движется.

Анимация — функция fn(now) -> bool (True — ещё жива). Планировщик говорит
циклу интерфейса, сколько можно спать в ожидании ввода: None — бесконечно
(процесс спит в блокирующем чтении, ЦП ≈ 0), иначе — до следующего кадра,
не чаще 30 кадров/с.
"""

from __future__ import annotations

import time

SPINNER_GLYPHS = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
SPINNER_ASCII = "|/-\\"


class Scheduler:
    def __init__(self, fps: int = 30):
        self.frame_interval = 1.0 / max(1, fps)
        self._anims: list = []

    def add(self, fn) -> None:
        if fn not in self._anims:
            self._anims.append(fn)

    def remove(self, fn) -> None:
        try:
            self._anims.remove(fn)
        except ValueError:
            pass

    @property
    def active(self) -> bool:
        return bool(self._anims)

    def next_timeout(self) -> float | None:
        """Сколько цикл может ждать ввода; None — спать без срока."""
        if not self._anims:
            return None
        return self.frame_interval

    def tick(self, now: float | None = None) -> bool:
        """Шагнуть все анимации; вернуть True, если хоть одна жива."""
        if not self._anims:
            return False
        now = time.monotonic() if now is None else now
        self._anims = [fn for fn in self._anims if _call(fn, now)]
        return bool(self._anims)


def _call(fn, now) -> bool:
    try:
        return bool(fn(now))
    except Exception:  # noqa: BLE001 — упавшая анимация не роняет интерфейс
        return False


class Spinner:
    """Крутилка: кадр по времени, glyph(now) — текущий символ."""

    def __init__(self, unicode_ok: bool = True, interval: float = 0.08):
        self.glyphs = SPINNER_GLYPHS if unicode_ok else SPINNER_ASCII
        self.interval = interval
        self.started = time.monotonic()

    def glyph(self, now: float | None = None) -> str:
        now = time.monotonic() if now is None else now
        idx = int((now - self.started) / self.interval) % len(self.glyphs)
        return self.glyphs[idx]
