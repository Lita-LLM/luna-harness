"""Вставки, не дошедшие до прерванного хода, /undo со вставками в ленте,
/redo для ленты, сборка истории из журнала и стрелки по перенесённым строкам."""

import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from luna.core.paths import DataDir
from luna.core.secrets import Secrets
from luna.core.sessions import build_state
from luna.core.settings import Settings
from luna.mind.loop import Agent
from luna.router.types import ChatResult, Usage
from luna.ui.chat import ChatApp
from luna.ui.editor import TextEditor as Editor


class StubSource:
    def __init__(self):
        self.commands = []

    def start(self):
        pass

    def handle_command(self, cmd):
        self.commands.append(cmd)

    def poll(self, now=None):
        return []

    def has_pending(self):
        return False


def make_app(case):
    """Чат во временном доме (case — TempHomeBase): настоящий ~/.luna не трогаем."""
    return ChatApp(source=StubSource(), settings=case.settings,
                   cwd=str(case.work), data=DataDir(case.tmp / "home"))


class BlockingRouter:
    """Первый вызов ждёт освобождения — ход «идёт», пока тест не отпустит."""

    def __init__(self, texts):
        self.texts = list(texts)
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls: list = []

    def chat(self, messages, tools=None, *, role="main", ref="", on_event=None):
        self.calls.append([dict(m) for m in messages])
        idx = len(self.calls) - 1
        if idx == 0:
            self.entered.set()
            self.release.wait(timeout=10)
        text = self.texts[idx] if idx < len(self.texts) else "готово"
        return ChatResult(model="fake", text=text, usage=Usage(1, 1))


class TempHomeBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="luna-carry-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.work = self.tmp / "work"
        self.work.mkdir()
        old = os.environ.get("LUNA_HOME")
        os.environ["LUNA_HOME"] = str(self.tmp / "home")
        self.addCleanup(lambda: os.environ.__setitem__("LUNA_HOME", old) if old is not None
                        else os.environ.pop("LUNA_HOME", None))
        self.settings = Settings(home=self.tmp / "home", cwd=self.work)
        self.secrets = Secrets(self.tmp / "home" / "secrets.json")
        self.events: list = []


class CarryAgentTest(TempHomeBase):
    def test_vstavka_prervannogo_hoda_idet_v_sleduyushchiy(self):
        router = BlockingRouter(["прерван", "ответ"])
        agent = Agent(settings=self.settings, secrets=self.secrets,
                      router=router, cwd=self.work)
        agent.publish = self.events.append
        worker = threading.Thread(target=lambda: agent.run_turn("старт"), daemon=True)
        worker.start()
        self.assertTrue(router.entered.wait(5))
        agent.handle_command({"type": "user.message", "text": "строка раз",
                              "delivery": "inject"})
        agent.handle_command({"type": "user.message", "text": "строка два",
                              "delivery": "inject"})
        agent.interrupt()
        router.release.set()
        worker.join(timeout=5)
        self.assertTrue(any(e.get("type") == "user.carry" for e in self.events))
        agent.run_turn("ну что")
        last_user = [m for m in router.calls[-1] if m.get("role") == "user"][-1]
        self.assertEqual(last_user["content"], "строка раз\n\nстрока два\n\nну что")
        self.assertEqual(agent._turn, 2)


class FeedUndoRedoTest(TempHomeBase):
    def test_vstavki_ne_schitayutsya_hodami(self):
        app = make_app(self)
        app.submit_text("ход один")
        app.on_agent_event({"type": "turn.done", "turn": 1, "reason": "end"})
        app.submit_text("ход два")
        for line in ("вставка а", "вставка б", "вставка в"):
            app.submit_text(line)  # busy: уходят вставками
        app.on_agent_event({"type": "turn.done", "turn": 2, "reason": "end"})
        app.on_agent_event({"type": "session.rewound", "turn": 1, "scope": "both"})
        users = [e.text for e in app.entries if e.kind == "user"]
        self.assertEqual(users, ["ход один"])
        self.assertEqual(app.editor.text, "ход два")
        app.on_agent_event({"type": "session.redone", "turn": 2})
        users = [e.text for e in app.entries if e.kind == "user"]
        self.assertEqual(users[:2], ["ход один", "ход два"])
        self.assertIn("вставка в", users)
        self.assertEqual(app.editor.text, "")

    def test_pustoy_enter_dosylaet_perenos(self):
        app = make_app(self)
        app.on_agent_event({"type": "user.carry", "count": 2})
        app.submit_text("")
        sent = [c for c in app.source.commands if c.get("type") == "user.message"]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["text"], "")


def _cmd(text, delivery=None):
    data = {"type": "user.message", "text": text}
    if delivery:
        data["delivery"] = delivery
    return {"kind": "command", "data": data}


def _ev(**data):
    return {"kind": "event", "data": data}


class BuildStateTest(unittest.TestCase):
    def test_vstavki_i_perenos(self):
        records = [
            _cmd("раз"), _ev(type="turn.start", turn=1),
            _cmd("вставка", "inject"),
            _ev(type="agent.text", text="шаг"), _ev(type="agent.text.done"),
            _ev(type="user.inject", text="вставка"),
            _ev(type="agent.text", text="ответ"), _ev(type="agent.text.done"),
            _ev(type="turn.done", turn=1),
            _cmd("два"), _ev(type="turn.start", turn=2),
            _cmd("не дошла", "inject"),
            _ev(type="turn.done", turn=2, reason="interrupted"),
        ]
        state = build_state(records)
        self.assertEqual(state["turn"], 2)
        users = [m["content"] for m in state["history"] if m["role"] == "user"]
        self.assertEqual(users, ["раз", "вставка", "два"])
        self.assertEqual(state["carry"], [{"text": "не дошла"}])

    def test_ochered_posle_hoda(self):
        records = [
            _cmd("раз"), _ev(type="turn.start", turn=1),
            _cmd("потом", "queue"),
            _ev(type="agent.text", text="ок"), _ev(type="agent.text.done"),
            _ev(type="turn.done", turn=1),
            _ev(type="turn.start", turn=2),
            _ev(type="turn.done", turn=2),
        ]
        state = build_state(records)
        self.assertEqual(state["turn"], 2)
        self.assertEqual([m["role"] for m in state["history"]],
                         ["user", "assistant", "user"])


class VisualArrowsTest(unittest.TestCase):
    def test_strelki_po_perenosu(self):
        ed = Editor()
        ed.set_text("abcdefghij")  # при ширине 4: abcd / efgh / ij
        self.assertTrue(ed.up_visual(4))
        self.assertEqual(ed.cursor, 6)
        self.assertTrue(ed.up_visual(4))
        self.assertEqual(ed.cursor, 2)
        self.assertFalse(ed.up_visual(4))  # выше некуда — история
        self.assertTrue(ed.down_visual(4))
        self.assertEqual(ed.cursor, 6)

    def test_klik_v_pozitsiyu(self):
        ed = Editor()
        ed.set_text("abcdefghij")
        self.assertEqual(ed.offset_at(4, 1, 1), 5)
        self.assertEqual(ed.offset_at(4, 0, 99), 3)  # конец переноса — на своей строке


if __name__ == "__main__":
    unittest.main()
