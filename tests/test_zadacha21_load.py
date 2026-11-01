"""ЗАДАЧА-21, раздел 3: НАГРУЗОЧНЫЙ стенд роя (помечен как нагрузочный).

Фальшивый OpenAI-совместимый сервер ведёт себя как настоящая модель:
пауза до первого токена, медленный стрим, случайные 429 и обрывы связи,
лимит параллельности по адресу. Сценарий: 60 ячеек на пуле из 3 адресов.

Проверки: пул делит параллельность по 429 и восстанавливается; все
ячейки сданы; прерывание роя гасит всё за ≤3 с (лимит — в тесте ниже);
после роя нет утечки потоков и открытых сокетов; память разумная.
Тест обязан укладываться в ~30 с.
"""

from __future__ import annotations

import json
import os
import random
import socket
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from luna.core.secrets import Secrets
from luna.core.settings import Settings
from luna.router.core import Router
from luna.starfall.engine import RouterCellRunner, SwarmRun
from luna.starfall.profiles import CellProfile

LOAD_TEST = True  # пометка: тест нагрузочный, не юнит


class _PoisonPlan:
    """Общий план отказов стенда: какие ВХОДЫ ячеек отравлены и как.

    План по номерам запросов адреса (было так) на параллельном рое
    недетерминирован: повторы одной ячейки бегут быстрее соседей и
    съедают все триггеры сами — на Windows CI одна ячейка так сожгла
    все 3 попытки об 429 и рой сдал 59/60. План по входам честен при
    любом переплетении: одна попытка ячейки — это ДВА запроса (стрим
    и его fallback у роутера), поэтому жертве отравляются первые
    четыре запроса: вход №7 получает 429, вход №23 — обрывы, обе
    первые попытки этих ячеек умирают при любом расписании потоков,
    третьи обязаны пройти, остальные входы не тронуты.
    """

    POISON = {"7": "429", "23": "drop"}
    PER_INPUT = 4

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.seen: dict = {}

    def mode_for(self, input_id: str) -> str:
        mode = self.POISON.get(input_id)
        if mode is None:
            return "ok"
        with self.lock:
            count = self.seen.get(input_id, 0)
            if count >= self.PER_INPUT:
                return "ok"
            self.seen[input_id] = count + 1
            return mode


class _LoadState:
    """Состояние одного фальшивого адреса: счётчики запросов."""

    def __init__(self, seed: int) -> None:
        self.lock = threading.Lock()
        self.requests = 0
        self.active = 0
        self.max_active = 0
        self.random = random.Random(seed)

    def begin(self) -> None:
        with self.lock:
            self.requests += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)

    def end(self) -> None:
        with self.lock:
            self.active -= 1


_INPUT_RE = None


def _input_id(raw: bytes) -> str:
    """Номер входа ячейки из тела запроса (вход — «вход N»)."""
    global _INPUT_RE
    if _INPUT_RE is None:
        import re

        _INPUT_RE = re.compile("вход (\\d+)".encode("utf-8"))
    match = _INPUT_RE.search(raw or b"")
    return match.group(1).decode("ascii") if match else ""


