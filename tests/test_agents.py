"""ЗАДАЧА-11: агенты-дети — права, глубина, бюджет, почта, прерывание,
--continue, составы и worktree. Всё на фальшивой модели, без сети;
потоки спутников синхронизируются через wait_idle, а не через паузы.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from luna.agents.host import SpawnError, host_for, identity_of
from luna.agents.mail import MailError
from luna.router.types import ChatResult, ToolCall, Usage


def make_owner(root: Path, mode: str = "auto", router=None, tools=None,
               ask_guard_fn=None, cwd: Path | None = None):
    from luna.core.secrets import Secrets
    from luna.core.settings import Settings
    from luna.mind.loop import Agent
    from luna.router.core import Router

    work = cwd or (root / "work")
    work.mkdir(parents=True, exist_ok=True)
    settings = Settings(home=root / "home", cwd=work)
    settings.set("model.provider", "fake")
    settings.set("model.kind", "fake")
    settings.set("model.main", "fake:echo")
    settings.set("model.fast", "fake:echo")
    secrets = Secrets(root / "home" / "secrets.json")
    agent = Agent(settings=settings, secrets=secrets,
                  router=router or Router(settings, secrets),
                  cwd=work, mode=mode, ask_guard_fn=ask_guard_fn, tools=tools)
    return agent, settings


class AgentsCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        owner = getattr(self, "owner", None)
        if owner is not None:
            host = getattr(owner, "_agents_host", None)
            if host is not None:
                for child in list(host.children.values()):
                    try:
                        host.dismiss(child)
                    except Exception:  # noqa: BLE001 — уборка не падает
                        pass

    def make(self, **kwargs):
        self.owner, self.settings = make_owner(self.root, **kwargs)
        return self.owner


class RightsTest(AgentsCase):
    def test_mode_never_freer_than_parent(self):
        owner = self.make(mode="normal")
        host = host_for(owner)
        child = host.spawn(kind="satellite", name="Вольный", mode="yolo")
        self.assertEqual(child.agent.mode, "normal")
        self.assertTrue(any("урезан" in note for note in child.spawn_notes))
        strict = host.spawn(kind="satellite", name="Тихий", mode="plan")
        self.assertEqual(strict.agent.mode, "plan")

    def test_plan_parent_clamps_child(self):
        owner = self.make(mode="plan")
        host = host_for(owner)
        child = host.spawn(kind="satellite", name="Наследник", mode="auto")
        self.assertEqual(child.agent.mode, "plan")

    def test_tools_subset_only(self):
        from luna.tools.files import read_tool

        owner = self.make(tools={"read": read_tool()})
        host = host_for(owner)
        with self.assertRaises(SpawnError):
            host.spawn(kind="satellite", name="Хакер", tools=["bash"])
        child = host.spawn(kind="satellite", name="Читатель", tools=["read"])
        self.assertEqual(sorted(child.agent.tools.keys()), ["read"])
        heir = host.spawn(kind="satellite", name="Наследник")
        self.assertEqual(sorted(heir.agent.tools.keys()), ["read"])

    def test_guard_question_marked_with_child_name(self):
        from luna.devtools.fake_llm import set_script

        asked: list[dict] = []

        def stub_ask(event):
            asked.append(event)
            return {"decision": "yes"}

        # Скрипт: настоящий mutating-вызов bash (фальшивый tools зовёт bash
        # с пустыми аргументами — Предохранитель такой вызов не спрашивает).
        set_script([
            {"tool_calls": [{"name": "bash",
                             "arguments": {"command": "echo привет"},
                             "intent": "поздороваться"}]},
            {"text": "Сводка: инструмент отработал"},
        ])
        owner = self.make(mode="normal", ask_guard_fn=stub_ask)
        host = host_for(owner)
        coder = host.spawn(kind="satellite", name="Кодер", model="fake:script")
        host.assign_task(coder, "Вызови инструмент", identity_of(owner))
        self.assertTrue(host.wait_idle(10.0))
        self.assertTrue(asked, "Предохранитель хозяина не спросил человека")
        self.assertIn("Кодер", asked[0]["intent"])
        self.assertIn("Кодер", asked[0]["reason"])
        self.assertIn("инструмент отработал", coder.summary)


class DepthTest(AgentsCase):
    def test_assistant_cannot_spawn(self):
        owner = self.make()
        host = host_for(owner)
        helper = host.spawn(kind="assistant", name="Пом")
        with self.assertRaises(SpawnError):
            host_for(helper.agent).spawn(kind="assistant", name="Внук")

    def test_satellite_generation_depth(self):
        owner = self.make()
        host = host_for(owner)
        first = host.spawn(kind="satellite", name="Первый")
        first_host = host_for(first.agent)
        # Ассистента спутник звать вправе (ассистенты — листья, не поколение).
        leaf = first_host.spawn(kind="assistant", name="Лист")
        self.assertEqual(leaf.kind, "assistant")
        # Спутник спутника при agents.max_depth=1 — нельзя.
        with self.assertRaises(SpawnError):
            first_host.spawn(kind="satellite", name="Второй")
        self.settings.set("agents.max_depth", 2)
        second = first_host.spawn(kind="satellite", name="Второй")
        self.assertEqual(second.gen, 2)


class BudgetTest(AgentsCase):
    def test_wallet_exhaustion_stops_child(self):
        owner = self.make()
        self.settings.set("model.price_in", 1000000.0)  # шаг эха = $10
        events: list[dict] = []
        owner.listen(events.append)
        host = host_for(owner)
        worker = host.spawn(kind="satellite", name="Труженик", model="fake:echo")
        host.assign_task(worker, "Работай", identity_of(owner))
        self.assertTrue(host.wait_idle(10.0))
        self.assertTrue(host.wallet.exhausted)
        self.assertTrue(worker.budget_out)
        self.assertEqual(worker.state, "сдал")
        self.assertTrue(any(ev.get("type") == "budget.update" and ev.get("exhausted")
                            for ev in events))
        host.assign_task(worker, "Ещё работай", identity_of(owner))
        self.assertTrue(host.wait_idle(10.0))
        self.assertIn("Не стал работать", worker.summary)

    def test_personal_token_budget(self):
        owner = self.make()
        host = host_for(owner)
        reviewer = host.spawn(kind="satellite", name="Ревьюер",
                              model="fake:echo", tokens=10)
        host.assign_task(reviewer, "Проверяй", identity_of(owner))
        self.assertTrue(host.wait_idle(10.0))
        self.assertTrue(reviewer.budget_out)
        self.assertIn("токенов", reviewer.budget_reason)
        self.assertEqual(reviewer.state, "сдал")
        self.assertIn("Остановлен", reviewer.summary)


class MailTest(AgentsCase):
    def test_rules_and_wake(self):
        owner = self.make()
        host = host_for(owner)
        events: list[dict] = []
        owner.listen(events.append)
        anya = host.spawn(kind="satellite", name="Аня", model="fake:echo")
        boris = host.spawn(kind="satellite", name="Борис", model="fake:echo")
        helper = host.spawn(kind="assistant", name="Пом")
        # Ассистент чужому спутнику писать не может.
        with self.assertRaises(MailError):
            host.send_mail(helper.endpoint(), "Борис", "привет")
        # Ассистент хозяину — можно; письмо ляжет в почту хозяина.
        host.send_mail(helper.endpoint(), "Луна", "доклад хозяину")
        self.assertIn("доклад хозяину", host.take_owner_notes())
        # Спутник спутнику по имени: письмо будит спящего.
        host.send_mail(anya.endpoint(), "Борис", "Сделай отчёт")
        self.assertTrue(host.wait_idle(10.0))
        self.assertIn("Сделай отчёт", boris.summary)
        self.assertEqual(boris.state, "спит")
        # Хозяин ассистенту — можно (родитель).
        host.send_mail(identity_of(owner), "Пом", "собери данные")
        self.assertIn("собери данные", helper.last_text)
        # Контракт событий: spawn и message помечены полями ребёнка.
        spawn = next(ev for ev in events if ev.get("type") == "agent.spawn")
        self.assertEqual(spawn["kind"], "satellite")
        self.assertEqual(spawn["name"], "Аня")
        self.assertIn("agent_id", spawn)
        self.assertIn("parent_id", spawn)
        message = next(ev for ev in events
                       if ev.get("type") == "agent.message" and ev.get("to") == "Борис")
        self.assertEqual(message["from"], "Аня")
        self.assertEqual(message["to"], "Борис")


class SlowRouter:
    """Роутер для теста прерывания: каждый шаг — вызов read с новым путём
    и пауза; без прерывания ход идёт все max_steps шагов."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.calls = 0
        self.settings = None

    def role_ref(self, role: str) -> str:
        return "fake:echo"

    def chat(self, messages, tools=None, role: str = "main", ref: str = "",
             on_event=None) -> ChatResult:
        self.calls += 1
        self.entered.set()
        time.sleep(0.25)
        n = self.calls
        return ChatResult(
            text="", model="fake:slow", usage=Usage(10, 5),
            tool_calls=[ToolCall("slow-" + str(n), "read",
                                 {"path": "f" + str(n) + ".txt"})])


