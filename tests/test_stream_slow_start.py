"""Стрим переживает долгую паузу до первого токена и при этом прерывается сразу.

Регресс ЗАДАЧИ-18: короткий таймаут сокета ломал ответ при первой же паузе
(модель думает) — каждый запрос обрывался, роутер видел 499.
"""
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from luna.net.http_client import HttpClient


class SlowHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        time.sleep(self.server.pause)  # модель «думает» до первого токена
        try:
            for i in range(self.server.lines):
                self.wfile.write(("data: " + str(i) + "\n\n").encode())
                self.wfile.flush()
                time.sleep(self.server.gap)
        except OSError:
            pass

    def log_message(self, *args):
        pass


class SlowStreamTest(unittest.TestCase):
    def serve(self, pause, lines, gap):
        server = ThreadingHTTPServer(("127.0.0.1", 0), SlowHandler)
        server.pause, server.lines, server.gap = pause, lines, gap
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return "http://127.0.0.1:" + str(server.server_address[1]) + "/v1"

    def test_long_pause_before_first_token_survives(self):
        url = self.serve(pause=1.2, lines=3, gap=0.05)
        got = [l for l in HttpClient(timeout=10).stream_lines(url, {"x": 1}) if l.startswith("data:")]
        self.assertEqual(len(got), 3)

    def test_cancel_breaks_quickly_while_waiting(self):
        url = self.serve(pause=30, lines=1, gap=0)
        client = HttpClient(timeout=60)
        threading.Timer(0.5, client.cancel_active).start()
        started = time.monotonic()
        list(client.stream_lines(url, {"x": 1}))
        self.assertLess(time.monotonic() - started, 3.0)


if __name__ == "__main__":
    unittest.main()
