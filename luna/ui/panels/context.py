"""Панель «Контекст» (ЗАДАЧА-16): из чего состоит контекст сессии.

Данные — лента экрана (app.entries) и PanelData: доли по видам записей,
крупнейшие сообщения, кандидаты на сжатие. Токены — оценка: занятый объём
(процент × размер окна из настройки model.context_tokens) делится между
видами пропорционально символам. Честная оценка, не замер провайдера.
"""

from __future__ import annotations

import json

from . import PanelContent, register_panel
from .builtin import panel_data

_GROUPS = (
    ("agent", "ответы агента"),
    ("tool", "тулы и их вывод"),
    ("user", "сообщения человека"),
    ("thinking", "рассуждения"),
    ("other", "служебное"),
)


def _group_of(kind: str) -> str:
    if kind in ("agent", "tool", "user", "thinking"):
        return kind
    return "other"


def _entry_chars(entry) -> int:
    total = len(entry.text or "")
    if entry.kind == "tool":
        total += len(entry.output or "") + len(entry.summary or "")
        if entry.args:
            try:
                total += len(json.dumps(entry.args, ensure_ascii=False, default=str))
            except (TypeError, ValueError):
                total += len(str(entry.args))
    return total


def _bar(pct: float, width: int) -> str:
    cells = max(4, min(16, width // 3))
    filled = int(round(cells * max(0.0, min(100.0, pct)) / 100.0))
    return "█" * filled + "░" * (cells - filled)


def _first_line(entry) -> str:
    text = (entry.text or entry.summary or "").strip().split("\n")[0]
    if entry.kind == "tool":
        text = (entry.name + " " + (entry.summary or "")).strip()
    return text[:44]


class ContextPanel(PanelContent):
    """Состав контекста по долям + крупнейшие записи + кандидаты на сжатие."""

    def __init__(self):
        super().__init__("Контекст")

    def lines(self, app, width: int) -> list:
        data = panel_data(app)
        entries = list(getattr(app, "entries", []) or [])
        settings = getattr(app, "settings", None)
        window = 0
        if settings is not None:
            try:
                window = int(settings.get("model.context_tokens") or 0)
            except Exception:  # noqa: BLE001 — панель не роняет экран
                window = 0
        used = int(window * data.context_pct / 100) if window else 0
        head = "занято: " + str(data.context_pct) + "%"
        if window:
            head += " · ≈" + str(used) + " из " + str(window) + " токенов"
        out = [head, ""]
        if not entries:
            out.append("лента пуста — контекста пока нет")
            return out
        chars: dict = {}
        total = 0
        for entry in entries:
            size = _entry_chars(entry)
            total += size
            group = _group_of(entry.kind)
            chars[group] = chars.get(group, 0) + size
        for group, title in _GROUPS:
            size = chars.get(group, 0)
            if not size:
                continue
            pct = 100.0 * size / total if total else 0.0
            out.append(title + ": " + format(pct, ".0f") + "%")
            out.append("  " + _bar(pct, width))
        out.append("")
        out.append("крупнейшие сообщения:")
        biggest = sorted(entries, key=_entry_chars, reverse=True)[:5]
        for entry in biggest:
            out.append("  " + str(_entry_chars(entry)) + " симв · "
                       + _first_line(entry))
        heavy = [e for e in entries
                 if e.kind == "tool" and len(e.output or "") > 2000]
        if heavy:
            heavy_chars = sum(_entry_chars(e) for e in heavy)
            share = format(100.0 * heavy_chars / total, ".0f") if total else "0"
            out.append("")
            out.append("кандидаты на сжатие: вывод тулов — " + str(len(heavy))
                       + " записей, ≈" + share + "% ленты (/compact)")
        return out


register_panel("context", "Контекст", ContextPanel,
               aliases=("context", "контекст"))
