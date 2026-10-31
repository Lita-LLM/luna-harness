# Перенесено из BENDER: bender/router/adapters.py (коммит 6552ed660bfabf5202569f9905212b77dbf6d31a), переписано синхронно и сокращено.
"""Адаптеры провайдеров: OpenAI-совместимые, Anthropic, Gemini.

Каждый адаптер: url/headers/body из внутреннего формата, parse_stream —
разбор SSE-строк с отдачей кусков через emit("text"|"thinking", строка),
parse_full — разбор одного JSON-ответа. Вызовы тулов собираются целиком.
"""

from __future__ import annotations

import json

from ..net.sse import parse_sse_lines
from .types import ChatResult, ToolCall, Usage

INTENT_FIELD = "intent"


def _with_intent(spec_params: dict) -> dict:
    """К схеме каждого тула добавляем intent: модель объясняет, что и зачем."""
    params = json.loads(json.dumps(spec_params or {"type": "object", "properties": {}}))
    props = params.setdefault("properties", {})
    props[INTENT_FIELD] = {"type": "string",
                           "description": "Одна фраза: что произойдёт и зачем (показывается человеку)"}
    return params


def _split_intent(args: dict) -> tuple[dict, str]:
    args = dict(args or {})
    intent = args.pop(INTENT_FIELD, "")
    return args, str(intent or "")


def _load_args(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}
    return {}


def _parts_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content
                       if isinstance(p, dict) and p.get("type") == "text")
    return ""


# ---------- OpenAI-совместимые ----------

class OpenAIAdapter:
    kind = "openai"

    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")

    def url(self) -> str:
        return self.base + "/chat/completions"

    def models_url(self) -> str:
        return self.base + "/models"

    def headers(self, key: str | None) -> dict:
        out = {}
        if key:
            out["Authorization"] = "Bearer " + key
        return out

    def _convert(self, messages: list[dict]) -> list[dict]:
        out = []
        for m in messages:
            role = m.get("role", "user")
            item: dict = {"role": role}
            content = m.get("content", "")
            if isinstance(content, list):
                parts = []
                for p in content:
                    if not isinstance(p, dict):
                        continue
                    if p.get("type") == "text":
                        parts.append({"type": "text", "text": p.get("text", "")})
                    elif p.get("type") == "image":
                        mt = p.get("media_type", "image/png")
                        url = "data:" + mt + ";base64," + p.get("data", "")
                        parts.append({"type": "image_url", "image_url": {"url": url}})
                item["content"] = parts
            else:
                item["content"] = content
            calls = m.get("tool_calls")
            if calls:
                item["tool_calls"] = [
                    {"id": c.get("id", ""), "type": "function",
                     "function": {"name": c.get("name", ""),
                                  "arguments": json.dumps(c.get("arguments") or {}, ensure_ascii=False)}}
                    for c in calls]
            if m.get("tool_call_id"):
                item["tool_call_id"] = m["tool_call_id"]
            out.append(item)
        return out

    def body(self, model: str, messages: list[dict], tools, stream: bool) -> dict:
        body: dict = {"model": model, "messages": self._convert(messages), "stream": stream}
        if stream:
            body["stream_options"] = {"include_usage": True}
        if tools:
            body["tools"] = [
                {"type": "function",
                 "function": {"name": t.name, "description": t.description,
                              "parameters": _with_intent(t.parameters)}}
                for t in tools]
        return body

    def parse_full(self, data: dict, model: str) -> ChatResult:
        result = ChatResult(model=model)
        choices = data.get("choices") or []
        if choices:
            msg = choices[0].get("message") or {}
            result.text = msg.get("content") or ""
            result.thinking = msg.get("reasoning_content") or ""
            for call in msg.get("tool_calls") or []:
                fn = call.get("function") or {}
                args, intent = _split_intent(_load_args(fn.get("arguments")))
                result.tool_calls.append(ToolCall(call.get("id", ""), fn.get("name", ""), args, intent))
            result.finish = choices[0].get("finish_reason") or ""
        usage = data.get("usage") or {}
        result.usage = Usage(int(usage.get("prompt_tokens") or 0),
                             int(usage.get("completion_tokens") or 0))
        return result

    def parse_stream(self, lines, model: str, emit) -> ChatResult:
        result = ChatResult(model=model)
        pending: dict[int, dict] = {}
        for event in parse_sse_lines(lines):
            if event.data.strip() == "[DONE]":
                break
            try:
                data = json.loads(event.data)
            except ValueError:
                continue
            usage = data.get("usage")
            if usage:
                result.usage = Usage(int(usage.get("prompt_tokens") or 0),
                                     int(usage.get("completion_tokens") or 0))
            choices = data.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            text = delta.get("content") or ""
            if text:
                result.text += text
                emit("text", text)
            thinking = delta.get("reasoning_content") or ""
            if thinking:
                result.thinking += thinking
                emit("thinking", thinking)
            for call in delta.get("tool_calls") or []:
                idx = int(call.get("index") or 0)
                slot = pending.setdefault(idx, {"id": "", "name": "", "args": ""})
                if call.get("id"):
                    slot["id"] = call["id"]
                fn = call.get("function") or {}
                if fn.get("name"):
                    slot["name"] = fn["name"]
                slot["args"] += fn.get("arguments") or ""
            if choices[0].get("finish_reason"):
                result.finish = choices[0]["finish_reason"]
        for idx in sorted(pending):
            slot = pending[idx]
            args, intent = _split_intent(_load_args(slot["args"]))
            result.tool_calls.append(ToolCall(slot["id"], slot["name"], args, intent))
        return result


