"""Панель «Диагностика» (ЗАДАЧА-16): вывод последнего линтера/компилятора.

Источник — события тулов (PanelData.diag): последний запуск тестов,
линтера или компилятора через bash, его вывод целиком (хвост — в панели,
прокрутка в фокусе). Это честный заменитель LSP: language server в Луне
нет и не обещается, а «что сказал последний прогон» — вот оно.
"""

from __future__ import annotations

import time

from . import register_panel
from .builtin import _TAIL_DEFAULT, _ScrollablePanel, panel_data


class DiagPanel(_ScrollablePanel):
    """Последний диагностический прогон: команда, время, вывод."""

    def __init__(self):
        super().__init__("Диагностика")

    def lines(self, app, width: int) -> list:
        diag = panel_data(app).diag
        if diag is None:
            return ["диагностики пока нет",
                    "сюда попадёт вывод последнего запуска",
                    "тестов, линтера или компилятора (тул bash)"]
        mark = "✓" if diag["ok"] else "✗"
        when = time.strftime("%H:%M:%S", time.localtime(diag["ts"]))
        head = [mark + " " + diag["title"] + " · " + when]
        body = list(diag["body"].splitlines())
        height = self.height_hint or _TAIL_DEFAULT
        return head + self._window(body)[-max(1, height - 1):]


register_panel("diag", "Диагностика", DiagPanel,
               aliases=("diag", "диагностика"))
