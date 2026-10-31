"""MCP-клиент (ЗАДАЧА-09): конфиги, фальшивый сервер, перезапуск, HTTP, реестр, CLI.

Сеть — только фальшивая: stdio-подпроцесс luna.devtools.fake_mcp_server и
HTTP-сервер на 127.0.0.1 в потоке. Никаких настоящих MCP-серверов и ключей.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
FAKE_CMD = [sys.executable, "-m", "luna.devtools.fake_mcp_server"]


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def fake_entry(**extra) -> dict:
    entry = {"command": FAKE_CMD[0], "args": FAKE_CMD[1:],
             "env": {"PYTHONPATH": str(ROOT)}}
    entry.update(extra)
    return entry


class ConfigParsingTest(unittest.TestCase):
    def test_claude_format_and_precedence(self):
        from luna.mcp.manager import load_configs

        with tempfile.TemporaryDirectory() as tmp:
            home, proj = Path(tmp) / "home", Path(tmp) / "proj"
            proj.mkdir(parents=True)
            write_json(home / "mcp.json", {"mcpServers": {
                "общий": {"command": "srv-a"},
                "выкл": {"command": "srv-b", "enabled": False},
            }})
            write_json(proj / ".mcp.json", {"mcpServers": {
                "общий": {"command": "srv-c", "args": ["--x"]},
                "веб": {"type": "http", "url": "https://example.test/mcp", "timeout": 45000},
                "старый": {"type": "sse", "url": "https://example.test/sse"},
            }})
            write_json(proj / ".luna" / "mcp.json", {"mcpServers": {
                "общий": {"command": "srv-d"},
            }})
            servers, problems = load_configs(proj, home)
            self.assertEqual(problems, [])
            # .luna/mcp.json сильнее .mcp.json и глобального файла
            self.assertEqual(servers["общий"].command, ["srv-d"])
            self.assertEqual(servers["общий"].source, ".luna/mcp.json")
            self.assertFalse(servers["выкл"].enabled)
            self.assertEqual(servers["веб"].transport, "http")
            self.assertEqual(servers["веб"].timeout, 45.0)  # миллисекунды совместимого формата
            self.assertEqual(servers["старый"].transport, "unsupported")
            self.assertIn("sse", servers["старый"].note)

    def test_opencode_format(self):
        from luna.mcp.manager import load_configs

        with tempfile.TemporaryDirectory() as tmp:
            home, proj = Path(tmp) / "home", Path(tmp) / "proj"
            proj.mkdir(parents=True)
            write_json(proj / "opencode.json", {"mcp": {
                "локальный": {"type": "local", "command": ["python3", "srv.py"],
                              "environment": {"A": "1"}, "timeout": 5000},
                "далёкий": {"type": "remote", "url": "https://example.test/mcp",
                            "headers": {"X-Key": "{{secret:api}}"}, "enabled": False},
            }})
            servers, problems = load_configs(proj, home)
            self.assertEqual(problems, [])
            local = servers["локальный"]
            self.assertEqual(local.transport, "stdio")
            self.assertEqual(local.command, ["python3", "srv.py"])
            self.assertEqual(local.env, {"A": "1"})
            self.assertEqual(local.timeout, 5.0)
            remote = servers["далёкий"]
            self.assertEqual(remote.transport, "http")
            self.assertFalse(remote.enabled)
            self.assertEqual(remote.headers, {"X-Key": "{{secret:api}}"})

    def test_secret_refs(self):
        from luna.core.errors import HumanError
        from luna.core.secrets import Secrets
        from luna.mcp.manager import resolve_value

        with tempfile.TemporaryDirectory() as tmp:
            secrets = Secrets(Path(tmp) / "secrets.json")
            secrets.set("api", "значение-секрета")
            env = {"HOME_DIR": "/home/test"}
            self.assertEqual(resolve_value("Bearer {{secret:api}}", secrets, env),
                             "Bearer значение-секрета")
            self.assertEqual(resolve_value("${HOME_DIR}/x", secrets, env), "/home/test/x")
            self.assertEqual(resolve_value("просто текст", secrets, env), "просто текст")
            with self.assertRaises(HumanError):
                resolve_value("{{secret:нет-такого}}", secrets, env)


class ManagerStdioTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.home, self.proj = tmp / "home", tmp / "proj"
        self.proj.mkdir(parents=True)
        self.manager = None

    def tearDown(self):
        if self.manager is not None:
            self.manager.close()
        self._tmp.cleanup()

    def make_manager(self, entry=None, *, via=".luna"):
        from luna.mcp.manager import Manager

        payload = {"mcpServers": {"fake": entry if entry is not None else fake_entry()}}
        if via == ".luna":
            write_json(self.proj / ".luna" / "mcp.json", payload)
        else:
            write_json(self.proj / ".mcp.json", payload)
        self.manager = Manager(cwd=self.proj, home=self.home)
        return self.manager

    def test_full_cycle_via_dot_mcp_json(self):
        manager = self.make_manager(via=".mcp")
        names = [d["name"] for d in manager.list_tools("fake")]
        for expected in ("echo", "add", "picture", "fail", "crash"):
            self.assertIn(expected, names)
        text, images, is_error = manager.call("fake", "echo", {"text": "привет"})
        self.assertEqual((text, images, is_error), ("Эхо: привет", [], False))
        text, _images, _err = manager.call("fake", "add", {"a": 2, "b": 3})
        self.assertEqual(text, "5.0")

    def test_picture_and_error_result(self):
        manager = self.make_manager()
        text, images, is_error = manager.call("fake", "picture", {})
        self.assertFalse(is_error)
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0]["type"], "image")
        self.assertEqual(images[0]["media_type"], "image/png")
        self.assertTrue(images[0]["data"])
        self.assertIn("картинка", text)
        _text, _images, is_error = manager.call("fake", "fail", {})
        self.assertTrue(is_error)

    def test_env_from_secret_ref(self):
        from luna.core.secrets import Secrets

        secrets = Secrets(self.home / "secrets.json")
        secrets.set("метка", "из-сейфа")
        entry = fake_entry(env={"PYTHONPATH": str(ROOT), "МЕТКА": "{{secret:метка}}"})
        manager = self.make_manager(entry)
        # менеджер по умолчанию читает тот же сейф из home
        text, _i, _e = manager.call("fake", "env_echo", {"name": "МЕТКА"})
        self.assertEqual(text, "из-сейфа")

    def test_crash_then_restart_on_next_call(self):
        from luna.core.errors import HumanError

        manager = self.make_manager()
        manager.list_tools("fake")
        with self.assertRaises(HumanError):
            manager.call("fake", "crash", {})
        text, _i, _e = manager.call("fake", "echo", {"text": "снова жив"})
        self.assertEqual(text, "Эхо: снова жив")

    def test_idle_death_restarts(self):
        manager = self.make_manager()
        manager.list_tools("fake")
        proc = manager._clients["fake"].transport.process
        proc.terminate()
        proc.wait(timeout=5)
        text, _i, _e = manager.call("fake", "echo", {"text": "после смерти"})
        self.assertEqual(text, "Эхо: после смерти")

    def test_timeout_then_recovers(self):
        from luna.core.errors import HumanError

        manager = self.make_manager(fake_entry(timeout=0.5))
        manager.list_tools("fake")
        with self.assertRaises(HumanError) as ctx:
            manager.call("fake", "slow", {"seconds": 5})
        self.assertEqual(ctx.exception.code, "mcp_timeout")
        text, _i, _e = manager.call("fake", "echo", {"text": "успел"})
        self.assertEqual(text, "Эхо: успел")

    def test_disabled_server_refuses(self):
        from luna.core.errors import HumanError

        manager = self.make_manager(fake_entry(enabled=False))
        with self.assertRaises(HumanError):
            manager.connect("fake")


def _http_handler(sse: bool):
    from luna.devtools import fake_mcp_server

    class Handler(BaseHTTPRequestHandler):
        seen_sessions: list = []

        def log_message(self, *args):  # тишина в тестах
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            Handler.seen_sessions.append(self.headers.get("Mcp-Session-Id"))
            reply = fake_mcp_server.handle(json.loads(raw.decode("utf-8")))
            if reply is None:
                body = b""
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            payload = json.dumps(reply, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            if "id" in (json.loads(raw.decode("utf-8")) or {}) and \
                    (json.loads(raw.decode("utf-8")) or {}).get("method") == "initialize":
                self.send_header("Mcp-Session-Id", "session-1")
            if sse:
                self.send_header("Content-Type", "text/event-stream")
                body = b"data: " + payload + b"\n\n"
            else:
                self.send_header("Content-Type", "application/json")
                body = payload
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


class ManagerHttpTest(unittest.TestCase):
    def run_server(self, sse: bool):
        handler = _http_handler(sse)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        return server, handler

    def make_manager(self, url: str):
        from luna.mcp.manager import Manager

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home, proj = Path(tmp.name) / "home", Path(tmp.name) / "proj"
        proj.mkdir(parents=True)
        write_json(proj / ".luna" / "mcp.json",
                   {"mcpServers": {"веб": {"type": "http", "url": url}}})
        manager = Manager(cwd=proj, home=home)
        self.addCleanup(manager.close)
        return manager

    def test_http_json_cycle_and_session(self):
        server, handler = self.run_server(sse=False)
        url = "http://127.0.0.1:" + str(server.server_address[1]) + "/mcp"
        manager = self.make_manager(url)
        names = [d["name"] for d in manager.list_tools("веб")]
        self.assertIn("echo", names)
        text, _i, _e = manager.call("веб", "echo", {"text": "по http"})
        self.assertEqual(text, "Эхо: по http")
        # сессия из initialize ушла в следующие запросы
        self.assertIn("session-1", handler.seen_sessions)

    def test_http_sse_cycle(self):
        server, _handler = self.run_server(sse=True)
        url = "http://127.0.0.1:" + str(server.server_address[1]) + "/mcp"
        manager = self.make_manager(url)
        text, _i, _e = manager.call("веб", "echo", {"text": "по sse"})
        self.assertEqual(text, "Эхо: по sse")


class RegistryAndAgentTest(unittest.TestCase):
    def test_no_config_no_cost(self):
        from luna.tools.registry import build_registry

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"LUNA_HOME": str(Path(tmp) / "home")}):
                with mock.patch.object(Path, "cwd", return_value=Path(tmp)):
                    registry = build_registry()
                    self.assertFalse(registry._mcp_loaded)  # догрузки ещё не было
                    names = [tool.name for tool in registry.values()]
                    self.assertIn("bash", names)
                    self.assertFalse(any(name.startswith("mcp__") for name in names))
                    self.assertIsNone(registry.mcp_manager)
                    self.assertEqual(registry.mcp_errors, [])

    def test_lazy_discovery_registers_tools(self):
        from luna.tools.registry import build_registry

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            write_json(home / "mcp.json", {"mcpServers": {"fake": fake_entry()}})
            with mock.patch.dict(os.environ, {"LUNA_HOME": str(home)}):
                with mock.patch.object(Path, "cwd", return_value=Path(tmp)):
                    registry = build_registry()
                    self.assertFalse(registry._mcp_loaded)
                    tool = registry.get("mcp__fake__echo")
                    self.assertIsNotNone(tool)
                    self.assertTrue(tool.mutating)  # через Предохранитель как изменяющий
                    self.assertIsNotNone(registry.mcp_manager)
                    registry.mcp_manager.close()

    def test_guard_asks_for_mcp_by_default(self):
        from luna.guard.policy import Guard

        guard = Guard(None, None)
        self.assertEqual(guard.decide("mcp__fake__echo", True, {"text": "x"}, "", "normal").decision, "ask")
        self.assertEqual(guard.decide("mcp__fake__echo", True, {"text": "x"}, "", "plan").decision, "deny")

    def test_agent_turn_calls_mcp_tool(self):
        from luna.core.secrets import Secrets
        from luna.core.settings import Settings
        from luna.devtools.fake_llm import set_script
        from luna.mind.loop import Agent
        from luna.router.core import Router

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home, proj = root / "home", root / "proj"
            proj.mkdir(parents=True)
            write_json(home / "mcp.json", {"mcpServers": {"fake": fake_entry()}})
            settings = Settings(home=home, cwd=proj)
            settings.set("model.provider", "fake")
            settings.set("model.kind", "fake")
            settings.set("model.main", "fake:script")
            secrets = Secrets(home / "secrets.json")
            router = Router(settings, secrets)
            set_script([
                {"tool_calls": [{"name": "mcp__fake__echo",
                                 "arguments": {"text": "привет"},
                                 "intent": "проверить MCP-тул фальшивого сервера"}]},
                {"text": "Готово."},
            ])
            with mock.patch.dict(os.environ, {"LUNA_HOME": str(home)}):
                agent = Agent(settings=settings, secrets=secrets, router=router,
                              cwd=proj, mode="auto")
                events: list = []
                agent.listen(events.append)
                reason = agent.run_turn("Позови эхо")
            self.assertEqual(reason, "end")
            results = [e for e in events if e.get("type") == "tool.result"]
            self.assertTrue(any(e.get("ok") and "Эхо: привет" in e.get("output", "")
                                for e in results))
            tool_msgs = [m for m in agent._history if m.get("role") == "tool"]
            self.assertTrue(any("Эхо: привет" in str(m.get("content")) for m in tool_msgs))
            manager = agent.tools.mcp_manager
            if manager is not None:
                manager.close()


class CliTest(unittest.TestCase):
    def test_add_list_test_remove(self):
        from luna.core.errors import HumanError
        from luna.mcp.cli import run_mcp

        with tempfile.TemporaryDirectory() as tmp:
            home, proj = Path(tmp) / "home", Path(tmp) / "proj"
            proj.mkdir(parents=True)
            lines: list = []
            with mock.patch.dict(os.environ, {"PYTHONPATH": str(ROOT)}):
                rc = run_mcp(["add", "fake", "--", sys.executable, "-m",
                              "luna.devtools.fake_mcp_server"],
                             cwd=proj, home=home, out=lines.append)
                self.assertEqual(rc, 0)
                saved = json.loads((home / "mcp.json").read_text(encoding="utf-8"))
                self.assertEqual(saved["mcpServers"]["fake"]["command"], sys.executable)
                lines.clear()
                run_mcp(["list"], cwd=proj, home=home, out=lines.append)
                self.assertTrue(any("fake" in line and "stdio" in line for line in lines))
                lines.clear()
                run_mcp(["test", "fake"], cwd=proj, home=home, out=lines.append)
                self.assertTrue(any("echo" in line for line in lines))
                run_mcp(["remove", "fake"], cwd=proj, home=home, out=lines.append)
                saved = json.loads((home / "mcp.json").read_text(encoding="utf-8"))
                self.assertNotIn("fake", saved["mcpServers"])
                with self.assertRaises(HumanError):
                    run_mcp(["remove", "fake"], cwd=proj, home=home, out=lines.append)

    def test_add_url_and_bad_name(self):
        from luna.core.errors import HumanError
        from luna.mcp.cli import run_mcp

        with tempfile.TemporaryDirectory() as tmp:
            home, proj = Path(tmp) / "home", Path(tmp) / "proj"
            proj.mkdir(parents=True)
            lines: list = []
            run_mcp(["add", "web", "--url", "https://example.test/mcp"],
                    cwd=proj, home=home, out=lines.append)
            saved = json.loads((home / "mcp.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["mcpServers"]["web"]["type"], "http")
            with self.assertRaises(HumanError):
                run_mcp(["add", "Плохое Имя", "--", "true"], cwd=proj, home=home, out=lines.append)


class HeadersTest(unittest.TestCase):
    def test_parameter_headers(self):
        from luna.mcp.headers import parameter_headers

        schema = {"type": "object", "properties": {
            "q": {"type": "string", "x-mcp-header": "Query"},
            "n": {"type": "integer", "x-mcp-header": "Count"},
            "plain": {"type": "string"}}}
        self.assertEqual(parameter_headers(schema, {"q": "abc", "n": 3, "plain": "x"}),
                         {"Mcp-Param-Query": "abc", "Mcp-Param-Count": "3"})
        self.assertEqual(parameter_headers(schema, {}), {})

    def test_annotations_reject_nonstatic(self):
        from luna.core.errors import HumanError
        from luna.mcp.headers import annotations

        schema = {"type": "object", "properties": {
            "items": {"type": "array", "items": {"type": "string", "x-mcp-header": "X"}}}}
        with self.assertRaises(HumanError):
            annotations(schema)


if __name__ == "__main__":
    unittest.main()
