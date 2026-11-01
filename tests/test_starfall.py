"""ЗАДАЧА-15: Звездопад — профили и права ячеек, потолки и бюджет, смета,
звенья и свёртка, режимы записи, повтор упавших, продолжение после сбоя,
Esc, адреса, пул моделей, 10 000 ячеек на фальшивой модели, Созвездие.

Всё без сети: движок — на runner-заглушках, настоящий исполнитель ячеек —
на фальшивой модели (fake:echo и сценарии fake:script).
"""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from luna.core.bus import EventBus
from luna.core.paths import DataDir
from luna.core.settings import Settings
from luna.net.http_client import HttpError
from luna.starfall import profiles as profiles_mod
from luna.starfall.addressing import (address, density_mask, host_address,
                                      link_address, next_stream, parse_address,
                                      stream_for_date)
from luna.starfall.engine import (RouterCellRunner, SwarmRun, estimate,
                                  find_unfinished, resume)
from luna.starfall.modelpool import ModelPool
from luna.starfall.profiles import CellProfile, get_profile
from luna.tools.registry import ToolContext


def make_owner(root: Path, mode: str = "auto"):
    from luna.core.secrets import Secrets
    from luna.mind.loop import Agent
    from luna.router.core import Router

    work = root / "work"
    work.mkdir(parents=True, exist_ok=True)
    settings = Settings(home=root / "home", cwd=work)
    settings.set("model.provider", "fake")
    settings.set("model.kind", "fake")
    settings.set("model.main", "fake:echo")
    settings.set("model.fast", "fake:echo")
    secrets = Secrets(root / "home" / "secrets.json")
    agent = Agent(settings=settings, secrets=secrets,
                  router=Router(settings, secrets), cwd=work, mode=mode)
    return agent, settings, work


def stub_runner(text="ok", tin=10, tout=5):
    def run(profile, task_text, meta):
        return text, tin, tout

    return run


class StarfallCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)


class AddressingTest(StarfallCase):
    def test_streams_by_date(self):
        import datetime

        self.assertEqual(stream_for_date(datetime.date(2026, 8, 12)), "per")
        self.assertEqual(stream_for_date(datetime.date(2026, 11, 17)), "leo")
        self.assertEqual(stream_for_date(datetime.date(2026, 12, 14)), "gem")
        self.assertEqual(next_stream("leo"), "gem")

    def test_address_forms(self):
        self.assertEqual(address("leo", "rev", 3, 12), "leo/rev.3.12")
        self.assertEqual(link_address("leo", "rev", 3), "leo/rev.3.0")
        self.assertEqual(host_address("leo"), "leo/0")
        parsed = parse_address("leo/rev.3.12")
        self.assertEqual((parsed["stream"], parsed["caste"], parsed["link"],
                          parsed["index"]), ("leo", "rev", 3, 12))
        self.assertEqual(density_mask(50), "12")
        self.assertEqual(density_mask(10000), "34")


class ProfilesTest(StarfallCase):
    def test_builtin_profiles_complete(self):
        profs = profiles_mod.library()
        castes = {p.caste for p in profs}
        self.assertEqual(castes, {"rd", "web", "cls", "ext", "sum", "tr", "rev",
                                  "aud", "tst", "cod", "jdg", "col", "th"})
        for prof in profs:
            profiles_mod.validate_profile(prof)
            self.assertTrue(prof.prompt)

    def test_home_profile_overrides_builtin(self):
        agent, settings, _work = make_owner(self.root)
        custom = CellProfile.from_dict({"caste": "cls", "name": "Свой",
                                        "prompt": "свой промпт", "tools": []})
        path = profiles_mod.save_home_profile(custom, settings)
        self.assertTrue(path.exists())
        got = get_profile("cls", settings)
        self.assertEqual(got.name, "Свой")
        self.assertEqual(got.source, "home")

    def test_cells_tool_subset_and_save(self):
        from luna.starfall.tools import _cells

        agent, settings, _work = make_owner(self.root)
        ctx = agent.ctx
        out = _cells(ctx, {"action": "list"})
        self.assertTrue(out.ok)
        self.assertIn("rev", out.text)
        # Профиль шире прав матери не создать: у матери нет тула «неттакого».
        out = _cells(ctx, {"action": "create", "profile": {
            "caste": "spy", "prompt": "шпионь", "tools": ["неттакого"]}})
        self.assertFalse(out.ok)
        self.assertIn("прав матери", out.text)
        out = _cells(ctx, {"action": "create", "profile": {
            "caste": "spy", "prompt": "смотри", "tools": ["read"],
            "write_mode": "none"}})
        self.assertTrue(out.ok, out.text)
        out = _cells(ctx, {"action": "save", "caste": "spy"})
        self.assertTrue(out.ok, out.text)
        saved = profiles_mod.home_dir(settings) / "spy.md"
        self.assertTrue(saved.exists())
        self.assertEqual(get_profile("spy", settings).source, "home")


