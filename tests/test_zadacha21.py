"""ЗАДАЧА-21: изоляция bash роя, ключи пула, luna pool key, luna -p.

Всё без внешней сети: пул — на локальном сервере-подделке, изоляция —
на подменённом окружении и (где есть) живом bwrap под skipUnless.
"""

from __future__ import annotations

import io
import json
import shutil
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from luna.core.errors import HumanError
from luna.core.secrets import Secrets
from luna.core.settings import Settings
from luna.guard.policy import Guard
from luna.headless import run_headless
from luna.router.core import ProviderConfig, Router
from luna.setup import pool_secret_name, run_pool_key
from luna.starfall import sandbox
from luna.starfall.engine import RouterCellRunner, SwarmRun
from luna.starfall.modelpool import ModelPool
from luna.starfall.profiles import CellProfile
from luna.tools.registry import ToolContext
from luna.tools.shell import _bash


def fake_router(settings, secrets) -> Router:
    providers = {"fake": ProviderConfig("fake", "fake", "", None)}
    return Router(settings, secrets, providers=providers)


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.work = self.root / "work"
        self.work.mkdir()
        self.settings = Settings(home=self.root / "home", cwd=self.work)
        self.secrets = Secrets(self.root / "home" / "secrets.json")


# --- изоляция: выбор бэкенда на подменённом окружении -------------------------


class SandboxChoiceTest(Case):
    def _bindir(self, *names: str) -> Path:
        bindir = self.root / "bin"
        bindir.mkdir(exist_ok=True)
        for name in names:
            path = bindir / name
            path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            path.chmod(0o755)
        return bindir

    def test_auto_takes_first_available(self):
        bindir = self._bindir("bwrap", "podman")
        found = {item["name"]: item for item in sandbox.detect_backends(
            platform_name="linux", environ={"PATH": str(bindir)})}
        self.assertTrue(found["bwrap"]["available"])
        status = sandbox.sandbox_status(
            self.settings, platform_name="linux",
            environ={"PATH": str(bindir)})
        self.assertEqual(status.backend, "bwrap")

    def test_no_backend_means_none_not_crash(self):
        bindir = self._bindir()
        status = sandbox.sandbox_status(
            self.settings, platform_name="linux",
            environ={"PATH": str(bindir)})
        self.assertEqual(status.backend, "none")
        self.assertFalse(status.verified)

    def test_explicit_missing_backend_is_unavailable(self):
        self.settings.set("starfall.sandbox", "docker")
        bindir = self._bindir()
        status = sandbox.sandbox_status(
            self.settings, platform_name="linux",
            environ={"PATH": str(bindir)})
        self.assertEqual(status.backend, "unavailable")

    def test_windows_and_macos_have_no_fs_isolation(self):
        for plat in ("win32", "darwin"):
            found = sandbox.detect_backends(platform_name=plat,
                                            environ={"PATH": "/usr/bin"})
            self.assertFalse(any(item["available"] for item in found), plat)
            status = sandbox.sandbox_status(self.settings, platform_name=plat)
            self.assertFalse(status.fs_isolated, plat)

    def test_wrap_bwrap_no_net_by_default_net_by_profile(self):
        spec = sandbox.SandboxSpec(backend="bwrap", executable="/usr/bin/bwrap",
                                   workdir=self.work, net=False)
        argv, _env = spec.wrap("echo hi")
        self.assertIn("--unshare-net", argv)
        self.assertIn(str(self.work), argv)
        spec_net = sandbox.SandboxSpec(backend="bwrap",
                                       executable="/usr/bin/bwrap",
                                       workdir=self.work, net=True)
        argv_net, _ = spec_net.wrap("echo hi")
        self.assertNotIn("--unshare-net", argv_net)

    def test_wrap_container_mounts_and_limits(self):
        spec = sandbox.SandboxSpec(backend="podman", executable="/usr/bin/podman",
                                   image="img:latest", workdir=self.work,
                                   memory_mb=256, max_processes=32)
        argv, _env = spec.wrap("echo hi")
        joined = " ".join(argv)
        self.assertIn("--network none", joined)
        self.assertIn("dst=/cell", joined)
        self.assertIn("256m", argv)
        self.assertIn("32", argv)


