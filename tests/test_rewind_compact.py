"""Откат ходов и сжатие контекста на уровне агента (ЗАДАЧА-13):
/undo, /rewind с режимами, /redo, ручная правка не затирается, журнал,
прилив по порогу и вручную, витрины luna try undo / try compact."""

from __future__ import annotations

import os
import shutil
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

    def chat(self, messages, tools=None, *, role="main", ref="", on_event=None):
        return self.client.chat("script", messages, tools,
                                on_event or (lambda k, t: None))


class FakeJournal:
    def __init__(self):
        self.records = []

    def append(self, record):
        self.records.append(record)

    def kinds(self):
        return [r.get("kind") for r in self.records]


class RewindBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="luna-rewind-"))
        self.work = self.tmp / "work"
        self.work.mkdir()
        self._old_home = os.environ.get("LUNA_HOME")
        os.environ["LUNA_HOME"] = str(self.tmp / "home")
        self.settings = Settings(home=self.tmp / "home", cwd=self.work)
        self.secrets = Secrets(self.tmp / "home" / "secrets.json")
        self.events: list = []

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("LUNA_HOME", None)
        else:
            os.environ["LUNA_HOME"] = self._old_home

    def make_agent(self, script, **kwargs) -> Agent:
        kwargs.setdefault("mode", "auto")
        agent = Agent(settings=self.settings, secrets=self.secrets,
                      router=FakeRouter(script), cwd=self.work, **kwargs)
        agent.listen(self.events.append)
        return agent

    def notices(self) -> list:
        return [e.get("text", "") for e in self.events if e.get("type") == "notice"]


def two_turn_script():
    return [
        {"tool_calls": [{"name": "write",
                         "arguments": {"path": "a.txt", "content": "версия 1\n"},
                         "intent": "создать"}]},
        {"text": "Готово 1."},
        {"tool_calls": [
            {"name": "edit",
             "arguments": {"path": "a.txt", "old": "версия 1", "new": "версия 2"},
             "intent": "обновить"},
            {"name": "write",
             "arguments": {"path": "b.txt", "content": "файл b\n"},
             "intent": "второй файл"}]},
        {"text": "Готово 2."},
    ]


class UndoRedoTest(RewindBase):
    def test_undo_restores_files_and_conversation_then_redo(self):
        agent = self.make_agent(two_turn_script())
        agent.run_turn("Создай a.txt")
        agent.run_turn("Обнови a.txt и добавь b.txt")
        a, b = self.work / "a.txt", self.work / "b.txt"
        self.assertEqual(a.read_text(encoding="utf-8"), "версия 2\n")
        agent.handle_command({"type": "undo"})
        self.assertEqual(a.read_text(encoding="utf-8"), "версия 1\n")
        self.assertFalse(b.exists())
        self.assertEqual(agent._turn, 1)
        self.assertEqual(len(agent.turns_overview()), 1)
        self.assertIn("session.rewound", [e["type"] for e in self.events])
        agent.handle_command({"type": "redo"})
        self.assertEqual(a.read_text(encoding="utf-8"), "версия 2\n")
        self.assertTrue(b.exists())
        self.assertEqual(agent._turn, 2)
        self.assertEqual(len(agent.turns_overview()), 2)

    def test_undo_without_turns_is_polite(self):
        agent = self.make_agent([])
        agent.handle_command({"type": "undo"})
        self.assertTrue(any("Откатывать нечего" in t for t in self.notices()))

    def test_rewind_listing(self):
        agent = self.make_agent(two_turn_script())
        agent.run_turn("Создай a.txt")
        agent.handle_command({"type": "rewind"})
        text = "\n".join(self.notices())
        self.assertIn("Ходы сессии", text)
        self.assertIn("Создай a.txt", text)