class GateTest(StarfallCase):
    """Настоящий исполнитель ячеек на фальшивой модели-сценарии."""

    def setUp(self) -> None:
        super().setUp()
        from luna.devtools.fake_llm import set_script

        self.addCleanup(lambda: set_script([]))
        self.agent, self.settings, self.work = make_owner(self.root)
        self.settings.set("model.fast", "fake:script")

    def _runner(self, **kw):
        mother_tools = {tool.name: tool for tool in self.agent.tools.values()}
        return RouterCellRunner(router=self.agent.router, settings=self.settings,
                                secrets=None, mother_tools=mother_tools,
                                guard=self.agent.guard, project_dir=self.work, **kw)

    def _meta(self, name="cell"):
        cell_dir = self.root / name
        return {"index": 0, "address": "leo/rd.0.0", "link": 0,
                "cell_dir": cell_dir, "write_dir": cell_dir, "endpoint": ""}

    def test_out_of_profile_tool_denied_without_human(self):
        from luna.devtools.fake_llm import set_script

        asked = []
        self.agent.ask_guard_fn = lambda ev: asked.append(ev) or {"decision": "yes"}
        set_script([
            {"tool_calls": [{"name": "write",
                             "arguments": {"path": "evil.txt", "content": "x"},
                             "intent": "записать мимо профиля"}]},
            {"text": "понял, не пишу"},
        ])
        prof = get_profile("rd")  # только чтение, write вне профиля
        text, _tin, _tout = self._runner()(prof, "прочти файл", self._meta())
        self.assertEqual(text, "понял, не пишу")
        self.assertFalse((self.root / "cell" / "evil.txt").exists())
        self.assertFalse((self.work / "evil.txt").exists())
        self.assertEqual(asked, [])  # человека ячейка не спрашивала

    def test_sandbox_write_inside_ok_outside_denied(self):
        from luna.devtools.fake_llm import set_script

        set_script([
            {"tool_calls": [{"name": "write",
                             "arguments": {"path": "../evil.txt", "content": "x"},
                             "intent": "вылезти из песочницы"}]},
            {"tool_calls": [{"name": "write",
                             "arguments": {"path": "ok.txt", "content": "привет"},
                             "intent": "записать к себе"}]},
            {"text": "сдал"},
        ])
        prof = CellProfile.from_dict({"caste": "tr", "prompt": "пиши",
                                      "tools": ["write"], "write_mode": "sandbox",
                                      "steps": 4, "model": "fast"})
        text, _tin, _tout = self._runner()(prof, "пиши", self._meta())
        self.assertEqual(text, "сдал")
        self.assertFalse((self.root / "evil.txt").exists())
        self.assertEqual((self.root / "cell" / "ok.txt").read_text(encoding="utf-8"),
                         "привет")


