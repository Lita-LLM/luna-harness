"""Клетка работника: профили, сужение флагами, headless-коды выхода, CLI, trust.

Без сети и настоящих моделей: агент — фальшивый со сценарием событий,
LUNA_HOME — временная папка.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from luna.core.errors import HumanError
from luna.core.secrets import Secrets
from luna.core.settings import Settings
from luna.headless import run_headless
from luna.worker.cage import Cage, McpRule
from luna.worker.profile import (cage_from_flags, intersect_patterns, load_profile,
                                 narrow, parse_mcp_flag)

NEWS = {
    "version": 1, "description": "Новости одной фразой", "prompt_file": "news.prompt.md",
    "model": "fast", "mode": "normal", "tools": ["web_search", "web_fetch"],
    "deny": ["mcp__*__send_*"], "allow": ["mcp__tg__*"],
    "mcp": {"config": "news.mcp.json",
            "servers": {"tg": {"tools": ["search_messages", "get_*"],
                               "deny": ["send_*", "delete_*"], "required": True}}},
    "secrets": ["tg_session"], "fs": {"mode": "none", "roots": []}, "bash": "off",
    "net": {"fetch_hosts": ["*.ria.ru", "t.me"], "fetch_query": False},
    "instructions": "none", "limits": {"max_usd": 0.3, "max_steps": 20, "timeout_s": 180},
    "output": {"schema": {"type": "object", "required": ["summary"],
                          "properties": {"summary": {"type": "string"}}}, "retries": 1},
    "ephemeral": True, "fail_on_deny": False,
}


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.work = self.root / "work"
        (self.home / "workers").mkdir(parents=True)
        self.work.mkdir()
        env = mock.patch.dict(os.environ, {"LUNA_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)

    def write_profile(self, name: str, data: dict, *, raw: str | None = None) -> Path:
        folder = self.home / "workers"
        (folder / "news.prompt.md").write_text("Собери новости одной фразой.", encoding="utf-8")
        (folder / "news.mcp.json").write_text('{"servers": {}}', encoding="utf-8")
        path = folder / (name + ".json")
        path.write_text(raw if raw is not None else json.dumps(data, ensure_ascii=False),
                        encoding="utf-8")
        return path

    def load(self, data: dict, **kw) -> Cage:
        self.write_profile("w", data)
        return load_profile("w", home=self.home, cwd=self.work, **kw)

    def assertProfileError(self, data: dict, needle: str = "") -> None:
        with self.assertRaises(HumanError) as ctx:
            self.load(data)
        if needle:
            self.assertIn(needle, ctx.exception.title + " " + ctx.exception.hint)


# --- профиль -------------------------------------------------------------------------


class ProfileTest(Base):
    def test_news_profile_loads(self):
        cage = self.load(NEWS)
        self.assertEqual(cage.name, "w")
        self.assertEqual(cage.prompt, "Собери новости одной фразой.")
        self.assertEqual(cage.model, "fast")
        self.assertEqual(cage.tools, ["web_search", "web_fetch"])
        self.assertTrue(cage.mcp["tg"].required)
        self.assertEqual(cage.mcp["tg"].deny, ["send_*", "delete_*"])
        self.assertTrue(Path(cage.mcp_config).is_file())
        self.assertEqual(cage.fetch_hosts, ["*.ria.ru", "t.me"])
        self.assertEqual(cage.max_steps, 20)
        self.assertEqual(cage.timeout_s, 180.0)
        self.assertEqual(cage.schema_retries, 1)
        self.assertTrue(cage.tool_allowed("mcp__tg__get_dialogs"))
        self.assertFalse(cage.tool_allowed("mcp__tg__send_message"))
        self.assertFalse(cage.tool_allowed("bash"))

    def test_minimal_profile_is_strictest(self):
        cage = self.load({"version": 1})
        self.assertEqual(cage.tools, [])
        self.assertEqual(cage.fs_mode, "none")
        self.assertEqual(cage.fetch_hosts, [])
        self.assertTrue(cage.ephemeral)

    def test_unknown_keys_anywhere(self):
        self.assertProfileError(dict(NEWS, tool=["bash"]), "tool")
        self.assertProfileError(dict(NEWS, limits={"max_usd_": 1}), "limits.max_usd_")
        bad_server = json.loads(json.dumps(NEWS))
        bad_server["mcp"]["servers"]["tg"]["requird"] = True
        self.assertProfileError(bad_server, "requird")

    def test_duplicate_key_is_error(self):
        self.write_profile("w", {}, raw='{"version": 1, "tools": [], "tools": ["web_search"]}')
        with self.assertRaises(HumanError):
            load_profile("w", home=self.home, cwd=self.work)

    def test_version_and_types(self):
        self.assertProfileError(dict(NEWS, version=2), "version")
        self.assertProfileError({k: v for k, v in NEWS.items() if k != "version"}, "version")
        self.assertProfileError(dict(NEWS, limits={"max_steps": True}), "max_steps")
        self.assertProfileError(dict(NEWS, tools="web_search"), "tools")
        self.assertProfileError(dict(NEWS, mode="turbo"), "mode")

    def test_tool_rules(self):
        self.assertProfileError(dict(NEWS, tools=["crew"]), "запрещён")
        self.assertProfileError(dict(NEWS, tools=["read"]), "fs.mode: none")
        self.assertProfileError(dict(NEWS, tools=["write"], fs={"mode": "ro", "roots": ["."]}), "ro")
        self.assertProfileError(dict(NEWS, tools=["bash"]), "bash")
        self.assertProfileError(dict(NEWS, tools=["mcp__tg__x"]), "mcp.servers")

    def test_roots(self):
        cage = self.load(dict(NEWS, tools=["read"], fs={"mode": "ro", "roots": ["."]}))
        self.assertEqual(cage.fs_roots, [self.work.resolve()])
        self.assertProfileError(dict(NEWS, fs={"mode": "ro", "roots": ["нет-такой"]}), "корня нет")
        self.assertProfileError(dict(NEWS, fs={"mode": "ro", "roots": []}), "корней нет")
        self.assertProfileError(dict(NEWS, fs={"mode": "none", "roots": ["."]}), "none")

    def test_prompt_file_missing_and_both(self):
        self.assertProfileError(dict(NEWS, prompt_file="нет.md"), "prompt_file")
        self.assertProfileError(dict(NEWS, prompt="x"), "prompt")

    def test_missing_name_and_project_not_scanned(self):
        project_workers = self.work / ".luna" / "workers"
        project_workers.mkdir(parents=True)
        (project_workers / "evil.json").write_text('{"version": 1}', encoding="utf-8")
        with self.assertRaises(HumanError) as ctx:
            load_profile("evil", home=self.home, cwd=self.work)
        self.assertIn("workers", ctx.exception.title)
        # явный путь — сознательный выбор того, кто запускает
        cage = load_profile(str(project_workers / "evil.json"), home=self.home, cwd=self.work)
        self.assertEqual(cage.name, "evil")


class NarrowTest(Base):
    def setUp(self) -> None:
        super().setUp()
        self.cage = self.load(dict(NEWS, tools=["web_*", "read", "glob"],
                                   fs={"mode": "ro", "roots": ["."]}))

    def test_never_wider(self):
        sub = self.work / "sub"
        sub.mkdir()
        out = narrow(self.cage, tools=["web_search", "bash", "glob"], deny=["web_fetch"],
                     max_usd=1.0, max_steps=5, timeout_s=0, mode="auto",
                     fs_roots=["sub"], cwd=self.work)
        self.assertEqual(out.tools, ["web_search", "glob"])
        self.assertIn("web_fetch", out.deny)
        self.assertIn("mcp__*__send_*", out.deny)
        self.assertEqual(out.max_usd, 0.3)
        self.assertEqual(out.max_steps, 5)
        self.assertEqual(out.timeout_s, 180.0)
        self.assertEqual(out.mode, "normal")
        self.assertEqual(out.fs_roots, [sub.resolve()])
        self.assertEqual(self.cage.tools, ["web_*", "read", "glob"])  # исходник не тронут
        self.assertEqual(narrow(self.cage, mode="plan").mode, "plan")

    def test_widening_is_error(self):
        for kw in ({"fs_mode": "rw"}, {"bash": "sandbox"}, {"fetch_any": True},
                   {"fetch_query": True}, {"instructions": "all"},
                   {"mcp": {"other": None}}, {"fs_roots": [str(self.root)]}):
            with self.subTest(kw=kw), self.assertRaises(HumanError):
                narrow(self.cage, cwd=self.work, **kw)

    def test_mcp_hosts_schema(self):
        out = narrow(self.cage, mcp={"tg": ["get_dialogs", "send_message"]},
                     fetch_hosts=["news.ria.ru", "evil.com"], schema={"type": "array"})
        self.assertEqual(out.mcp["tg"].tools, ["get_dialogs"])
        self.assertEqual(out.fetch_hosts, ["news.ria.ru"])
        self.assertEqual(out.output_schema, NEWS["output"]["schema"])  # схема профиля главнее
        bare = Cage()
        self.assertEqual(narrow(bare, schema={"type": "object"}).output_schema, {"type": "object"})
        self.assertEqual(narrow(self.cage, fs_mode="none").tools, ["web_*"])

    def test_intersect_patterns(self):
        self.assertEqual(intersect_patterns(["*"], ["web_*"]), ["web_*"])
        self.assertEqual(intersect_patterns(["web_*"], ["*"]), ["web_*"])
        self.assertEqual(intersect_patterns(["a*"], ["a?"]), ["a?"])
        self.assertEqual(intersect_patterns(["web_*"], ["*_search"]), [])  # сомнение — уже
        self.assertEqual(parse_mcp_flag("tg:get_*, search"), ("tg", ["get_*", "search"]))
        self.assertEqual(parse_mcp_flag("tg"), ("tg", None))


class FlagsCageTest(Base):
    def test_strict_defaults(self):
        cage = cage_from_flags(cwd=self.work)
        self.assertEqual(cage.tools, [])
        self.assertEqual(cage.fetch_hosts, [])
        self.assertFalse(cage.tool_allowed("web_search"))
        cage = cage_from_flags(tools=["read"], fs="ro", mcp={"tg": ["get_*"]}, cwd=self.work)
        self.assertEqual(cage.fs_roots, [self.work.resolve()])
        self.assertTrue(cage.mcp["tg"].required)
        self.assertIsNone(cage_from_flags(fetch_any=True).fetch_hosts)
        with self.assertRaises(HumanError):
            cage_from_flags(tools=["luna"])
        with self.assertRaises(HumanError):
            cage_from_flags(tools=["edit"], fs="ro", cwd=self.work)


# --- headless с фальшивым агентом -----------------------------------------------------


class Tools(dict):
    def __init__(self, names, failed=()):
        super().__init__({n: object() for n in names})
        self.mcp_required_failed = list(failed)
        self.mcp_errors = ["tg: не поднялся"] if failed else []


class FakeAgent:
    """Сценарий: список шагов на ход; шаг — dict с text/tool/deny/ask/usage/wait/reason."""

    def __init__(self, turns, tools=("web_search",), failed=()):
        self.turns = list(turns)
        self.tools = Tools(tools, failed)
        self.guard = None
        self.ask_guard_fn = None
        self.prompts: list = []
        self.modes: list = []
        self.cleaned = 0
        self._listeners: list = []
        self._stop = threading.Event()

    def listen(self, fn):
        self._listeners.append(fn)

    def publish(self, event):
        for fn in self._listeners:
            fn(dict(event))

    def set_mode(self, mode):
        self.modes.append(mode)

    def interrupt(self):
        self._stop.set()

    def cleanup(self):
        self.cleaned += 1

    def run_turn(self, text):
        self.prompts.append(text)
        self._stop.clear()
        steps = self.turns.pop(0) if self.turns else [{"text": "пусто"}]
        for i, step in enumerate(steps):
            if step.get("raise"):
                raise step["raise"]
            if step.get("wait"):
                self._stop.wait(step["wait"])
            if self._stop.is_set():
                return "interrupted"
            if "usage" in step:
                self.publish({"type": "usage", "cost": step["usage"]})
            if self._stop.is_set():
                return "interrupted"
            if step.get("text"):
                self.publish({"type": "agent.text", "text": step["text"]})
            self.publish({"type": "agent.text.done"})
            for name in [step.get("tool"), step.get("deny"), step.get("ask")]:
                if not name:
                    continue
                cid = "c" + str(i) + name
                self.publish({"type": "tool.call", "id": cid, "name": name, "args": {}})
                decision = "allow"
                reason = "ok"
                if name == step.get("deny"):
                    decision, reason = "deny", "клетка: закрыто"
                elif name == step.get("ask"):
                    answer = self.ask_guard_fn({"type": "guard.ask", "tool": name, "args": {},
                                                "reason": "спросить"})
                    decision = "allow" if answer["decision"] == "yes" else "deny"
                    reason = answer["reason"]
                self.publish({"type": "guard.verdict", "id": cid, "decision": decision,
                              "by": "test", "reason": reason})
                if self._stop.is_set():
                    return "interrupted"
            if step.get("reason"):
                return step["reason"]
        return "end"


class HeadlessCageTest(Base):
    def setUp(self) -> None:
        super().setUp()
        self.settings = Settings(home=self.home, cwd=self.work)
        self.secrets = Secrets(self.home / "secrets.json")

    def run_cage(self, agent, cage=None, prompt="новости", **kw):
        out, err = io.StringIO(), io.StringIO()
        cage = cage if cage is not None else Cage(name="news", tools=["web_search"])
        code = run_headless(prompt, agent=agent, cage=cage, settings=self.settings,
                            secrets=self.secrets, out=out, err=err, cwd=self.work, **kw)
        return code, out.getvalue(), err.getvalue()

    def test_json_single_final_object(self):
        agent = FakeAgent([[{"text": "Сейчас поищу", "tool": "web_search"},
                            {"text": "Итог: всё тихо", "usage": 0.01}]])
        code, out, _err = self.run_cage(agent, output="json")
        self.assertEqual(code, 0)
        lines = [ln for ln in out.splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1)
        obj = json.loads(lines[0])
        self.assertEqual(obj["status"], "ok")
        self.assertEqual(obj["result"], "Итог: всё тихо")
        self.assertEqual(obj["tools_used"], ["web_search"])
        self.assertEqual(obj["steps"], 2)
        self.assertEqual(obj["worker"], "news")
        self.assertEqual(obj["cost_usd"], 0.01)
        self.assertIsNone(obj["error"])
        self.assertEqual(agent.cleaned, 1)

    def test_text_prints_only_final_answer_and_no_journal(self):
        agent = FakeAgent([[{"text": "Сейчас поищу", "tool": "web_search"}, {"text": "Итог"}]])
        code, out, _err = self.run_cage(agent)
        self.assertEqual(code, 0)
        self.assertEqual(out, "Итог\n")
        sessions = self.home / "sessions"
        self.assertFalse(sessions.exists() and any(sessions.iterdir()))

    def test_check_prints_cage_without_model(self):
        agent = FakeAgent([], tools=("web_search", "mcp__tg__get_dialogs"))
        code, out, _err = self.run_cage(agent, prompt="", check=True)
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(report["tools"], ["mcp__tg__get_dialogs", "web_search"])
        self.assertEqual(report["mcp_required_failed"], [])
        self.assertEqual(agent.prompts, [])

    def test_check_needs_cage(self):
        code = run_headless("", check=True, agent=FakeAgent([]), settings=self.settings,
                            out=io.StringIO(), err=io.StringIO())
        self.assertEqual(code, 2)

    def test_continue_with_cage_is_usage_error(self):
        code, _out, err = self.run_cage(FakeAgent([]), continue_last=True)
        self.assertEqual(code, 2)
        self.assertIn("эфемерен", err)
        code, _out, _err = self.run_cage(FakeAgent([]), resume_id="abc")
        self.assertEqual(code, 2)

    def test_required_mcp_failed(self):
        agent = FakeAgent([], failed=["tg"])
        code, out, err = self.run_cage(agent, output="json")
        self.assertEqual(code, 6)
        self.assertEqual(json.loads(out)["status"], "mcp")
        self.assertIn("tg", err)
        self.assertEqual(agent.prompts, [])

    def test_fail_on_deny(self):
        turns = [[{"text": "пробую", "deny": "mcp__tg__send_message"}, {"text": "не должно"}]]
        cage = Cage(name="news", fail_on_deny=True)
        code, out, _err = self.run_cage(FakeAgent(turns), cage=cage, output="json")
        self.assertEqual(code, 4)
        obj = json.loads(out)
        self.assertEqual(obj["status"], "denied")
        self.assertEqual(obj["denials"][0]["tool"], "mcp__tg__send_message")
        # без fail_on_deny отказ — просто запись в denials
        code, out, _err = self.run_cage(FakeAgent(json.loads(json.dumps(turns))), output="json")
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(out)["denials"]), 1)

    def test_allow_comes_from_cage_not_headless_setting(self):
        self.settings.set("headless.allow", "web_fetch")
        turn = [[{"ask": "web_fetch"}, {"text": "готово"}]]
        code, out, _err = self.run_cage(FakeAgent(turn), output="json")
        self.assertEqual(json.loads(out)["denials"][0]["tool"], "web_fetch")
        cage = Cage(name="news", allow=["web_fetch"])
        code, out, _err = self.run_cage(FakeAgent(json.loads(json.dumps(turn))), cage=cage,
                                        output="json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["denials"], [])

    def test_timeout_is_limit(self):
        cage = Cage(name="news", timeout_s=0.2)
        code, out, err = self.run_cage(FakeAgent([[{"wait": 5}]]), cage=cage, output="json")
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(out)["status"], "limit")
        self.assertIn("времени", err)

    def test_usd_and_steps_limits(self):
        cage = Cage(name="news", max_usd=0.05)
        code, _out, _err = self.run_cage(FakeAgent([[{"usage": 0.1}, {"text": "x"}]]), cage=cage)
        self.assertEqual(code, 3)
        code, _out, _err = self.run_cage(FakeAgent([[{"reason": "limit"}]]))
        self.assertEqual(code, 3)

    def test_error_and_interrupt_codes(self):
        code, _out, _err = self.run_cage(FakeAgent([[{"reason": "error"}]]))
        self.assertEqual(code, 1)
        code, _out, _err = self.run_cage(FakeAgent([[{"reason": "interrupted"}]]))
        self.assertEqual(code, 130)
        agent = FakeAgent([[{"raise": KeyboardInterrupt()}]])
        code, out, _err = self.run_cage(agent, output="json")
        self.assertEqual(code, 130)
        self.assertEqual(json.loads(out)["status"], "interrupted")
        self.assertEqual(agent.cleaned, 1)

    def test_schema_retry_then_ok(self):
        schema = {"type": "object", "required": ["summary"],
                  "properties": {"summary": {"type": "string"}}}
        cage = Cage(name="news", output_schema=schema, schema_retries=1)
        agent = FakeAgent([[{"text": "Вот: всё тихо"}], [{"text": '{"summary": "всё тихо"}'}]])
        code, out, err = self.run_cage(agent, cage=cage)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"summary": "всё тихо"})
        self.assertEqual(len(agent.prompts), 2)
        self.assertIn("JSON-схеме", agent.prompts[1])
        self.assertIn("схему", err)

    def test_schema_fails(self):
        cage = Cage(name="news", output_schema={"type": "object", "required": ["summary"]},
                    schema_retries=1)
        agent = FakeAgent([[{"text": "{}"}], [{"text": '{"other": 1}'}]])
        code, out, _err = self.run_cage(agent, cage=cage, output="json")
        self.assertEqual(code, 5)
        obj = json.loads(out)
        self.assertEqual(obj["status"], "schema")
        self.assertIsNone(obj["structured"])

    def test_mode_never_softer_than_cage(self):
        agent = FakeAgent([[{"text": "ok"}]])
        self.run_cage(agent, cage=Cage(name="n", mode="plan"), mode="yolo")
        self.assertEqual(agent.modes, ["plan"])

    def test_settings_without_project_layer(self):
        calls = []

        class FakeSettings:
            home_dir = self.home

            def __init__(self, **kw):
                calls.append(kw)

            def get(self, key, default=None):
                return default

        built = {}

        def fake_agent(**kw):
            built.update(kw)
            return FakeAgent([[{"text": "ok"}]])

        cage = Cage(name="n")
        with mock.patch("luna.core.settings.Settings", FakeSettings), \
                mock.patch("luna.router.core.Router", lambda s, sec: object()), \
                mock.patch("luna.mind.loop.Agent", fake_agent):
            code = run_headless("x", cage=cage, cwd=self.work,
                                out=io.StringIO(), err=io.StringIO())
        self.assertEqual(code, 0)
        self.assertEqual(calls, [{"cwd": self.work, "project_layer": False}])
        self.assertIs(built["cage"], cage)
        self.assertEqual(built["session_id"], "")


class HeadlessPlainJsonTest(Base):
    """--output json без клетки — тоже один итоговый объект (настоящий агент, фальшивая модель)."""

    def test_plain_json_object(self):
        from luna.router.core import ProviderConfig, Router

        settings = Settings(home=self.home, cwd=self.work)
        secrets = Secrets(self.home / "secrets.json")
        settings.set("model.provider", "fake")
        settings.set("model.kind", "fake")
        settings.set("model.main", "fake:echo")
        router = Router(settings, secrets,
                        providers={"fake": ProviderConfig("fake", "fake", "", None)})
        out, err = io.StringIO(), io.StringIO()
        code = run_headless("привет", output="json", settings=settings, secrets=secrets,
                            router=router, out=out, err=err, cwd=self.work)
        self.assertEqual(code, 0)
        obj = json.loads(out.getvalue())
        self.assertEqual(obj["status"], "ok")
        self.assertIn("Эхо: привет", obj["result"])
        self.assertIsNone(obj["worker"])


# --- CLI ------------------------------------------------------------------------------


class CliTest(Base):
    def main(self, argv):
        from luna.__main__ import main

        captured = {}

        def fake_run(prompt, **kw):
            captured["prompt"] = prompt
            captured.update(kw)
            return 0

        out = io.StringIO()
        with mock.patch("luna.headless.run_headless", fake_run), contextlib.redirect_stdout(out):
            code = main(argv)
        return code, captured, out.getvalue()

    def test_worker_narrowed_by_flags(self):
        self.write_profile("news", NEWS)
        code, kw, _out = self.main(["-p", "--worker", "news", "--tools", "web_search,bash",
                                    "--deny", "web_fetch", "--max-usd=0.1", "--timeout", "30",
                                    "--mcp", "tg:get_*", "--cwd", str(self.work), "что нового"])
        self.assertEqual(code, 0)
        cage = kw["cage"]
        self.assertEqual(kw["prompt"], "что нового")
        self.assertEqual(cage.tools, ["web_search"])
        self.assertIn("web_fetch", cage.deny)
        self.assertEqual(cage.max_usd, 0.1)
        self.assertEqual(cage.timeout_s, 30.0)
        self.assertEqual(cage.mcp["tg"].tools, ["get_*"])
        self.assertEqual(kw["cwd"], self.work.resolve())

    def test_check_without_prompt(self):
        self.write_profile("news", NEWS)
        code, kw, _out = self.main(["-p", "--worker", "news", "--check"])
        self.assertEqual(code, 0)
        self.assertTrue(kw["check"])

    def test_flags_only_cage_and_plain(self):
        code, kw, _out = self.main(["-p", "--tools", "web_search", "--fetch-host", "t.me", "x"])
        self.assertEqual(code, 0)
        self.assertEqual(kw["cage"].tools, ["web_search"])
        self.assertEqual(kw["cage"].fetch_hosts, ["t.me"])
        code, kw, _out = self.main(["-p", "--allow", "bash", "x"])
        self.assertIsNone(kw["cage"])
        self.assertEqual(kw["allow"], ["bash"])

    def test_bad_profile_is_exit_2(self):
        code, kw, out = self.main(["-p", "--worker", "nope", "x"])
        self.assertEqual(code, 2)
        self.assertEqual(kw, {})
        self.assertIn("nope", out)
        code, _kw, _out = self.main(["-p", "--tools", "crew", "x"])
        self.assertEqual(code, 2)

    def test_trust_untrust_list(self):
        from luna.__main__ import main

        project = self.root / "proj"
        project.mkdir()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(["trust", str(project)]), 0)
            self.assertEqual(main(["trust", str(project)]), 0)
            self.assertEqual(main(["trust", "--list"]), 0)
        self.assertIn("уже доверенный", out.getvalue())
        data = json.loads((self.home / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(data["security"]["trusted_projects"], [str(project.resolve())])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["untrust", str(project)]), 0)
        data = json.loads((self.home / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(data["security"]["trusted_projects"], [])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["trust", str(self.root / "нет")]), 2)


if __name__ == "__main__":
    unittest.main()
