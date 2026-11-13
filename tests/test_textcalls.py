import unittest

from luna.router import textcalls
from luna.router.types import ChatResult, ToolSpec

TOOLS = [
    ToolSpec("read", "", {"type": "object", "properties": {
        "path": {"type": "string"}, "limit": {"type": "integer"}}}),
    ToolSpec("bash", "", {"type": "object", "properties": {"command": {"type": "string"}}}),
]


class ExtractTest(unittest.TestCase):
    def test_qwen_xml_full(self):
        text = ("Сначала посмотрю файл.\n<tool_call>\n<function=read>\n<parameter=path>\n"
                "a.py\n</parameter>\n<parameter=limit>\n20\n</parameter>\n</function>\n</tool_call>")
        calls, rest = textcalls.extract(text, TOOLS)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].name, "read")
        self.assertEqual(calls[0].arguments, {"path": "a.py", "limit": 20})
        self.assertEqual(rest, "Сначала посмотрю файл.")

    def test_qwen_xml_unclosed(self):
        # Сервер срезал стоп-токен: закрывающих тегов нет.
        calls, _ = textcalls.extract("<tool_call><function=read><parameter=path>b.py", TOOLS)
        self.assertEqual(calls[0].arguments, {"path": "b.py"})

    def test_string_param_keeps_digits(self):
        calls, _ = textcalls.extract("<function=bash><parameter=command>42</parameter></function>",
                                     TOOLS)
        self.assertEqual(calls[0].arguments, {"command": "42"})

    def test_hermes_json_two_calls(self):
        text = ('<tool_call>{"name": "read", "arguments": {"path": "a"}}</tool_call>\n'
                '<tool_call>{"name": "bash", "arguments": "{\\"command\\": \\"ls\\"}"}</tool_call>')
        calls, rest = textcalls.extract(text, TOOLS)
        self.assertEqual([c.name for c in calls], ["read", "bash"])
        self.assertEqual(calls[1].arguments, {"command": "ls"})
        self.assertEqual(rest, "")

    def test_invoke_xml(self):
        text = ('<function_calls><invoke name="read"><parameter name="path">c.py</parameter>'
                '</invoke></function_calls>')
        calls, rest = textcalls.extract(text, TOOLS)
        self.assertEqual(calls[0].arguments, {"path": "c.py"})
        self.assertEqual(rest, "")

    def test_deepseek_tokens(self):
        text = ("<｜tool▁calls▁begin｜><｜tool▁call▁begin｜>read<｜tool▁sep｜>"
                '{"path": "d.py"}<｜tool▁call▁end｜><｜tool▁calls▁end｜>')
        calls, rest = textcalls.extract(text, TOOLS)
        self.assertEqual(calls[0].arguments, {"path": "d.py"})
        self.assertEqual(rest, "")

    def test_unknown_tool_ignored(self):
        calls, rest = textcalls.extract("<function=rm_rf><parameter=x>1</parameter></function>",
                                        TOOLS)
        self.assertEqual(calls, [])

    def test_plain_talk_is_not_a_call(self):
        calls, _ = textcalls.extract("Вызову read для a.py, потом bash.", TOOLS)
        self.assertEqual(calls, [])

    def test_intent_split(self):
        text = '<tool_call>{"name": "read", "arguments": {"path": "a", "intent": "гляну"}}</tool_call>'
        calls, _ = textcalls.extract(text, TOOLS)
        self.assertEqual(calls[0].arguments, {"path": "a"})
        self.assertEqual(calls[0].intent, "гляну")


class RescueTest(unittest.TestCase):
    def test_from_thinking(self):
        res = ChatResult(thinking="надо прочитать\n<tool_call><function=read><parameter=path>a")
        textcalls.rescue(res, TOOLS)
        self.assertEqual(res.tool_calls[0].name, "read")
        self.assertEqual(res.thinking, "надо прочитать")
        self.assertTrue(res.notes)

    def test_draft_in_thinking_is_ignored(self):
        res = ChatResult(thinking="<tool_call><function=bash><parameter=command>rm -rf x"
                                  "</parameter></function></tool_call>\nнет, лучше иначе.\n"
                                  "<tool_call><function=read><parameter=path>a</parameter>"
                                  "</function></tool_call>\n")
        textcalls.rescue(res, TOOLS)
        self.assertEqual([c.name for c in res.tool_calls], ["read"])
        self.assertIn("rm -rf", res.thinking)  # черновик остался мыслью

    def test_thinking_ending_in_talk_gives_nothing(self):
        res = ChatResult(thinking="<function=bash><parameter=command>ls</parameter></function>"
                                  "\nхотя нет, сначала спрошу.")
        textcalls.rescue(res, TOOLS)
        self.assertEqual(res.tool_calls, [])

    def test_native_calls_win(self):
        from luna.router.types import ToolCall

        res = ChatResult(text="<function=read><parameter=path>a</parameter></function>",
                         tool_calls=[ToolCall("1", "bash", {})])
        textcalls.rescue(res, TOOLS)
        self.assertEqual(len(res.tool_calls), 1)
        self.assertIn("<function=read>", res.text)


if __name__ == "__main__":
    unittest.main()