class EngineTest(StarfallCase):
    def _run(self, total=16, runner=None, **kw):
        prof = get_profile("cls")
        args = {"stream": "leo", "profiles": [prof],
                "inputs": ["вход " + str(i) for i in range(total)],
                "output_dir": self.root / "out",
                "runner": runner or stub_runner(), "parallel": 4,
                "link_size": 8, "budget_usd": 1.0, "mail": "tree"}
        args.update(kw)
        return SwarmRun(**args)

    def test_links_and_code_collapse_json(self):
        def runner(profile, task_text, meta):
            num = task_text.rsplit(" ", 1)[-1]
            return json.dumps({"v": int(num)}), 10, 5

        run = self._run(16, runner=runner)
        summary = run.run()
        self.assertEqual((summary["done"], summary["failed"]), (16, 0))
        self.assertEqual(summary["links"], 2)
        first = summary["link_summaries"][0]
        merged = json.loads(first.split("\n", 1)[1])
        self.assertEqual(len(merged), 8)
        self.assertTrue((run.dir / "summary.md").exists())
        self.assertEqual(len(list((run.dir / "results").glob("*.json"))), 16)
        self.assertEqual(run.address_of(13), "leo/cls.1.5")

    def test_budget_stops_swarm(self):
        _agent, settings, _work = make_owner(self.root / "b")
        settings.set("model.price_in", 1000.0)  # ячейка: 10 вх. токенов = $0.01
        run = self._run(10, settings=settings, parallel=1, budget_usd=0.025)
        summary = run.run()
        self.assertEqual(summary["done"], 3)
        self.assertEqual(summary["skipped"], 7)
        self.assertIn("бюджет", summary["stopped_reason"])

    def test_estimate_math(self):
        _agent, settings, _work = make_owner(self.root / "e")
        settings.set("model.price_in", 100.0)
        settings.set("model.price_out", 200.0)
        prof = get_profile("cls")
        est = estimate(stub_runner(tin=100, tout=50), prof,
                       ["x"] * 10, "", settings, tmp_dir=self.root)
        self.assertEqual(est["samples"], 3)
        self.assertAlmostEqual(est["per_cost_usd"], 0.02, places=6)
        self.assertAlmostEqual(est["total_cost_usd"], 0.2, places=4)

    def test_retry_failed_cells(self):
        attempts = {}

        def flaky(profile, task_text, meta):
            idx = meta["index"]
            attempts[idx] = attempts.get(idx, 0) + 1
            if attempts[idx] == 1:
                raise RuntimeError("упала")
            return "ok", 10, 5

        run = self._run(8, runner=flaky, max_attempts=3)
        summary = run.run()
        self.assertEqual(summary["done"], 8)
        self.assertTrue(all(run.attempts[i] == 2 for i in range(8)))

    def test_stop_and_resume(self):
        def slow(profile, task_text, meta):
            time.sleep(0.02)
            return "ok", 10, 5

        run = self._run(40, runner=slow, parallel=2)
        box = {}
        thread = threading.Thread(target=lambda: box.update(run.run()))
        thread.start()
        deadline = time.time() + 5
        while run.done < 6 and time.time() < deadline:
            time.sleep(0.01)
        run.stop()
        thread.join(timeout=10)
        self.assertLess(box["done"], 40)
        self.assertEqual(find_unfinished(self.root / "out"), [run.dir])
        summary = resume(run.dir, runner=stub_runner())
        self.assertEqual(summary["done"], 40)
        self.assertEqual(find_unfinished(self.root / "out"), [])

    def test_esc_via_agent_interrupt(self):
        run = self._run(10, runner=stub_runner())
        agent, _settings, _work = make_owner(self.root / "esc")
        agent._starfall_run = run
        agent.interrupt()
        self.assertTrue(run._stop.is_set())

    def test_pool_rate_limit_recovery(self):
        pool = ModelPool(["http://a", "http://b"], base_parallel=2)
        first = pool.acquire()
        pool.release(first, rate_limited=True)
        self.assertEqual(first.limit, 1)
        self.assertEqual(pool.rate_hits, 1)
        for _ in range(8):
            ep = pool.acquire()
            if ep is not None:
                pool.release(ep, ok=True)
        self.assertGreaterEqual(first.limit, 1)

    def test_engine_pool_429_counts(self):
        state = {"raised": False}

        def runner(profile, task_text, meta):
            if not state["raised"]:
                state["raised"] = True
                raise HttpError(429, "медленнее")
            return "ok", 10, 5

        prof = CellProfile.from_dict({"caste": "cls", "prompt": "x", "tools": [],
                                      "pool": ["http://node1"], "model": "fast"})
        run = SwarmRun(stream="leo", profiles=[prof], inputs=["a", "b"],
                       output_dir=self.root / "out", runner=runner, parallel=1,
                       budget_usd=1.0)
        summary = run.run()
        self.assertEqual(summary["done"], 2)
        self.assertGreaterEqual(run._pools["cls"].rate_hits, 1)


