"""Фальшивый HTTP-сервер моделей: каталог и чат, стрим и одним ответом.
Сеть — только 127.0.0.1 и только фальшивая (условие задачи)."""

from __future__ import annotations

import json
import threading
import unittest

from luna.devtools.fake_llm import run_fake_server
from luna.net.http_client import HttpClient
from luna.router.adapters import OpenAIAdapter


class FakeServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = run_fake_server(0)
        cls.port = cls.server.server_address[1]
        cls.base = "http://127.0.0.1:" + str(cls.port) + "/v1"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.http = HttpClient(timeout=10.0)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_models_endpoint(self):
        data = self.http.get_json(self.base + "/models")
        ids = [item["id"] for item in data["data"]]
        self.assertIn("echo", ids)

    def test_chat_full_and_stream_agree(self):
        adapter = OpenAIAdapter(self.base)
        messages = [{"role": "user", "content": "сервер"}]
        full = self.http.post_json(self.base + "/chat/completions",
                                   adapter.body("echo", messages, None, False),
                                   adapter.headers(None))
        result_full = adapter.parse_full(full, "echo")
        self.assertEqual(result_full.text, "Эхо: сервер")
        lines = self.http.stream_lines(self.base + "/chat/completions",
                                       adapter.body("echo", messages, None, True),
                                       adapter.headers(None))
        chunks: list[str] = []
        result_stream = adapter.parse_stream(lines, "echo", lambda k, t: chunks.append(t))
        self.assertEqual(result_stream.text, "Эхо: сервер")
        self.assertEqual("".join(chunks), "Эхо: сервер")

    def test_error_model_returns_http_error(self):
        adapter = OpenAIAdapter(self.base)
        with self.assertRaises(Exception) as caught:
            self.http.post_json(self.base + "/chat/completions",
                                adapter.body("error-429", [], None, False),
                                adapter.headers(None))
        status = getattr(caught.exception, "http_status", None)
        self.assertEqual(status, 429)


if __name__ == "__main__":
    unittest.main()