class InterruptTest(AgentsCase):
    def test_owner_interrupt_kills_all_children(self):
        router = SlowRouter()
        owner = self.make(router=router)
        host = host_for(owner)
        satellite = host.spawn(kind="satellite", name="Долгий", steps=8)
        helper = host.spawn(kind="assistant", name="Торопыга", steps=8)
        host.assign_task(satellite, "Читай всё подряд", identity_of(owner))
        host.run_assistant(helper, "Читай всё подряд", identity_of(owner), wait=False)
        self.assertTrue(router.entered.wait(5.0))
        owner.interrupt()
        self.assertTrue(host.wait_idle(10.0))
        self.assertEqual(satellite.state, "спит")
        self.assertTrue(helper.done_event.is_set())
        # Без прерывания два ребёнка сделали бы 16 шагов модели.
        self.assertLess(router.calls, 12)


class FakeJournal:
    def __init__(self, records=None) -> None:
        self.records = list(records or [])
        self.path = None

    def append(self, record) -> None:
        self.records.append(record)


class ContinueTest(AgentsCase):
    def test_satellites_survive_continue_sleeping(self):
        owner = self.make()
        journal = FakeJournal()
        owner.journal = journal
        host = host_for(owner)
        keeper = host.spawn(kind="satellite", name="Хранитель",
                            role="сторож", model="fake:echo")
        host.assign_task(keeper, "Запомни слово: луна", identity_of(owner))
        self.assertTrue(host.wait_idle(10.0))
        self.assertTrue(any(r.get("kind") == "crew" for r in journal.records))

        owner2, _settings2 = make_owner(self.root / "second")
        self.owner2 = owner2
        journal2 = FakeJournal(journal.records)
        owner2.journal = journal2
        host2 = host_for(owner2)
        host2.ensure_restored()
        roster = host2.roster()
        self.assertEqual([c.name for c in roster], ["Хранитель"])
        restored = roster[0]
        self.assertEqual(restored.state, "спит")
        snapshot = restored.agent._state_snapshot()
        self.assertGreaterEqual(len(snapshot.get("history") or []), 2)
        # И работает после возвращения.
        host2.assign_task(restored, "Что запомнил?", identity_of(owner2))
        self.assertTrue(host2.wait_idle(10.0))
        self.assertIn("Эхо", restored.summary)
        for child in list(host2.children.values()):
            host2.dismiss(child)