_TEN_K_SCRIPT = """
import resource, sys, time
sys.path.insert(0, sys.argv[1])
from pathlib import Path
base = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
from luna.starfall.engine import SwarmRun
from luna.starfall.profiles import get_profile
run = SwarmRun(stream="leo", profiles=[get_profile("cls")],
               inputs=["вход " + str(i) for i in range(10000)],
               output_dir=Path(sys.argv[2]),
               runner=lambda p, t, m: ("ok", 1, 1),
               parallel=8, link_size=8, budget_usd=0.0, mail="none")
started = time.time()
summary = run.run()
peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
print(summary["done"], summary["failed"], round(time.time() - started, 1),
      peak - base)
"""


class TenThousandTest(StarfallCase):
    def test_10000_cells_no_memory_growth(self):
        # Отдельный процесс: его ru_maxrss — честный пик именно этого
        # прогона (в общем процессе тестов пик уже поднят соседями).
        import subprocess
        import sys

        repo = Path(__file__).resolve().parents[1]
        proc = subprocess.run(
            [sys.executable, "-c", _TEN_K_SCRIPT, str(repo), str(self.root / "out")],
            capture_output=True, text=True, timeout=300)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        done, failed, seconds, growth_kb = proc.stdout.split()
        self.assertEqual((int(done), int(failed)), (10000, 0))
        self.assertLess(float(seconds), 120)
        if sys.platform.startswith("linux"):
            # ru_maxrss в килобайтах: рост пика на 10 000 ячеек — мегабайты,
            # а не «поток на ячейку» с её сотнями килобайт.
            self.assertLess(int(growth_kb), 80 * 1024,
                            "рост пика RSS: " + growth_kb + " КБ")


class StarfallToolTest(StarfallCase):
    def setUp(self) -> None:
        super().setUp()
        from luna.starfall.tools import _starfall

        self._starfall = _starfall
        self.agent, self.settings, self.work = make_owner(self.root)
        self.settings.set("starfall.output_dir", str(self.root / "sf"))
        self.ctx = self.agent.ctx

    def test_caps_via_tool(self):
        self.settings.set("starfall.max_cells", 5)
        out = self._starfall(self.ctx, {"action": "run", "profile": "cls",
                                        "inputs": ["x"] * 6})
        self.assertFalse(out.ok)
        self.assertIn("max_cells", out.text)
        out = self._starfall(self.ctx, {"action": "run", "profile": "cls",
                                        "inputs": ["x"], "budget_usd": 2.0})
        self.assertFalse(out.ok)
        self.assertIn("budget_usd", out.text)

    def test_project_mode_needs_confirmation(self):
        out = self._starfall(self.ctx, {"action": "run", "profile": "cls",
                                        "inputs": ["x"], "write_mode": "project"})
        self.assertFalse(out.ok)
        self.assertIn("project", out.text)

    def test_estimate_and_run_on_fake(self):
        args = {"profile": "cls", "inputs": ["отзыв " + str(i) for i in range(8)],
                "template": "Разметь: {input}", "parallel": 4}
        est = self._starfall(self.ctx, dict(args, action="estimate"))
        self.assertTrue(est.ok, est.text)
        self.assertIn("Смета", est.text)
        out = self._starfall(self.ctx, dict(args, action="run",
                                            estimate="смета получена"))
        self.assertTrue(out.ok, out.text)
        self.assertIn("сдали 8 из 8", out.text)
        status = self._starfall(self.ctx, {"action": "status"})
        self.assertTrue(status.ok)


