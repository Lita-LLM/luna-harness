"""Данные панели «Экипаж» (ЗАДАЧА-11): состав команды из потока событий.

Отдельный модуль данных (как просила задача): PanelData только маршрутизирует
сюда события детей (agent.* и budget.update, а также usage с agent_id),
вся агрегация команды живёт здесь. Строка budget_line() — для панели «Луна»:
общий кошелёк детей и прогноз по темпу.
"""

from __future__ import annotations

_KIND_RU = {"assistant": "ассистент", "satellite": "спутник", "cell": "ячейка роя",
            "main": "хозяин"}


class CrewData:
    """Кто в команде, что делает, сколько потратил, последние письма."""

    def __init__(self) -> None:
        self.agents: dict[str, dict] = {}
        self.order: list[str] = []
        self.letters: list[dict] = []
        self.budget: dict | None = None

    def _row(self, agent_id: str) -> dict:
        row = self.agents.get(agent_id)
        if row is None:
            row = {"name": agent_id, "kind": "", "role": "", "model": "",
                   "state": "", "tokens": 0, "cost": 0.0, "leader": False,
                   "summary": "", "last_letter": ""}
            self.agents[agent_id] = row
            self.order.append(agent_id)
        return row

    def observe(self, ev: dict) -> None:
        etype = ev.get("type")
        if etype == "session.start":
            # session.start главного агента = новая сессия: вернувшиеся
            # спутники спят. Чужие session.start (у детей свои) не трогаем.
            if not ev.get("agent_id"):
                for row in self.agents.values():
                    if row["state"] not in ("сдал", "упал", "распущен"):
                        row["state"] = "спит"
            return
        if etype == "budget.update":
            self.budget = ev
            return
        agent_id = str(ev.get("agent_id") or "")
        if not agent_id:
            return
        row = self._row(agent_id)
        if ev.get("name"):
            row["name"] = str(ev["name"])
        if ev.get("kind"):
            row["kind"] = str(ev["kind"])
        if etype == "agent.spawn":
            row["role"] = str(ev.get("role") or "")
            row["model"] = str(ev.get("model") or "")
            row["leader"] = bool(ev.get("leader"))
            row["state"] = "спит" if ev.get("kind") == "satellite" else "ждёт"
        elif etype == "agent.state":
            row["state"] = str(ev.get("state") or row["state"])
        elif etype == "agent.message":
            letter = {"from": str(ev.get("from") or ""), "to": str(ev.get("to") or ""),
                      "text": str(ev.get("text") or "")}
            self.letters.append(letter)
            del self.letters[:-8]
            row["last_letter"] = letter["text"]
        elif etype == "agent.done":
            row["state"] = "распущен" if ev.get("dismissed") else "сдал"
            if ev.get("summary"):
                row["summary"] = str(ev["summary"])
            if ev.get("tokens") is not None:
                row["tokens"] = int(ev.get("tokens") or 0)
            if ev.get("cost") is not None:
                row["cost"] = float(ev.get("cost") or 0.0)
        elif etype == "usage":
            row["tokens"] += int(ev.get("input") or 0) + int(ev.get("output") or 0)
            if ev.get("cost") is not None:
                row["cost"] = float(ev.get("cost") or 0.0)  # у агента cost накопительный

    def budget_line(self) -> str:
        """Строка для панели «Луна»: кошелёк детей и прогноз."""
        if not self.budget:
            return ""
        spent = float(self.budget.get("spent_usd") or 0.0)
        limit = float(self.budget.get("limit_usd") or 0.0)
        rate = float(self.budget.get("rate_per_min_usd") or 0.0)
        line = "бюджет детей: $" + format(spent, ".4f")
        if limit:
            line += " из $" + format(limit, ".2f")
        if rate:
            line += " · прогноз $" + format(rate, ".4f") + "/мин"
        if self.budget.get("exhausted"):
            line += " · исчерпан"
        return line

    def lines(self, width: int) -> list:
        if not self.order:
            return ["Команда пуста.", "Спутников создаёт тул crew, ассистентов — assistant."]
        out = []
        for agent_id in self.order:
            row = self.agents[agent_id]
            kind_ru = _KIND_RU.get(row["kind"], row["kind"])
            head = row["name"] + (" ★" if row["leader"] else "") + " · " + kind_ru
            if row["role"]:
                head += " · " + row["role"]
            out.append(head[:width])
            stats = "  " + (row["state"] or "—") + " · " + (row["model"] or "—") + \
                " · " + str(row["tokens"]) + " ток · $" + format(row["cost"], ".4f")
            out.append(stats[:width])
        if self.budget:
            out.append("")
            out.append(self.budget_line()[:width])
        if self.letters:
            out.append("")
            out.append("Письма:")
            for letter in self.letters[-3:]:
                text = letter["from"] + " → " + letter["to"] + ": " + letter["text"]
                out.append(("  " + text)[:width])
        return out
