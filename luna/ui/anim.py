"""Планировщик анимаций: кадры только пока что-то движется.

Анимация — функция fn(now) -> bool (True — ещё жива). Планировщик говорит
циклу интерфейса, сколько можно спать в ожидании ввода: None — бесконечно
(процесс спит в блокирующем чтении, ЦП ≈ 0), иначе — до следующего кадра,
не чаще 30 кадров/с.
"""

from __future__ import annotations

import time

SPINNER_GLYPHS = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
SPINNER_NARROW = "◐◓◑◒"  # вращающиеся полукруги шириной 1 — режим без эмодзи
SPINNER_ASCII = "|/-\\"
_SPINNER_BY_MODE = {"emoji": SPINNER_GLYPHS, "narrow": SPINNER_NARROW,
                    "ascii": SPINNER_ASCII}


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


def _clamp01(t: float) -> float:
    return 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)


def ease_in_out(t: float) -> float:
    """Плавный старт и плавный конец (smoothstep): спокойное движение."""
    t = _clamp01(t)
    return t * t * (3.0 - 2.0 * t)


def ease_out(t: float) -> float:
    """Быстрый старт, мягкое приземление: так восходит луна на заставке."""
    t = _clamp01(t)
    u = 1.0 - t
    return 1.0 - u * u * u


def ease_in(t: float) -> float:
    """Мягкий разгон из покоя."""
    t = _clamp01(t)
    return t * t * t


def fade(t: float) -> float:
    """Появление через затухание: доля видимости по прогрессу 0..1."""
    return ease_in_out(t)


class PhraseWheel:
    """Фразы по кругу, смена — затуханием, а не прыжком.

    text_alpha(now) -> (фраза, доля видимости 0..1): в конце периода фраза
    уходит в фон (доля → 0), в начале следующего — выходит из фона.
    Видимость дальше превращается в цвет смешением с фоном.
    """

    def __init__(self, phrases, period: float = 5.0, fade_seconds: float = 0.4,
                 started: float | None = None):
        self.phrases = list(phrases) or ["Думает"]
        self.period = max(0.5, float(period))
        self.fade = max(0.0, min(float(fade_seconds), self.period / 2))
        self.started = time.monotonic() if started is None else started

    def text_alpha(self, now: float) -> tuple:
        elapsed = max(0.0, now - self.started)
        idx = int(elapsed / self.period) % len(self.phrases)
        local = elapsed - int(elapsed / self.period) * self.period
        if self.fade <= 0:
            return self.phrases[idx], 1.0
        if local < self.fade:
            return self.phrases[idx], ease_in_out(local / self.fade)
        if local > self.period - self.fade:
            return self.phrases[idx], ease_in_out((self.period - local) / self.fade)
        return self.phrases[idx], 1.0


class Spinner:
    """Крутилка: кадр по времени, glyph(now) — текущий символ.

    Режим глифов — как у фаз луны (emoji | narrow | ascii); bool принимается
    для старых вызовов: True — emoji, False — ascii.
    """

    def __init__(self, mode="emoji", interval: float = 0.08):
        if mode is True:
            mode = "emoji"
        elif mode is False:
            mode = "ascii"
        if mode not in _SPINNER_BY_MODE:
            raise ValueError(f"Неизвестный режим спиннера: {mode!r}")
        self.mode = mode
        self.glyphs = _SPINNER_BY_MODE[mode]
        self.interval = interval
        self.started = time.monotonic()

    def glyph(self, now: float | None = None) -> str:
        now = time.monotonic() if now is None else now
        idx = int((now - self.started) / self.interval) % len(self.glyphs)
        return self.glyphs[idx]
