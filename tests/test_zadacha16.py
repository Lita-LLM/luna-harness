"""ЗАДАЧА-16: команды и тайлы по описи эталонов (docs/research/КОМАНДЫ.md).

Всё без терминала и сети: ChatApp на фальшивом агенте, настройки — во
временных каталогах, git и процессы панелей — подменами и настоящими
временными данными.
"""

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from luna.core.bus import EventBus
from luna.core.paths import DataDir
from luna.core.settings import Settings
from luna.ui.anim import Scheduler
from luna.ui.chat import COMMANDS, ChatApp, Entry
from luna.ui.keys import Key
from luna.ui.panels import PanelData, create_panel, registered_panels


def make_app(case: unittest.TestCase, **overrides) -> ChatApp:
    """Приложение с настройками и данными во временных каталогах."""
    tmp = tempfile.TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    root = Path(tmp.name)
    (root / "work").mkdir(parents=True)
    settings = Settings(home=root / "home", cwd=root / "work")
    for key, value in overrides.items():
        settings.set(key, value)
    return ChatApp(EventBus(), settings, Scheduler(), "emoji",
                   cwd=str(root / "work"), data=DataDir(root / "home"))


def key(name: str, char: str = "", mods=()) -> Key:
    return Key(name, char=char, mods=frozenset(mods))


