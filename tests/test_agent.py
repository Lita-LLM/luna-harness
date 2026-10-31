"""Цикл агента на фальшивой модели: тулы, отказы, plan, зацикливание,
прерывание, обрезка вывода, секреты в событиях, очередь. Сеть — фальшивая."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from luna.core.secrets import Secrets
from luna.core.settings import Settings
from luna.devtools.fake_llm import FakeModelClient
from luna.mind.loop import Agent


class FakeRouter:
    """Тот же контракт, что у Router.chat, но внутрипроцессно и по сценарию."""

    def __init__(self, script):
        self.client = FakeModelClient(script=list(script))
        self.seen_specs: list[list[str]] = []

    def chat(self, messages, tools=None, *, role="main", ref="", on_event=None):
        self.seen_specs.append([t.name for t in tools or []])
        return self.client.chat("script", messages, tools, on_event or (lambda k, t: None))


class AgentTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="luna-agent-"))
        self.work = self.tmp / "work"
        self.work.mkdir()
        self._old_home = os.environ.get("LUNA_HOME")
        os.environ["LUNA_HOME"] = str(self.tmp / "home")
        self.settings = Settings(home=self.tmp / "home", cwd=self.work)
        self.secrets = Secrets(self.tmp / "home" / "secrets.json")
        self.events: list[dict] = []

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("LUNA_HOME", None)
        else:
            os.environ["LUNA_HOME"] = self._old_home

    def make_agent(self, script, **kwargs) -> Agent:
        agent = Agent(settings=self.settings, secrets=self.secrets,
                      router=FakeRouter(script), cwd=self.work, **kwargs)
        agent.listen(self.events.append)
        return agent

    def types(self) -> list[str]:
        return [e["type"] for e in self.events]

    def results(self) -> list[dict]:
        return [e for e in self.events if e["type"] == "tool.result"]


class AgentTurnTest(AgentTestBase):
    def test_turn_with_tools_writes_file(self):
        agent = self.make_agent([
            {"tool_calls": [{"name": "write",
                             "arguments": {"path": "a.txt", "content": "привет\n"},
                             "intent": "создать файл для проверки"}]},
            {"text": "Файл записан."},
        ], mode="auto")
        reason = agent.run_turn("запиши файл")
        self.assertEqual(reason, "end")
        self.assertEqual((self.work / "a.txt").read_text(encoding="utf-8"), "привет\n")
        types = self.types()
        for expected in ("session.start", "turn.start", "tool.call", "guard.verdict",
                         "tool.result", "agent.text", "usage", "turn.done"):
            self.assertIn(expected, types)
        call = [e for e in self.events if e["type"] == "tool.call"][0]
        self.assertEqual(call["intent"], "создать файл для проверки")
        for event in self.events:
            self.assertIn("session", event)
            self.assertIn("ts", event)

    def test_denial_is_final_and_clear(self):
        agent = self.make_agent([
            {"tool_calls": [{"name": "write",
                             "arguments": {"path": "b.txt", "content": "нельзя"},
                             "intent": "попытка записи"}]},
            {"text": "Понял, не делаю."},
        ], mode="normal", ask_guard_fn=lambda event: {"decision": "no"})
        reason = agent.run_turn("запиши")
        self.assertEqual(reason, "end")
        self.assertFalse((self.work / "b.txt").exists())
        results = self.results()
        self.assertFalse(results[0]["ok"])
        self.assertIn("НЕ ВЫПОЛНЕНО", results[0]["output"])
        self.assertIn("не обходи", results[0]["output"].lower())
        kinds = self.types()
        self.assertIn("guard.ask", kinds)
        verdict = [e for e in self.events if e["type"] == "guard.verdict"][0]
        self.assertEqual(verdict["decision"], "deny")
        self.assertEqual(verdict["by"], "human")

    def test_edit_confirmation_applies_new_args(self):
        def edit_answer(event):
            return {"decision": "edit",
                    "args": {"path": "c.txt", "content": "исправлено человеком"}}

        agent = self.make_agent([
            {"tool_calls": [{"name": "write",
                             "arguments": {"path": "c.txt", "content": "черновик"},
                             "intent": "записать черновик"}]},
            {"text": "Готово."},
        ], mode="normal", ask_guard_fn=edit_answer)
        reason = agent.run_turn("запиши")
        self.assertEqual(reason, "end")
        self.assertEqual((self.work / "c.txt").read_text(encoding="utf-8"),
                         "исправлено человеком")
        verdict = [e for e in self.events if e["type"] == "guard.verdict"][-1]
        self.assertEqual(verdict["decision"], "allow")

    def test_plan_mode_hides_and_denies_write_tools(self):
        router_holder = {}

        agent = self.make_agent([
            {"tool_calls": [{"name": "write",
                             "arguments": {"path": "d.txt", "content": "x"},
                             "intent": "попытка в plan"}]},
            {"text": "В plan только план."},
        ], mode="plan")
        router_holder["router"] = agent.router
        reason = agent.run_turn("спланируй")
        self.assertEqual(reason, "end")
        self.assertFalse((self.work / "d.txt").exists())
        specs = agent.router.seen_specs[0]
        self.assertNotIn("write", specs)
        self.assertNotIn("edit", specs)
        self.assertNotIn("bash", specs)
        self.assertIn("read", specs)
        self.assertIn("НЕ ВЫПОЛНЕНО", self.results()[0]["output"])

    def test_loop_protection_stops(self):
        step = {"tool_calls": [{"name": "read", "arguments": {"path": "нет.txt"},
                                "intent": "снова и снова"}]}
        agent = self.make_agent([dict(step), dict(step), dict(step), {"text": "не дойдёт"}],
                                mode="auto")
        reason = agent.run_turn("читай")
        self.assertEqual(reason, "limit")
        notices = [e for e in self.events if e["type"] == "notice"]
        self.assertTrue(any("зацикливания" in n["text"] for n in notices))
        done = [e for e in self.events if e["type"] == "turn.done"][0]
        self.assertEqual(done["reason"], "limit")

    def test_interrupt_after_tool(self):
        agent = self.make_agent([
            {"tool_calls": [{"name": "read", "arguments": {"path": "нет.txt"},
                             "intent": "чтение"}]},
            {"text": "не дойдёт"},
        ], mode="auto")

        def listener(event):
            if event["type"] == "tool.result":
                agent.interrupt()

        agent.listen(listener)
        reason = agent.run_turn("читай и стоп")
        self.assertEqual(reason, "interrupted")

    def test_step_limit(self):
        self.settings.set("agent.max_steps", 2)
        steps = [{"tool_calls": [{"name": "read",
                                  "arguments": {"path": "нет" + str(i) + ".txt"},
                                  "intent": "шаг"}]} for i in range(5)]
        agent = self.make_agent(steps, mode="auto")
        reason = agent.run_turn("много шагов")
        self.assertEqual(reason, "limit")

    def test_queued_message_goes_next_step(self):
        agent = self.make_agent([
            {"text": "Первый ответ."},
            {"text": "Второй ответ на очередь."},
        ], mode="auto")
        sent = {"done": False}

        def listener(event):
            if event["type"] == "agent.text.done" and not sent["done"]:
                sent["done"] = True
                agent.handle_command({"type": "user.message", "text": "ещё вопрос"})

        agent.listen(listener)
        reason = agent.run_turn("первый вопрос")
        self.assertEqual(reason, "end")
        texts = "".join(e.get("text", "") for e in self.events if e["type"] == "agent.text")
        self.assertIn("Первый ответ.", texts)
        self.assertIn("Второй ответ на очередь.", texts)

    def test_todo_event(self):
        agent = self.make_agent([
            {"tool_calls": [{"name": "todo",
                             "arguments": {"items": [{"text": "шаг один", "status": "active"}]},
                             "intent": "план"}]},
            {"text": "План готов."},
        ], mode="auto")
        agent.run_turn("спланируй")
        todos = [e for e in self.events if e["type"] == "todo"]
        self.assertEqual(len(todos), 1)
        self.assertEqual(todos[0]["items"][0]["text"], "шаг один")

    def test_ask_user_tool(self):
        agent = self.make_agent([
            {"tool_calls": [{"name": "ask_user",
                             "arguments": {"question": "Какой цвет?", "options": ["синий"]},
                             "intent": "уточнить"}]},
            {"text": "Принято."},
        ], mode="auto", ask_user_fn=lambda qid, q, options: "синий")
        reason = agent.run_turn("спроси")
        self.assertEqual(reason, "end")
        self.assertIn("ask.user", self.types())
        self.assertIn("синий", self.results()[0]["output"])


class AgentOutputAndSecretsTest(AgentTestBase):
    def test_bash_output_truncated_to_file(self):
        self.settings.set("tools.output_limit", 1000)
        command = "\"" + sys.executable + "\" -c \"print('x' * 20000)\""
        agent = self.make_agent([
            {"tool_calls": [{"name": "bash", "arguments": {"command": command},
                             "intent": "большой вывод"}]},
            {"text": "Вывел."},
        ], mode="auto")
        reason = agent.run_turn("выведи много")
        self.assertEqual(reason, "end")
        output = self.results()[0]["output"]
        self.assertLess(len(output), 2500)
        self.assertIn("обрезано", output)
        saved = list((self.tmp / "home" / "local" / "outputs").glob("*.txt"))
        self.assertEqual(len(saved), 1)
        self.assertGreater(len(saved[0].read_text(encoding="utf-8")), 19000)

    def test_no_secret_in_events(self):
        secret_value = "sk-very-secret-987654"
        self.secrets.set("provider:openai", secret_value)
        command = "\"" + sys.executable + "\" -c \"print('" + secret_value + "')\""
        agent = self.make_agent([
            {"tool_calls": [{"name": "bash", "arguments": {"command": command},
                             "intent": "случайно печатает секрет"}]},
            {"text": "В выводе было: " + secret_value},
        ], mode="yolo")
        reason = agent.run_turn("проверь")
        self.assertEqual(reason, "end")
        blob = json.dumps(self.events, ensure_ascii=False)
        self.assertNotIn(secret_value, blob)
        ask = [e for e in self.events if e["type"] == "guard.ask"]
        self.assertEqual(len(ask), 1)  # секрет в аргументах bash — спросили даже в yolo


if __name__ == "__main__":
    unittest.main()
