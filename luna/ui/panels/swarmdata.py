"""Данные роя для панелей (ЗАДАЧА-15): состояние потока из событий swarm.*.

Отдельный модуль данных, как crewdata у «Экипажа»: PanelData только
маршрутизирует сюда события swarm.start/progress/cells/done и письма
ячеек (agent.message с адресами вида leo/rev.3.12). «Созвездие» рисует
по этому состоянию, «Луна» и «Экипаж» берут одну строку line().
Хранится не больше _MAX_CELLS ячеек: в рое на 10 000 сюда приходят
агрегаты по звеньям (swarm.cells с links=True), а не каждая звезда.
"""

from __future__ import annotations

_MAX_CELLS = 8192


class SwarmData:
    """Текущий (или последний) поток: счётчики, звёзды, письма, прогноз."""

    def __init__(self) -> None:
        self.swarm = ""
        self.stream = ""
        self.host = ""
        self.total = 0
        self.done = 0
        self.failed = 0
        self.skipped = 0
        self.spent = 0.0
        self.eta_sec = None
        self.finished = False
        self.stopped_reason = ""
        self.cells: dict = {}        # адрес → состояние (done|failed|run|wait|link-агрегат)
        self.links_mode = False
        self.letters: list = []      # последние письма {from, to, text}

    def observe(self, ev: dict) -> None:
        etype = ev.get("type")
        if etype == "agent.message":
            frm = str(ev.get("from") or "")
            to = str(ev.get("to") or "")
            if "/" in frm and "/" in to:  # письмо роя — по адресам, не по именам
                self.letters.append({"from": frm, "to": to,
                                     "text": str(ev.get("text") or "")})
                del self.letters[:-16]
            return
        if etype == "swarm.start":
            self.swarm = str(ev.get("swarm") or "")
            self.stream = str(ev.get("stream") or "")
            self.host = str(ev.get("host") or "")
            self.total = int(ev.get("total") or 0)
            self.done = self.failed = self.skipped = 0
            self.spent = 0.0
            self.eta_sec = None
            self.finished = False
            self.stopped_reason = ""
            self.cells = {}
            self.links_mode = False
        elif etype == "swarm.progress":
            self.done = int(ev.get("done") or 0)
            self.failed = int(ev.get("failed") or 0)
            self.skipped = int(ev.get("skipped") or 0)
            self.total = int(ev.get("total") or self.total)
            self.spent = float(ev.get("spent_usd") or 0.0)
            self.eta_sec = ev.get("eta_sec")
        elif etype == "swarm.cells":
            self.links_mode = bool(ev.get("links"))
            cells = {}
            for row in ev.get("cells") or []:
                if not isinstance(row, (list, tuple)) or len(row) < 2:
                    continue
                cells[str(row[0])] = str(row[1])
                if len(cells) >= _MAX_CELLS:
                    break
            self.cells = cells
        elif etype == "swarm.done":
            self.done = int(ev.get("done") or 0)
            self.failed = int(ev.get("failed") or 0)
            self.skipped = int(ev.get("skipped") or 0)
            self.total = int(ev.get("total") or self.total)
            self.spent = float(ev.get("spent_usd") or 0.0)
            self.stopped_reason = str(ev.get("stopped_reason") or "")
            self.finished = True

    def active(self) -> bool:
        return bool(self.swarm) and not self.finished

    def line(self) -> str:
        """Одна строка потока для панелей «Луна» и «Экипаж»."""
        if not self.swarm:
            return ""
        text = ("рой " + self.stream + ": сдано " + str(self.done) + "/" +
                str(self.total) + " · $" + format(self.spent, ".4f"))
        if self.failed:
            text += " · упало " + str(self.failed)
        if self.finished:
            text += " · завершён"
            if self.stopped_reason:
                text += " (" + self.stopped_reason + ")"
        elif self.eta_sec:
            text += " · прогноз ≈" + format(float(self.eta_sec), ".0f") + " с"
        return text
