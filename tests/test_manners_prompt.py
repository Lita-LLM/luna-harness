"""ЗАДАЧА-07: слои системного промпта и манеры.

Промпт: ядро → окружение → самоосознанность → инструкции проекта → манера.
Манеры: встроенные, свои из LUNA_HOME и проекта (своя перекрывает),
переключение настройкой и тулом, незнакомая манера — понятная ошибка.
Сеть не нужна, модель — фальшивая.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from luna.core.errors import HumanError
from luna.core.secrets import Secrets
from luna.core.settings import Settings
from luna.devtools import fake_llm
from luna.devtools.fake_llm import FakeModelClient
from luna.mind import manners
from luna.mind.loop import Agent
from luna.mind.prompt import build_system_prompt
from luna.tools.registry import ToolContext, build_registry
from luna.tools.shell import shell_name
from tests.test_main import run as cli_run

ROOT = Path(__file__).resolve().parents[1]


class RecordingRouter:
    """Контракт Router.chat + запись сообщений каждого вызова."""

    def __init__(self, script):
        self.client = FakeModelClient(script=list(script))
        self.calls: list[list[dict]] = []

    def chat(self, messages, tools=None, *, role="main", ref="", on_event=None):
        self.calls.append([dict(m) for m in messages])
        return self.client.chat("script", messages, tools, on_event or (lambda k, t: None))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="luna-manners-"))
        self.work = self.tmp / "work"
        self.work.mkdir()
        self.home = self.tmp / "home"
        self.settings = Settings(home=self.home, cwd=self.work)

    def write_home_manner(self, mid: str, text: str, name: str = "") -> Path:
        directory = self.home / "manners"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (mid + ".md")
        path.write_text(manners.render_manner(name or mid, "", text), encoding="utf-8")
        return path

    def write_project_manner(self, mid: str, text: str, name: str = "") -> Path:
        directory = self.work / ".luna" / "manners"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (mid + ".md")
        path.write_text(manners.render_manner(name or mid, "", text), encoding="utf-8")
        return path


class PromptLayersTest(Base):
    def setUp(self):
        super().setUp()
        (self.work / "AGENTS.md").write_text("Правило проекта: помечай ответы цифрой 7.",
                                             encoding="utf-8")

    def test_layers_in_order_and_manner_last(self):
        prompt = build_system_prompt(self.work, settings=self.settings)
        core = prompt.index("Ты — Луна, сессионный ИИ-агент-работник")
        env = prompt.index("# Окружение")
        self_layer = prompt.index("# Кто ты и где ты живёшь")
        project = prompt.index("Инструкции проекта")
        manner = prompt.index("# Манера речи")
        self.assertTrue(core < env < self_layer < project < manner)
        self.assertIn("помечай ответы цифрой 7", prompt)
        # Манера по умолчанию — luna, и её текст стоит после заголовка слоя.
        self.assertIn("тёплая и спокойная", prompt[manner:])

    def test_environment_facts_present(self):
        prompt = build_system_prompt(self.work, settings=self.settings)
        self.assertIn("- Оболочка тула bash: " + shell_name(), prompt)
        self.assertIn("- Рабочий каталог: " + str(self.work), prompt)
        self.assertIn("- ОС: ", prompt)
        self.assertIn("- Дата и время: ", prompt)
        self.assertIn("- Язык пользователя: ", prompt)
        self.assertIn("не git-репозиторий", prompt)

    def test_core_rules_present(self):
        prompt = build_system_prompt(self.work, settings=self.settings)
        self.assertIn("НЕ ВЫПОЛНЕНО", prompt)          # отказ Предохранителя не обходить
        self.assertIn("поле intent", prompt)           # intent — в поле тула
        self.assertIn("В обычном тексте intent не пиши", prompt)
        self.assertIn("agent.manner", prompt)          # самоосознанность по фактам

    def test_plan_mode_note(self):
        prompt = build_system_prompt(self.work, mode="plan", settings=self.settings)
        self.assertIn("Режим plan", prompt)

    def test_no_manner_layer_when_setting_empty(self):
        self.settings.set("agent.manner", "")
        prompt = build_system_prompt(self.work, settings=self.settings)
        self.assertNotIn("# Манера речи", prompt)

    def test_git_branch_in_environment(self):
        if shutil.which("git") is None:
            self.skipTest("git не установлен")
        subprocess.run(["git", "init", "-q"], cwd=self.work, check=True)
        prompt = build_system_prompt(self.work, settings=self.settings)
        self.assertIn("git, ветка", prompt)
        self.assertNotIn("не git-репозиторий", prompt)


class MannersLibraryTest(Base):
    def test_builtins_present(self):
        items = {m["id"]: m for m in manners.library(self.settings, self.work)}
        for mid in ("luna", "terse", "mentor"):
            self.assertIn(mid, items)
            self.assertEqual(items[mid]["source"], "builtin")
            self.assertTrue(items[mid]["text"])

    def test_home_overrides_builtin(self):
        self.write_home_manner("luna", "Своя домашняя Луна.", name="Домашняя")
        item = manners.get(self.settings, "luna", self.work)
        self.assertEqual(item["source"], "home")
        self.assertIn("Своя домашняя Луна.", item["text"])
        prompt = build_system_prompt(self.work, settings=self.settings)
        self.assertIn("Своя домашняя Луна.", prompt)

    def test_project_overrides_home(self):
        self.write_home_manner("terse", "Домашняя краткость.")
        self.write_project_manner("terse", "Проектная краткость.")
        item = manners.get(self.settings, "terse", self.work)
        self.assertEqual(item["source"], "project")
        self.assertIn("Проектная краткость.", item["text"])

    def test_resolve_by_name_and_prefix(self):
        self.assertEqual(manners.resolve(self.settings, "Краткая", self.work)["id"], "terse")
        self.assertEqual(manners.resolve(self.settings, "Крат", self.work)["id"], "terse")
        self.assertEqual(manners.resolve(self.settings, "MENTOR", self.work)["id"], "mentor")

    def test_unknown_manner_is_clear_error(self):
        with self.assertRaises(HumanError) as ctx:
            manners.resolve(self.settings, "нет-такой", self.work)
        self.assertIn("Нет манеры", ctx.exception.title)
        self.assertIn("luna manners", ctx.exception.hint)

    def test_configured_unknown_manner_breaks_prompt_loudly(self):
        self.settings.set("agent.manner", "нет-такой")
        with self.assertRaises(HumanError):
            build_system_prompt(self.work, settings=self.settings)

    def test_choose_writes_setting(self):
        entry = manners.choose(self.settings, "mentor", self.work)
        self.assertEqual(entry["id"], "mentor")
        self.assertEqual(self.settings.get("agent.manner"), "mentor")
        self.assertIsNone(manners.choose(self.settings, "", self.work))
        self.assertEqual(self.settings.get("agent.manner"), "")


class MannerToolTest(Base):
    def setUp(self):
        super().setUp()
        self.tool = build_registry()["manner"]
        self.ctx = ToolContext(cwd=self.work, settings=self.settings)

    def run_tool(self, **args):
        return self.tool.run(self.ctx, args)

    def test_mutating_only_for_changing_actions(self):
        for action in ("list", "show"):
            self.assertFalse(self.tool.is_mutating({"action": action}), action)
        for action in ("use", "off", "create", "edit"):
            self.assertTrue(self.tool.is_mutating({"action": action}), action)

    def test_list_and_show(self):
        res = self.run_tool(action="list")
        self.assertTrue(res.ok, res.text)
        self.assertIn("Луна", res.text)
        self.assertIn("Сейчас:", res.text)
        res = self.run_tool(action="show", name="mentor")
        self.assertTrue(res.ok, res.text)
        self.assertIn("терпеливый наставник", res.text)

    def test_use_and_off(self):
        res = self.run_tool(action="use", name="terse")
        self.assertTrue(res.ok, res.text)
        self.assertEqual(self.settings.get("agent.manner"), "terse")
        res = self.run_tool(action="off")
        self.assertTrue(res.ok, res.text)
        self.assertEqual(self.settings.get("agent.manner"), "")
        res = self.run_tool(action="use", name="нет-такой")
        self.assertFalse(res.ok)
        self.assertIn("Нет манеры", res.text)

    def test_create_and_duplicate(self):
        res = self.run_tool(action="create", name="strict", title="Строгая",
                            description="Без лишних слов", text="Говори строго и по делу.")
        self.assertTrue(res.ok, res.text)
        self.assertTrue((self.home / "manners" / "strict.md").is_file())
        item = manners.get(self.settings, "strict", self.work)
        self.assertEqual(item["name"], "Строгая")
        self.assertEqual(item["source"], "home")
        res = self.run_tool(action="create", name="strict", title="Строгая",
                            description="", text="Ещё одна.")
        self.assertFalse(res.ok)
        self.assertIn("уже есть", res.text)

    def test_create_bad_id(self):
        res = self.run_tool(action="create", name="Плохой Ид", title="Х",
                            description="", text="Текст.")
        self.assertFalse(res.ok)
        self.assertIn("id", res.text)

    def test_edit_builtin_creates_home_override(self):
        res = self.run_tool(action="edit", name="terse", text="Совсем коротко, точка.")
        self.assertTrue(res.ok, res.text)
        item = manners.get(self.settings, "terse", self.work)
        self.assertEqual(item["source"], "home")
        self.assertEqual(item["text"], "Совсем коротко, точка.")
        self.assertEqual(item["name"], "Краткая")  # не переданное поле осталось как было
        builtin_text = (manners.BUILTIN_DIR / "terse.md").read_text(encoding="utf-8")
        self.assertIn("Говори только суть", builtin_text)  # встроенный файл не тронут


class AgentMannerTest(Base):
    def make_agent(self, script, **kwargs) -> Agent:
        router = RecordingRouter(script)
        agent = Agent(settings=self.settings, secrets=Secrets(self.home / "secrets.json"),
                      router=router, cwd=self.work, **kwargs)
        self.events: list[dict] = []
        agent.listen(self.events.append)
        return agent

    def test_manner_switch_applies_next_turn(self):
        agent = self.make_agent([{"text": "раз"}, {"text": "два"}])
        agent.run_turn("привет")
        first = agent.router.calls[0][0]["content"]
        self.assertIn("«Луна»", first)
        self.settings.set("agent.manner", "terse")
        agent.run_turn("ещё")
        second = agent.router.calls[1][0]["content"]
        self.assertIn("«Краткая»", second)
        self.assertNotIn("«Луна»", second)

    def test_broken_manner_turn_reports_error(self):
        self.settings.set("agent.manner", "нет-такой")
        agent = self.make_agent([{"text": "не дойдёт"}])
        reason = agent.run_turn("привет")
        self.assertEqual(reason, "error")
        errors = [e for e in self.events if e["type"] == "error"]
        self.assertTrue(errors)
        self.assertIn("Нет манеры", errors[0]["message"])

    def test_manner_use_asks_in_normal_mode(self):
        agent = self.make_agent([
            {"tool_calls": [{"name": "manner", "arguments": {"action": "use", "name": "terse"},
                             "intent": "включить краткую манеру по просьбе"}]},
            {"text": "Итог."},
        ], mode="normal")
        reason = agent.run_turn("будь краткой")
        self.assertEqual(reason, "end")
        asks = [e for e in self.events if e["type"] == "guard.ask"]
        self.assertEqual(len(asks), 1)
        results = [e for e in self.events if e["type"] == "tool.result"]
        self.assertIn("НЕ ВЫПОЛНЕНО", results[0]["output"])  # без канала — отказ
        self.assertEqual(self.settings.get("agent.manner"), "luna")  # не переключилась

    def test_manner_use_with_human_yes(self):
        agent = self.make_agent([
            {"tool_calls": [{"name": "manner", "arguments": {"action": "use", "name": "terse"},
                             "intent": "включить краткую манеру по просьбе"}]},
            {"text": "Итог."},
        ], mode="normal", ask_guard_fn=lambda event: {"decision": "yes"})
        reason = agent.run_turn("будь краткой")
        self.assertEqual(reason, "end")
        self.assertEqual(self.settings.get("agent.manner"), "terse")


class HeadlessPromptTest(Base):
    def test_fake_model_receives_environment(self):
        from luna.headless import run_headless
        from luna.router.core import Router

        self.settings.set("model.provider", "fake")
        self.settings.set("model.kind", "fake")
        self.settings.set("model.main", "fake:script")
        fake_llm.set_script([{"text": "Моя оболочка видна в промпте."}])
        before = len(fake_llm.seen_requests())
        secrets = Secrets(self.home / "secrets.json")
        router = Router(self.settings, secrets)
        old_cwd = Path.cwd()
        os.chdir(self.work)
        try:
            code = run_headless("какая у тебя оболочка и где ты работаешь?",
                                settings=self.settings, secrets=secrets, router=router)
        finally:
            os.chdir(old_cwd)
        self.assertEqual(code, 0)
        requests = fake_llm.seen_requests()[before:]
        self.assertTrue(requests)
        system = requests[0]["messages"][0]
        self.assertEqual(system["role"], "system")
        self.assertIn("- Оболочка тула bash: " + shell_name(), system["content"])
        self.assertIn(str(self.work), system["content"])


class MannersCliTest(unittest.TestCase):
    def test_list_use_show_and_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            res = cli_run(["manners"], home)
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("Луна (luna", res.stdout)
            self.assertIn("Сейчас: luna", res.stdout)
            res = cli_run(["manners", "use", "mentor"], home)
            self.assertEqual(res.returncode, 0, res.stderr)
            res = cli_run(["manners"], home)
            self.assertIn("Сейчас: mentor", res.stdout)
            res = cli_run(["manners", "show", "mentor"], home)
            self.assertIn("Наставник", res.stdout)
            res = cli_run(["manners", "use", "нет-такой"], home)
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("Хьюстон", res.stdout)

    def test_try_prompt_prints_layers(self):
        with tempfile.TemporaryDirectory() as tmp:
            res = cli_run(["try", "prompt"], Path(tmp) / "home", cwd=ROOT)
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("# Окружение", res.stdout)
            self.assertIn("- Оболочка тула bash: ", res.stdout)
            self.assertIn("# Манера речи", res.stdout)


if __name__ == "__main__":
    unittest.main()
