"""Вывод в UTF-8: переконфигурация stdout/stderr одним помощником.

Урок РЕВЬЮ-01 (раунд 2) был выучен наполовину: потоки настраивала только
точка входа main(), а печатающие библиотечные команды (doctor, measure,
snapshot) зовут и в обход неё — из тестов и чужих программ. На Windows
системная кодировка пайпа (cp1251/cp1252) роняет русский текст и знаки
вроде ✓/✗ UnicodeEncodeError-ом — так покраснел CI ветки zadacha-03.

Правило: печатаешь сам — сам и обеспечивай кодировку. Каждый печатающий
вход зовёт ensure_utf8_stdio() первой строкой; точка входа библиотеке
не нянька.
"""

from __future__ import annotations

import sys


def ensure_utf8_stdio() -> None:
    """stdout и stderr — UTF-8 с заменой неподдерживаемых символов.

    Идемпотентно и молча: заменённый поток без reconfigure (например,
    StringIO в чужих тестах) — не повод падать, вывод просто останется
    как есть.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 — не повод ронять команду
            pass
