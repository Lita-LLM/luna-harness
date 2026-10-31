# Перенесено из BENDER: bender/router/types.py (коммит 6552ed660bfabf5202569f9905212b77dbf6d31a), сокращено.
"""Общие типы роутера: внутренний формат сообщений один на все провайдеры.

Сообщение — словарь в OpenAI-стиле: {role, content, tool_calls?, tool_call_id?}.
content — строка или список частей: {"type": "text", "text": …} и
{"type": "image", "data": base64, "media_type": "image/png"} (зрение).
tool_calls у assistant: [{"id": …, "name": …, "arguments": {…}}].
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Usage:
    input: int = 0
    output: int = 0

    def add(self, other: "Usage") -> None:
        self.input += other.input
        self.output += other.output


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict = field(default_factory=dict)
    intent: str = ""


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict  # JSON-схема аргументов

    def openai(self) -> dict:
        return {"type": "function",
                "function": {"name": self.name, "description": self.description,
                             "parameters": self.parameters}}


@dataclass
class ChatResult:
    text: str = ""
    thinking: str = ""
    tool_calls: list = field(default_factory=list)  # list[ToolCall]
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    finish: str = ""
