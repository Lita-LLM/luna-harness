"""Стык ЗАДАЧ 10 и 13: откат и сжатие переживают продолжение сессии."""
import unittest

from luna.core.sessions import build_state


def cmd(text):
    return {"kind": "command", "data": {"type": "user.message", "text": text}}


def ev(**data):
    return {"kind": "event", "data": data}


def turn(text, answer):
    return [cmd(text), ev(type="turn.start"), ev(type="agent.text", text=answer),
            ev(type="agent.text.done"), ev(type="turn.done", reason="done")]


class BuildStateTest(unittest.TestCase):
    def test_plain_turns_numbered(self):
        state = build_state(turn("раз", "1") + turn("два", "2"))
        self.assertEqual(state["turn"], 2)
        self.assertEqual([t for t, _ in state["marks"]], [1, 2])
        self.assertEqual(state["history"][state["marks"][1][1]]["content"], "два")

    def test_rewind_snapshot_wins(self):
        snap = {"history": [{"role": "user", "content": "раз"},
                            {"role": "assistant", "content": "1"}],
                "turn": 1, "marks": [[1, 0]]}
        records = turn("раз", "1") + turn("два", "2") + [
            {"kind": "rewind", "data": {"to_turn": 1, "scope": "both", "state": snap}}
        ] + turn("три", "3")
        state = build_state(records)
        texts = [m["content"] for m in state["history"]]
        self.assertEqual(texts, ["раз", "1", "три", "3"])
        self.assertEqual(state["turn"], 2)
        self.assertEqual(state["marks"], [(1, 0), (2, 2)])

    def test_old_record_without_snapshot_ignored(self):
        records = turn("раз", "1") + [{"kind": "compact", "data": {"summary": "x"}}]
        self.assertEqual(len(build_state(records)["history"]), 2)


class AgentSnapshotTest(unittest.TestCase):
    def test_marks_shift_with_system_prompt(self):
        from luna.mind.loop import Agent

        agent = Agent.__new__(Agent)
        agent._history = []
        agent._turn_marks = []
        agent.load_history([{"role": "user", "content": "раз"},
                            {"role": "assistant", "content": "1"}])
        self.assertEqual((agent._turn, agent._turn_marks), (1, [(1, 0)]))
        agent._history.insert(0, {"role": "system", "content": "п"})
        agent._turn_marks = [(t, i + 1) for t, i in agent._turn_marks]
        snap = agent._state_snapshot()
        self.assertEqual(snap["marks"], [[1, 0]])
        self.assertEqual(snap["history"][0]["content"], "раз")


if __name__ == "__main__":
    unittest.main()