class ScopeTest(RewindBase):
    def test_talk_only_keeps_files(self):
        agent = self.make_agent(two_turn_script())
        agent.run_turn("Создай a.txt")
        agent.run_turn("Обнови a.txt и добавь b.txt")
        agent.handle_command({"type": "rewind", "turn": 1, "scope": "talk"})
        a = self.work / "a.txt"
        self.assertEqual(a.read_text(encoding="utf-8"), "версия 2\n")  # файлы целы
        self.assertEqual(agent._turn, 1)  # разговор откатился

    def test_files_only_keeps_conversation(self):
        agent = self.make_agent(two_turn_script())
        agent.run_turn("Создай a.txt")
        agent.run_turn("Обнови a.txt и добавь b.txt")
        agent.handle_command({"type": "rewind", "turn": 1, "scope": "files"})
        a = self.work / "a.txt"
        self.assertEqual(a.read_text(encoding="utf-8"), "версия 1\n")
        self.assertEqual(agent._turn, 2)  # разговор на месте
        self.assertEqual(len(agent.turns_overview()), 2)


class ConflictAgentTest(RewindBase):
    def test_manual_edit_survives_undo_without_channel(self):
        agent = self.make_agent(two_turn_script())
        agent.run_turn("Создай a.txt")
        agent.run_turn("Обнови a.txt и добавь b.txt")
        a = self.work / "a.txt"
        a.write_text("правка руками\n", encoding="utf-8")
        agent.handle_command({"type": "undo"})  # канала спросить нет — пропуск
        self.assertEqual(a.read_text(encoding="utf-8"), "правка руками\n")
        self.assertTrue(any("правленые вручную" in t for t in self.notices()))
        self.assertEqual(agent._turn, 1)  # разговор при этом откатился

    def test_manual_edit_overwritten_when_human_says_so(self):
        ask = lambda qid, question, options: options[1]  # «Перезаписать и их»
        agent = self.make_agent(two_turn_script(), ask_user_fn=ask)
        agent.run_turn("Создай a.txt")
        agent.run_turn("Обнови a.txt и добавь b.txt")
        a = self.work / "a.txt"
        a.write_text("правка руками\n", encoding="utf-8")
        agent.handle_command({"type": "undo"})
        self.assertEqual(a.read_text(encoding="utf-8"), "версия 1\n")


class DisabledTest(RewindBase):
    def test_checkpoints_disabled_agent_lives(self):
        self.settings.set("checkpoints.enabled", False)
        agent = self.make_agent(two_turn_script())
        self.assertIsNone(agent._checkpoints)
        agent.run_turn("Создай a.txt")
        agent.run_turn("Обнови a.txt и добавь b.txt")
        agent.handle_command({"type": "undo"})
        # Файлы не откатились (честно сказано), разговор — откатился.
        self.assertEqual((self.work / "a.txt").read_text(encoding="utf-8"),
                         "версия 2\n")
        self.assertEqual(agent._turn, 1)
        self.assertTrue(any("чекпоинты выключены" in t for t in self.notices()))


class JournalTest(RewindBase):
    def test_rewind_and_redo_records(self):
        journal = FakeJournal()
        agent = self.make_agent(two_turn_script())
        agent.journal = journal
        agent.run_turn("Создай a.txt")
        agent.run_turn("Обнови a.txt и добавь b.txt")
        agent.handle_command({"type": "undo"})
        agent.handle_command({"type": "redo"})
        self.assertEqual(journal.kinds(), ["rewind", "redo"])
        rec = journal.records[0]
        self.assertEqual(rec["data"]["to_turn"], 1)
        self.assertEqual(rec["data"]["from_turn"], 2)
        self.assertEqual(rec["data"]["scope"], "both")


def five_turn_script(summary="СВОДКА: цель достигнута, сделано пять ходов."):
    steps = [{"text": "Ответ хода " + str(n) + ". " + "детали " * 10}
             for n in range(1, 6)]
    steps.append({"text": summary})
    return steps