# ---------- Anthropic ----------

class AnthropicAdapter:
    kind = "anthropic"

    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")

    def url(self) -> str:
        return self.base + "/v1/messages"

    def models_url(self) -> str:
        return self.base + "/v1/models"

    def headers(self, key: str | None) -> dict:
        out = {"anthropic-version": "2023-06-01"}
        if key:
            out["x-api-key"] = key
        return out

    def _convert(self, messages: list[dict]) -> tuple[str, list[dict]]:
        system_parts: list[str] = []
        out: list[dict] = []
        for m in messages:
            role = m.get("role", "user")
            if role == "system":
                system_parts.append(_parts_text(m.get("content", "")))
                continue
            if role == "tool":
                block = {"type": "tool_result", "tool_use_id": m.get("tool_call_id", ""),
                         "content": _parts_text(m.get("content", ""))}
                out.append({"role": "user", "content": [block]})
                continue
            blocks: list[dict] = []
            content = m.get("content", "")
            if isinstance(content, list):
                for p in content:
                    if not isinstance(p, dict):
                        continue
                    if p.get("type") == "text" and p.get("text"):
                        blocks.append({"type": "text", "text": p["text"]})
                    elif p.get("type") == "image":
                        blocks.append({"type": "image", "source": {
                            "type": "base64",
                            "media_type": p.get("media_type", "image/png"),
                            "data": p.get("data", "")}})
            elif content:
                blocks.append({"type": "text", "text": content})
            for call in m.get("tool_calls") or []:
                args = dict(call.get("arguments") or {})
                if call.get("intent"):
                    args[INTENT_FIELD] = call["intent"]
                blocks.append({"type": "tool_use", "id": call.get("id", ""),
                               "name": call.get("name", ""), "input": args})
            if blocks:
                out.append({"role": "assistant" if role == "assistant" else "user",
                            "content": blocks})
        return "\n\n".join(p for p in system_parts if p), out

    def body(self, model: str, messages: list[dict], tools, stream: bool) -> dict:
        system, converted = self._convert(messages)
        body: dict = {"model": model, "messages": converted, "max_tokens": 8192, "stream": stream}
        if system:
            body["system"] = system
        if tools:
            body["tools"] = [{"name": t.name, "description": t.description,
                              "input_schema": _with_intent(t.parameters)} for t in tools]
        return body

    def _blocks_to_result(self, data: dict, model: str) -> ChatResult:
        result = ChatResult(model=model)
        for block in data.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                result.text += block.get("text", "")
            elif block.get("type") == "thinking":
                result.thinking += block.get("thinking", "")
            elif block.get("type") == "tool_use":
                args, intent = _split_intent(block.get("input"))
                result.tool_calls.append(ToolCall(block.get("id", ""), block.get("name", ""), args, intent))
        usage = data.get("usage") or {}
        result.usage = Usage(int(usage.get("input_tokens") or 0),
                             int(usage.get("output_tokens") or 0))
        result.finish = data.get("stop_reason") or ""
        return result

    def parse_full(self, data: dict, model: str) -> ChatResult:
        return self._blocks_to_result(data, model)

    def parse_stream(self, lines, model: str, emit) -> ChatResult:
        result = ChatResult(model=model)
        pending: dict[int, dict] = {}
        for event in parse_sse_lines(lines):
            try:
                data = json.loads(event.data)
            except ValueError:
                continue
            kind = data.get("type", "")
            if kind == "message_start":
                usage = (data.get("message") or {}).get("usage") or {}
                result.usage.input = int(usage.get("input_tokens") or 0)
            elif kind == "content_block_start":
                block = data.get("content_block") or {}
                if block.get("type") == "tool_use":
                    pending[int(data.get("index") or 0)] = {
                        "id": block.get("id", ""), "name": block.get("name", ""), "args": ""}
            elif kind == "content_block_delta":
                delta = data.get("delta") or {}
                if delta.get("type") == "text_delta":
                    result.text += delta.get("text", "")
                    emit("text", delta.get("text", ""))
                elif delta.get("type") == "thinking_delta":
                    result.thinking += delta.get("thinking", "")
                    emit("thinking", delta.get("thinking", ""))
                elif delta.get("type") == "input_json_delta":
                    slot = pending.get(int(data.get("index") or 0))
                    if slot is not None:
                        slot["args"] += delta.get("partial_json", "")
            elif kind == "message_delta":
                usage = data.get("usage") or {}
                if usage:
                    result.usage.output = int(usage.get("output_tokens") or 0)
                result.finish = (data.get("delta") or {}).get("stop_reason") or result.finish
        for idx in sorted(pending):
            slot = pending[idx]
            args, intent = _split_intent(_load_args(slot["args"]))
            result.tool_calls.append(ToolCall(slot["id"], slot["name"], args, intent))
        return result


