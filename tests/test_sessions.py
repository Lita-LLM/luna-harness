"""Сессии на диск (ЗАДАЧА-06, п. 5): журнал JSONL, список, восстановление."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from luna.core.paths import DataDir
from luna.core.sessions import (SessionJournal, find_last, list_sessions,
                                load_records)
from luna.ui.chat import ChatApp


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


class JournalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = DataDir(Path(self.tmp.name))
        self.data.ensure()

    def test_create_writes_meta_and_records(self):
        journal = SessionJournal.create(self.data, "/work/proj")
        journal.append_event({"type": "session.start", "cwd": "/work/proj"})
        journal.append_command({"type": "user.message", "text": "привет"})
        journal.close()
        self.assertTrue(journal.path.exists())
        records = load_records(journal.path)
        self.assertEqual(records[0]["kind"], "meta")
        self.assertEqual(records[0]["cwd"], "/work/proj")
        self.assertEqual(records[1], {"kind": "event",
                                      "data": {"type": "session.start",
                                               "cwd": "/work/proj"}})
        self.assertEqual(records[2]["kind"], "command")

    def test_list_and_find_last_by_cwd(self):
        j1 = SessionJournal.create(self.data, "/work/a")
        j1.close()
        j2 = SessionJournal.create(self.data, "/work/b")
        j2.close()
        items = list_sessions(self.data)
        self.assertEqual(len(items), 2)
        self.assertEqual(find_last(self.data, "/work/a"), j1.path)
        self.assertEqual(find_last(self.data, "/work/b"), j2.path)
        self.assertIsNone(find_last(self.data, "/work/zzz"))

    def test_broken_lines_are_skipped(self):
        journal = SessionJournal.create(self.data, "/work/a")
        journal.close()
        with open(journal.path, "a", encoding="utf-8") as f:
            f.write("{битая строка\n")
            f.write('{"kind": "event", "data": {"type": "notice"}}\n')
        records = load_records(journal.path)
        self.assertEqual(records[-1]["data"]["type"], "notice")


class RestoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = DataDir(Path(self.tmp.name))
        self.data.ensure()

    def run_session(self):
        """Живая сессия: сообщение, тул с подтверждением, ответ, результат."""
        journal = SessionJournal.create(self.data, "/work/proj")
        app = ChatApp(source=StubSource(), journal=journal, cwd="/work/proj")
        app.on_agent_event({"type": "session.start", "model": "m",
                            "mode": "normal", "cwd": "/work/proj"})
        app.submit_text("подтверди чистку")
        app.on_agent_event({"type": "turn.start", "turn": 1})
        app.on_agent_event({"type": "tool.call", "id": "t1", "name": "bash",
                            "args": {"command": "rm -rf build/"}, "intent": "Чищу"})
        app.on_agent_event({"type": "guard.ask", "id": "g1", "tool": "bash",
                            "args": {"command": "rm -rf build/"}, "intent": "Чищу",
                            "risk": "high", "reason": "удаление", "choices": []})
        from luna.ui.keys import Key

        app.handle_key(Key("enter"))
        app.on_agent_event({"type": "tool.result", "id": "t1", "ok": True,
                            "summary": "Код выхода: 0", "output": ""})
        app.on_agent_event({"type": "turn.done", "turn": 1, "reason": "end"})
        journal.close()
        return journal

    def test_restore_rebuilds_feed_without_resending(self):
        journal = self.run_session()
        records = load_records(journal.path)
        source = StubSource()
        app2 = ChatApp(source=source, cwd="/work/proj")
        app2.restore(records)
        kinds = [e.kind for e in app2.entries]
        self.assertIn("user", kinds)
        self.assertIn("tool", kinds)
        guard = next(e for e in app2.entries if e.kind == "guard")
        self.assertEqual(guard.status, "yes")       # решение применено из журнала
        self.assertEqual(guard.guard_of, "t1")      # и связь с тулом на месте
        tool = next(e for e in app2.entries if e.kind == "tool")
        self.assertEqual(tool.status, "done")
        self.assertFalse(app2.busy)
        self.assertEqual(source.commands, [])       # источнику ничего не ушло

    def test_restore_does_not_write_to_journal(self):
        journal = self.run_session()
        records = load_records(journal.path)
        journal2 = SessionJournal.create(self.data, "/work/proj")
        app2 = ChatApp(source=StubSource(), journal=journal2, cwd="/work/proj")
        app2.restore(records)
        journal2.close()
        written = load_records(journal2.path)
        self.assertEqual([r["kind"] for r in written], ["meta"])

    def test_pending_guard_restored_as_interrupted(self):
        journal = SessionJournal.create(self.data, "/work/proj")
        app = ChatApp(source=StubSource(), journal=journal, cwd="/work/proj")
        app.on_agent_event({"type": "guard.ask", "id": "g1", "tool": "bash",
                            "args": {"command": "ls"}, "intent": "",
                            "risk": "", "reason": "", "choices": []})
        journal.close()
        app2 = ChatApp(source=StubSource(), cwd="/work/proj")
        app2.restore(load_records(journal.path))
        guard = next(e for e in app2.entries if e.kind == "guard")
        self.assertEqual(guard.status, "прервано")
        self.assertIsNone(app2.pending_guard)  # экран не ждёт ответа в пустоту

    def test_sessions_command_lists_disk(self):
        journal = SessionJournal.create(self.data, "/work/proj")
        journal.close()
        app = ChatApp(source=StubSource(), cwd="/work/proj")
        with mock.patch.dict(os.environ, {"LUNA_HOME": str(self.data.root)}):
            app.submit_text("/sessions")
        help_entry = next(e for e in app.entries if e.kind == "help")
        self.assertIn("/work/proj", help_entry.text)
        self.assertIn("записей:", help_entry.text)


if __name__ == "__main__":
    unittest.main()
