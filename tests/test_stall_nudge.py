"""Модель обещает действие и не вызывает тул, пишет вызов текстом в
незнакомом формате или отвечает пустотой: Луна узнаёт больше форматов
и подталкивает модель, а не заканчивает ход обещанием."""

import unittest

from luna.core.envelope import harness_only
from luna.core.sessions import build_state
from luna.mind import stall
from luna.router import textcalls
from luna.router.types import ToolSpec
from tests.test_rewind_compact import RewindBase

TOOLS = [
    ToolSpec("read", "", {"type": "object", "properties": {"path": {"type": "string"}}}),
    ToolSpec("bash", "", {"type": "object", "properties": {"command": {"type": "string"}}}),
]
READ = {"path": "a.py"}


def names(text):
    calls, rest = textcalls.extract(text, TOOLS)
    return [(c.name, c.arguments) for c in calls], rest


class MoreFormatsTest(unittest.TestCase):
    def test_llama_function_json(self):
        calls, rest = names('Смотрю.\n<function=read>{"path": "a.py"}</function>')
        self.assertEqual(calls, [("read", READ)])
        self.assertEqual(rest, "Смотрю.")

    def test_glm_arg_pairs_and_json(self):
        self.assertEqual(names("<tool_call>read\n<arg_key>path</arg_key>\n"
                               "<arg_value>a.py</arg_value>\n</tool_call>")[0], [("read", READ)])
        self.assertEqual(names('<tool_call>read\n{"path": "a.py"}\n</tool_call>')[0],
                         [("read", READ)])

    def test_kimi_tokens(self):
        calls, rest = names("<|tool_calls_section_begin|><|tool_call_begin|>functions.read:0"
                            '<|tool_call_argument_begin|>{"path": "a.py"}<|tool_call_end|>'
                            "<|tool_calls_section_end|>")
        self.assertEqual(calls, [("read", READ)])
        self.assertEqual(rest, "")

    def test_mistral_list_and_args(self):
        calls, _ = names('[TOOL_CALLS][{"name": "read", "arguments": {"path": "a.py"}}, '
                         '{"name": "bash", "arguments": {"command": "ls"}}]')
        self.assertEqual([c[0] for c in calls], ["read", "bash"])
        self.assertEqual(names('[TOOL_CALLS]read[ARGS]{"path": "a.py"}')[0], [("read", READ)])

    def test_harmony(self):
        calls, rest = names("<|channel|>commentary to=functions.read <|constrain|>json"
                            '<|message|>{"path": "a.py"}<|call|>')
        self.assertEqual(calls, [("read", READ)])
        self.assertEqual(rest, "")

    def test_functions_prefix_in_hermes(self):
        self.assertEqual(names('<tool_call>{"name": "functions.read", "arguments": '
                               '{"path": "a.py"}}</tool_call>')[0], [("read", READ)])

    def test_fenced_json_at_the_end(self):
        calls, rest = names('Сейчас прочитаю:\n```json\n{"name": "read", "arguments": '
                            '{"path": "a.py"}}\n```')
        self.assertEqual(calls, [("read", READ)])
        self.assertEqual(rest, "Сейчас прочитаю:")

    def test_bare_json_other_keys(self):
        self.assertEqual(names('Вызываю {"tool": "bash", "args": {"command": "ls"}}')[0],
                         [("bash", {"command": "ls"})])

    def test_tool_code_and_react(self):
        self.assertEqual(names('```tool_code\nread(path="a.py")\n```')[0], [("read", READ)])
        self.assertEqual(names('Action: read\nAction Input: {"path": "a.py"}')[0],
                         [("read", READ)])

    def test_live_sample_with_mangled_name(self):
        # Живой ответ модели с роутера: китайская шапка, JSON-список и имя
        # без подчёркиваний.
        text = 'toolcalls参数（需要保持原语言不变）： [{"name": "listprocesses", "arguments": "{}"}]'
        for tool in ("list_processes", "mcp__sys__list_processes"):
            specs = [ToolSpec(tool, "", {"type": "object", "properties": {}})]
            calls, _ = textcalls.extract(text, specs)
            self.assertEqual([(c.name, c.arguments) for c in calls], [(tool, {})])
        twins = [ToolSpec(n, "", {}) for n in ("list_processes", "listProcesses")]
        self.assertEqual(textcalls.extract(text, twins)[0], [])

    def test_example_in_the_middle_is_not_a_call(self):
        text = 'Пример: {"name": "read", "arguments": {"path": "a.py"}} — вот так. Всё.'
        self.assertEqual(names(text), ([], text))

    def test_python_with_code_is_not_a_call(self):
        text = '```python\nx = read(path="a.py")\nprint(x)\n```'
        self.assertEqual(names(text)[0], [])


