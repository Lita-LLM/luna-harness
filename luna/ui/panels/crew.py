"""Панель «Экипаж» (ЗАДАЧА-11): команда агентов-детей на виду.

Имя, вид, роль, модель, состояние (думает/действует/ждёт/спит/сдал/упал),
токены и деньги каждого, общий кошелёк и последние письма. Данные —
panels/crewdata.py (CrewData внутри PanelData), сюда приходит только
отрисовка. Созвездие роя здесь не рисуем — это ЗАДАЧА-15.
"""

from __future__ import annotations

from . import PanelContent, register_panel


class CrewPanel(PanelContent):
    """Состав команды и её жизнь."""

    def __init__(self):
        super().__init__("Экипаж")

    def lines(self, app, width: int) -> list:
        from . import panel_data  # лениво: panel_data приезжает из builtin

        return panel_data(app).crew.lines(width)


register_panel("crew", "Экипаж", CrewPanel, aliases=("экипаж", "команда", "crew"))
