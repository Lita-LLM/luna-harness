"""Кошелёк сессии для агентов-детей (ЗАДАЧА-11, docs/РОЙ.md §3 и §7).

Один кошелёк на сессию: из него тратят все дети всех хозяев (и ассистенты
главного агента, и дети спутников). Лимит — настройка
agents.session_budget_usd; 0 — без лимита (но траты всё равно считаются).

Прогноз честный и простой: по текущему темпу трат (spent / elapsed)
считаем темп в минуту, сколько осталось при таком темпе (eta) и сколько
сгорит ещё за минуту. Событие budget.update хозяин публикует после каждой
траты ребёнка (см. host.py).
"""

from __future__ import annotations

import threading
import time


class Wallet:
    """Общий кошелёк детей сессии. Потокобезопасен."""

    def __init__(self, limit_usd: float = 0.0) -> None:
        self.limit = float(limit_usd or 0.0)
        self.spent = 0.0
        self.tokens = 0
        self.started = time.monotonic()
        self.per_agent: dict[str, dict] = {}
        self._lock = threading.Lock()

    @property
    def exhausted(self) -> bool:
        with self._lock:
            return self.limit > 0 and self.spent >= self.limit

    @property
    def remaining(self) -> float:
        with self._lock:
            if self.limit <= 0:
                return 0.0
            return max(0.0, self.limit - self.spent)

    def charge(self, agent_id: str, name: str, cost: float, tokens: int) -> dict:
        """Списать трату ребёнка; вернуть снимок кошелька (для budget.update)."""
        with self._lock:
            self.spent += float(cost or 0.0)
            self.tokens += int(tokens or 0)
            row = self.per_agent.setdefault(
                agent_id, {"name": name, "cost": 0.0, "tokens": 0})
            row["name"] = name
            row["cost"] += float(cost or 0.0)
            row["tokens"] += int(tokens or 0)
            return self.snapshot()

    def snapshot(self) -> dict:
        """Текущее состояние + прогноз по темпу трат."""
        elapsed = max(0.001, time.monotonic() - self.started)
        rate_per_min = self.spent / elapsed * 60.0 if self.spent > 0 else 0.0
        remaining = max(0.0, self.limit - self.spent) if self.limit > 0 else 0.0
        eta = None
        if self.limit > 0 and rate_per_min > 0:
            eta = remaining / (rate_per_min / 60.0)
        return {
            "spent_usd": round(self.spent, 6),
            "limit_usd": self.limit,
            "remaining_usd": round(remaining, 6),
            "tokens": self.tokens,
            "rate_per_min_usd": round(rate_per_min, 6),
            "eta_sec": None if eta is None else round(eta, 1),
            "forecast_min_usd": round(rate_per_min, 6),
            "exhausted": bool(self.limit > 0 and self.spent >= self.limit),
        }
