"""Пул моделей роя (ЗАДАЧА-15, docs/РОЙ.md §7): несколько адресов одной модели.

Профиль может назвать пул OpenAI-совместимых адресов (узлы сети автора):
задания раскладываются по свободным адресам. У каждого адреса — свой
ограничитель параллельности: по ответу 429 параллельность адреса
уполовинивается (и адрес briefly отдыхает), по успехам — растёт обратно
до двойной базовой. Это и есть «распределённый инференс» со стороны Луны.
"""

from __future__ import annotations

import threading
import time


def is_rate_limited(exc: BaseException) -> bool:
    """Ошибка похожа на 429: по статусу исключения или по тексту."""
    status = getattr(exc, "http_status", None) or getattr(exc, "status", None)
    if status == 429:
        return True
    return "429" in str(exc)


class Endpoint:
    """Один адрес пула: сколько летит сейчас, какой потолок, до когда отдых."""

    __slots__ = ("url", "in_flight", "limit", "base", "cool_until", "ok_streak")

    def __init__(self, url: str, base: int) -> None:
        self.url = url
        self.in_flight = 0
        self.base = max(1, int(base))
        self.limit = self.base
        self.cool_until = 0.0
        self.ok_streak = 0


class ModelPool:
    """Потокобезопасный пул адресов одной модели."""

    def __init__(self, urls, base_parallel: int = 2) -> None:
        self._lock = threading.Lock()
        self.endpoints = [Endpoint(str(u), base_parallel) for u in urls if str(u).strip()]
        self.rate_hits = 0

    def __len__(self) -> int:
        return len(self.endpoints)

    def acquire(self) -> Endpoint | None:
        """Свободный адрес (наименее загруженный) или None — все заняты/отдыхают."""
        now = time.monotonic()
        with self._lock:
            best = None
            for ep in self.endpoints:
                if ep.in_flight >= ep.limit or now < ep.cool_until:
                    continue
                if best is None or ep.in_flight < best.in_flight:
                    best = ep
            if best is not None:
                best.in_flight += 1
            return best

    def release(self, ep: Endpoint, *, rate_limited: bool = False,
                ok: bool = True) -> None:
        """Вернуть адрес: 429 — параллельность вниз и отдых; успехи — обратно вверх."""
        with self._lock:
            ep.in_flight = max(0, ep.in_flight - 1)
            if rate_limited:
                self.rate_hits += 1
                ep.limit = max(1, ep.limit // 2)
                ep.ok_streak = 0
                ep.cool_until = time.monotonic() + 1.0
            elif ok:
                ep.ok_streak += 1
                if ep.ok_streak >= 4 and ep.limit < ep.base * 2:
                    ep.limit += 1
                    ep.ok_streak = 0
            else:
                ep.ok_streak = 0

    def snapshot(self) -> list:
        with self._lock:
            return [{"url": ep.url, "in_flight": ep.in_flight, "limit": ep.limit}
                    for ep in self.endpoints]