# ---------- Gemini ----------

class GeminiAdapter:
    kind = "gemini"

    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")

    def url(self, model: str = "") -> str:
        verb = "streamGenerateContent?alt=sse"
        return self.base + "/v1beta/models/" + model + ":" + verb

    def full_url(self, model: str = "") -> str:
        return self.base + "/v1beta/models/" + model + ":generateContent"

    def models_url(self) -> str:
        return self.base + "/v1beta/models"

    def headers(self, key: str | None) -> dict:
        out = {}
        if key:
            out["x-goog-api-key"] = key
        return out

    def _convert(self, messages: list[dict]) -> tuple[str, list[dict]]:
        system_parts: list[str] = []
        out: list[dict] = []
        for m in messages:
            role = m.get("role", "user")
            if role == "system":
                system_parts.append(_parts_text(m.get("content", "")))
                continue
            if role == "tool":
                part = {"functionResponse": {"name": m.get("name", ""),
                                             "response": {"result": _parts_text(m.get("content", ""))}}}
                out.append({"role": "user", "parts": [part]})
                continue
            parts: list[dict] = []
            content = m.get("content", "")
            if isinstance(content, list):
                for p in content:
                    if not isinstance(p, dict):
                        continue
                    if p.get("type") == "text" and p.get("text"):
                        parts.append({"text": p["text"]})
                    elif p.get("type") == "image":
                        parts.append({"inlineData": {
                            "mimeType": p.get("media_type", "image/png"),
                            "data": p.get("data", "")}})
            elif content:
                parts.append({"text": content})
            for call in m.get("tool_calls") or []:
                args = dict(call.get("arguments") or {})
                if call.get("intent"):
                    args[INTENT_FIELD] = call["intent"]
                parts.append({"functionCall": {"name": call.get("name", ""), "args": args}})
            if parts:
                out.append({"role": "model" if role == "assistant" else "user", "parts": parts})
        return "\n\n".join(p for p in system_parts if p), out

    def body(self, model: str, messages: list[dict], tools, stream: bool) -> dict:
        system, converted = self._convert(messages)
        body: dict = {"contents": converted}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if tools:
            decls = [{"name": t.name, "description": t.description,
                      "parameters": _with_intent(t.parameters)} for t in tools]
            body["tools"] = [{"functionDeclarations": decls}]
        return body

    def _parts_to_result(self, data: dict, model: str, emit=None) -> ChatResult:
        result = ChatResult(model=model)
        for cand in data.get("candidates") or []:
            for part in (cand.get("content") or {}).get("parts") or []:
                if not isinstance(part, dict):
                    continue
                if part.get("text"):
                    result.text += part["text"]
                    if emit is not None:
                        emit("text", part["text"])
                fc = part.get("functionCall")
                if fc:
                    args, intent = _split_intent(fc.get("args"))
                    result.tool_calls.append(ToolCall("", fc.get("name", ""), args, intent))
            if cand.get("finishReason"):
                result.finish = cand["finishReason"]
        usage = data.get("usageMetadata") or {}
        if usage:
            result.usage = Usage(int(usage.get("promptTokenCount") or 0),
                                 int(usage.get("candidatesTokenCount") or 0))
        return result

    def parse_full(self, data: dict, model: str) -> ChatResult:
        return self._parts_to_result(data, model)

    def parse_stream(self, lines, model: str, emit) -> ChatResult:
        result = ChatResult(model=model)
        for event in parse_sse_lines(lines):
            try:
                data = json.loads(event.data)
            except ValueError:
                continue
            piece = self._parts_to_result(data, model, emit)
            result.text += piece.text
            result.tool_calls.extend(piece.tool_calls)
            if piece.usage.input or piece.usage.output:
                result.usage = piece.usage
            if piece.finish:
                result.finish = piece.finish
        # текст уже послан через emit внутри _parts_to_result кусками; result.text
        # собран там же по кускам, второй проход ниже не нужен
        return result


def adapter_for(kind: str, base_url: str):
    if kind == "anthropic":
        return AnthropicAdapter(base_url)
    if kind == "gemini":
        return GeminiAdapter(base_url)
    return OpenAIAdapter(base_url)
