"""Легендарные фразы из docs/ДИЗАЙН.md: лежат в phrases.json рядом, читаются лениво."""

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


def spinner_phrases() -> list[str]:
    return list(_load().get("spinner") or [])


def splash_phrases() -> list[str]:
    return list(_load().get("splash") or [])


def random_spinner() -> str:
    phrases = spinner_phrases()
    return random.choice(phrases) if phrases else "Думает"


def random_splash() -> str:
    phrases = splash_phrases()
    return random.choice(phrases) if phrases else "Лунапарк открыт."


def event_phrase(name: str) -> str:
    return str((_load().get("events") or {}).get(name) or "")
