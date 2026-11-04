"""Клетка работника в ядре: реестр, MCP, агент, промпт, файлы, bash.

Сеть и модели — только фальшивые; LUNA_HOME — временная папка.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from luna.worker.cage import Cage, McpRule

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


class _Settings:
    """Минимальные настройки: get, home_dir и проверка доверия проекта."""

    def __init__(self, home: Path, trusted=True, values=None):
        self.home_dir = home
        self._trusted = trusted
        self._values = dict(values or {})

    def get(self, key, default=None):
        return self._values.get(key, default)

    def project_trusted(self):
        return self._trusted


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.home = self.root / "home"
        self.proj = self.root / "proj"
        self.proj.mkdir(parents=True)
        self.home.mkdir(parents=True)
        self._env = mock.patch.dict(os.environ, {"LUNA_HOME": str(self.home)})
        self._env.start()
        self._managers = []

    def tearDown(self):
        for manager in self._managers:
            try:
                manager.close()
            except Exception:  # noqa: BLE001
                pass
        self._env.stop()
        self._tmp.cleanup()

    def registry(self, cage=None, settings=None):
        from luna.tools.registry import build_registry

        reg = build_registry(cage=cage, cwd=self.proj,
                             settings=settings or _Settings(self.home))
        dict(reg.items())  # догрузка MCP
        if reg.mcp_manager is not None:
            self._managers.append(reg.mcp_manager)
        return reg


class RegistryTest(_Base):
    def test_builtin_filter(self):
        cage = Cage(tools=["web_*", "read", "bash", "luna", "todo"], fs_mode="none")
        names = set(self.registry(cage).keys())
        self.assertEqual(names, {"web_fetch", "web_search", "todo"})

    def test_no_cage_unchanged(self):
        names = set(self.registry().keys())
        self.assertIn("luna", names)
        self.assertIn("bash", names)

    def test_cage_mcp_only_allowed_servers_and_tools(self):
        write_json(self.home / "mcp.json", {"mcpServers": {
            "fake": fake_entry(), "other": fake_entry()}})
        # проектный конфиг клетке не виден, даже доверенный
        write_json(self.proj / ".mcp.json", {"mcpServers": {"proj": fake_entry()}})
        cage = Cage(tools=[], deny=["mcp__*__add"],
                    mcp={"fake": McpRule(tools=["echo", "add", "env_*"], deny=["env_echo"])})
        reg = self.registry(cage)
        mcp = sorted(n for n in reg.keys() if n.startswith("mcp__"))
        self.assertEqual(mcp, ["mcp__fake__echo"])
        self.assertEqual([c.name for c in reg.mcp_manager.configs()], ["fake"])
        self.assertEqual(reg.mcp_required_failed, [])

    def test_cage_mcp_custom_config_file(self):
        write_json(self.home / "mcp.json", {"mcpServers": {"fake": {"command": "нет-такой-команды"}}})
        own = self.root / "worker-mcp.json"
        write_json(own, {"mcpServers": {"fake": fake_entry()}})
        cage = Cage(mcp={"fake": McpRule(tools=["echo"], required=True)}, mcp_config=str(own))
        reg = self.registry(cage)
        self.assertIn("mcp__fake__echo", reg.keys())
        self.assertEqual(reg.mcp_required_failed, [])

    def test_secret_filter_and_required(self):
        from luna.core.secrets import Secrets

        secrets = Secrets(self.home / "secrets.json")
        secrets.set("ok", "можно")
        secrets.set("bad", "нельзя")
        write_json(self.home / "mcp.json", {"mcpServers": {
            "good": fake_entry(env={"PYTHONPATH": str(ROOT), "S": "{{secret:ok}}"}),
            "evil": fake_entry(env={"PYTHONPATH": str(ROOT), "S": "{{secret:bad}}"})}})
        cage = Cage(secrets=["ok"], mcp={
            "good": McpRule(tools=["env_echo"]),
            "evil": McpRule(tools=["*"], required=True),
            "ghost": McpRule(required=True)})
        reg = self.registry(cage)
        self.assertIn("mcp__good__env_echo", reg.keys())
        self.assertFalse(any(n.startswith("mcp__evil__") for n in reg.keys()))
        self.assertEqual(sorted(reg.mcp_required_failed), ["evil", "ghost"])
        text = " | ".join(reg.mcp_errors)
        self.assertIn("evil", text)
        self.assertIn("ghost", text)
        out, _i, _e = reg.mcp_manager.call("good", "env_echo", {"name": "S"})
        self.assertEqual(out, "можно")

    def test_untrusted_project_mcp_skipped(self):
        write_json(self.proj / ".mcp.json", {"mcpServers": {"proj": fake_entry()}})
        reg = self.registry(settings=_Settings(self.home, trusted=False))
        self.assertFalse(any(n.startswith("mcp__") for n in reg.keys()))
        self.assertTrue(any(".mcp.json" in e and "luna trust" in e for e in reg.mcp_errors))

    def test_trusted_project_mcp_uses_agent_cwd(self):
        write_json(self.proj / ".mcp.json", {"mcpServers": {"proj": fake_entry()}})
        # текущий каталог процесса — другой: реестр обязан смотреть в cwd агента
        with mock.patch.object(Path, "cwd", return_value=self.root):
            reg = self.registry(settings=_Settings(self.home, trusted=True))
        self.assertIn("mcp__proj__echo", reg.keys())

    def test_manager_project_configs_flag(self):
        from luna.mcp.manager import config_paths, has_mcp_config, load_configs

        write_json(self.proj / ".mcp.json", {"mcpServers": {"proj": fake_entry()}})
        self.assertTrue(has_mcp_config(self.proj, self.home))
        self.assertFalse(has_mcp_config(self.proj, self.home, project_configs=False))
        self.assertEqual(config_paths(self.proj, self.home, project_configs=False),
                         [self.home / "mcp.json"])
        servers, _p = load_configs(self.proj, self.home, project_configs=False)
        self.assertEqual(servers, {})


class _Tool:
    pass


class AgentTest(_Base):
    def make_agent(self, cage, tools=None, **kw):
        from luna.guard.policy import Guard
        from luna.mind.loop import Agent

        settings = _Settings(self.home)
        router = mock.Mock()
        return Agent(settings=settings, secrets=None, router=router, cwd=self.proj,
                     guard=Guard(None, None), cage=cage, tools=tools, **kw)

    def test_specs_and_defaults(self):
        cage = Cage(tools=["web_fetch", "todo"], model="fast", mode="auto", max_steps=7)
        agent = self.make_agent(cage)
        self.assertEqual(sorted(s.name for s in agent._specs()), ["todo", "web_fetch"])
        self.assertEqual(agent.model_ref, "fast")
        self.assertEqual(agent.mode, "auto")
        self.assertEqual(agent.max_steps_override, 7)
        self.assertIsNone(agent._checkpoints)
        self.assertIs(agent.ctx.cage, cage)
        out = agent.ctx.output_dir
        self.assertTrue(out.is_dir())
        self.assertTrue(out.name.startswith("luna-cage-"))
        agent.cleanup()
        self.assertFalse(out.exists())
        agent.cleanup()  # повторно — без ошибок

    def test_passed_tools_filtered_and_defense_in_depth(self):
        from luna.tools.registry import Tool, ToolResult

        ran = []

        def handler(ctx, args):
            ran.append(args)
            return ToolResult(True, "сделано")

        tools = {"todo": Tool("todo", "", {"type": "object"}, handler),
                 "bash": Tool("bash", "", {"type": "object"}, handler, mutating=True)}
        agent = self.make_agent(Cage(tools=["todo"]), tools=tools)
        self.assertEqual(list(agent.tools), ["todo"])
        # тул пролез в реестр мимо фильтра — исполнять всё равно нельзя
        dict.__setitem__(agent.tools, "bash", tools.get("bash") or
                         Tool("bash", "", {"type": "object"}, handler, mutating=True))
        events = []
        agent.listen(events.append)
        agent._execute_tool(agent.tools["bash"], {"command": "echo"}, "", "c1")
        self.assertEqual(ran, [])
        verdicts = [e for e in events if e.get("type") == "guard.verdict"]
        self.assertEqual(verdicts[0]["decision"], "deny")
        self.assertIn("клетка: тул не выдан", verdicts[0]["reason"])

    def test_not_ephemeral_keeps_normal_output_dir(self):
        agent = self.make_agent(Cage(tools=["todo"], ephemeral=False))
        self.assertEqual(agent.ctx.output_dir, self.home / "local" / "outputs")

    def test_system_prompt_has_worker_section(self):
        cage = Cage(name="news", tools=["todo"], prompt="Собери новости.",
                    output_schema={"type": "object"})
        agent = self.make_agent(cage)
        agent._ensure_system_prompt()
        system = agent._history[0]["content"]
        self.assertIn("Инструкции работника", system)
        self.assertIn("Собери новости.", system)
        sig = agent._current_prompt_sig()
        cage.prompt = "Другое."
        self.assertNotEqual(sig, agent._current_prompt_sig())

    def test_default_guard_enforced(self):
        import inspect

        from luna.guard.policy import Guard
        from luna.mind.loop import Agent

        if "enforce" not in inspect.signature(Guard.__init__).parameters:
            self.skipTest("Guard(enforce=…) ещё не влит")
        agent = Agent(settings=_Settings(self.home), secrets=None, router=mock.Mock(),
                      cwd=self.proj, cage=Cage(tools=["todo"]))
        self.assertTrue(agent.guard.enforce)


class PromptTest(_Base):
    def setUp(self):
        super().setUp()
        self.fake_home = self.root / "user"
        (self.fake_home / ".claude").mkdir(parents=True)
        (self.fake_home / ".claude" / "CLAUDE.md").write_text("ГЛОБАЛЬНОЕ ПРАВИЛО", encoding="utf-8")
        (self.proj / "AGENTS.md").write_text("ПРОЕКТНОЕ ПРАВИЛО", encoding="utf-8")
        self._home_patch = mock.patch("luna.mind.compat._home", return_value=self.fake_home)
        self._home_patch.start()

    def tearDown(self):
        self._home_patch.stop()
        super().tearDown()

    def build(self, cage):
        from luna.mind.prompt import build_system_prompt

        return build_system_prompt(self.proj, "normal", settings=_Settings(self.home), cage=cage)

    def test_none(self):
        text = self.build(Cage(instructions="none", prompt="Делай X.",
                               output_schema={"type": "object", "required": ["a"]}))
        self.assertNotIn("ПРОЕКТНОЕ", text)
        self.assertNotIn("ГЛОБАЛЬНОЕ", text)
        self.assertNotIn("luna about", text)       # нет самоосознанности
        self.assertNotIn("правишь код", text)      # нет обычного ядра
        self.assertIn("# Окружение", text)
        self.assertIn("Делай X.", text)
        self.assertIn('ровно один JSON-объект по схеме', text)
        self.assertIn('"required": ["a"]', text)

    def test_project(self):
        text = self.build(Cage(instructions="project"))
        self.assertIn("ПРОЕКТНОЕ", text)
        self.assertNotIn("ГЛОБАЛЬНОЕ", text)

    def test_all(self):
        text = self.build(Cage(instructions="all"))
        self.assertIn("ПРОЕКТНОЕ", text)
        self.assertIn("ГЛОБАЛЬНОЕ", text)

    def test_no_cage_unchanged(self):
        from luna.mind.prompt import build_system_prompt

        text = build_system_prompt(self.proj, "normal", settings=None)
        self.assertIn("luna about", text)
        self.assertIn("ПРОЕКТНОЕ", text)


class ImportGuardTest(_Base):
    def test_imports_never_inline_secrets(self):
        from luna.mind.compat import collect_instructions

        fake_home = self.root / "user"
        (fake_home / ".ssh").mkdir(parents=True)
        (fake_home / ".ssh" / "config").write_text("SSH-ТАЙНА", encoding="utf-8")
        (self.home / "notes.md").write_text("ЛУННАЯ-ТАЙНА", encoding="utf-8")
        (self.proj / ".env").write_text("ENV-ТАЙНА", encoding="utf-8")
        (self.proj / "server.pem").write_text("PEM-ТАЙНА", encoding="utf-8")
        (self.proj / "ok.md").write_text("МОЖНО", encoding="utf-8")
        (self.proj / "CLAUDE.md").write_text(
            "@" + str(fake_home / ".ssh" / "config") + "\n@" + str(self.home / "notes.md") +
            "\n@.env\n@server.pem\n@ok.md\n", encoding="utf-8")
        with mock.patch("luna.mind.compat._home", return_value=fake_home):
            found = collect_instructions(self.proj, None)
        text = "\n".join(t for _p, t in found)
        self.assertIn("МОЖНО", text)
        for secret in ("SSH-ТАЙНА", "ЛУННАЯ-ТАЙНА", "ENV-ТАЙНА", "PEM-ТАЙНА"):
            self.assertNotIn(secret, text)


class FilesTest(_Base):
    def ctx(self, cage):
        from luna.tools.registry import ToolContext

        return ToolContext(cwd=self.proj, cage=cage)

    def test_paths_checked(self):
        from luna.tools.files import edit_tool, glob_tool, grep_tool, read_tool, write_tool

        inside = self.proj / "data"
        inside.mkdir()
        (inside / "a.txt").write_text("привет", encoding="utf-8")
        outside = self.root / "outside.txt"
        outside.write_text("наружу", encoding="utf-8")
        ro = self.ctx(Cage(fs_mode="ro", fs_roots=[inside]))
        self.assertTrue(read_tool().run(ro, {"path": str(inside / "a.txt")}).ok)
        res = read_tool().run(ro, {"path": str(outside)})
        self.assertFalse(res.ok)
        self.assertTrue(res.text.startswith("клетка: "))
        self.assertFalse(read_tool().run(ro, {"path": "data/../../outside.txt"}).ok)
        res = write_tool().run(ro, {"path": str(inside / "b.txt"), "content": "x"})
        self.assertFalse(res.ok)
        self.assertFalse((inside / "b.txt").exists())
        self.assertFalse(edit_tool().run(ro, {"path": str(inside / "a.txt"), "old": "п",
                                              "new": "П"}).ok)
        self.assertFalse(glob_tool().run(ro, {"pattern": "*", "path": str(self.root)}).ok)
        self.assertFalse(grep_tool().run(ro, {"pattern": "н", "path": str(self.root)}).ok)
        rw = self.ctx(Cage(fs_mode="rw", fs_roots=[inside]))
        self.assertTrue(write_tool().run(rw, {"path": str(inside / "b.txt"), "content": "x"}).ok)
        none = self.ctx(Cage(fs_mode="none"))
        self.assertFalse(read_tool().run(none, {"path": str(inside / "a.txt")}).ok)

    def test_luna_home_closed_even_inside_root(self):
        from luna.tools.files import read_tool

        (self.home / "secrets.json").write_text("{}", encoding="utf-8")
        ctx = self.ctx(Cage(fs_mode="ro", fs_roots=[self.root]))
        res = read_tool().run(ctx, {"path": str(self.home / "secrets.json")})
        self.assertFalse(res.ok)
        self.assertIn("клетка", res.text)

    def test_symlink_out_of_root_hidden(self):
        from luna.tools.files import glob_tool, grep_tool

        inside = self.proj / "data"
        inside.mkdir()
        (inside / "a.txt").write_text("общее слово", encoding="utf-8")
        secret = self.root / "secret.txt"
        secret.write_text("общее слово тайна", encoding="utf-8")
        try:
            os.symlink(secret, inside / "link.txt")
        except (OSError, NotImplementedError):
            self.skipTest("симлинки недоступны")
        ctx = self.ctx(Cage(fs_mode="ro", fs_roots=[inside]))
        listed = glob_tool().run(ctx, {"pattern": "*", "path": str(inside)}).text
        self.assertIn("a.txt", listed)
        self.assertNotIn("link.txt", listed)
        found = grep_tool().run(ctx, {"pattern": "общее", "path": str(inside)}).text
        self.assertIn("a.txt", found)
        self.assertNotIn("тайна", found)


class ShellTest(_Base):
    def ctx(self, cage):
        from luna.tools.registry import ToolContext

        return ToolContext(cwd=self.proj, cage=cage)

    def test_bash_off(self):
        from luna.tools.shell import bash_tool

        with mock.patch("subprocess.Popen") as popen:
            res = bash_tool().run(self.ctx(Cage(bash="off")), {"command": "echo hi"})
        self.assertFalse(res.ok)
        self.assertIn("клетка", res.text)
        popen.assert_not_called()

    def test_sandbox_unavailable_refuses(self):
        from luna.starfall.sandbox import SandboxSpec
        from luna.tools.shell import bash_tool

        spec = SandboxSpec(backend="none", reason="ни одного бэкенда")
        ctx = self.ctx(Cage(bash="sandbox"))
        with mock.patch("luna.starfall.sandbox.resolve_spec", return_value=spec), \
                mock.patch("subprocess.Popen") as popen:
            res = bash_tool().run(ctx, {"command": "echo hi"})
            res_bg = bash_tool().run(ctx, {"command": "echo hi", "background": True})
        self.assertFalse(res.ok)
        self.assertIn("НЕ выполнена", res.text)
        self.assertFalse(res_bg.ok)
        popen.assert_not_called()
        if ctx.scratch_dir is not None:
            import shutil

            shutil.rmtree(ctx.scratch_dir, ignore_errors=True)

    def test_sandbox_runs_wrapped(self):
        from luna.starfall.sandbox import SandboxSpec
        from luna.tools.shell import bash_tool
        from luna.worker.cage import env_for_bash

        root = self.proj / "work"
        root.mkdir()
        cage = Cage(bash="sandbox", fs_mode="rw", fs_roots=[root])
        seen = {}

        def fake_resolve(settings, workdir, *, net=False):
            seen["workdir"] = Path(workdir)
            seen["net"] = net
            return SandboxSpec(backend="bwrap", workdir=Path(workdir), executable="bwrap",
                               verified=True, fs_isolated=True)

        proc = mock.Mock()
        proc.communicate.return_value = (b"hi\n", None)
        proc.returncode = 0
        with mock.patch("luna.starfall.sandbox.resolve_spec", side_effect=fake_resolve), \
                mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-тест"}), \
                mock.patch("subprocess.Popen", return_value=proc) as popen:
            res = bash_tool().run(self.ctx(cage), {"command": "echo hi"})
            expected_env = env_for_bash()
        self.assertTrue(res.ok, res.text)
        self.assertEqual(seen, {"workdir": root, "net": False})
        argv = popen.call_args[0][0]
        kwargs = popen.call_args[1]
        self.assertEqual(argv[0], "bwrap")
        self.assertIn("--unshare-net", argv)
        self.assertFalse(kwargs.get("shell"))
        self.assertEqual(kwargs["env"], expected_env)
        self.assertNotIn("OPENAI_API_KEY", kwargs["env"])


if __name__ == "__main__":
    unittest.main()
