"""ЗАДАЧА-20: мышь везде, быстрые панели, память раскладки, привычки.

Всё — на подменённых событиях мыши и клавиш, фальшивом агенте и ручных
часах: живой терминал и сеть тестам не нужны.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from luna.core.bus import EventBus
from luna.core.paths import DataDir
from luna.core.secrets import Secrets
from luna.core.sessions import SessionJournal
from luna.core.settings import Settings
from luna.guard.policy import Guard
from luna.mind.loop import Agent
from luna.router.core import Router
from luna.ui.anim import Scheduler
from luna.ui.chat import ChatApp
from luna.ui.editor import TextEditor
from luna.ui.keys import Key, Mouse, Paste


def make_app(case: unittest.TestCase, **overrides) -> ChatApp:
    tmp = tempfile.TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    root = Path(tmp.name)
    (root / "work").mkdir(parents=True)
    settings = Settings(home=root / "home", cwd=root / "work")
    for k, v in overrides.items():
        settings.set(k, v)
    app = ChatApp(EventBus(), settings, Scheduler(), "emoji",
                  cwd=str(root / "work"), data=DataDir(root / "home"))
    app._root = root
    return app


def key(name: str, char: str = "", mods=()) -> Key:
    return Key(name, char=char, mods=frozenset(mods))


def mouse(kind: str, x: int, y: int, mods=()) -> Mouse:
    """Мышь по клеткам 0-based: события SGR — 1-based."""
    return Mouse(kind, x + 1, y + 1, button=0, mods=frozenset(mods))


def buffer_text(buf) -> str:
    return "\n".join("".join(ch for ch, _f, _b, _a in row).rstrip()
                     for row in buf.cells)


def zone_of(app, action_kind: str, action_name=None):
    for zone in app.zones:
        action = zone[4]
        if action[0] == action_kind and (action_name is None
                                         or action[1] == action_name):
            return zone
    return None


def click_zone(app, zone) -> None:
    x0, y0, _x1, _y1, _action = zone
    app.handle_mouse(mouse("press", x0, y0))


def feed_with_blocks(app) -> None:
    app.on_agent_event({"type": "session.start", "model": "m1", "mode": "normal"})
    app.submit_text("почини")
    app.on_agent_event({"type": "turn.start", "turn": 1})
    app.on_agent_event({"type": "agent.thinking", "text": "думаю о сборке"})
    app.on_agent_event({"type": "agent.text.done"})
    app.on_agent_event({"type": "tool.call", "id": "t1", "name": "bash",
                        "args": {"command": "make"}, "intent": "собираю"})
    app.on_agent_event({"type": "tool.result", "id": "t1", "ok": True,
                        "summary": "готово", "output": "строка1\nстрока2\n"})


# --- раздел 1: Ctrl+O и блоки -------------------------------------------------


class ExpandAllTest(unittest.TestCase):
    def test_ctrl_o_toggles_all_blocks(self):
        app = make_app(self)
        feed_with_blocks(app)
        thinking = next(e for e in app.entries if e.kind == "thinking")
        tool = next(e for e in app.entries if e.kind == "tool")
        self.assertFalse(app._exp(thinking))
        app.handle_key(key("o", mods=("ctrl",)))
        self.assertTrue(app.expand_all)
        self.assertTrue(app._exp(thinking))
        self.assertTrue(app._exp(tool))
        text = "\n".join("".join(t for t, _f, _a in s) for s, _b
                         in app._feed_lines(80, app._clock()))
        self.assertIn("строка1", text)  # вывод тула виден целиком
        app.handle_key(key("o", mods=("ctrl",)))
        self.assertFalse(app.expand_all)
        self.assertFalse(app._exp(tool))

    def test_block_header_click_toggles_only_it(self):
        app = make_app(self)
        feed_with_blocks(app)
        app.draw(100, 30, app._clock())
        thinking = next(e for e in app.entries if e.kind == "thinking")
        tool = next(e for e in app.entries if e.kind == "tool")
        zone = zone_of(app, "block")
        self.assertIsNotNone(zone)
        # Зона заголовка первой раскрываемой записи (мышление).
        click_zone(app, zone)
        self.assertTrue(app._exp(zone[4][1]))
        other = tool if zone[4][1] is thinking else thinking
        self.assertFalse(app._exp(other))

    def test_block_cursor_keys(self):
        app = make_app(self)
        feed_with_blocks(app)
        app.handle_key(key("down", mods=("alt",)))
        self.assertIsNotNone(app._block_sel)
        first = app._block_sel
        app.handle_key(key("enter"))
        self.assertTrue(app._exp(first))
        app.handle_key(key("enter"))
        self.assertFalse(app._exp(first))


# --- раздел 2: мышь везде -------------------------------------------------------


class MouseEverywhereTest(unittest.TestCase):
    def test_status_line_zones_open_pickers(self):
        app = make_app(self)
        app.draw(120, 30, app._clock())
        for name, kind in (("mode", "mode"), ("model", "models"),
                           ("send", "send"), ("theme", "theme"),
                           ("thinking", "thinking"), ("effort", "effort")):
            zone = zone_of(app, "status", name)
            self.assertIsNotNone(zone, name)
            click_zone(app, zone)
            self.assertIsNotNone(app.picker, name)
            self.assertEqual(app._picker_kind, kind, name)
            app.picker = None

    def test_mode_picker_changes_mode_via_command(self):
        app = make_app(self)
        app.open_mode_picker()
        item = next(it for it in app.picker.items if it.key == "plan")
        app.picker.pick_item(item)
        action = app.picker.pick_item(item)
        app._route_picker_action(action)
        self.assertEqual(app.sent_commands[-1],
                         {"type": "mode.set", "mode": "plan"})

    def test_picker_mouse_hover_click_wheel(self):
        app = make_app(self)
        app.open_help_picker()
        app.draw(100, 30, app._clock())
        zone = next(z for z in app.zones if z[4][0] == "picker")
        app.handle_mouse(mouse("move", zone[0], zone[1]))
        self.assertEqual(app.picker.current().key, zone[4][1].key)
        app.handle_mouse(mouse("wheel_down", zone[0], zone[1]))
        app.handle_mouse(mouse("press", zone[0], zone[1]))

    def test_mouse_off_and_scroll_modes(self):
        app = make_app(self, **{"ui.mouse": "off"})
        feed_with_blocks(app)
        app.draw(100, 30, app._clock())
        app.handle_mouse(mouse("press", 5, 5))
        self.assertIsNone(app.selection)
        app2 = make_app(self, **{"ui.mouse": "scroll"})
        feed_with_blocks(app2)
        app2.draw(100, 30, app2._clock())
        before = app2.scroll
        app2.handle_mouse(mouse("wheel_up", 5, 5))
        self.assertEqual(app2.scroll, before + 3)
        app2.handle_mouse(mouse("press", 5, 5))
        self.assertIsNone(app2.selection)

    def test_shift_drag_is_bypass(self):
        app = make_app(self)
        feed_with_blocks(app)
        app.draw(100, 30, app._clock())
        app.handle_mouse(mouse("press", 2, 2, mods=("shift",)))
        self.assertIsNone(app._sel_anchor)

    def test_link_click_copies(self):
        app = make_app(self)
        copied: list = []
        app.copy_hook = copied.append
        app.on_agent_event({"type": "session.start", "model": "m1", "mode": "normal"})
        app.on_agent_event({"type": "agent.text",
                            "text": "смотри luna/ui/chat.py и https://example.com/x"})
        app.on_agent_event({"type": "agent.text.done"})
        app.draw(100, 30, app._clock())
        link_zone = next(z for z in app.zones if z[4][0] == "link")
        click_zone(app, link_zone)
        self.assertEqual(copied, [link_zone[4][1]])

    def test_panel_close_cross_and_focus(self):
        app = make_app(self)
        app.toggle_panel("moon")
        app.toggle_panel("limits")
        app.draw(120, 30, app._clock() + 1.0)  # анимация колонки уже доехала
        rect = app._panel_rects["moon"]
        # Клик по заголовку — фокус.
        app.handle_mouse(mouse("press", rect[0] + 1, rect[1]))
        self.assertEqual(app.layout.focused, "moon")
        # Клик по крестику — закрыть.
        app.handle_mouse(mouse("press", rect[0] + rect[2] - 1, rect[1]))
        self.assertNotIn("moon", app.layout)


# --- раздел 3 и 4: /panels и память раскладки ----------------------------------


class PanelsPickerAndLayoutTest(unittest.TestCase):
    def test_space_toggles_and_stays_open(self):
        app = make_app(self)
        app.open_panels_picker()
        item = next(it for it in app.picker.items if it.key == "moon")
        app.picker._sel_key = item.key
        app.handle_key(key("space", " "))
        self.assertIn("moon", app.layout)
        self.assertIsNotNone(app.picker)  # список не закрылся
        refreshed = next(it for it in app.picker.items if it.key == "moon")
        self.assertEqual(refreshed.mark, "→")
        app.handle_key(key("space", " "))
        self.assertNotIn("moon", app.layout)
        app.handle_key(key("tab"))
        app.handle_key(key("esc"))
        self.assertIsNone(app.picker)

    def test_layout_saved_and_restored(self):
        app = make_app(self)
        app.toggle_panel("moon")
        app.toggle_panel("limits")
        app.layout.column_w = 34  # ручная ширина — тоже в память
        app._save_layout()
        app2 = ChatApp(EventBus(), app.settings, Scheduler(), "emoji",
                       cwd=app.cwd, data=DataDir(app._root / "home"),
                       journal=object())  # журнал есть — сеанс настоящий
        self.assertIn("moon", app2.layout)
        self.assertIn("limits", app2.layout)
        self.assertEqual(app2.layout.column_w, 34)
        app2.submit_text("/panels reset")
        app3 = ChatApp(EventBus(), app.settings, Scheduler(), "emoji",
                       cwd=app.cwd, data=DataDir(app._root / "home"),
                       journal=object())
        self.assertEqual(app3.layout.leaves(), [])

    def test_reopen_panel_uses_saved_place(self):
        app = make_app(self)
        app.toggle_panel("moon")
        app.toggle_panel("limits")
        app.toggle_panel("files")
        app._save_layout()
        app.toggle_panel("limits")  # закрыть
        self.assertEqual(app.layout.leaves(), ["moon", "files"])
        app.toggle_panel("limits")  # открыть — на старое место (индекс 1)
        self.assertEqual(app.layout.leaves(), ["moon", "limits", "files"])


# --- раздел 5: полный список на «/» ---------------------------------------------


class FullCommandListTest(unittest.TestCase):
    def test_slash_lists_everything(self):
        app = make_app(self)
        work = Path(app.cwd)
        (work / ".claude" / "commands").mkdir(parents=True)
        (work / ".claude" / "commands" / "deploy.md").write_text(
            "---\ndescription: Деплой проекта\n---\nСделай деплой $ARGUMENTS",
            encoding="utf-8")
        (work / ".claude" / "skills" / "review").mkdir(parents=True)
        (work / ".claude" / "skills" / "review" / "SKILL.md").write_text(
            "---\nname: review\ndescription: Ревью кода\n---\nТекст",
            encoding="utf-8")
        app.editor.set_text("/")
        app._update_completions()
        names = [name for name, _desc in app.cmd_completions]
        self.assertGreaterEqual(len(names), 40)  # не обрезан восьмёркой
        self.assertIn("help", names)
        self.assertIn("панели", names)          # русский алиас рядом
        self.assertIn("deploy", names)          # команда из файлов
        self.assertIn("review", names)          # скилл
        app.editor.set_text("/sen")
        app._update_completions()
        self.assertEqual([n for n, _d in app.cmd_completions], ["send"])


# --- раздел 6: /undo возвращает промпт ------------------------------------------


class UndoPromptTest(unittest.TestCase):
    def test_rewound_returns_prompt_to_editor(self):
        app = make_app(self)
        app.submit_text("первый ход")
        app.on_agent_event({"type": "turn.done", "turn": 1, "reason": "end"})
        app.submit_text("второй ход про сборку")
        app.on_agent_event({"type": "turn.done", "turn": 2, "reason": "end"})
        app.on_agent_event({"type": "session.rewound", "turn": 1, "scope": "both"})
        self.assertEqual(app.editor.text, "второй ход про сборку")

    def test_rewound_keeps_draft_in_history(self):
        app = make_app(self)
        app.submit_text("ход")
        app.on_agent_event({"type": "turn.done", "turn": 1, "reason": "end"})
        app.editor.set_text("набросок")
        app.on_agent_event({"type": "session.rewound", "turn": 0, "scope": "both"})
        self.assertEqual(app.editor.text, "ход")
        self.assertIn("набросок", app.editor.history)


# --- раздел 7: fade --------------------------------------------------------------


class StreamFadeTest(unittest.TestCase):
    def test_fade_by_frames(self):
        app = make_app(self, **{"ui.stream_fade": "char"})
        app._clock = lambda: 1000.0
        app.on_agent_event({"type": "session.start", "model": "m1", "mode": "normal"})
        app.on_agent_event({"type": "agent.text", "text": "привет"})
        buf1, _ = app.draw(80, 24, 1000.0)
        buf2, _ = app.draw(80, 24, 1000.0 + 0.5)
        row1 = next(r for r in buf1.cells if any(c[0] == "п" for c in r))
        row2 = next(r for r in buf2.cells if any(c[0] == "п" for c in r))
        cell1 = next(c for c in row1 if c[0] == "п")
        cell2 = next(c for c in row2 if c[0] == "п")
        self.assertNotEqual(cell1[1], cell2[1])  # цвет хвоста меняется по кадрам
        self.assertTrue(app.needs_frame(1000.05))
        self.assertFalse(app._fade_busy(1001.0))

    def test_fade_off_by_default_and_16_colors(self):
        from luna.ui.markdown import render_markdown

        app = make_app(self)
        app.on_agent_event({"type": "agent.text", "text": "привет"})
        entry = app.entries[-1]
        lines = render_markdown(entry.text, 80)
        self.assertIsNone(app._fade_last_line(entry, lines, 0.0))
        app2 = make_app(self, **{"ui.stream_fade": "char"})
        app2.color_level = "16"
        app2.on_agent_event({"type": "agent.text", "text": "привет"})
        entry2 = app2.entries[-1]
        self.assertIsNone(app2._fade_last_line(
            entry2, render_markdown(entry2.text, 80), 0.0))


# --- раздел 8: отправка и заначка -------------------------------------------------


class SendAndStashTest(unittest.TestCase):
    def test_status_line_always_shows_send_mode(self):
        app = make_app(self)
        buf, _ = app.draw(140, 24, app._clock())
        self.assertIn("отправка", buffer_text(buf))
        self.assertIn("inject", buffer_text(buf))
        self.assertIsNotNone(zone_of(app, "status", "send"))

    def test_send_picker_and_command(self):
        app = make_app(self)
        app.submit_text("/send")
        self.assertEqual(app._picker_kind, "send")
        item = next(it for it in app.picker.items if it.key == "queue")
        app._route_picker_action(app.picker.pick_item(item))
        self.assertEqual(app._send_mode(), "queue")
        app.submit_text("/отправка")
        self.assertEqual(app._picker_kind, "send")

    def test_first_busy_send_hint_once(self):
        app = make_app(self)
        app.busy = True
        app.submit_text("раз")
        self.assertIsNotNone(app._hint)
        app._hint = None
        app.submit_text("два")
        self.assertIsNone(app._hint)  # второй раз — тишина

    def test_ctrl_s_stash_cycle(self):
        app = make_app(self)
        app.editor.set_text("важный набросок")
        app.editor.cursor = 3
        app.handle_key(key("s", mods=("ctrl",)))
        self.assertEqual(app.editor.text, "")
        meta = app._meta_lines(app._clock())
        self.assertTrue(any("заначке" in line for line in meta))
        app.handle_key(key("s", mods=("ctrl",)))
        self.assertEqual(app.editor.text, "важный набросок")
        self.assertEqual(app.editor.cursor, 3)

    def test_ctrl_s_swaps_with_nonempty(self):
        app = make_app(self)
        app.editor.set_text("первый")
        app.handle_key(key("s", mods=("ctrl",)))
        app.editor.set_text("второй")
        app.handle_key(key("s", mods=("ctrl",)))
        self.assertEqual(app.editor.text, "первый")
        app.handle_key(key("s", mods=("ctrl",)))
        self.assertEqual(app.editor.text, "второй")

    def test_ctrl_s_works_in_panel_focus_and_alt_s_cycles(self):
        app = make_app(self)
        app.toggle_panel("todo")
        app.panel_focus = True
        app.editor.set_text("текст")
        app.handle_key(key("s", mods=("ctrl",)))
        self.assertEqual(app.editor.text, "")  # заначка, а не смена режима
        self.assertEqual(app._send_mode(), "inject")
        app.panel_focus = False
        app.handle_key(key("s", mods=("alt",)))
        self.assertEqual(app._send_mode(), "queue")


# --- раздел 9: привычки ------------------------------------------------------------


class HabitsTest(unittest.TestCase):
    def test_readline_editing(self):
        ed = TextEditor()
        ed.set_text("раз два три")
        ed.word_left()
        self.assertEqual(ed.cursor, len("раз два "))
        ed.kill_word_back()
        self.assertEqual(ed.text, "раз три")
        ed.yank()
        self.assertEqual(ed.text, "раз два три")
        ed.cursor = len(ed.text)
        ed.kill_to_start()
        self.assertEqual(ed.text, "")
        ed.yank()
        self.assertEqual(ed.text, "раз два три")
        ed.undo_edit()
        self.assertEqual(ed.text, "")

    def test_editor_keys_in_chat(self):
        app = make_app(self)
        app.editor.set_text("привет мир")
        app.handle_key(key("a", mods=("ctrl",)))
        self.assertEqual(app.editor.cursor, 0)
        app.handle_key(key("e", mods=("ctrl",)))
        self.assertEqual(app.editor.cursor, len("привет мир"))
        app.handle_key(key("w", mods=("ctrl",)))
        self.assertEqual(app.editor.text, "привет ")
        app.handle_key(key("y", mods=("ctrl",)))
        self.assertEqual(app.editor.text, "привет мир")
        app.handle_key(key("_", mods=("ctrl",)))
        self.assertEqual(app.editor.text, "привет ")
        app.handle_key(key("left", mods=("alt",)))
        self.assertEqual(app.editor.cursor, 0)

    def test_backslash_enter_newline(self):
        app = make_app(self)
        app.editor.set_text("строка\\")
        app.handle_key(key("enter"))
        self.assertEqual(app.editor.text, "строка\n")
        self.assertEqual(app.entries, [])

    def test_hash_writes_project_memory(self):
        app = make_app(self)
        app.submit_text("# тесты гонять только через make test")
        path = Path(app.cwd) / "AGENTS.md"
        self.assertIn("make test", path.read_text(encoding="utf-8"))
        self.assertTrue(any(e.kind == "notice" for e in app.entries))

    def test_at_fuzzy_search(self):
        app = make_app(self)
        sub = Path(app.cwd) / "pkg"
        sub.mkdir()
        (sub / "chat_engine.py").write_text("x", encoding="utf-8")
        app.editor.set_text("глянь @chteng")
        app._update_completions()
        self.assertIn("pkg/chat_engine.py", app.completions)

    def test_paste_collapses_and_submit_expands(self):
        app = make_app(self)
        big = "\n".join("строка " + str(i) for i in range(30))
        app.handle_paste(Paste(big))
        self.assertIn("[вставлено 30 строк]", app.editor.text)
        self.assertNotIn("строка 29", app.editor.text)
        app.submit_text()
        cmd = app.sent_commands[-1]
        self.assertIn("строка 29", cmd["text"])

    def test_paste_file_path_becomes_attachment(self):
        app = make_app(self)
        target = Path(app.cwd) / "pic.png"
        target.write_bytes(b"\x89PNG")
        app.handle_paste(Paste(str(target)))
        self.assertTrue(app.editor.text.startswith("@"))

    def test_paste_click_expands(self):
        app = make_app(self)
        big = "\n".join("s" + str(i) for i in range(15))
        app.handle_paste(Paste(big))
        app._expand_paste_in_editor()
        self.assertIn("s14", app.editor.text)

    def test_ctrl_g_opens_editor(self):
        app = make_app(self)
        app.editor.set_text("черновик")
        app.handle_key(key("g", mods=("ctrl",)))
        self.assertIsNotNone(app.edit_request)

    def test_question_mark_help(self):
        app = make_app(self)
        app.handle_key(key("?", "?"))
        self.assertTrue(any(e.kind == "help" for e in app.entries))

    def test_copy_code_block(self):
        app = make_app(self)
        copied: list = []
        app.copy_hook = copied.append
        app.on_agent_event({"type": "agent.text",
                            "text": "вот код:\n```python\nprint(1)\n```\nконец"})
        app.on_agent_event({"type": "agent.text.done"})
        app.submit_text("/copy code")
        self.assertEqual(copied, ["print(1)"])

    def test_notify_hook_on_turn_done_and_guard(self):
        app = make_app(self, **{"ui.notify": "bell"})
        seqs: list = []
        app.notify_hook = seqs.append
        app.on_agent_event({"type": "turn.start", "turn": 1})
        app.on_agent_event({"type": "turn.done", "turn": 1, "reason": "end"})
        self.assertEqual(seqs, ["\a"])
        app.on_agent_event({"type": "guard.ask", "id": "g1", "tool": "bash",
                            "args": {}, "intent": "", "risk": "high",
                            "reason": ""})
        self.assertEqual(len(seqs), 2)

    def test_title_hook_states(self):
        app = make_app(self)
        titles: list = []
        app.title_hook = titles.append
        app.on_agent_event({"type": "turn.start", "turn": 1})
        self.assertIn("идёт ход", titles[-1])
        app.on_agent_event({"type": "turn.done", "turn": 1, "reason": "end"})
        self.assertIn("ждёт", titles[-1])

    def test_auto_title_and_rename(self):
        app = make_app(self)
        journal = SessionJournal.create(DataDir(app._root / "home"), app.cwd)
        # На Windows открытый журнал держит файл — чистка temp упадёт
        # с WinError 32; addCleanup идёт LIFO: закроется раньше tempdir.
        self.addCleanup(journal.close)
        app.journal = journal
        app._send_user_message("почини сборку на windows")
        self.assertEqual(app.session_title, "почини сборку на windows")
        app.rename_session("своя тема")
        self.assertEqual(app.session_title, "своя тема")
        app._send_user_message("ещё ход")
        self.assertEqual(app.session_title, "своя тема")  # не перезаписалось


class GuardHabitsTest(unittest.TestCase):
    def _app_with_guard(self):
        app = make_app(self)
        app.on_agent_event({"type": "tool.call", "id": "t1", "name": "bash",
                            "args": {"command": "rm -rf build"}, "intent": ""})
        app.on_agent_event({"type": "guard.ask", "id": "t1", "tool": "bash",
                            "args": {"command": "rm -rf build"}, "intent": "",
                            "risk": "high", "reason": "удаление"})
        return app

    def test_always_project_answer(self):
        app = self._app_with_guard()
        app.handle_key(key("p", "p"))
        cmd = app.sent_commands[-1]
        self.assertEqual(cmd["decision"], "always_project")

    def test_no_replace_answer_with_text(self):
        app = self._app_with_guard()
        app.handle_key(key("r", "r"))
        self.assertTrue(app._guard_replace)
        for ch in "сделай копию":
            app.handle_key(key(ch, ch))
        app.handle_key(key("enter"))
        cmd = app.sent_commands[-1]
        self.assertEqual(cmd["decision"], "no_replace")
        self.assertEqual(cmd["text"], "сделай копию")

    def test_guard_project_always_persists(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        settings = Settings(home=root / "home", cwd=root)
        guard = Guard(settings)
        guard.remember_always_project("high")
        fresh = Settings(home=root / "home", cwd=root)
        guard2 = Guard(fresh)
        decision = guard2.decide("bash", True, {"command": "ls"},
                                 mode="normal")
        # Чтение/низкий риск не спрашивает; высокий — по проектному «всегда».
        self.assertEqual(decision.decision, "allow")
        saved = json.loads((root / ".luna" / "settings.json").read_text(
            encoding="utf-8"))
        self.assertEqual(saved["guard"]["always_rank_project"], 2)


class BangShellTest(unittest.TestCase):
    """!команда на настоящем цикле агента с фальшивой моделью."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.work = root / "work"
        self.work.mkdir()
        self._old_home = os.environ.get("LUNA_HOME")
        os.environ["LUNA_HOME"] = str(root / "home")
        self.addCleanup(self._restore_home)
        self.settings = Settings(home=root / "home", cwd=self.work)
        self.secrets = Secrets(root / "home" / "secrets.json")
        self.events: list = []

    def _restore_home(self):
        if self._old_home is None:
            os.environ.pop("LUNA_HOME", None)
        else:
            os.environ["LUNA_HOME"] = self._old_home

    def test_bang_bypasses_guard_and_triggers_turn(self):
        from luna.devtools.fake_llm import FakeModelClient

        class FakeRouter:
            def chat(self, messages, tools=None, *, role="main", ref="",
                     on_event=None):
                return FakeModelClient(script=[]).chat(
                    "script", messages, tools, on_event or (lambda k, t: None))

        agent = Agent(settings=self.settings, secrets=self.secrets,
                      router=FakeRouter(), cwd=self.work,
                      ask_guard_fn=lambda ev: (_ for _ in ()).throw(
                          AssertionError("Предохранитель не должен спрашивать")))
        agent.listen(self.events.append)
        agent.handle_command({"type": "shell", "command": "echo luna-bang"})
        types = [e["type"] for e in self.events]
        self.assertIn("tool.result", types)
        result = next(e for e in self.events if e["type"] == "tool.result")
        self.assertIn("luna-bang", result["output"])
        verdict = next(e for e in self.events if e["type"] == "guard.verdict")
        self.assertEqual(verdict["by"], "human")
        # Ход модели запущен следом (chat.bang_triggers по умолчанию on).
        self.assertIn("turn.start", types)


class RouterRetryTest(unittest.TestCase):
    def test_retry_with_countdown_events(self):
        settings = Settings(home=Path(tempfile.mkdtemp()) / "home",
                            cwd=Path(tempfile.mkdtemp()))
        settings.set("net.retry_delay_s", 0)
        router = Router(settings, Secrets(Path(tempfile.mkdtemp()) / "s.json"))
        router._sleep = lambda _s: None
        calls = {"n": 0}
        from luna.core.errors import HumanError
        from luna.router.types import ChatResult, Usage

        def flaky(cfg, model, messages, tools, emit, role="main"):
            calls["n"] += 1
            if calls["n"] < 3:
                raise HumanError("network", "Не удалось связаться", "",
                                 status=502)
            return ChatResult(text="ok", model=model, usage=Usage(0, 0))

        router._chat_once = flaky
        router.role_ref = lambda role: "fake:echo"
        seen: list = []
        result = router.chat([], None, on_event=lambda k, t: seen.append((k, t)))
        self.assertEqual(result.text, "ok")
        self.assertEqual(calls["n"], 3)
        self.assertTrue(any(k == "retry" for k, _t in seen))
