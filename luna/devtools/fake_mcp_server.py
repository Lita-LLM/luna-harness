"""Фальшивый MCP-сервер на stdio: тесты и ручные проверки luna mcp, без сети.

Запуск: python -m luna.devtools.fake_mcp_server
Протокол — построчный JSON-RPC, как у настоящих stdio-серверов.
Тулы:
  echo     {text}    — вернуть текст обратно
  add      {a, b}    — сумма чисел текстом
  picture  {}        — картинка 1x1 PNG + подпись
  fail     {}        — результат с isError
  slow     {seconds} — спать и ответить (проверка таймаутов)
  env_echo {name}    — значение переменной окружения сервера (проверка env и секретов)
  crash    {}        — умереть без ответа (проверка перезапуска)
"""

from __future__ import annotations

import json
import os
import sys
import time

PNG_1X1 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
           "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")

TOOLS = [
    {"name": "echo", "description": "Вернуть текст обратно",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}},
                     "required": ["text"]}},
    {"name": "add", "description": "Сложить два числа",
     "inputSchema": {"type": "object",
                     "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                     "required": ["a", "b"]}},
    {"name": "picture", "description": "Отдать картинку 1x1 PNG",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "fail", "description": "Вернуть ошибку тула (isError)",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "slow", "description": "Спать указанное число секунд и ответить",
     "inputSchema": {"type": "object", "properties": {"seconds": {"type": "number"}}}},
    {"name": "env_echo", "description": "Показать переменную окружения сервера",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}},
                     "required": ["name"]}},
    {"name": "crash", "description": "Умереть без ответа",
     "inputSchema": {"type": "object", "properties": {}}},
]

SUPPORTED = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")


def _text(value: str) -> dict:
    return {"content": [{"type": "text", "text": value}]}


def call_tool(name: str, args: dict) -> dict:
    if name == "echo":
        return _text("Эхо: " + str(args.get("text", "")))
    if name == "add":
        return _text(str(float(args.get("a", 0)) + float(args.get("b", 0))))
    if name == "picture":
        return {"content": [{"type": "text", "text": "Картинка готова"},
                            {"type": "image", "data": PNG_1X1, "mimeType": "image/png"}]}
    if name == "fail":
        return {"content": [{"type": "text", "text": "Фальшивая ошибка тула"}], "isError": True}
    if name == "slow":
        time.sleep(float(args.get("seconds", 1)))
        return _text("Выспался")
    if name == "env_echo":
        return _text(os.environ.get(str(args.get("name", "")), "<нет такой переменной>"))
    if name == "crash":
        sys.stdout.flush()
        os._exit(3)
    return {"content": [{"type": "text", "text": "Нет такого тула: " + name}], "isError": True}


def handle(msg: dict) -> dict | None:
    method = msg.get("method")
    if "id" not in msg:
        return None  # уведомление: initialized и прочие — молча принимаем
    mid = msg.get("id")
    params = msg.get("params") or {}
    if method == "initialize":
        version = params.get("protocolVersion")
        if version not in SUPPORTED:
            version = "2025-06-18"
        result = {"protocolVersion": version, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "luna-fake-mcp", "version": "1.0"}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        result = call_tool(str(params.get("name", "")), params.get("arguments") or {})
    else:
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32601, "message": "Метод не поддерживается: " + str(method)}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main() -> int:
    out = sys.stdout.buffer
    for line in sys.stdin.buffer:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line.decode("utf-8"))
        except ValueError:
            continue
        reply = handle(msg)
        if reply is not None:
            out.write(json.dumps(reply, ensure_ascii=False).encode("utf-8") + b"\n")
            out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