class DiagnoseTest(unittest.TestCase):
    N = ["read", "bash"]

    def test_promises(self):
        for text in ("Сейчас прочитаю файл config.py.", "Давай посмотрим, что внутри:",
                     "Let me check the file.", "Начну с README.\n\nПотом проверю тесты."):
            self.assertEqual(stall.diagnose(text, self.N), "promise", text)

    def test_answers_and_offers(self):
        for text in ("Готово! Тесты зелёные.", "Если хочешь, проверю ещё логи.",
                     "Посмотреть ещё раз?", "Я исправила баг. Теперь всё работает."):
            self.assertEqual(stall.diagnose(text, self.N), "", text)

    def test_broken_and_empty(self):
        self.assertEqual(stall.diagnose('read(path="a.py")', self.N), "broken")
        self.assertEqual(stall.diagnose("<tool_call>что-то странное", self.N), "broken")
        self.assertEqual(stall.diagnose("  ", self.N), "empty")


class NudgeLoopTest(RewindBase):
    def test_promise_gets_a_nudge_and_the_tool_runs(self):
        (self.work / "a.txt").write_text("привет\n", encoding="utf-8")
        agent = self.make_agent([
            {"text": "Сейчас прочитаю файл a.txt."},
            {"tool_calls": [{"name": "read", "arguments": {"path": "a.txt"}}]},
            {"text": "В файле — «привет»."},
        ])
        agent.run_turn("что в a.txt?")
        kinds = [e.get("type") for e in self.events]
        self.assertIn("harness.nudge", kinds)
        self.assertIn("tool.call", kinds)
        self.assertEqual(agent._history[-1]["content"], "В файле — «привет».")
        nudges = [m for m in agent._history if harness_only(m.get("content"))]
        self.assertEqual(len(nudges), 1)
        # Продолженная сессия видит ту же подсказку на том же месте.
        records = [{"kind": "command", "data": {"type": "user.message",
                                                "text": "что в a.txt?"}}]
        records += [{"kind": "event", "data": e} for e in self.events]
        roles = [m["role"] for m in build_state(records)["history"]]
        live = [m["role"] for m in agent._history if m.get("role") != "system"]
        self.assertEqual(roles, live)

    def test_limit_and_off_switch(self):
        agent = self.make_agent([{"text": "Сейчас проверю."}] * 5)
        agent.run_turn("проверь")
        self.assertEqual(sum(e.get("type") == "harness.nudge" for e in self.events), 2)
        self.events.clear()
        self.settings.set("agent.stall_nudges", 0)
        agent.run_turn("ещё раз")
        self.assertNotIn("harness.nudge", [e.get("type") for e in self.events])

    def test_undo_skips_the_nudge(self):
        agent = self.make_agent([{"text": "Сейчас проверю."}, {"text": "Всё в порядке."}])
        agent.run_turn("проверь")
        agent.handle_command({"type": "undo"})
        self.assertEqual(agent._turn, 0)
        self.assertFalse(any(harness_only(m.get("content")) for m in agent._history))


if __name__ == "__main__":
    unittest.main()