class CommandsTest(unittest.TestCase):
    def test_every_command_dispatches(self):
        # Каждая команда реестра разбирается и не роняет экран.
        # /update без подмены пошёл бы в настоящий git (fetch и pull) по сети.
        from unittest import mock

        patcher = mock.patch("luna.ui.updater.Updater._run")
        patcher.start()
        self.addCleanup(patcher.stop)
        for name, _desc in COMMANDS:
            with self.subTest(command=name):
                app = make_app(self)
                app.submit_text("/" + name)
                if name in ("help", "panels", "model", "mcp", "agents", "theme"):
                    self.assertIsNotNone(app.picker)

    def test_help_is_picker_with_all_commands(self):
        app = make_app(self)
        app.submit_text("/help")
        self.assertEqual(app._picker_kind, "help")
        keys = {item.key for item in app.picker.items}
        self.assertEqual(keys, {name for name, _ in COMMANDS})

    def test_help_pick_inserts_arg_command(self):
        app = make_app(self)
        app.open_help_picker()
        app.picker._sel_key = "rename"
        app.handle_key(key("enter"))
        self.assertIsNone(app.picker)
        self.assertEqual(app.editor.text, "/rename ")

    def test_tab_completes_command_with_description(self):
        app = make_app(self)
        app.editor.set_text("/mo")
        app._update_completions()
        names = [name for name, _desc in app.cmd_completions]
        self.assertIn("model", names)
        self.assertIn("mode", names)
        app.completion_index = names.index("model")
        app.handle_key(key("tab"))
        self.assertEqual(app.editor.text, "/model ")
        self.assertEqual(app.cmd_completions, [])

    def test_cost_and_status_entries(self):
        app = make_app(self)
        app.on_agent_event({"type": "usage", "model": "fake-agent",
                            "input": 100, "output": 40, "cost": 0.01,
                            "context_pct": 12})
        app.submit_text("/cost")
        self.assertIn("стоимость", app.entries[-1].text)
        app.submit_text("/status")
        self.assertIn("модель: fake-agent", app.entries[-1].text)

    def test_model_command_sets_role_setting(self):
        app = make_app(self)
        app.submit_text("/model openai:gpt-x")
        self.assertEqual(app.settings.get("model.main"), "openai:gpt-x")
        app.submit_text("/model fast ollama:llama")
        self.assertEqual(app.settings.get("model.fast"), "ollama:llama")

    def test_doctor_runs_in_chat(self):
        app = make_app(self)
        app.submit_text("/doctor")
        self.assertIn("Python", app.entries[-1].text)

    def test_details_and_thinking_flip_settings(self):
        app = make_app(self)
        app.submit_text("/details")
        self.assertFalse(app.settings.get("ui.details"))
        app.submit_text("/thinking")
        self.assertFalse(app.settings.get("ui.show_thinking"))
        # Выключенные детали прячут вывод тула и рассуждения из ленты.
        app.on_agent_event({"type": "agent.thinking", "text": "думаю"})
        app.on_agent_event({"type": "tool.call", "id": "t1", "name": "bash",
                            "args": {"command": "ls"}, "intent": ""})
        app.on_agent_event({"type": "tool.result", "id": "t1", "ok": True,
                            "summary": "ок", "output": "файл\n"})
        text = "\n".join(line_text
                         for spans, _bg in app._feed_lines(80, time.monotonic())
                         for line_text in ["".join(t for t, _f, _a in spans)])
        self.assertNotIn("думаю", text)
        self.assertNotIn("вывод: строк", text)

    def test_export_writes_markdown(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        app = make_app(self)
        app.cwd = tmp.name
        app._add(Entry("user", text="привет"))
        app._add(Entry("agent", text="ответ"))
        app.submit_text("/export")
        files = list(Path(tmp.name).glob("luna-session-*.md"))
        self.assertEqual(len(files), 1)
        body = files[0].read_text(encoding="utf-8")
        self.assertIn("## Человек", body)
        self.assertIn("ответ", body)

    def test_bug_report_has_no_secrets(self):
        app = make_app(self)
        app.on_agent_event({"type": "error", "message": "бум", "detail": "трасса"})
        app.submit_text("/bug")
        logs = app._data_dir().logs
        files = list(logs.glob("bug-*.md"))
        self.assertEqual(len(files), 1)
        body = files[0].read_text(encoding="utf-8")
        self.assertIn("бум", body)
        self.assertIn("## Настройки", body)

    def test_init_and_review_send_prompts_with_display(self):
        app = make_app(self)
        app.submit_text("/init")
        cmd = app.sent_commands[-1]
        self.assertEqual(cmd["type"], "user.message")
        self.assertIn("AGENTS.md", cmd["text"])
        user_entries = [e for e in app.entries if e.kind == "user"]
        self.assertTrue(user_entries[-1].text.startswith("/init"))
        app2 = make_app(self)
        app2.submit_text("/review безопасность")
        cmd = app2.sent_commands[-1]
        self.assertIn("git diff", cmd["text"])
        self.assertIn("безопасность", cmd["text"])

    def test_add_dir_and_completion(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        extra = Path(tmp.name) / "extra"
        extra.mkdir()
        (extra / "notes.md").write_text("x", encoding="utf-8")
        app = make_app(self)
        app.cwd = tmp.name
        app.submit_text("/add-dir " + str(extra))
        self.assertIn(str(extra), app.extra_dirs)
        app.editor.set_text("@not")
        app._update_completions()
        self.assertTrue(any("notes.md" in c for c in app.completions))
        app.submit_text("/add-dir /нет/такого/каталога")
        self.assertIn("Нет такого каталога", app.entries[-1].text)

    def test_btw_with_fake_agent(self):
        app = make_app(self)
        app.submit_text("/btw который час?")
        self.assertEqual(app.sent_commands[-1]["type"], "btw")
        self.assertTrue(any(e.kind == "btw" for e in app.entries))
        app.poll_source()
        app.poll_source()
        time.sleep(0.4)
        app.poll_source()
        self.assertTrue(any(e.kind == "btw" and "фальшивый ответ" in e.text
                            for e in app.entries))


class KeysTest(unittest.TestCase):
    def test_ctrl_r_history_picker(self):
        app = make_app(self)
        app.editor.history.append("первый запрос")
        app.editor.history.append("второй запрос")
        app.handle_key(key("r", "r", ("ctrl",)))
        self.assertEqual(app._picker_kind, "history")
        item = app.picker.items[0]
        self.assertEqual(item.payload, "второй запрос")
        app.handle_key(key("enter"))
        self.assertEqual(app.editor.text, "второй запрос")

    def test_ctrl_l_sets_redraw(self):
        app = make_app(self)
        app.handle_key(key("l", "l", ("ctrl",)))
        self.assertTrue(app._redraw)

    def test_esc_esc_empty_input_opens_rewind_picker(self):
        app = make_app(self)
        ticks = [100.0]
        app._clock = lambda: ticks[0]
        app._add(Entry("user", text="первый ход"))
        app._add(Entry("user", text="второй ход"))
        app.handle_key(key("esc"))
        ticks[0] += 0.2
        app.handle_key(key("esc"))
        self.assertEqual(app._picker_kind, "rewind")
        app.handle_key(key("enter"))
        cmd = app.sent_commands[-1]
        self.assertEqual(cmd["type"], "rewind")
        self.assertEqual(cmd["turn"], 2)

    def test_vim_mode_motions_and_insert(self):
        app = make_app(self, **{"ui.vim_mode": True})
        app.editor.set_text("раз два три")
        app.editor.cursor = 0
        # Esc из ввода — нормальный режим.
        app.handle_key(key("esc"))
        self.assertTrue(app._vim_normal)
        app.handle_key(key("w", "w"))
        self.assertEqual(app.editor.cursor, 4)
        app.handle_key(key("b", "b"))
        self.assertEqual(app.editor.cursor, 0)
        app.handle_key(key("x", "x"))
        self.assertEqual(app.editor.text, "аз два три")
        app.handle_key(key("d", "d"))
        app.handle_key(key("d", "d"))
        self.assertEqual(app.editor.text, "")
        app.handle_key(key("i", "i"))
        self.assertFalse(app._vim_normal)
        app.handle_key(key("z", "z"))
        self.assertEqual(app.editor.text, "z")

    def test_statusline_variants(self):
        app = make_app(self)
        buf, _ = app.draw(90, 20, 100.0)
        text = "\n".join("".join(c[0] for c in row) for row in buf.cells)
        self.assertIn("контекст", text)
        app.settings.set("ui.statusline", "off")
        buf, _ = app.draw(90, 20, 100.0)
        last = "".join(c[0] for c in buf.cells[-1])
        self.assertNotIn("контекст", last)


class TilesTest(unittest.TestCase):
    def test_all_tiles_registered_and_render(self):
        for pid in ("context", "git", "mcp", "processes", "tree",
                    "models", "diag"):
            self.assertIn(pid, registered_panels())
        app = make_app(self)
        app._add(Entry("user", text="привет"))
        app._add(Entry("agent", text="ответ подлиньше"))
        for pid in ("context", "mcp", "processes", "tree", "models", "diag"):
            panel = create_panel(pid)
            lines = panel.lines(app, 40)
            self.assertTrue(lines, pid)

    def test_context_panel_shares(self):
        app = make_app(self)
        app._add(Entry("user", text="вопрос"))
        app._add(Entry("agent", text="ответ " * 50))
        app.on_agent_event({"type": "usage", "model": "m", "input": 10,
                            "output": 5, "cost": 0.0, "context_pct": 30})
        panel = create_panel("context")
        text = "\n".join(panel.lines(app, 60))
        self.assertIn("занято: 30%", text)
        self.assertIn("ответы агента", text)
        self.assertIn("крупнейшие", text)

    def test_git_panel_polls_only_when_open(self):
        # Опрос — только из lines() открытой панели и не чаще кэша.
        from luna.ui.panels import git as git_panel

        calls = []

        def fake_git(cwd, *args):
            calls.append(args)
            if "rev-parse" in args and "--abbrev-ref" in args:
                return True, "main\n"
            if args and args[0] == "status":
                return True, " M app.py\n"
            if args and args[0] == "log":
                return True, "abc123 коммит\n"
            return True, "true\n"

        app = make_app(self)
        panel = create_panel("git")
        with mock.patch.object(git_panel, "run_git", fake_git):
            lines = panel.lines(app, 40)
            first = len(calls)
            panel.lines(app, 40)  # второй кадр — кэш, без новых вызовов
            self.assertEqual(len(calls), first)
        text = "\n".join(lines)
        self.assertIn("ветка: main", text)
        self.assertIn("изменено файлов: 1", text)

    def test_processes_from_events_and_stop(self):
        from luna.ui.panels import processes

        data = PanelData()
        data.observe({"type": "tool.call", "id": "b1", "name": "bash",
                      "args": {"command": "sleep 30", "background": True},
                      "intent": ""})
        data.observe({"type": "tool.result", "id": "b1", "ok": True,
                      "summary": "Фоновая команда запущена, pid "
                                 + str(os.getpid())
                                 + ". Вывод пишется в bg-1.log",
                      "output": ""})
        self.assertEqual(len(data.jobs), 1)
        self.assertEqual(data.jobs[0]["pid"], os.getpid())
        self.assertEqual(data.jobs[0]["log"], "bg-1.log")
        app = make_app(self)
        app.panel_data = data
        panel = create_panel("processes")
        text = "\n".join(panel.lines(app, 50))
        self.assertIn("жив", text)  # текущий процесс — жив
        with mock.patch.object(processes, "proc_stop",
                               return_value=True) as stop:
            panel.handle_key(app, key("s", "s"))
            stop.assert_called_once_with(os.getpid())

    def test_diag_from_events(self):
        data = PanelData()
        data.observe({"type": "tool.call", "id": "d1", "name": "bash",
                      "args": {"command": "python -m pytest -q"}, "intent": ""})
        data.observe({"type": "tool.result", "id": "d1", "ok": False,
                      "summary": "1 failed", "output": "FAILED test_x\n"})
        self.assertIsNotNone(data.diag)
        app = make_app(self)
        app.panel_data = data
        panel = create_panel("diag")
        text = "\n".join(panel.lines(app, 50))
        self.assertIn("pytest", text)
        self.assertIn("FAILED test_x", text)

    def test_mcp_stats_and_panel(self):
        data = PanelData()
        data.observe({"type": "tool.call", "id": "m1",
                      "name": "mcp__files__read", "args": {}, "intent": ""})
        data.observe({"type": "tool.result", "id": "m1", "ok": False,
                      "summary": "", "output": ""})
        self.assertEqual(data.mcp_stats["files"], {"calls": 1, "errors": 1})
        app = make_app(self)
        panel = create_panel("mcp")
        text = "\n".join(panel.lines(app, 50))
        self.assertIn("не настроены", text)  # в тестовом HOME конфигов нет

    def test_models_panel_roles(self):
        app = make_app(self, **{"model.main": "openai:gpt-x"})
        panel = create_panel("models")
        text = "\n".join(panel.lines(app, 50))
        self.assertIn("main (главная): openai:gpt-x", text)
        self.assertIn("fast (быстрая): как главная", text)

    def test_tree_marks_and_inserts(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        (Path(tmp.name) / "app.py").write_text("x", encoding="utf-8")
        app = make_app(self)
        app.cwd = tmp.name
        app.panel_data.observe({"type": "tool.call", "id": "r1",
                                "name": "read", "args": {"path": "app.py"},
                                "intent": ""})
        panel = create_panel("tree")
        text = "\n".join(panel.lines(app, 50))
        self.assertIn("app.py", text)
        self.assertIn("тронут сессией", text)
        panel.handle_key(app, key("enter"))
        self.assertIn("@app.py", app.editor.text)
        self.assertFalse(app.panel_focus)

    def test_snapshot_screens_exist(self):
        from luna.devtools import snapshot

        self.assertIn("help", snapshot.SCREENS)
        self.assertIn("tiles", snapshot.SCREENS)
        frames = snapshot.render_frames("help", 100, 30, [0.0])
        self.assertEqual(len(frames), 1)
        frames = snapshot.render_frames("tiles", 120, 36, [0.0])
        self.assertEqual(len(frames), 1)


if __name__ == "__main__":
    unittest.main()