class CompactTest(RewindBase):
    def test_manual_compact_replaces_old_turns(self):
        agent = self.make_agent(five_turn_script())
        for n in range(1, 6):
            agent.run_turn("Сообщение " + str(n))
        agent.ctx.todos = [{"text": "Довести проверку", "status": "active"}]
        before = len(agent._history)
        agent.handle_command({"type": "compact", "hint": "сохрани цель"})
        self.assertLess(len(agent._history), before)
        joined = "\n".join(str(m.get("content", "")) for m in agent._history)
        self.assertIn("Сводка прежнего разговора", joined)
        self.assertIn("СВОДКА: цель достигнута", joined)
        self.assertIn("Довести проверку", joined)  # план — как есть
        self.assertEqual(len(agent._turn_marks), 2)  # последние ходы целы
        compacted = [e for e in self.events if e["type"] == "context.compacted"]
        self.assertEqual(len(compacted), 1)
        self.assertGreater(compacted[0]["before_messages"],
                           compacted[0]["after_messages"])
        self.assertTrue(any("Прилив" in t for t in self.notices()))

    def test_compact_journal_record_has_summary(self):
        journal = FakeJournal()
        agent = self.make_agent(five_turn_script())
        agent.journal = journal
        for n in range(1, 6):
            agent.run_turn("Сообщение " + str(n))
        agent.handle_command({"type": "compact"})
        self.assertEqual(journal.kinds(), ["compact"])
        self.assertIn("СВОДКА", journal.records[0]["data"]["summary"])

    def test_auto_compact_by_threshold(self):
        self.settings.set("model.context_tokens", 1000)
        self.settings.set("compact.threshold_pct", 30)
        steps = [{"text": "Ход " + str(n), "usage": {"input": 400, "output": 10}}
                 for n in range(1, 4)]
        steps.append({"text": "Автосводка."})
        agent = self.make_agent(steps)
        for n in range(1, 4):
            agent.run_turn("Сообщение " + str(n))
        compacted = [e for e in self.events if e["type"] == "context.compacted"]
        self.assertEqual(len(compacted), 1)  # прилив сам, после третьего хода
        self.assertEqual(len(agent._turn_marks), 2)

    def test_compact_too_short_history(self):
        agent = self.make_agent([{"text": "привет"}])
        agent.run_turn("Привет")
        agent.handle_command({"type": "compact"})
        self.assertTrue(any("Сжимать пока нечего" in t for t in self.notices()))
        self.assertNotIn("context.compacted", [e["type"] for e in self.events])


@unittest.skipUnless(shutil.which("git"), "нет git")
@unittest.skipIf(sys.platform == "win32", "команда сценария — POSIX shell")
class BashGitUndoTest(RewindBase):
    def test_bash_change_rolls_back_files_stay_honest(self):
        import subprocess

        work = self.work
        subprocess.run(["git", "init", "-q"], cwd=work, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=work, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=work, check=True)
        a = work / "a.txt"
        a.write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=work, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=work, check=True)
        script = [
            {"tool_calls": [{"name": "bash",
                             "arguments": {"command": "printf 'changed\\n' > a.txt"
                                                      " && printf 'x\\n' > new.txt"},
                             "intent": "поменять файл командой"}]},
            {"text": "Сделано."},
        ]
        agent = self.make_agent(script)
        agent.run_turn("Поменяй a.txt через bash")
        self.assertEqual(a.read_text(encoding="utf-8"), "changed\n")
        agent.handle_command({"type": "undo"})
        self.assertEqual(a.read_text(encoding="utf-8"), "base\n")  # git вернул
        self.assertTrue((work / "new.txt").exists())  # untracked — честно остался


class TryTest(unittest.TestCase):
    def test_try_undo_and_compact(self):
        from luna.devtools.rewind_try import run_compact_try, run_undo_try

        self.assertEqual(run_undo_try(out=lambda *a: None), 0)
        self.assertEqual(run_compact_try(out=lambda *a: None), 0)
