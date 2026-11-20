"""Легендарные фразы из docs/ДИЗАЙН.md: лежат в phrases.json рядом, читаются лениво.

В Новолунии у заставки и спиннера свои, тёмные фразы (splash_newmoon,
spinner_newmoon). Событие может быть строкой или списком — тогда фраза
выбирается случайно.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

_cache: dict | None = None


def _load() -> dict:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads((Path(__file__).with_name("phrases.json")).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _cache = {"spinner": [], "splash": [], "events": {}}
    return _cache


def _dark() -> bool:
    try:
        from ..core import newmoon

        return bool(newmoon.active())
    except Exception:  # noqa: BLE001 — фраза не важнее старта
        return False


def _pick(key: str, newmoon) -> list[str]:
    data = _load()
    if newmoon is None:
        newmoon = _dark()
    if newmoon and data.get(key + "_newmoon"):
        return [str(p) for p in data[key + "_newmoon"]]
    return [str(p) for p in data.get(key) or []]


def spinner_phrases(newmoon: bool | None = None) -> list[str]:
    return _pick("spinner", newmoon)


def splash_phrases(newmoon: bool | None = False) -> list[str]:
    return _pick("splash", newmoon)


def random_spinner(newmoon: bool | None = None) -> str:
    phrases = spinner_phrases(newmoon)
    return random.choice(phrases) if phrases else "Думает"


def random_splash(newmoon: bool | None = None) -> str:
    phrases = _pick("splash", newmoon)
    return random.choice(phrases) if phrases else "Лунапарк открыт."


def event_phrase(name: str) -> str:
    value = (_load().get("events") or {}).get(name)
    if isinstance(value, list):
        value = random.choice(value) if value else ""
    return str(value or "")
