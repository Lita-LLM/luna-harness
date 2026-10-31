"""Переходник настоящего агента к чату: поток хода, ожидание подтверждения, прерывание."""

import time
import unittest

from luna.mind.source import AgentSource


class _StubAgent:
    def __init__(self):
        self.listeners = []
        self.source = None
        self.answers = []
        self.interrupted = False

    def listen(self, fn):
        self.listeners.append(fn)

    def _model_label(self):
        return "stub"

    def interrupt(self):
        self.interrupted = True

    def handle_command(self, cmd):
        for fn in self.listeners:
            fn({"type": "turn.start", "turn": 1})
        ans = self.source._ask_guard({"type": "guard.ask", "id": "g1"})
        self.answers.append(ans)
        for fn in self.listeners:
            fn({"type": "turn.done", "turn": 1, "reason": "end"})


def _wait(cond, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


class AgentSourceTest(unittest.TestCase):
    def make(self):
        agent = _StubAgent()
        src = AgentSource(agent=agent)
        agent.source = src
        src.start()
        return agent, src

    def test_turn_waits_for_guard_answer(self):
        agent, src = self.make()
        src.handle_command({"type": "user.message", "text": "привет"})
        self.assertTrue(_wait(lambda: "g1" in src._waits))
        self.assertTrue(src.has_pending())
        src.handle_command({"type": "guard.answer", "id": "g1", "decision": "yes"})
        self.assertTrue(_wait(lambda: not src._busy()))
        self.assertEqual(agent.answers[0]["decision"], "yes")
        kinds = [e["type"] for e in src.poll()]
        self.assertEqual(kinds, ["turn.start", "turn.done"])

    def test_interrupt_releases_waiting_turn(self):
        agent, src = self.make()
        src.handle_command({"type": "user.message", "text": "привет"})
        self.assertTrue(_wait(lambda: "g1" in src._waits))
        src.handle_command({"type": "interrupt"})
        self.assertTrue(_wait(lambda: not src._busy()))
        self.assertTrue(agent.interrupted)
        self.assertEqual(agent.answers[0]["decision"], "stop")


if __name__ == "__main__":
    unittest.main()
