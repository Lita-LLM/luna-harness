"""ЗАДАЧА-10: память при продолжении, выбиралка, настоящие пути, манеры."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from luna.core.paths import DataDir
from luna.core.secrets import Secrets
from luna.core.sessions import (SessionJournal, build_history, find_by_prefix,
                                list_sessions, load_records)
from luna.core.settings import Settings
from luna.devtools.fake_llm import FakeModelClient, seen_requests
from luna.mind.loop import Agent
from luna.mind.source import AgentSource
from luna.ui.chat import ChatApp
from luna.ui.keys import Key
from luna.ui.picker import Picker, PickerItem


class FakeRouter:
    """Тот же контракт, что у Router.chat, но внутрипроцессно и по сценарию."""

    def __init__(self, script):
        self.client = FakeModelClient(script=list(script))

    def chat(self, messages, tools=None, *, role="main", ref="", on_event=None):
        return self.client.chat("script", messages, tools,
                                on_event or (lambda k, t: None))


class StubSource:
    """Источник-стаб: команды пишет, историю принимает и хранит."""

    def __init__(self):
        self.commands = []
        self.history = None

    def start(self):
        pass

    def handle_command(self, cmd):
        self.commands.append(cmd)

    def poll(self, now=None):
        return []

    def has_pending(self):
        return False

    def load_history(self, messages):
        self.history = messages


def key(char):
    return Key(char, char=char)


# --- 1. build_history ---------------------------------------------------------


class BuildHistoryTest(unittest.TestCase):
    def records(self):
        return [
            {"kind": "meta", "session": "s1", "cwd": "/w", "started": 1.0},
            {"kind": "command", "data": {"type": "user.message", "text": "прочитай файл"}},
            {"kind": "event", "data": {"type": "turn.start", "turn": 1}},
            {"kind": "event", "data": {"type": "agent.text", "text": "Смотрю. "}},
            {"kind": "event", "data": {"type": "agent.text.done"}},
            {"kind": "event", "data": {"type": "tool.call", "id": "t1", "name": "read",
                                       "args": {"path": "a.txt"}, "intent": "читать"}},
            {"kind": "event", "data": {"type": "tool.result", "id": "t1", "ok": True,
                                       "summary": "ok", "output": "содержимое"}},
            {"kind": "event", "data": {"type": "agent.text", "text": "Готово."}},
            {"kind": "event", "data": {"type": "agent.text.done"}},
            {"kind": "event", "data": {"type": "turn.done", "turn": 1, "reason": "end"}},
        ]

    def test_history_shape(self):
        history = build_history(self.records())
        self.assertEqual([m["role"] for m in history],
                         ["user", "assistant", "tool", "assistant"])
        call_msg = history[1]
        self.assertEqual(call_msg["content"], "Смотрю. ")
        self.assertEqual(call_msg["tool_calls"][0]["name"], "read")
        self.assertEqual(call_msg["tool_calls"][0]["arguments"], {"path": "a.txt"})
        self.assertEqual(history[2]["tool_call_id"], "t1")
        self.assertEqual(history[2]["content"], "содержимое")
        self.assertEqual(history[3]["content"], "Готово.")

    def test_dangling_call_marked_interrupted(self):
        records = self.records()
        # Сессия закрылась на подтверждении: результата у вызова нет.
        cut = [r for r in records
               if not (r.get("data") or {}).get("type") == "tool.result"]
        history = build_history(cut)
        self.assertEqual(history[-1]["role"], "tool")
        self.assertEqual(history[-1]["tool_call_id"], "t1")
        self.assertIn("Прервано", history[-1]["content"])

    def test_two_calls_one_step_grouped(self):
        records = [
            {"kind": "command", "data": {"type": "user.message", "text": "сразу два"}},
            {"kind": "event", "data": {"type": "agent.text.done"}},
            {"kind": "event", "data": {"type": "tool.call", "id": "a", "name": "read",
                                       "args": {}, "intent": ""}},
            {"kind": "event", "data": {"type": "tool.result", "id": "a", "ok": True,
                                       "summary": "", "output": "1"}},
            {"kind": "event", "data": {"type": "tool.call", "id": "b", "name": "glob",
                                       "args": {}, "intent": ""}},
            {"kind": "event", "data": {"type": "tool.result", "id": "b", "ok": True,
                                       "summary": "", "output": "2"}},
        ]
        history = build_history(records)
        self.assertEqual([m["role"] for m in history],
                         ["user", "assistant", "tool", "tool"])
        self.assertEqual(len(history[1]["tool_calls"]), 2)


# --- 1. память модели после продолжения ---------------------------------------


class AgentMemoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="luna-z10-"))
        self.work = self.tmp / "work"
        self.work.mkdir()
        (self.work / "a.txt").write_text("секретный текст файла\n", encoding="utf-8")
        self._old_home = os.environ.get("LUNA_HOME")
        os.environ["LUNA_HOME"] = str(self.tmp / "home")
        self.settings = Settings(home=self.tmp / "home", cwd=self.work)
        self.secrets = Secrets(self.tmp / "home" / "secrets.json")
        self.data = DataDir(self.tmp / "home")
        self.data.ensure()

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("LUNA_HOME", None)
        else:
            os.environ["LUNA_HOME"] = self._old_home

    def test_model_remembers_after_continue(self):
        journal = SessionJournal.create(self.data, str(self.work))
        agent = Agent(settings=self.settings, secrets=self.secrets,
                      router=FakeRouter([
                          {"tool_calls": [{"name": "read",
                                           "arguments": {"path": "a.txt"},
                                           "intent": "прочитать файл"}]},
                          {"text": "В файле секретный текст."},
                      ]),
                      cwd=self.work, mode="auto")
        agent.listen(journal.append_event)
        journal.append_command({"type": "user.message", "text": "прочитай a.txt"})
        self.assertEqual(agent.run_turn("прочитай a.txt"), "end")
        journal.close()

        records = load_records(journal.path)
        history = build_history(records)
        agent2 = Agent(settings=self.settings, secrets=self.secrets,
                       router=FakeRouter([{"text": "Помню всё."}]),
                       cwd=self.work, mode="auto")
        agent2.load_history(history)
        before = len(seen_requests())
        self.assertEqual(agent2.run_turn("что было в файле?"), "end")
        request = seen_requests()[before]
        roles = [m["role"] for m in request["messages"]]
        self.assertEqual(roles[:6],
                         ["system", "user", "assistant", "tool", "assistant", "user"])
        self.assertEqual(request["messages"][1]["content"], "прочитай a.txt")
        self.assertIn("секретный текст", request["messages"][3]["content"])
        self.assertIn("В файле секретный текст.",
                      request["messages"][4]["content"])

    def test_source_stashes_and_rebuilds(self):
        agent = Agent(settings=self.settings, secrets=self.secrets,
                      router=FakeRouter([]), cwd=self.work, mode="auto")
        src = AgentSource(settings=self.settings, cwd=str(self.work), agent=agent)
        src.load_history([{"role": "user", "content": "раньше"}])
        # Агент был жив — источник пересобрал его с историей.
        self.assertIsNot(src._agent, agent)
        self.assertEqual(src._agent._history,
                         [{"role": "user", "content": "раньше"}])


# --- 2. выбиралка как компонент -------------------------------------------------


class PickerTest(unittest.TestCase):
    def make(self):
        items = [PickerItem("a", "Альфа", "первая"),
                 PickerItem("b", "Бета", "вторая"),
                 PickerItem("c", "Гамма", "третья")]
        return Picker("Тест", items, extra={"n": "new"})

    def test_navigation_and_filter(self):
        picker = self.make()
        self.assertEqual(picker.current().key, "a")
        picker.handle_key(Key("down"))
        self.assertEqual(picker.current().key, "b")
        picker.handle_key(Key("pageup"))
        self.assertEqual(picker.current().key, "a")
        for ch in "гам":
            picker.handle_key(key(ch))
        self.assertEqual([it.key for it in picker.filtered()], ["c"])
        self.assertEqual(picker.current().key, "c")
        picker.handle_key(Key("backspace"))
        picker.handle_key(Key("backspace"))
        picker.handle_key(Key("backspace"))
        self.assertEqual(len(picker.filtered()), 3)

    def test_extra_key_only_with_empty_filter(self):
        picker = self.make()
        action = picker.handle_key(key("n"))
        self.assertEqual(action[0], "action")
        self.assertEqual(action[1], "new")

    def test_confirm_and_input_modes(self):
        picker = self.make()
        picker.ask_confirm("Точно?", ("pick", picker.items[1]))
        self.assertEqual(picker.mode, "confirm")
        action = picker.handle_key(Key("enter"))
        self.assertEqual(action, ("confirmed", ("pick", picker.items[1])))
        picker.ask_input("Имя: ", ("new", None))
        for ch in "dry":
            picker.handle_key(key(ch))
        action = picker.handle_key(Key("enter"))
        self.assertEqual(action, ("input", "dry", ("new", None)))
        self.assertEqual(picker.mode, "list")


# --- 2. переключение сессий в чате -----------------------------------------------


class ChatSwitchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = DataDir(Path(self.tmp.name))
        self.data.ensure()

    def make_session(self, text):
        journal = SessionJournal.create(self.data, "/work/proj")
        app = ChatApp(source=StubSource(), journal=journal, cwd="/work/proj",
                      data=self.data)
        app.on_agent_event({"type": "session.start", "model": "m",
                            "mode": "normal", "cwd": "/work/proj"})
        app.submit_text(text)
        app.on_agent_event({"type": "turn.start", "turn": 1})
        app.on_agent_event({"type": "agent.text", "text": "ответ на " + text})
        app.on_agent_event({"type": "agent.text.done"})
        app.on_agent_event({"type": "turn.done", "turn": 1, "reason": "end"})
        journal.close()
        return journal

    def test_pick_switches_feed_memory_and_journal(self):
        j1 = self.make_session("первая сессия")
        j2 = self.make_session("вторая сессия")
        source = StubSource()
        app = ChatApp(source=source, journal=j1, cwd="/work/proj", data=self.data)
        app.submit_text("/sessions")
        self.assertIsNotNone(app.picker)
        self.assertEqual(len(app.picker.items), 2)
        app.handle_key(Key("enter"))  # свежие сверху: j2 — первая в списке
        self.assertIsNone(app.picker)
        self.assertEqual(str(app.journal.path), str(j2.path))
        users = [e.text for e in app.entries if e.kind == "user"]
        self.assertIn("вторая сессия", users)
        self.assertIsNotNone(source.history)
        roles = [m["role"] for m in source.history]
        self.assertEqual(roles, ["user", "assistant"])
        app.submit_text("продолжаем")
        records = load_records(j2.path)
        commands = [r["data"] for r in records if r.get("kind") == "command"]
        self.assertEqual(commands[-1]["text"], "продолжаем")

    def test_busy_switch_asks_then_interrupts(self):
        j1 = self.make_session("первая")
        j2 = self.make_session("вторая")
        source = StubSource()
        app = ChatApp(source=source, journal=j1, cwd="/work/proj", data=self.data)
        app.on_agent_event({"type": "turn.start", "turn": 1})
        self.assertTrue(app.busy)
        app.submit_text("/sessions")
        app.handle_key(Key("enter"))
        self.assertIsNotNone(app.picker)
        self.assertEqual(app.picker.mode, "confirm")
        app.handle_key(Key("enter"))  # подтвердить прерывание
        kinds = [c["type"] for c in source.commands]
        self.assertIn("interrupt", kinds)
        app.on_agent_event({"type": "turn.done", "turn": 1, "reason": "interrupted"})
        self.assertEqual(str(app.journal.path), str(j2.path))

    def test_new_and_rename(self):
        j1 = self.make_session("старая")
        source = StubSource()
        journal, _records = SessionJournal.resume(self.data, j1.path)
        app = ChatApp(source=source, journal=journal, cwd="/work/proj",
                      data=self.data)
        app.submit_text("/rename Моё имя")
        items = list_sessions(self.data)
        self.assertEqual(items[0]["title"], "Моё имя")
        app.submit_text("/new")
        self.assertNotEqual(str(app.journal.path), str(j1.path))
        self.assertEqual(source.history, [])
        notices = [e.text for e in app.entries if e.kind == "notice"]
        self.assertTrue(any("Новая сессия" in t for t in notices))

    def test_resume_prefix_lookup(self):
        j1 = self.make_session("по префиксу")
        found = find_by_prefix(self.data, j1.session_id[:4])
        self.assertIsNotNone(found)
        self.assertEqual(str(found["path"]), str(j1.path))
        self.assertIsNone(find_by_prefix(self.data, "zzzz"))


# --- 3. настоящие пути ------------------------------------------------------------


class PathsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="luna-z10-paths-"))
        self._old_home = os.environ.get("LUNA_HOME")
        os.environ["LUNA_HOME"] = str(self.tmp)

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("LUNA_HOME", None)
        else:
            os.environ["LUNA_HOME"] = self._old_home

    def run_main(self, argv):
        from luna.__main__ import main

        out = io.StringIO()
        with redirect_stdout(out):
            rc = main(argv)
        return rc, out.getvalue()

    def test_manners_list_shows_real_paths(self):
        rc, text = self.run_main(["manners"])
        self.assertEqual(rc, 0)
        self.assertIn(str(self.tmp / "manners"), text)
        self.assertIn("Встроенные манеры живут в программе", text)
        self.assertNotIn("LUNA_HOME/manners", text)

    def test_manners_new_creates_file_and_prints_path(self):
        rc, text = self.run_main(["manners", "new", "dry"])
        self.assertEqual(rc, 0)
        target = self.tmp / "manners" / "dry.md"
        self.assertTrue(target.exists())
        self.assertIn(str(target), text)

    def test_help_shows_data_dir(self):
        rc, text = self.run_main(["--help"])
        self.assertEqual(rc, 0)
        self.assertIn("Каталог данных: " + str(self.tmp), text)


# --- 4. манеры в чате и тулом -------------------------------------------------------


class MannersChatTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="luna-z10-manners-"))
        self.work = self.tmp / "work"
        self.work.mkdir()
        self._old_home = os.environ.get("LUNA_HOME")
        os.environ["LUNA_HOME"] = str(self.tmp / "home")
        self.settings = Settings(home=self.tmp / "home", cwd=self.work)

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("LUNA_HOME", None)
        else:
            os.environ["LUNA_HOME"] = self._old_home

    def test_picker_enter_applies_and_n_creates(self):
        app = ChatApp(settings=self.settings, source=StubSource(),
                      cwd=str(self.work))
        app.submit_text("/manners")
        self.assertIsNotNone(app.picker)
        first = app.picker.items[0]
        app.handle_key(Key("enter"))
        self.assertIsNone(app.picker)
        self.assertEqual(self.settings.get("agent.manner"), first.key)
        app.submit_text("/manners")
        app.handle_key(key("n"))
        self.assertEqual(app.picker.mode, "input")
        for ch in "dry":
            app.handle_key(key(ch))
        app.handle_key(Key("enter"))
        self.assertTrue((self.tmp / "home" / "manners" / "dry.md").exists())
        keys = [it.key for it in app.picker.items]
        self.assertIn("dry", keys)

    def test_builtin_editable_path_creates_home_copy(self):
        from luna.mind import manners

        path = manners.editable_path(self.settings, "luna", self.work)
        self.assertEqual(path, self.tmp / "home" / "manners" / "luna.md")
        self.assertTrue(path.exists())

    def test_agent_creates_and_uses_manner_by_words(self):
        (self.work / ".luna").mkdir(exist_ok=True)
        secrets = Secrets(self.tmp / "home" / "secrets.json")
        agent = Agent(settings=self.settings, secrets=secrets,
                      router=FakeRouter([
                          {"tool_calls": [{"name": "manner",
                                           "arguments": {"action": "create",
                                                         "name": "dry",
                                                         "title": "Сухая",
                                                         "description": "посуше",
                                                         "text": "Коротко. Без шуток."},
                                           "intent": "сделать манеру посуше"}]},
                          {"tool_calls": [{"name": "manner",
                                           "arguments": {"action": "use", "name": "dry"},
                                           "intent": "включить сухую"}]},
                          {"text": "Готово."},
                          {"text": "Отвечаю сухо."},
                      ]),
                      cwd=self.work, mode="auto")
        reason = agent.run_turn("сделай манеру посуше и без шуток и включи её")
        self.assertEqual(reason, "end")
        self.assertTrue((self.tmp / "home" / "manners" / "dry.md").exists())
        self.assertEqual(self.settings.get("agent.manner"), "dry")
        agent.run_turn("расскажи о задаче")
        system = agent._history[0]["content"]
        self.assertIn("Манера речи: «Сухая»", system)
        self.assertIn("Коротко. Без шуток.", system)

    def test_manner_text_edit_applies_next_turn(self):
        from luna.mind import manners

        manners.create(self.settings, "dry", "Сухая", "", "Коротко.")
        manners.choose(self.settings, "dry", self.work)
        secrets = Secrets(self.tmp / "home" / "secrets.json")
        agent = Agent(settings=self.settings, secrets=secrets,
                      router=FakeRouter([{"text": "раз"}, {"text": "два"}]),
                      cwd=self.work, mode="auto")
        agent.run_turn("привет")
        self.assertIn("Коротко.", agent._history[0]["content"])
        manners.edit(self.settings, "dry", self.work, text="Совсем сухо, точками.")
        agent.run_turn("ещё")
        self.assertIn("Совсем сухо, точками.", agent._history[0]["content"])


# --- снимки -------------------------------------------------------------------------


class SnapshotSessionsTest(unittest.TestCase):
    def test_sessions_snapshot_writes_html(self):
        from luna.devtools.snapshot import run_snapshot

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "sessions.html"
            rc = run_snapshot(["sessions", "-o", str(out)])
            self.assertEqual(rc, 0)
            page = out.read_text(encoding="utf-8")
            self.assertIn("Сессии", page)
            self.assertIn("Разбор логов", page)


if __name__ == "__main__":
    unittest.main()