def _make_handler(state: _LoadState, *, slow: bool = False,
                  plan: _PoisonPlan | None = None):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def do_POST(self):  # noqa: N802
            state.begin()
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) or b"{}"
                mode = plan.mode_for(_input_id(raw)) if plan else "ok"
                if mode == "drop":
                    # Обрыв: соединение умирает без ответа.
                    self.close_connection = True
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    self.connection.close()
                    return
                if mode == "429":
                    self._send(429, json.dumps(
                        {"error": {"message": "медленнее"}}).encode(),
                        "application/json")
                    return
                try:
                    body = json.loads(raw)
                except ValueError:
                    body = {}
                if slow:
                    # Заголовки уже ушли (_send ниже — нет): для медленного
                    # сценария заголовки шлём сразу, тело — после паузы,
                    # как у думающей модели.
                    pass
                if body.get("stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    # Пауза до первого токена.
                    time.sleep(30.0 if slow else 0.10)
                    if slow:
                        # Клиент к этому моменту уже отменён; ответ
                        # честно заканчиваем закрытием, а не висящим
                        # keep-alive (первый вариант стенда держал
                        # соединение — и неотменённый читатель висел).
                        self.close_connection = True
                        return
                    chunks = 5
                    for i in range(chunks):
                        payload = {"choices": [{"index": 0,
                                                "delta": {"content": "кусок "},
                                                "finish_reason": None}]}
                        self.wfile.write(("data: " + json.dumps(payload) +
                                          "\n\n").encode())
                        self.wfile.flush()
                        time.sleep(state.random.uniform(0.01, 0.025))
                    payload = {"choices": [{"index": 0, "delta": {},
                                            "finish_reason": "stop"}],
                               "usage": {"prompt_tokens": 10,
                                         "completion_tokens": 5}}
                    self.wfile.write(("data: " + json.dumps(payload) +
                                      "\n\n").encode())
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                    return
                data = {"choices": [{"message": {"content": "ответ"}}],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
                self._send(200, json.dumps(data).encode(), "application/json")
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass  # клиент ушёл (отмена) — для стенда это норма
            finally:
                state.end()

    return Handler


class _LoadServer:
    def __init__(self, seed: int, *, slow: bool = False,
                 plan: _PoisonPlan | None = None) -> None:
        self.state = _LoadState(seed)
        self.httpd = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            _make_handler(self.state, slow=slow, plan=plan))
        self.httpd.daemon_threads = True
        self.base = ("http://127.0.0.1:" +
                     str(self.httpd.server_address[1]) + "/v1")
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def _fd_count() -> int:
    try:
        return len(os.listdir("/proc/self/fd"))
    except OSError:
        return -1


class SwarmLoadTest(unittest.TestCase):
    """load: 60 ячеек, пул из 3 адресов, 429/обрывы, прерывание ≤3 с."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.work = self.root / "work"
        self.work.mkdir()
        self.settings = Settings(home=self.root / "home", cwd=self.work)
        self.secrets = Secrets(self.root / "home" / "secrets.json")

    def _runner(self, router) -> RouterCellRunner:
        return RouterCellRunner(router=router, settings=self.settings,
                                secrets=self.secrets, mother_tools={},
                                guard=None, project_dir=self.work)

    def _profile(self, urls) -> CellProfile:
        return CellProfile.from_dict({"caste": "cls", "prompt": "разметь",
                                      "tools": [], "model": "load-model",
                                      "pool": urls, "steps": 1, "ttl": 60})

    def test_load_60_cells_pool_of_3(self):
        plan = _PoisonPlan()
        servers = [_LoadServer(2100 + i, plan=plan) for i in range(3)]
        for server in servers:
            self.addCleanup(server.close)
        urls = [s.base for s in servers]
        router = Router(self.settings, self.secrets)
        baseline_threads = threading.active_count()
        baseline_fds = _fd_count()
        peak = {"threads": baseline_threads}
        stop_monitor = threading.Event()

        def monitor():
            while not stop_monitor.is_set():
                peak["threads"] = max(peak["threads"], threading.active_count())
                time.sleep(0.01)

        watcher = threading.Thread(target=monitor, daemon=True)
        watcher.start()
        try:
            import resource

            rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        except ImportError:
            rss_before = 0
        started = time.monotonic()
        run = SwarmRun(stream="leo", profiles=[self._profile(urls)],
                       inputs=["вход " + str(i) for i in range(60)],
                       output_dir=self.root / "out",
                       runner=self._runner(router), parallel=8,
                       budget_usd=0.0, max_attempts=3,
                       secrets=self.secrets)
        summary = run.run()
        seconds = time.monotonic() - started
        stop_monitor.set()
        watcher.join(timeout=2)
        try:
            import resource

            rss_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        except ImportError:
            rss_after = 0
        pool = run._pools["cls"]
        print("НАГРУЗКА: ячеек 60, время " + format(seconds, ".1f") +
              " с, пиковые потоки " + str(peak["threads"]) +
              ", запросы по адресам " +
              str([s.state.requests for s in servers]) +
              ", пик параллельности адресов " +
              str([s.state.max_active for s in servers]) +
              ", 429 у пула " + str(pool.rate_hits) +
              ", рост RSS " + str(max(0, rss_after - rss_before)) + " КБ")
        self.assertEqual(summary["done"], 60, summary)
        self.assertEqual(summary["failed"], 0, summary)
        self.assertLess(seconds, 30)
        # Пул видел 429 (план: два на вход №7), уполовинил параллельность
        # адреса и восстановил её успехами.
        self.assertGreaterEqual(pool.rate_hits, 2)
        for ep in pool.endpoints:
            self.assertGreaterEqual(ep.limit, 2)
        # Параллельность адреса не вылезла за потолок пула с запасом на гонки.
        for server in servers:
            self.assertLessEqual(server.state.max_active, 4)
            self.assertEqual(server.state.active, 0)
        # Утечек нет: потоки роя умерли, сокеты закрыты.
        deadline = time.time() + 3
        while time.time() < deadline:
            extra = [t for t in threading.enumerate()
                     if t.name.startswith("luna-cell-") and t.is_alive()]
            if not extra and threading.active_count() <= baseline_threads + 3:
                break
            time.sleep(0.05)
        self.assertFalse([t for t in threading.enumerate()
                          if t.name.startswith("luna-cell-") and t.is_alive()])
        self.assertLessEqual(threading.active_count(), baseline_threads + 3)
        if baseline_fds >= 0:
            self.assertLessEqual(_fd_count(), baseline_fds + 6)
        if rss_before:
            self.assertLess(rss_after - rss_before, 64 * 1024)

    def test_interrupt_kills_swarm_within_3s(self):
        servers = [_LoadServer(3100 + i, slow=True) for i in range(3)]
        for server in servers:
            self.addCleanup(server.close)
        urls = [s.base for s in servers]
        router = Router(self.settings, self.secrets)
        run = SwarmRun(stream="leo", profiles=[self._profile(urls)],
                       inputs=["вход " + str(i) for i in range(24)],
                       output_dir=self.root / "out",
                       runner=self._runner(router), parallel=8,
                       budget_usd=0.0, max_attempts=3,
                       secrets=self.secrets)
        box = {}
        thread = threading.Thread(
            target=lambda: box.update(run.run()), daemon=True)
        thread.start()
        # Дождаться, пока ячейки реально повисли в стримах.
        deadline = time.time() + 10
        while time.time() < deadline:
            if sum(s.state.active for s in servers) >= 4:
                break
            time.sleep(0.02)
        self.assertGreaterEqual(sum(s.state.active for s in servers), 1)
        stopped_at = time.monotonic()
        run.stop()
        thread.join(timeout=5)
        elapsed = time.monotonic() - stopped_at
        print("НАГРУЗКА: прерывание роя за " + format(elapsed, ".2f") + " с")
        self.assertFalse(thread.is_alive())
        self.assertLessEqual(elapsed, 3.0)
        self.assertEqual(box["done"] + box["failed"] + box["skipped"], 24)


if __name__ == "__main__":
    unittest.main()
