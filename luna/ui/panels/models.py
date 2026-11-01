"""Панель «Модели» (ЗАДАЧА-16).

Роли из настроек (model.main/fast/guard/vision), чем настроены, какая
модель отвечает в сессии и сколько занял последний ход. Сеть не дёргаем:
«живость» провайдера честно видна только по событиям usage.
"""

from __future__ import annotations

from . import PanelContent, register_panel
from .builtin import panel_data

ROLES = (("main", "главная"), ("fast", "быстрая"),
         ("guard", "охранник"), ("vision", "зрение"))


class ModelsPanel(PanelContent):
    """Роли моделей и их настройка; текущая модель сессии — из событий."""

    def __init__(self):
        super().__init__("Модели")

    def lines(self, app, width: int) -> list:
        settings = getattr(app, "settings", None)
        data = panel_data(app)
        out = []
        provider = ""
        if settings is not None:
            provider = str(settings.get("model.provider") or "")
        out.append("провайдер по умолчанию: " + (provider or "—"))
        main_ref = ""
        for role, title in ROLES:
            ref = ""
            if settings is not None:
                ref = str(settings.get("model." + role) or "")
            if role == "main":
                main_ref = ref
            label = ref if ref else ("как главная" if role != "main"
                                     and main_ref else "не задана")
            out.append(role + " (" + title + "): " + label)
        out.append("")
        if data.model:
            out.append("в сессии отвечает: " + data.model)
        else:
            out.append("в сессии модель ещё не отвечала")
        if data.last_turn_sec is not None:
            out.append("последний ход: " + format(data.last_turn_sec, ".1f") + " с")
        out.append("смена на лету: /model")
        return out


register_panel("models", "Модели", ModelsPanel,
               aliases=("models", "модели"))