class ConstellationTest(StarfallCase):
    def _app(self):
        from luna.ui.panels import PanelData

        data = PanelData()
        settings = Settings(home=self.root / "home", cwd=self.root)
        app = type("App", (), {})()
        app.panel_data = data
        app.settings = settings
        app._clock = lambda: 1000.0
        return app, data

    def test_panel_renders_braille_and_zoom(self):
        from luna.ui.panels.constellation import ConstellationPanel
        from luna.ui.keys import Key

        app, data = self._app()
        data.observe({"type": "swarm.start", "swarm": "leo-1", "stream": "leo",
                      "total": 16, "host": "leo/0"})
        cells = [["leo/cls." + str(i // 8) + "." + str(i % 8),
                  ["run", "wait", "done", "failed"][i % 4]] for i in range(16)]
        data.observe({"type": "swarm.cells", "cells": cells, "links": False})
        data.observe({"type": "swarm.progress", "done": 4, "failed": 1,
                      "skipped": 0, "total": 16, "spent_usd": 0.001,
                      "eta_sec": 30.0})
        panel = ConstellationPanel()
        lines = panel.lines(app, 40)
        joined = "\n".join(lines)
        self.assertIn("рой leo", joined)
        self.assertTrue(any("⠀" <= ch <= "⣿" and ch != "⠀"
                            for ch in joined if len(ch) == 1 and ord(ch) >= 0x2800))
        self.assertTrue(panel.handle_key(app, Key("enter")))
        self.assertEqual(panel.zoom_link, 0)
        zoomed = "\n".join(panel.lines(app, 40))
        self.assertIn("leo/cls.0.0", zoomed)

    def test_swarm_line_in_moon_and_crew(self):
        from luna.ui.panels.builtin import MoonPanel
        from luna.ui.panels.crew import CrewPanel

        app, data = self._app()
        data.observe({"type": "swarm.start", "swarm": "leo-1", "stream": "leo",
                      "total": 10, "host": "leo/0"})
        data.observe({"type": "swarm.progress", "done": 3, "failed": 0,
                      "skipped": 0, "total": 10, "spent_usd": 0.0,
                      "eta_sec": 12.0})
        self.assertIn("рой leo", "\n".join(MoonPanel().lines(app, 40)))
        self.assertIn("рой leo", "\n".join(CrewPanel().lines(app, 40)))


class ChatCellsTest(StarfallCase):
    def test_cells_command_opens_picker(self):
        from luna.ui.anim import Scheduler
        from luna.ui.chat import COMMANDS, ChatApp

        self.assertIn("cells", {name for name, _ in COMMANDS})
        work = self.root / "work"
        work.mkdir()
        settings = Settings(home=self.root / "home", cwd=work)
        app = ChatApp(EventBus(), settings, Scheduler(), "emoji",
                      cwd=str(work), data=DataDir(self.root / "home"))
        app.submit_text("/cells")
        self.assertEqual(app._picker_kind, "cells")
        keys = {item.key for item in app.picker.items}
        self.assertIn("rev", keys)
        self.assertIn("cls", keys)

    def test_restore_offers_resume(self):
        from luna.ui.anim import Scheduler
        from luna.ui.chat import ChatApp

        work = self.root / "work"
        work.mkdir()
        settings = Settings(home=self.root / "home", cwd=work)
        app = ChatApp(EventBus(), settings, Scheduler(), "emoji",
                      cwd=str(work), data=DataDir(self.root / "home"))
        app.restore([{"kind": "swarm",
                      "data": {"swarm": "leo-x", "dir": "/tmp/zz",
                               "finished": False}}])
        texts = [e.text for e in app.entries if e.kind == "notice"]
        self.assertTrue(any("не закончен" in t for t in texts), texts)


if __name__ == "__main__":
    unittest.main()
