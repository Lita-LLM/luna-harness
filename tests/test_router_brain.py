"""Роутер и адаптеры на фальшивой сети: стрим, запасные, роли, каталог."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from luna.core.secrets import Secrets
from luna.core.settings import Settings
from luna.net.http_client import HttpClient
from luna.router.adapters import AnthropicAdapter, GeminiAdapter, OpenAIAdapter
from luna.router.core import ProviderConfig, Router
from luna.router.types import ToolSpec


def sse(payload: dict) -> list[str]:
    return ["data: " + json.dumps(payload), ""]


def make_router(tmp: Path) -> Router:
    settings = Settings(home=tmp / "home", cwd=tmp)
    settings.set("model.provider", "fake")
    settings.set("model.kind", "fake")
    settings.set("model.main", "fake:echo")
    secrets = Secrets(tmp / "secrets.json")
    providers = {"fake": ProviderConfig("fake", "fake", "", None)}
    return Router(settings, secrets, providers=providers)


class AdaptersTest(unittest.TestCase):
    def test_openai_stream_text_thinking_tools_usage(self):
        adapter = OpenAIAdapter("http://fake/v1")
        lines: list[str] = []
        lines += sse({"choices": [{"delta": {"reasoning_content": "думаю "}}]})
        lines += sse({"choices": [{"delta": {"content": "при"}}]})
        lines += sse({"choices": [{"delta": {"content": "вет"}}]})
        lines += sse({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "c1", "function": {"name": "read",
             "arguments": "{\"path\": \"a.txt\", \"intent\": \"глянуть\"}"}}]}}]})
        lines += sse({"choices": [{"delta": {}}],
                      "usage": {"prompt_tokens": 7, "completion_tokens": 3}})
        lines += ["data: [DONE]", ""]
        seen: list[tuple[str, str]] = []
        result = adapter.parse_stream(lines, "m", lambda k, t: seen.append((k, t)))
        self.assertEqual(result.text, "привет")
        self.assertEqual(result.thinking, "думаю ")
        self.assertEqual(result.usage.input, 7)
        self.assertEqual(len(result.tool_calls), 1)
        call = result.tool_calls[0]
        self.assertEqual(call.name, "read")
        self.assertEqual(call.arguments, {"path": "a.txt"})
        self.assertEqual(call.intent, "глянуть")
        self.assertIn(("text", "при"), seen)

    def test_openai_body_adds_intent_and_image(self):
        adapter = OpenAIAdapter("http://fake/v1")
        tools = [ToolSpec("read", "читать", {"type": "object", "properties": {"path": {"type": "string"}}})]
        messages = [{"role": "user", "content": [
            {"type": "text", "text": "что на картинке"},
            {"type": "image", "data": "AAAA", "media_type": "image/png"}]}]
        body = adapter.body("m", messages, tools, False)
        props = body["tools"][0]["function"]["parameters"]["properties"]
        self.assertIn("intent", props)
        parts = body["messages"][0]["content"]
        self.assertEqual(parts[1]["image_url"]["url"], "data:image/png;base64,AAAA")

    def test_anthropic_full_tool_use(self):
        adapter = AnthropicAdapter("http://fake")
        data = {"content": [
            {"type": "thinking", "thinking": "хм"},
            {"type": "text", "text": "делаю"},
            {"type": "tool_use", "id": "t1", "name": "write",
             "input": {"path": "a", "content": "b", "intent": "записать"}}],
            "usage": {"input_tokens": 5, "output_tokens": 2}, "stop_reason": "tool_use"}
        result = adapter.parse_full(data, "claude")
        self.assertEqual(result.text, "делаю")
        self.assertEqual(result.thinking, "хм")
        self.assertEqual(result.tool_calls[0].arguments, {"path": "a", "content": "b"})
        self.assertEqual(result.tool_calls[0].intent, "записать")
        self.assertEqual(result.usage.output, 2)

    def test_anthropic_convert_separates_system_and_tools(self):
        adapter = AnthropicAdapter("http://fake")
        system, converted = adapter._convert([
            {"role": "system", "content": "будь луной"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "t1", "name": "read", "arguments": {"path": "a"}, "intent": "глянуть"}]},
            {"role": "tool", "tool_call_id": "t1", "content": "текст файла"},
        ])
        self.assertEqual(system, "будь луной")
        self.assertEqual(converted[0]["content"][0]["type"], "tool_use")
        self.assertEqual(converted[1]["content"][0]["type"], "tool_result")

    def test_gemini_full_function_call(self):
        adapter = GeminiAdapter("http://fake")
        data = {"candidates": [{"content": {"parts": [
            {"text": "ок"},
            {"functionCall": {"name": "bash", "args": {"command": "ls", "intent": "список"}}}]},
            "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 4, "candidatesTokenCount": 1}}
        result = adapter.parse_full(data, "gemini")
        self.assertEqual(result.text, "ок")
        self.assertEqual(result.tool_calls[0].name, "bash")
        self.assertEqual(result.tool_calls[0].arguments, {"command": "ls"})
        self.assertEqual(result.tool_calls[0].intent, "список")


class RouterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="luna-router-"))
        self.router = make_router(self.tmp)

    def test_role_empty_means_main(self):
        self.assertEqual(self.router.role_ref("fast"), "fake:echo")
        self.assertEqual(self.router.role_ref("guard"), "fake:echo")
        self.router.settings.set("model.fast", "fake:think")
        self.assertEqual(self.router.role_ref("fast"), "fake:think")

    def test_chat_echo(self):
        result = self.router.chat([{"role": "user", "content": "привет"}], None,
                                  on_event=lambda k, t: None)
        self.assertEqual(result.text, "Эхо: привет")

    def test_fallback_on_retryable_error(self):
        self.router.settings.set("model.main", "fake:error-503")
        self.router.settings.set("model.fallbacks", "fake:echo")
        result = self.router.chat([{"role": "user", "content": "запас"}], None,
                                  on_event=lambda k, t: None)
        self.assertEqual(result.text, "Эхо: запас")

    def test_no_fallback_on_client_error(self):
        self.router.settings.set("model.main", "fake:error-401")
        self.router.settings.set("model.fallbacks", "fake:echo")
        with self.assertRaises(Exception):
            self.router.chat([{"role": "user", "content": "x"}], None,
                             on_event=lambda k, t: None)

    def test_fetch_models_fake(self):
        models = self.router.fetch_models("fake")
        self.assertIn("echo", models)
        self.assertIn("script", models)


if __name__ == "__main__":
    unittest.main()
