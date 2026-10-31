# Перенесено из BENDER: bender/net/sse.py (коммит 6552ed660bfabf5202569f9905212b77dbf6d31a), добавлен синхронный разбор.
"""Разбор потока Server-Sent Events (так модели присылают ответ кусками)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Iterable, Iterator

MAX_LINE = 1024 * 1024
MAX_EVENT = 1024 * 1024


@dataclass
class Event:
    event: str
    data: str
    id: str | None = None


def parse_sse_lines(lines: Iterable[str]) -> Iterator[Event]:
    """События из строк потока. Комментарии (`: ping`) пропускаются,
    многострочный data склеивается, пустая строка завершает событие."""
    event = ""
    data: list[str] = []
    ev_id = None
    size = 0
    for raw in lines:
        line = raw.rstrip("\r\n")
        if len(line) > MAX_LINE:
            raise ValueError("Слишком длинная строка SSE")
        if line == "":
            if data or event:
                yield Event(event or "message", "\n".join(data), ev_id)
            event, data, ev_id = "", [], None
            size = 0
            continue
        if line.startswith(":"):
            continue
        name, sep, value = line.partition(":")
        if sep and value.startswith(" "):
            value = value[1:]
        if name == "data":
            size += len(value)
            if size > MAX_EVENT or len(data) >= 4096:
                raise ValueError("Слишком большое событие SSE")
            data.append(value)
        elif name == "event":
            event = value
        elif name == "id":
            ev_id = value
    if data:
        yield Event(event or "message", "\n".join(data), ev_id)


async def parse_sse(lines: AsyncIterator[str]) -> AsyncIterator[Event]:
    """Асинхронный вариант того же разбора (совместимость с Бендером)."""
    event = ""
    data: list[str] = []
    ev_id = None
    size = 0
    async for raw in lines:
        line = raw.rstrip("\r\n")
        if len(line) > MAX_LINE:
            raise ValueError("Слишком длинная строка SSE")
        if line == "":
            if data or event:
                yield Event(event or "message", "\n".join(data), ev_id)
            event, data, ev_id = "", [], None
            size = 0
            continue
        if line.startswith(":"):
            continue
        name, sep, value = line.partition(":")
        if sep and value.startswith(" "):
            value = value[1:]
        if name == "data":
            size += len(value)
            if size > MAX_EVENT or len(data) >= 4096:
                raise ValueError("Слишком большое событие SSE")
            data.append(value)
        elif name == "event":
            event = value
        elif name == "id":
            ev_id = value
    if data:
        yield Event(event or "message", "\n".join(data), ev_id)
