# Перенесено из BENDER: bender/devtools/fake_llm.py (коммит 6552ed660bfabf5202569f9905212b77dbf6d31a), сокращено до нужного Луне.
"""Фальшивая модель: ничего не стоит, сеть не нужна.

Два лица одного сценария:
- FakeModelClient — внутрипроцессный клиент для тестов агента (без сокетов);
- HTTP-сервер (run_fake_server) — OpenAI-совместимый, для ручной проверки:
  провайдер http://127.0.0.1:5199/v1, модели ниже.

Поведение — по имени модели: echo (эхо последнего сообщения), think
(рассуждения, потом ответ), tools (один вызов первого тула, потом итог),
script (шаги из сценария), error-NNN (HTTP-ошибка NNN), empty, long.
Шаг сценария: {"text": …} | {"reasoning": …, "text": …} |
{"tool_calls": [{"name": …, "arguments": {…}, "intent": …}]} | {"error": 429}.
"""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..net.http_client import HttpError
from ..router.types import ChatResult, ToolCall, Usage

MODELS = ["echo", "think", "tools", "script", "empty", "long",
          "error-401", "error-429", "error-500", "error-503"]

_SCRIPT: list[dict] = []
_REQUESTS: list[dict] = []


def set_script(steps: list[dict]) -> None:
    """Задать сценарий модели script (и шаги для любого клиента без своего)."""
    _SCRIPT.clear()
    _SCRIPT.extend(steps)


def script_steps() -> list[dict]:
    return _SCRIPT


def seen_requests() -> list[dict]:
    return _REQUESTS


def _last_user(messages) -> str:
    for m in reversed(messages or []):
        if m.get("role") == "user":
            content = m.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return " ".join(p.get("text", "") for p in content
                                if isinstance(p, dict) and p.get("type") == "text")
    return ""


def _last_tool(messages) -> str | None:
    if messages and messages[-1].get("role") == "tool":
        content = messages[-1].get("content")
        return content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    return None


class FakeModelClient:
    """Синхронный фальшивый провайдер: тот же интерфейс, что у Router._chat_once."""

    def __init__(self, script: list[dict] | None = None):
        self.script = script if script is not None else _SCRIPT

    def chat(self, model: str, messages, tools, emit) -> ChatResult:
        _REQUESTS.append({"model": model, "messages": messages,
                          "tools": [t.name for t in tools or []]})
        name = (model or "echo").split(":")[-1]
        result = ChatResult(model=model)
        match = re.match(r"error-(\d{3})$", name)
        if match:
            code = int(match.group(1))
            raise HttpError(code, json.dumps({"error": {"message": "фальшивая ошибка " + str(code)}}))
        if self.script:
            step = self.script.pop(0)
            if "error" in step:
                code = int(step["error"])
                raise HttpError(code, json.dumps({"error": {"message": "ошибка сценария"}}))
            reasoning = step.get("reasoning", "")
            if reasoning:
                result.thinking = reasoning
                emit("thinking", reasoning)
            text = step.get("text", "")
            if text:
                result.text = text
                emit("text", text)
            for call in step.get("tool_calls") or []:
                result.tool_calls.append(ToolCall(
                    call.get("id", "fake-" + call.get("name", "tool")),
                    call.get("name", ""), call.get("arguments") or {}, call.get("intent", "")))
            usage = step.get("usage") or {}
            result.usage = Usage(int(usage.get("input", 10)), int(usage.get("output", 5)))
            return result
        if name == "empty":
            pass
        elif name == "think":
            result.thinking = "Думаю фальшиво, но честно."
            emit("thinking", result.thinking)
            result.text = "Готово: " + _last_user(messages)
            emit("text", result.text)
        elif name == "tools":
            tool_result = _last_tool(messages)
            if tool_result is not None:
                result.text = "Результат тула: " + tool_result[:200]
                emit("text", result.text)
            elif tools:
                first = tools[0]
                result.tool_calls.append(ToolCall("fake-1", first.name, {},
                                                  "фальшивый вызов для проверки цикла"))
            else:
                result.text = "Тулов не предложили."
                emit("text", result.text)
        elif name == "long":
            result.text = "Длинный фальшивый ответ. " * 400
            emit("text", result.text)
        else:  # echo и всё остальное
            result.text = "Эхо: " + _last_user(messages)
            emit("text", result.text)
        result.usage = Usage(10, 5)
        return result


# ---------- HTTP-сервер (OpenAI-совместимый) ----------

def _openai_chunk(model: str, delta: dict, finish=None) -> str:
    payload = {"id": "fake", "object": "chat.completion.chunk", "model": model,
               "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def make_handler():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # тишина: фальшивому серверу логи не нужны
            pass

        def _json(self, code: int, data: dict) -> None:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802 — имя задаёт http.server
            if self.path.rstrip("/").endswith("/models"):
                self._json(200, {"object": "list",
                                 "data": [{"id": m, "object": "model"} for m in MODELS]})
            else:
                self._json(404, {"error": {"message": "нет такого пути"}})

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                body = {}
            model = body.get("model", "echo")
            messages = body.get("messages") or []
            specs = []
            for spec in body.get("tools") or []:
                fn = spec.get("function") or {}
                from ..router.types import ToolSpec

                specs.append(ToolSpec(fn.get("name", ""), fn.get("description", ""),
                                      fn.get("parameters") or {}))
            chunks: list[tuple[str, str]] = []

            def emit(kind, text):
                chunks.append((kind, text))

            client = FakeModelClient()
            try:
                result = client.chat(model, messages, specs, emit)
            except HttpError as exc:
                self._json(exc.http_status, {"error": {"message": "фальшивая ошибка"}})
                return
            if body.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for kind, text in chunks:
                    key = "reasoning_content" if kind == "thinking" else "content"
                    self.wfile.write(_openai_chunk(model, {key: text}).encode("utf-8"))
                for call in result.tool_calls:
                    delta = {"tool_calls": [{"index": 0, "id": call.id, "type": "function",
                                             "function": {"name": call.name,
                                                          "arguments": json.dumps(call.arguments, ensure_ascii=False)}}]}
                    self.wfile.write(_openai_chunk(model, delta).encode("utf-8"))
                self.wfile.write(_openai_chunk(model, {}, "stop").encode("utf-8"))
                self.wfile.write(b"data: [DONE]\n\n")
                return
            message: dict = {"role": "assistant", "content": result.text}
            if result.thinking:
                message["reasoning_content"] = result.thinking
            if result.tool_calls:
                message["tool_calls"] = [
                    {"id": c.id, "type": "function",
                     "function": {"name": c.name,
                                  "arguments": json.dumps(c.arguments, ensure_ascii=False)}}
                    for c in result.tool_calls]
            self._json(200, {"id": "fake", "object": "chat.completion", "model": model,
                             "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
                             "usage": {"prompt_tokens": result.usage.input,
                                       "completion_tokens": result.usage.output}})

    return Handler


def run_fake_server(port: int = 5199) -> ThreadingHTTPServer:
    """Запустить фальшивый сервер моделей (для luna try fake и ручных проверок)."""
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler())
    return server
