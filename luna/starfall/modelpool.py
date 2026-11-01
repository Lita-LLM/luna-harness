"""Пул моделей роя (ЗАДАЧА-15, docs/РОЙ.md §7): несколько адресов одной модели.

Профиль может назвать пул OpenAI-совместимых адресов (узлы сети автора):
задания раскладываются по свободным адресам. У каждого адреса — свой
ограничитель параллельности: по ответу 429 параллельность адреса
уполовинивается (и адрес briefly отдыхает), по успехам — растёт обратно
до двойной базовой. Это и есть «распределённый инференс» со стороны Луны.

ЗАДАЧА-21: у адреса может быть имя секрета (pool:<имя> в сейфе; значение
ключа сюда не попадает никогда — только имя) и тип/провайдер для схемы
заголовка авторизации. Ответ 401/403 выводит адрес из пула совсем, с
понятной причиной: повторять запрос с тем же ключом бессмысленно.
"""

from __future__ import annotations

import re
import threading
import time


def is_rate_limited(exc: BaseException) -> bool:
    """Ошибка похожа на 429: по статусу исключения или по тексту."""
    status = getattr(exc, "http_status", None) or getattr(exc, "status", None)
    if status == 429:
        return True
    return "429" in str(exc)


def error_status(exc: BaseException):
    """HTTP-статус ошибки, если он у неё честно есть."""
    for attr in ("http_status", "status", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int) and 100 <= value <= 599:
            return value
    match = re.search(r"(?:ошибкой|HTTP|статус)\s*(401|403|429)\b", str(exc))
    return int(match.group(1)) if match else None


def is_auth_error(exc: BaseException) -> bool:
    """401/403: ключ не принят — адрес выводится из пула, без повторов."""
    return error_status(exc) in (401, 403)


class PoolEndpointError(RuntimeError):
    """Адрес пула нельзя использовать (нет ключа в сейфе и т.п.) —
    движок выводит его из пула по флагу pool_disable."""

    pool_disable = True


class Endpoint:
    """Один адрес пула: сколько летит сейчас, какой потолок, до когда отдых."""

    __slots__ = ("url", "in_flight", "limit", "base", "cool_until", "ok_streak",
                 "secret_name", "kind", "provider", "disabled", "disabled_reason")

    def __init__(self, url: str, base: int, spec: dict | None = None) -> None:
        spec = spec or {}
        self.url = url
        self.in_flight = 0
        self.base = max(1, int(base))
        self.limit = self.base
        self.cool_until = 0.0
        self.ok_streak = 0
        self.secret_name = str(spec.get("secret") or "")
        self.kind = str(spec.get("kind") or "")
        self.provider = str(spec.get("provider") or "")
        self.disabled = False
        self.disabled_reason = ""

    def spec(self) -> dict:
        """Описание адреса для исполнителя ячейки: только имя секрета."""
        return {"url": self.url, "secret": self.secret_name,
                "kind": self.kind, "provider": self.provider}


class ModelPool:
    """Потокобезопасный пул адресов одной модели."""

    def __init__(self, urls, base_parallel: int = 2) -> None:
        from .profiles import parse_pool_entry

        self._lock = threading.Lock()
        self.endpoints = []
        for item in urls or []:
            spec = parse_pool_entry(item)
            if spec.get("url"):
                self.endpoints.append(Endpoint(spec["url"], base_parallel, spec))
        self.rate_hits = 0
        self.auth_hits = 0

    def __len__(self) -> int:
        return len(self.endpoints)

    def acquire(self) -> Endpoint | None:
        """Свободный адрес (наименее загруженный) или None — все заняты/отдыхают/выведены."""
        now = time.monotonic()
        with self._lock:
            best = None
            for ep in self.endpoints:
                if ep.disabled or ep.in_flight >= ep.limit or now < ep.cool_until:
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

    def disable(self, ep: Endpoint, reason: str) -> None:
        """Вывести адрес из пула совсем (401/403, нет ключа): без повторов."""
        with self._lock:
            ep.in_flight = max(0, ep.in_flight - 1)
            if not ep.disabled:
                ep.disabled = True
                ep.disabled_reason = reason
                self.auth_hits += 1

    def all_disabled(self) -> bool:
        with self._lock:
            return bool(self.endpoints) and all(ep.disabled for ep in self.endpoints)

    def disabled_reasons(self) -> list:
        with self._lock:
            return [ep.url + " — " + ep.disabled_reason
                    for ep in self.endpoints if ep.disabled]

    def snapshot(self) -> list:
        with self._lock:
            return [{"url": ep.url, "in_flight": ep.in_flight, "limit": ep.limit,
                     "disabled": ep.disabled}
                    for ep in self.endpoints]