class SandboxGuardTest(Case):
    def _decide(self, verified: bool, **args) -> str:
        fake = lambda settings=None: (verified, "подмена теста")
        with mock.patch.object(sandbox, "isolation_verified", fake):
            guard = Guard(self.settings)
            return guard.decide("starfall", True, args, mode="auto").decision

    def test_bash_asks_without_isolation(self):
        self.assertEqual(self._decide(False, action="run", profile="cod"), "ask")

    def test_bash_allowed_with_verified_isolation(self):
        self.assertEqual(self._decide(True, action="run", profile="cod"), "allow")
        self.assertEqual(self._decide(True, action="estimate", profile="tst"),
                         "allow")

    def test_project_with_bash_always_asks(self):
        self.assertEqual(self._decide(True, action="run", profile="cod",
                                      write_mode="project"), "ask")
        self.assertEqual(self._decide(True, action="run", profile="tst",
                                      project_confirmed=True), "ask")


@unittest.skipUnless(shutil.which("bwrap"), "bwrap не установлен")
class BwrapLiveTest(Case):
    """Живой bwrap: запись только в папку ячейки, сети без net нет."""

    def _ctx(self, net: bool) -> ToolContext:
        spec = sandbox.resolve_spec(self.settings, self.work, net=net)
        self.assertTrue(spec.verified, spec.reason)
        return ToolContext(cwd=self.work, settings=self.settings, sandbox=spec)

    def test_write_inside_ok_outside_denied(self):
        ctx = self._ctx(net=False)
        inside = _bash(ctx, {"command": ": > inside.txt", "timeout": 20})
        self.assertTrue(inside.ok, inside.text)
        self.assertTrue((self.work / "inside.txt").exists())
        outside = self.root / "outside.txt"
        res = _bash(ctx, {"command": ": > " + str(outside), "timeout": 20})
        self.assertFalse(outside.exists())

    def test_network_off_without_net_on_with_net(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        self.addCleanup(listener.close)
        port = listener.getsockname()[1]
        code = ("import socket; "
                "socket.create_connection(('127.0.0.1', " + str(port) + "), 2)")
        import sys

        cmd = sys.executable + " -c \"" + code + "\""
        off = _bash(self._ctx(net=False), {"command": cmd, "timeout": 20})
        self.assertFalse(off.ok, off.text)
        listener.settimeout(5)
        box = {}

        def accept():
            try:
                conn, _addr = listener.accept()
                box["conn"] = conn
            except OSError:
                pass

        thread = threading.Thread(target=accept, daemon=True)
        thread.start()
        on = _bash(self._ctx(net=True), {"command": cmd, "timeout": 20})
        self.assertTrue(on.ok, on.text)
        thread.join(timeout=5)
        if "conn" in box:
            box["conn"].close()


class DoctorIsolationTest(Case):
    def test_doctor_shows_isolation_row(self):
        import contextlib

        from luna.diagnostics.doctor import run_doctor

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            run_doctor()
        self.assertIn("Изоляция роя", buf.getvalue())


# --- ключи пула -----------------------------------------------------------------


class PoolKeyCommandTest(Case):
    def test_key_goes_to_secrets_only(self):
        outputs = []
        value = "sk-pool-secret-987654"
        code = run_pool_key("node-1", secrets=self.secrets,
                            getpass_fn=lambda prompt="": value,
                            out=outputs.append)
        self.assertEqual(code, 0)
        self.assertEqual(self.secrets.get("pool:node-1"), value)
        self.assertNotIn(value, "\n".join(outputs))
        self.assertIn("pool:node-1", "\n".join(outputs))

    def test_bad_name_and_empty_key_rejected(self):
        with self.assertRaises(HumanError):
            pool_secret_name("плохое имя")
        with self.assertRaises(HumanError):
            run_pool_key("node-1", secrets=self.secrets,
                         getpass_fn=lambda prompt="": "  ",
                         out=lambda text: None)
        self.assertIsNone(self.secrets.get("pool:node-1"))


class _PoolServer:
    """Локальный узел пула: смотрит заголовки, умеет 401 и эхо ключа."""

    def __init__(self, *, good_key: str = "", echo_key: bool = False,
                 anthropic: bool = False):
        self.good_key = good_key
        self.echo_key = echo_key
        self.anthropic = anthropic
        self.headers_seen: list = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code: int, body: bytes, ctype: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) or b"{}"
                try:
                    body = json.loads(raw)
                except ValueError:
                    body = {}
                outer.headers_seen.append(
                    {k.lower(): v for k, v in self.headers.items()})
                key = (self.headers.get("x-api-key") if outer.anthropic
                       else (self.headers.get("Authorization") or "")
                       .replace("Bearer ", ""))
                if outer.good_key and key != outer.good_key:
                    self._send(401, json.dumps(
                        {"error": {"message": "ключ не принят"}}).encode(),
                        "application/json")
                    return
                text = "ответ узла"
                if outer.echo_key and key:
                    text = "эхо ключа: " + key  # нечестный узел: ключ в ответе
                if outer.anthropic:
                    if body.get("stream"):
                        self._send(400, b"{}", "application/json")
                        return
                    data = {"content": [{"type": "text", "text": text}],
                            "usage": {"input_tokens": 10, "output_tokens": 5}}
                    self._send(200, json.dumps(data).encode(), "application/json")
                    return
                if body.get("stream"):
                    chunks = []
                    payload = {"choices": [{"index": 0, "delta": {"content": text},
                                            "finish_reason": None}]}
                    chunks.append("data: " + json.dumps(payload) + "\n\n")
                    payload = {"choices": [{"index": 0, "delta": {},
                                            "finish_reason": "stop"}],
                               "usage": {"prompt_tokens": 10,
                                         "completion_tokens": 5}}
                    chunks.append("data: " + json.dumps(payload) + "\n\n")
                    chunks.append("data: [DONE]\n\n")
                    self._send(200, "".join(chunks).encode(),
                               "text/event-stream")
                    return
                data = {"choices": [{"message": {"content": text}}],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
                self._send(200, json.dumps(data).encode(), "application/json")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = "http://127.0.0.1:" + str(self.httpd.server_address[1]) + "/v1"
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class PoolKeyFlowTest(Case):
    def setUp(self) -> None:
        super().setUp()
        self.settings.set("model.provider", "fake")
        self.settings.set("model.kind", "fake")
        self.settings.set("model.main", "fake:echo")
        self.key = "sk-pool-secret-123456"
        self.secrets.set("pool:node-1", self.key)

    def _runner(self) -> RouterCellRunner:
        from luna.mind.loop import Agent

        agent = Agent(settings=self.settings, secrets=self.secrets,
                      router=Router(self.settings, self.secrets),
                      cwd=self.work, mode="auto")
        self.events: list = []
        agent.listen(self.events.append)
        self.agent = agent
        return RouterCellRunner(router=agent.router, settings=self.settings,
                                secrets=self.secrets, mother_tools={},
                                guard=agent.guard, project_dir=self.work)

    def _profile(self, pool) -> CellProfile:
        return CellProfile.from_dict({"caste": "cls", "prompt": "x",
                                      "tools": [], "model": "some-model",
                                      "pool": pool, "steps": 1})

    def test_bearer_header_from_secret(self):
        server = _PoolServer(good_key=self.key)
        self.addCleanup(server.close)
        run = SwarmRun(stream="leo", profiles=[self._profile(
            [{"url": server.base, "secret": "pool:node-1"}])],
            inputs=["а", "б"], output_dir=self.root / "out",
            runner=self._runner(), parallel=2, budget_usd=0.0,
            secrets=self.secrets, publish=self.agent.publish)
        summary = run.run()
        self.assertEqual(summary["done"], 2)
        auths = [h.get("authorization") for h in server.headers_seen]
        self.assertIn("Bearer " + self.key, auths)
        # Ключ не утёк ни в события, ни в результаты на диске.
        self.assertNotIn(self.key, json.dumps(self.events, ensure_ascii=False))
        result_text = (run.dir / "results" / "0.json").read_text(encoding="utf-8")
        self.assertNotIn(self.key, result_text)

    def test_anthropic_scheme_from_kind(self):
        server = _PoolServer(good_key=self.key, anthropic=True)
        self.addCleanup(server.close)
        prof = self._profile([{"url": server.base, "secret": "pool:node-1",
                               "kind": "anthropic"}])
        run = SwarmRun(stream="leo", profiles=[prof], inputs=["а"],
                       output_dir=self.root / "out", runner=self._runner(),
                       parallel=1, budget_usd=0.0, secrets=self.secrets)
        summary = run.run()
        self.assertEqual(summary["done"], 1)
        keys = [h.get("x-api-key") for h in server.headers_seen]
        self.assertIn(self.key, keys)

    def test_401_disables_endpoint_and_swarm_uses_other(self):
        bad = _PoolServer(good_key="другой-ключ")
        good = _PoolServer()
        self.addCleanup(bad.close)
        self.addCleanup(good.close)
        prof = self._profile([{"url": bad.base, "secret": "pool:node-1"},
                              {"url": good.base}])
        run = SwarmRun(stream="leo", profiles=[prof],
                       inputs=["а", "б", "в", "г"],
                       output_dir=self.root / "out", runner=self._runner(),
                       parallel=2, budget_usd=0.0, secrets=self.secrets)
        summary = run.run()
        self.assertEqual(summary["done"], 4)
        pool = run._pools["cls"]
        self.assertTrue(pool.all_disabled() is False)
        reasons = pool.disabled_reasons()
        self.assertTrue(any(bad.base in r for r in reasons), reasons)
        self.assertGreaterEqual(pool.auth_hits, 1)

    def test_all_endpoints_401_fail_with_reason(self):
        bad = _PoolServer(good_key="другой-ключ")
        self.addCleanup(bad.close)
        prof = self._profile([{"url": bad.base, "secret": "pool:node-1"}])
        run = SwarmRun(stream="leo", profiles=[prof], inputs=["а"],
                       output_dir=self.root / "out", runner=self._runner(),
                       parallel=1, budget_usd=0.0, secrets=self.secrets)
        summary = run.run()
        self.assertEqual(summary["failed"], 1)
        result = json.loads((run.dir / "results" / "0.json")
                            .read_text(encoding="utf-8"))
        self.assertIn("выведен", result["error"])
        self.assertNotIn(self.key, result["error"])

    def test_leaky_node_key_scrubbed_from_results(self):
        server = _PoolServer(echo_key=True)
        self.addCleanup(server.close)
        prof = self._profile([{"url": server.base, "secret": "pool:node-1"}])
        run = SwarmRun(stream="leo", profiles=[prof], inputs=["а"],
                       output_dir=self.root / "out", runner=self._runner(),
                       parallel=1, budget_usd=0.0, secrets=self.secrets,
                       publish=self.agent.publish)
        summary = run.run()
        self.assertEqual(summary["done"], 1)
        result_text = (run.dir / "results" / "0.json").read_text(encoding="utf-8")
        self.assertNotIn(self.key, result_text)
        self.assertNotIn(self.key, json.dumps(self.events, ensure_ascii=False))
        self.assertNotIn(self.key, summary["summary_text"])

    def test_pool_unit_auth_disable(self):
        from luna.net.http_client import HttpError

        pool = ModelPool(["http://a", "http://b"], base_parallel=2)
        ep = pool.acquire()
        pool.disable(ep, "HTTP 403")
        self.assertTrue(ep.disabled)
        nxt = pool.acquire()
        self.assertIsNot(nxt, ep)
        from luna.starfall.modelpool import is_auth_error

        self.assertTrue(is_auth_error(HttpError(401, "нет")))
        self.assertTrue(is_auth_error(HttpError(403, "нет")))
        self.assertFalse(is_auth_error(HttpError(429, "нет")))


# --- luna -p ---------------------------------------------------------------------


class Headless21Test(Case):
    def setUp(self) -> None:
        super().setUp()
        from luna.devtools.fake_llm import set_script

        self.addCleanup(lambda: set_script([]))
        self.settings.set("model.provider", "fake")
        self.settings.set("model.kind", "fake")
        self.settings.set("model.main", "fake:echo")
        self.router = fake_router(self.settings, self.secrets)

    def _run(self, prompt: str = "привет", **kw):
        out, err = io.StringIO(), io.StringIO()
        kw.setdefault("settings", self.settings)
        kw.setdefault("secrets", self.secrets)
        kw.setdefault("router", self.router)
        kw.setdefault("cwd", self.work)
        code = run_headless(prompt, out=out, err=err, **kw)
        return code, out.getvalue(), err.getvalue()

    def test_answer_stdout_progress_stderr(self):
        code, out, err = self._run()
        self.assertEqual(code, 0)
        self.assertIn("Эхо: привет", out)
        self.assertIn("ход завершён: end", err)
        self.assertNotIn("тул", out)

    def test_prompt_from_stdin(self):
        code, out, _err = self._run("", stdin=io.StringIO("из stdin"))
        self.assertEqual(code, 0)
        self.assertIn("Эхо: из stdin", out)

    def test_tool_turn_and_guard_deny_without_human(self):
        from luna.devtools.fake_llm import set_script

        marker = self.work / "marker.txt"
        set_script([
            {"tool_calls": [{"name": "bash",
                             "arguments": {"command": ": > marker.txt"},
                             "intent": "создать файл"}]},
            {"text": "модель сдала ход"},
        ])
        self.settings.set("model.main", "fake:script")
        code, out, err = self._run("сделай файл")
        self.assertEqual(code, 0)
        self.assertIn("модель сдала ход", out)
        self.assertFalse(marker.exists())  # bash не выполнился
        self.assertIn("Предохранитель", err)
        self.assertIn("headless", err)  # понятная причина отказа — в stderr

    def test_allow_pattern_lets_tool_run(self):
        from luna.devtools.fake_llm import set_script

        marker = self.work / "marker.txt"
        set_script([
            {"tool_calls": [{"name": "bash",
                             "arguments": {"command": ": > marker.txt"},
                             "intent": "создать файл"}]},
            {"text": "готово"},
        ])
        self.settings.set("model.main", "fake:script")
        code, out, err = self._run("сделай файл", allow=["bash"])
        self.assertEqual(code, 0)
        self.assertTrue(marker.exists())
        self.assertIn("разрешено явно шаблоном", err)

    def test_allow_from_settings(self):
        from luna.devtools.fake_llm import set_script

        marker = self.work / "marker2.txt"
        self.settings.set("headless.allow", "bash")
        set_script([
            {"tool_calls": [{"name": "bash",
                             "arguments": {"command": ": > marker2.txt"},
                             "intent": "создать файл"}]},
            {"text": "готово"},
        ])
        self.settings.set("model.main", "fake:script")
        code, _out, _err = self._run("сделай файл")
        self.assertEqual(code, 0)
        self.assertTrue(marker.exists())

    def test_hard_block_not_allowed_by_anything(self):
        from luna.devtools.fake_llm import set_script

        victim = self.work / "victim"
        victim.mkdir()
        (victim / "keep.txt").write_text("живи", encoding="utf-8")
        set_script([
            {"tool_calls": [{"name": "bash",
                             "arguments": {"command": "rm -rf victim"},
                             "intent": "удалить всё"}]},
            {"text": "не вышло"},
        ])
        self.settings.set("model.main", "fake:script")
        code, _out, err = self._run("удали", allow=["bash", "*"])
        self.assertEqual(code, 0)
        self.assertTrue((victim / "keep.txt").exists())
        self.assertIn("hard_block", err)

    def test_stream_json_events(self):
        code, out, _err = self._run(output="stream-json")
        self.assertEqual(code, 0)
        events = [json.loads(line) for line in out.splitlines() if line.strip()]
        types = [e["type"] for e in events]
        self.assertEqual(types[0], "session.start")
        self.assertIn("agent.text", types)
        self.assertEqual(types[-1], "turn.done")

    def test_session_continuation_and_resume(self):
        from luna.core.sessions import list_sessions
        from luna.core.paths import DataDir
        from luna.devtools.fake_llm import seen_requests

        code, _out, _err = self._run("первый вопрос")
        self.assertEqual(code, 0)
        seen_requests().clear()
        code, out, _err = self._run("второй вопрос", continue_last=True)
        self.assertEqual(code, 0)
        self.assertIn("Эхо: второй вопрос", out)
        messages = seen_requests()[-1]["messages"]
        texts = [m.get("content") for m in messages
                 if isinstance(m.get("content"), str)]
        self.assertIn("первый вопрос", texts)
        self.assertIn("Эхо: первый вопрос", texts)
        sessions = list_sessions(DataDir(self.root / "home"))
        self.assertEqual(len(sessions), 1)
        sid = sessions[0]["id"]
        seen_requests().clear()
        code, _out, _err = self._run("третий", resume_id=sid[:6])
        self.assertEqual(code, 0)
        texts = [m.get("content") for m in seen_requests()[-1]["messages"]
                 if isinstance(m.get("content"), str)]
        self.assertIn("второй вопрос", texts)

    def test_resume_unknown_id_is_error(self):
        code, _out, err = self._run("привет", resume_id="неттакой")
        self.assertEqual(code, 2)
        self.assertIn("не нашёл", err)

    def test_max_usd_stops_turn(self):
        self.settings.set("model.price_in", 1000000.0)  # 10 токенов = $10
        code, _out, err = self._run(max_usd=0.01)
        self.assertEqual(code, 2)
        self.assertIn("max-usd", err)

    def test_max_turns_limits_steps(self):
        from luna.devtools.fake_llm import set_script

        (self.work / "a.txt").write_text("а", encoding="utf-8")
        (self.work / "b.txt").write_text("б", encoding="utf-8")
        set_script([
            {"tool_calls": [{"name": "read", "arguments": {"path": "a.txt"},
                             "intent": "читаю а"}]},
            {"tool_calls": [{"name": "read", "arguments": {"path": "b.txt"},
                             "intent": "читаю б"}]},
            {"text": "всё прочитал"},
        ])
        self.settings.set("model.main", "fake:script")
        code, out, _err = self._run("прочитай оба", max_turns=1)
        self.assertEqual(code, 2)
        self.assertNotIn("всё прочитал", out)


if __name__ == "__main__":
    unittest.main()