class CrewFileTest(AgentsCase):
    def test_save_and_load_crew(self):
        old_home = os.environ.get("LUNA_HOME")
        os.environ["LUNA_HOME"] = str(self.root / "luna-home")
        self.addCleanup(lambda: os.environ.pop("LUNA_HOME", None)
                        if old_home is None else
                        os.environ.__setitem__("LUNA_HOME", old_home))
        owner = self.make()
        host = host_for(owner)
        host.spawn(kind="satellite", name="Тимлид", role="координатор",
                   model="fake:echo", leader=True)
        host.spawn(kind="satellite", name="Кодер1", role="пишет код",
                   model="fake:echo")
        path = host.save_crew("альфа")
        self.assertTrue(path.exists())
        for child in list(host.children.values()):
            host.dismiss(child)
        self.assertEqual(host.roster(), [])
        notes = host.load_crew("альфа")
        self.assertTrue(any("Тимлид" in note for note in notes))
        roster = {c.name: c for c in host.roster()}
        self.assertEqual(sorted(roster), ["Кодер1", "Тимлид"])
        self.assertTrue(roster["Тимлид"].leader)
        self.assertEqual(roster["Кодер1"].state, "спит")


@unittest.skipUnless(shutil.which("git"), "нет git")
class WorktreeTest(AgentsCase):
    def test_satellite_worktree(self):
        repo = self.root / "repo"
        repo.mkdir()
        env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1")
        subprocess.run(["git", "init", "-q", str(repo)], check=True, env=env)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
        (repo / "a.txt").write_text("привет", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "старт"], check=True)

        old_home = os.environ.get("LUNA_HOME")
        os.environ["LUNA_HOME"] = str(self.root / "luna-home")
        self.addCleanup(lambda: os.environ.pop("LUNA_HOME", None)
                        if old_home is None else
                        os.environ.__setitem__("LUNA_HOME", old_home))
        owner = self.make(cwd=repo)
        host = host_for(owner)
        solo = host.spawn(kind="satellite", name="Соло", model="fake:echo",
                          worktree=True)
        self.assertTrue(solo.worktree)
        worktree = Path(solo.agent.cwd)
        self.assertNotEqual(worktree, repo)
        self.assertTrue((worktree / "a.txt").exists())
        host.dismiss(solo)
        self.assertFalse(worktree.exists())


class AssistantToolTest(AgentsCase):
    def test_tool_run_start_result(self):
        from luna.agents.tools import _assistant

        owner = self.make()
        ctx = owner.ctx
        out = _assistant(ctx, {"action": "run", "task": "Посчитай звёзды"})
        self.assertIn("сдал работу", out)
        self.assertIn("Эхо: Посчитай звёзды", out)
        out = _assistant(ctx, {"action": "start", "task": "Параллельное дело",
                               "name": "Параллельный"})
        self.assertIn("запущен", out)
        host = host_for(owner)
        self.assertTrue(host.wait_idle(10.0))
        out = _assistant(ctx, {"action": "result", "name": "Параллельный"})
        self.assertIn("Эхо: Параллельное дело", out)

    def test_disabled_by_setting(self):
        from luna.agents.tools import _assistant

        owner = self.make()
        self.settings.set("agents.enabled", False)
        out = _assistant(owner.ctx, {"action": "run", "task": "Дело"})
        self.assertIn("выключены", out)

    def test_settings_defaults(self):
        self.make()
        self.assertEqual(self.settings.get("agents.session_budget_usd"), 5.0)
        self.assertEqual(self.settings.get("agents.max_depth"), 1)


if __name__ == "__main__":
    unittest.main()
