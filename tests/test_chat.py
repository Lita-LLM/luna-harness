"""Чат вехи 1 (ЗАДАЧА-05): markdown, перенос, подтверждение, очередь,
фальшивый агент, панели-тайлинг, настоящая фаза луны, снимки чата."""

from __future__ import annotations

import re
import unicodedata
import unittest
from datetime import datetime, timezone

from luna.devtools import snapshot
from luna.devtools.fake_agent import FakeAgent
from luna.ui import astro, markdown
from luna.ui.chat import ChatApp, diff_counts
from luna.ui.editor import TextEditor
from luna.ui.keys import Key
from luna.ui.panels import LayoutTree, create_panel, register_panel, PanelContent


class StubSource:
    """Источник событий-заглушка: команды записывает, событий не шлёт."""

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


def make_app():
    return ChatApp(source=StubSource())


def buffer_text(buf) -> str:
    return "\n".join("".join(cell[0] for cell in row).rstrip()
                     for row in buf.cells)


class MarkdownTest(unittest.TestCase):
    def test_blocks(self):
        blocks = markdown.parse_markdown(
            "# Заголовок\n\nТекст с **жирным** и `кодом`.\n\n"
            "- раз\n- два\n\n```python\nx = 1\n```")
        kinds = [b.kind for b in blocks]
        self.assertEqual(kinds, ["heading", "para", "list", "code"])
        para_styles = {s.style for s in blocks[1].spans}
        self.assertIn("bold", para_styles)
        self.assertIn("code", para_styles)
        self.assertEqual(blocks[3].lines, ["x = 1"])
        self.assertEqual(blocks[3].lang, "python")

    def test_inline_link_and_italic(self):
        spans = markdown.parse_inline("см. [доки](https://example.com) и *курсив*")
        styles = [s.style for s in spans]
        self.assertIn("link", styles)
        self.assertIn("italic", styles)
        texts = "".join(s.text for s in spans)
        self.assertIn("доки", texts)
        self.assertNotIn("https://", texts)

    def test_code_highlight(self):
        spans = markdown.highlight_code_line("def f(x):  # комм")
        styles = {s.style for s in spans}
        self.assertIn("keyword", styles)
        self.assertIn("comment", styles)
        spans = markdown.highlight_code_line("s = 'строка' + 42")
        styles = {s.style for s in spans}
        self.assertIn("string", styles)
        self.assertIn("number", styles)

    def test_render_width(self):
        lines = markdown.render_markdown("## Тема\n\nДлинный абзац со **смелым** словом", 12)
        from luna.ui.width import str_width
        for line in lines:
            width = sum(str_width(s.text) for s in line)
            self.assertLessEqual(width, 12)


class WrapTest(unittest.TestCase):
    def test_wrap_words(self):
        self.assertEqual(markdown.wrap_text("раз два три четыре", 7),
                         ["раз два", "три", "четыре"])

    def test_wrap_long_word(self):
        lines = markdown.wrap_text("abcdefghij", 4)
        self.assertEqual(lines, ["abcd", "efgh", "ij"])

    def test_wrap_wide_chars_not_split(self):
        lines = markdown.wrap_text("кот 你好世界 кот", 6)
        from luna.ui.width import str_width
        for line in lines:
            self.assertLessEqual(str_width(line), 6)
        self.assertEqual("".join(lines).replace(" ", ""), "кот你好世界кот")

    def test_wrap_newlines_preserved(self):
        self.assertEqual(markdown.wrap_text("а\nб", 10), ["а", "б"])


class EditorTest(unittest.TestCase):
    def test_multiline_and_history(self):
        ed = TextEditor()
        ed.insert("первая")
        ed.newline()
        ed.insert("вторая")
        self.assertEqual(ed.text, "первая\nвторая")
        self.assertTrue(ed.up())
        ed.insert("!")
        self.assertEqual(ed.text, "первая!\nвторая")
        submitted = ed.submit()
        self.assertEqual(submitted, "первая!\nвторая")
        self.assertEqual(ed.text, "")
        ed.insert("черновик")
        self.assertTrue(ed.history_prev())
        self.assertEqual(ed.text, "первая!\nвторая")
        self.assertTrue(ed.history_next())
        self.assertEqual(ed.text, "черновик")

    def test_visual_lines_cursor(self):
        ed = TextEditor()
        ed.set_text("аб\nвг")
        lines, pos = ed.visual_lines(10)
        self.assertEqual(lines, ["аб", "вг"])
        self.assertEqual(pos, (1, 2))


class GuardFlowTest(unittest.TestCase):
    def feed_guard(self, app):
        app.on_agent_event({"type": "guard.ask", "id": "g1", "tool": "bash",
                            "args": {"command": "rm -rf build/"},
                            "intent": "Чищу сборку", "risk": "high",
                            "reason": "удаление", "choices": []})

    def test_yes_no_always_stop(self):
        for key, decision in ((Key("enter"), "yes"),
                              (Key("n", char="n"), "no"),
                              (Key("a", char="a"), "always"),
                              (Key("q", char="q"), "stop")):
            app = make_app()
            self.feed_guard(app)
            app.handle_key(key)
            answers = [c for c in app.sent_commands if c["type"] == "guard.answer"]
            self.assertEqual(len(answers), 1, decision)
            self.assertEqual(answers[0]["decision"], decision)
            self.assertIsNone(app.pending_guard)

    def test_edit_rewrites_args_in_line(self):
        app = make_app()
        self.feed_guard(app)
        app.handle_key(Key("e", char="e"))
        self.assertTrue(app.guard_editing)
        self.assertEqual(app.editor.text, "rm -rf build/")
        app.editor.set_text("rm -rf build/old")
        app.handle_key(Key("enter"))
        answers = [c for c in app.sent_commands if c["type"] == "guard.answer"]
        self.assertEqual(answers[0]["decision"], "edit")
        self.assertEqual(answers[0]["args"], {"command": "rm -rf build/old"})
        self.assertFalse(app.guard_editing)

    def test_edit_cancel(self):
        app = make_app()
        self.feed_guard(app)
        app.handle_key(Key("e", char="e"))
        app.handle_key(Key("esc"))
        self.assertFalse(app.guard_editing)
        self.assertIsNotNone(app.pending_guard)  # вопрос остался открытым
        self.assertEqual(app.sent_commands, [])

    def test_guard_block_shows_ta_choices(self):
        app = make_app()
        self.feed_guard(app)
        buf, _cursor = app.draw(90, 24, 100.0)
        text = buffer_text(buf)
        self.assertIn("Enter — да · n — нет · e — править", text)
        self.assertIn("rm -rf build/", text)


class GuardOrderTest(unittest.TestCase):
    """ЗАДАЧА-06, п. 3: подтверждение — между вызовом тула и результатом;
    после ответа блок сворачивается в одну строку решения на месте."""

    def feed_pair(self, app, same_id=False):
        app.on_agent_event({"type": "tool.call", "id": "t1", "name": "bash",
                            "args": {"command": "rm -rf build/"},
                            "intent": "Чищу каталог сборки"})
        gid = "t1" if same_id else "g1"
        app.on_agent_event({"type": "guard.ask", "id": gid, "tool": "bash",
                            "args": {"command": "rm -rf build/"},
                            "intent": "Чищу каталог сборки", "risk": "high",
                            "reason": "необратимое удаление", "choices": []})

    def rows(self, app):
        buf, _cursor = app.draw(90, 24, 100.0)
        return buf, buffer_text(buf).split("\n")

    def test_block_between_call_and_result(self):
        app = make_app()
        self.feed_pair(app)
        _buf, rows = self.rows(app)
        head = next(i for i, r in enumerate(rows) if "bash" in r and "rm -rf" in r)
        block = next(i for i, r in enumerate(rows) if "нужно решение" in r)
        self.assertLess(head, block)
        app.handle_key(Key("enter"))
        app.on_agent_event({"type": "tool.result", "id": "t1", "ok": True,
                            "summary": "Код выхода: 0", "output": ""})
        _buf, rows = self.rows(app)
        text = "\n".join(rows)
        self.assertNotIn("нужно решение", text)
        decision = next(i for i, r in enumerate(rows) if "решение: да" in r)
        result = next(i for i, r in enumerate(rows) if "Код выхода: 0" in r)
        head = next(i for i, r in enumerate(rows) if "bash" in r and "rm -rf" in r)
        self.assertLess(head, decision)
        self.assertLess(decision, result)

    def test_same_id_links_like_real_agent(self):
        # Настоящий агент шлёт guard.ask с id вызова тула — связь по id.
        app = make_app()
        self.feed_pair(app, same_id=True)
        guard = next(e for e in app.entries if e.kind == "guard")
        self.assertEqual(guard.guard_of, "t1")

    def test_decided_block_collapses_and_bg_gone(self):
        from luna.ui.chat import GUARD_BG

        app = make_app()
        self.feed_pair(app)
        buf, _rows = self.rows(app)
        warm = sum(1 for row in buf.cells for cell in row if cell[2] == GUARD_BG)
        self.assertGreater(warm, 0)  # пока ждёт — блок тёплый
        app.handle_key(Key("n", char="n"))
        buf, rows = self.rows(app)
        text = "\n".join(rows)
        self.assertIn("решение: нет", text)
        warm = sum(1 for row in buf.cells for cell in row if cell[2] == GUARD_BG)
        self.assertEqual(warm, 0)  # после решения фона блока не осталось

    def test_guard_without_tool_stays_standalone(self):
        app = make_app()
        app.on_agent_event({"type": "guard.ask", "id": "g9", "tool": "bash",
                            "args": {"command": "ls"}, "intent": "",
                            "risk": "", "reason": "", "choices": []})
        _buf, rows = self.rows(app)
        self.assertTrue(any("нужно решение" in r for r in rows))


class AskFlowTest(unittest.TestCase):
    def test_choose_option_and_custom(self):
        app = make_app()
        app.on_agent_event({"type": "ask.user", "id": "a1", "question": "Формат?",
                            "options": ["Короткий", "Подробный"]})
        app.handle_key(Key("down"))
        app.handle_key(Key("enter"))
        answers = [c for c in app.sent_commands if c["type"] == "user.answer"]
        self.assertEqual(answers[0]["text"], "Подробный")
        app2 = make_app()
        app2.on_agent_event({"type": "ask.user", "id": "a2", "question": "Формат?",
                             "options": ["Короткий"]})
        for ch in "Свой":
            app2.handle_key(Key(ch, char=ch))
        app2.handle_key(Key("enter"))
        answers = [c for c in app2.sent_commands if c["type"] == "user.answer"]
        self.assertEqual(answers[0]["text"], "Свой")


class QueueAndModesTest(unittest.TestCase):
    def test_queue_while_busy(self):
        # ЗАДАЧА-18: очередь — теперь режим chat.send_mode=queue (по
        # умолчанию inject: сообщение уходит агенту сразу, вставкой в ход).
        app = make_app()
        app.settings.set("chat.send_mode", "queue")
        # Настройка пишется в общий файл прогона — вернуть как было.
        self.addCleanup(app.settings.set, "chat.send_mode", "inject")
        app.submit_text("первое")
        app.on_agent_event({"type": "turn.start", "turn": 1})
        app.submit_text("второе")
        sent = [c for c in app.sent_commands if c["type"] == "user.message"]
        self.assertEqual(len(sent), 1)          # второе ждёт в очереди UI
        self.assertEqual(app.queue, ["второе"])
        app.on_agent_event({"type": "turn.done", "turn": 1, "reason": "end"})
        sent = [c for c in app.sent_commands if c["type"] == "user.message"]
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[1]["text"], "второе")
        self.assertEqual(sent[1].get("delivery"), "queue")
        self.assertEqual(app.queue, [])

    def test_tab_cycles_mode(self):
        app = make_app()
        app.handle_key(Key("tab"))
        self.assertEqual(app.sent_commands[-1], {"type": "mode.set", "mode": "plan"})
        app.on_agent_event({"type": "mode.changed", "mode": "plan"})
        app.handle_key(Key("tab", mods=frozenset({"shift"})))
        self.assertEqual(app.sent_commands[-1]["mode"], "normal")

    def test_slash_commands(self):
        app = make_app()
        app.submit_text("/help")
        # ЗАДАЧА-16: /help — выбиралка с поиском, а не запись-простыня.
        self.assertIsNotNone(app.picker)
        self.assertEqual(app._picker_kind, "help")
        app.handle_key(Key("esc"))
        self.assertIsNone(app.picker)
        app.submit_text("/clear")
        self.assertTrue(all(e.kind == "notice" for e in app.entries))
        app.submit_text("/mode auto")
        self.assertEqual(app.sent_commands[-1], {"type": "mode.set", "mode": "auto"})
        app.submit_text("/exit")
        self.assertTrue(app.done)

    def test_shell_command_bypasses_model(self):
        app = make_app()
        app.submit_text("!ls -la")
        self.assertEqual(app.sent_commands[-1], {"type": "shell", "command": "ls -la"})

    def test_double_esc_interrupts_and_sends_draft(self):
        app = make_app()
        app._clock = lambda: 50.0
        app.on_agent_event({"type": "turn.start", "turn": 1})
        app.editor.set_text("подожди, уточню")
        app.handle_key(Key("esc"))
        app.handle_key(Key("esc"))
        kinds = [c["type"] for c in app.sent_commands]
        self.assertIn("interrupt", kinds)
        app.on_agent_event({"type": "turn.done", "turn": 1, "reason": "interrupted"})
        sent = [c for c in app.sent_commands if c["type"] == "user.message"]
        self.assertEqual(sent[-1]["text"], "подожди, уточню")

    def test_status_line_has_context_phase_mode_model_cost(self):
        app = make_app()
        app.on_agent_event({"type": "session.start", "model": "m1", "mode": "plan",
                            "cwd": "/tmp"})
        app.on_agent_event({"type": "usage", "model": "m1", "input": 1, "output": 2,
                            "cost": 0.5, "context_pct": 100})
        buf, _cursor = app.draw(90, 24, 100.0)
        text = buffer_text(buf)
        self.assertIn("контекст 100%", text)
        self.assertIn("plan", text)
        self.assertIn("$0.5000", text)
        self.assertIn("🌕", text)  # полный контекст — полнолуние


class DiffCountsTest(unittest.TestCase):
    def test_from_summary_and_output(self):
        self.assertEqual(diff_counts("app.py: +12 −3", ""), (12, 3))
        self.assertEqual(diff_counts("ok", "+ a\n+ b\n- c\n"), (2, 1))
        self.assertIsNone(diff_counts("ok", "просто текст"))


class FakeAgentTest(unittest.TestCase):
    def run_until_idle(self, agent, now, limit=200):
        events = []
        for _ in range(limit):
            now[0] += 0.5
            events.extend(agent.poll())
            if not agent.busy and not agent.has_pending():
                break
        return events

    def make(self):
        now = [1000.0]
        agent = FakeAgent(clock=lambda: now[0])
        agent.start()
        return agent, now

    def test_plain_scenario_covers_contract(self):
        agent, now = self.make()
        agent.handle_command({"type": "user.message", "text": "привет",
                              "attachments": []})
        events = self.run_until_idle(agent, now)
        types = [e["type"] for e in events]
        for needed in ("session.start", "turn.start", "agent.thinking",
                       "agent.text", "agent.text.done", "tool.call",
                       "tool.output", "tool.result", "todo", "usage",
                       "turn.done"):
            self.assertIn(needed, types)
        self.assertFalse(agent.busy)
        usage = [e for e in events if e["type"] == "usage"][0]
        self.assertGreater(usage["context_pct"], 0)

    def test_message_during_turn_queues_inside_source(self):
        agent, now = self.make()
        agent.handle_command({"type": "user.message", "text": "раз", "attachments": []})
        agent.handle_command({"type": "user.message", "text": "два", "attachments": []})
        events = self.run_until_idle(agent, now, limit=400)
        starts = [e for e in events if e["type"] == "turn.start"]
        self.assertEqual(len(starts), 2)

    def test_guard_scenario_waits_and_answers(self):
        agent, now = self.make()
        agent.handle_command({"type": "user.message", "text": "подтверди чистку",
                              "attachments": []})
        events = []
        for _ in range(20):
            now[0] += 0.5
            events.extend(agent.poll())
            if any(e["type"] == "guard.ask" for e in events):
                break
        self.assertTrue(any(e["type"] == "guard.ask" for e in events))
        self.assertTrue(agent.busy)  # ход ждёт человека, а не закончился
        agent.handle_command({"type": "guard.answer", "id": "guard-1",
                              "decision": "yes"})
        events = self.run_until_idle(agent, now)
        types = [e["type"] for e in events]
        self.assertIn("guard.verdict", types)
        self.assertIn("turn.done", types)

    def test_ask_and_error_scenarios(self):
        agent, now = self.make()
        agent.handle_command({"type": "user.message", "text": "вопрос про формат",
                              "attachments": []})
        events = []
        for _ in range(20):
            now[0] += 0.5
            events.extend(agent.poll())
            if any(e["type"] == "ask.user" for e in events):
                break
        self.assertTrue(any(e["type"] == "ask.user" for e in events))
        agent.handle_command({"type": "user.answer", "id": "ask-1", "text": "Короткий"})
        events = self.run_until_idle(agent, now)
        self.assertIn("turn.done", [e["type"] for e in events])
        agent.handle_command({"type": "user.message", "text": "ошибка сети",
                              "attachments": []})
        events = self.run_until_idle(agent, now)
        self.assertIn("error", [e["type"] for e in events])

    def test_interrupt(self):
        agent, now = self.make()
        agent.handle_command({"type": "user.message", "text": "привет",
                              "attachments": []})
        now[0] += 0.5
        agent.poll()
        agent.handle_command({"type": "interrupt"})
        events = self.run_until_idle(agent, now)
        done = [e for e in events if e["type"] == "turn.done"]
        self.assertEqual(done[0]["reason"], "interrupted")


class PanelsTest(unittest.TestCase):
    def test_open_close_focus(self):
        tree = LayoutTree()
        self.assertEqual(tree.leaves(), [])
        tree.open("todo")
        tree.open("limits")
        self.assertEqual(tree.leaves(), ["todo", "limits"])
        self.assertEqual(tree.focused, "limits")
        tree.focus_next(-1)
        self.assertEqual(tree.focused, "todo")
        tree.close("todo")
        self.assertEqual(tree.leaves(), ["limits"])
        tree.toggle("limits")
        self.assertEqual(tree.leaves(), [])

    def test_move_resize_maximize(self):
        tree = LayoutTree()
        tree.open("todo")
        tree.open("limits")
        self.assertTrue(tree.move("limits", -1))
        self.assertEqual(tree.leaves(), ["limits", "todo"])
        self.assertTrue(tree.resize("limits", 0.5))
        rects = tree.rects(0, 0, 40, 10)
        # сплит по умолчанию вертикальный: панели стопкой, тянется высота
        self.assertGreater(rects["limits"][3], rects["todo"][3])
        tree.toggle_maximize("todo")
        self.assertEqual(tree.visible_leaves(), ["todo"])
        self.assertEqual(tree.rects(0, 0, 40, 10)["todo"], (0, 0, 40, 10))
        tree.toggle_maximize("todo")
        self.assertEqual(tree.visible_leaves(), ["limits", "todo"])

    def test_saved_layout_roundtrip(self):
        tree = LayoutTree()
        tree.open("todo")
        tree.open("limits")
        tree.save_layout("рабочая")
        other = LayoutTree()
        other.saved = tree.saved
        self.assertTrue(other.load_layout("рабочая"))
        self.assertEqual(other.leaves(), ["todo", "limits"])
        self.assertFalse(other.load_layout("нет такой"))

    def test_registry_for_modules(self):
        register_panel("probe", "Проба",
                       lambda: PanelContent("Проба", lambda app, w: ["строка"]))
        content = create_panel("probe")
        self.assertEqual(content.lines(None, 20), ["строка"])
        self.assertIsNone(create_panel("не существует"))

    def test_layout_commands_via_slash(self):
        app = make_app()
        app.toggle_panel("todo")
        app.submit_text("/layout save рабочая")
        app.toggle_panel("todo")
        self.assertEqual(app.layout.leaves(), [])
        app.submit_text("/layout load рабочая")
        self.assertEqual(app.layout.leaves(), ["todo"])
        app.submit_text("/layout list")
        self.assertTrue(any("рабочая" in e.text for e in app.entries
                            if e.kind == "notice"))

    def test_scroll_wheel_and_pages(self):
        app = make_app()
        for i in range(30):
            app.on_agent_event({"type": "notice", "text": "строка " + str(i)})
        app.draw(60, 10, 100.0)
        app.handle_key(Key("pageup"))
        self.assertGreater(app.scroll, 0)
        app.handle_key(Key("pagedown"))
        self.assertEqual(app.scroll, 0)
        from luna.ui.keys import Mouse
        app.handle_mouse(Mouse("wheel_up", 1, 1))
        self.assertEqual(app.scroll, 3)
        app.handle_mouse(Mouse("wheel_down", 1, 1))
        self.assertEqual(app.scroll, 0)

    def test_panel_draw_in_chat(self):
        app = make_app()
        app.on_agent_event({"type": "todo", "items": [
            {"text": "Сделать дело", "status": "active"},
            {"text": "Проверить", "status": "pending"}]})
        app.toggle_panel("todo")
        buf, _cursor = app.draw(90, 24, 100.0)
        text = buffer_text(buf)
        self.assertIn("План", text)
        self.assertIn("Сделать дело", text)


class AstroTest(unittest.TestCase):
    # Известные даты 2026 года (астрономические календари): новолуния и
    # полнолуния; линейная модель обязана попадать в сутки и по освещённости.
    NEW_MOONS = [(2026, 1, 18), (2026, 3, 19), (2026, 6, 15),
                 (2026, 9, 11), (2026, 12, 9)]
    FULL_MOONS = [(2026, 2, 1), (2026, 4, 2), (2026, 5, 31),
                  (2026, 8, 28), (2026, 10, 26)]

    def test_new_moons_2026(self):
        for y, m, d in self.NEW_MOONS:
            sky = astro.moon_sky(datetime(y, m, d, 12, tzinfo=timezone.utc))
            self.assertLess(sky.illumination, 0.06, sky)
            self.assertTrue(sky.age_days < 1.2 or sky.age_days > 28.3, sky)

    def test_full_moons_2026(self):
        for y, m, d in self.FULL_MOONS:
            sky = astro.moon_sky(datetime(y, m, d, 12, tzinfo=timezone.utc))
            self.assertGreater(sky.illumination, 0.94, sky)

    def test_hemisphere_mirrors_crescent(self):
        # Растущий серп: север — свет справа (mirror False), юг — наоборот.
        moment = datetime(2026, 1, 22, 12, tzinfo=timezone.utc)
        north = astro.moon_sky(moment, "north")
        south = astro.moon_sky(moment, "south")
        self.assertTrue(north.waxing)
        self.assertFalse(north.mirror)
        self.assertTrue(south.mirror)
        self.assertEqual(north.illumination, south.illumination)


class TerminalModel:
    """Модель настоящего терминала (Windows Terminal): применяет поток
    Screen.present к собственной сетке ячеек.

    Ширина глифов — СВОЯ таблица модели, а не luna.ui.width: дингбаты и
    текстовые символы — 1 ячейка, эмодзи с представлением и CJK — 2.
    Если модель ширины Луны разойдётся с терминалом, якорь курсора в
    present перепрыгнет живую ячейку и в сетке останется «хвост»
    прошлого кадра — сравнение сетки с буфером это ловит.
    """

    WIDE2 = set("⚠✨☀❤➕⬛⬜⭐⭕🌑🌒🌓🌔🌕🌖🌗🌘🌙🚀")

    def __init__(self, cols, rows):
        self.cols, self.rows = cols, rows
        self.cells = [[(" ", None) for _x in range(cols)] for _y in range(rows)]
        self.x = self.y = 0
        self.bg = None

    @classmethod
    def term_width(cls, ch):
        if not ch or unicodedata.combining(ch):
            return 0
        if ch in cls.WIDE2 or ord(ch) >= 0x1F000:
            return 2
        if unicodedata.east_asian_width(ch) in ("W", "F"):
            return 2
        return 1

    def feed(self, frame: str) -> None:
        i = 0
        while i < len(frame):
            if frame[i] == "\x1b":
                m = re.match(r"\x1b\[([0-9;?]*)([A-Za-z])", frame[i:])
                self.assert_match = m
                if m is None:
                    raise AssertionError("непонятная последовательность: "
                                         + repr(frame[i:i + 16]))
                params, final = m.group(1), m.group(2)
                i += m.end()
                if final == "H":
                    parts = params.split(";")
                    self.y = int(parts[0]) - 1
                    self.x = int(parts[1]) - 1 if len(parts) > 1 and parts[1] else 0
                elif final == "m":
                    self._sgr(params)
                continue
            self._put(frame[i])
            i += 1

    def _sgr(self, params: str) -> None:
        nums = [int(p) if p else 0 for p in params.split(";")] if params else [0]
        j = 0
        while j < len(nums):
            n = nums[j]
            if n == 0:
                self.bg = None
            elif n == 48 and j + 1 < len(nums) and nums[j + 1] == 2:
                self.bg = (nums[j + 2], nums[j + 3], nums[j + 4])
                j += 4
            elif n == 48 and j + 1 < len(nums) and nums[j + 1] == 5:
                from luna.ui import color

                self.bg = color.rgb_of_256(nums[j + 2])
                j += 2
            j += 1

    def _put(self, ch: str) -> None:
        w = self.term_width(ch)
        if w == 0 or not (0 <= self.y < self.rows):
            return
        x, y = self.x, self.y
        if w == 2 and x + 1 >= self.cols:
            return
        # Запись поверх половины широкого символа зачищает и вторую половину.
        if self.cells[y][x][0] == "" and x > 0:
            self.cells[y][x - 1] = (" ", self.bg)
        elif (self.term_width(self.cells[y][x][0]) == 2 and x + 1 < self.cols
                and self.cells[y][x + 1][0] == ""):
            self.cells[y][x + 1] = (" ", self.bg)
        if w == 2:
            self.cells[y][x] = (ch, self.bg)
            self.cells[y][x + 1] = ("", self.bg)
            self.x = x + 2
        else:
            self.cells[y][x] = (ch, self.bg)
            self.x = x + 1

    def assert_matches(self, tc, buf) -> None:
        for y in range(buf.rows):
            model_text = "".join(c[0] for c in self.cells[y])
            buf_text = "".join(c[0] for c in buf.cells[y])
            tc.assertEqual(model_text, buf_text, "текст строки " + str(y))
            for x in range(buf.cols):
                tc.assertEqual(self.cells[y][x][1], buf.cells[y][x][2],
                               "фон ячейки " + str(x) + "," + str(y))


class NoTailsTest(unittest.TestCase):
    """ЗАДАЧА-06, п. 1: после сворачивания/разворачивания блока и сдвига
    ленты в терминале не остаётся хвостов прошлого кадра."""

    def step(self, app, screen, model, frames, tc):
        buf, cursor = app.draw(60, 16, 100.0)
        screen.present(buf, cursor=cursor)
        model.feed(frames[-1])
        model.assert_matches(tc, buf)

    def test_collapse_expand_and_block_shift(self):
        from luna.ui.screen import Screen

        app = make_app()
        frames = []
        screen = Screen(frames.append, "truecolor", 60, 16)
        model = TerminalModel(60, 16)
        app.on_agent_event({"type": "session.start", "model": "m", "mode": "normal",
                            "cwd": "/tmp"})
        app.submit_text("подтверди чистку")
        app.on_agent_event({"type": "turn.start", "turn": 1})
        app.on_agent_event({"type": "tool.call", "id": "t1", "name": "bash",
                            "args": {"command": "rm -rf build/"},
                            "intent": "Чищу каталог сборки"})
        self.step(app, screen, model, frames, self)          # строка тула, идёт ход
        app.on_agent_event({"type": "guard.ask", "id": "g1", "tool": "bash",
                            "args": {"command": "rm -rf build/"},
                            "intent": "Чищу каталог сборки", "risk": "high",
                            "reason": "необратимое удаление", "choices": []})
        self.step(app, screen, model, frames, self)          # блок подтверждения
        app.handle_key(Key("enter"))                  # человек сказал «да»
        app.on_agent_event({"type": "tool.result", "id": "t1", "ok": True,
                            "summary": "Код выхода: 0",
                            "output": "первая строка\nвторая строка\n"})
        self.step(app, screen, model, frames, self)          # решение + результат
        app.handle_key(Key("o", mods=frozenset({"ctrl"})))
        self.step(app, screen, model, frames, self)          # вывод развёрнут
        app.handle_key(Key("o", mods=frozenset({"ctrl"})))
        self.step(app, screen, model, frames, self)          # вывод свёрнут обратно
        app.on_agent_event({"type": "agent.text", "text": "Готово, чисто."})
        app.on_agent_event({"type": "turn.done", "turn": 1, "reason": "end"})
        self.step(app, screen, model, frames, self)          # лента сдвинулась


class CopyTest(unittest.TestCase):
    """ЗАДАЧА-06, п. 4: OSC 52, выделение мышью, /copy и /copy err."""

    def test_osc52_roundtrip(self):
        import base64

        from luna.ui import clipboard

        seq = clipboard.osc52_sequence("привет ✦")
        self.assertTrue(seq.startswith("\x1b]52;c;"))
        self.assertTrue(seq.endswith("\a"))
        payload = seq[len("\x1b]52;c;"):-1]
        self.assertEqual(base64.b64decode(payload).decode("utf-8"), "привет ✦")

    def test_copy_text_write_path(self):
        from unittest import mock

        from luna.ui import clipboard

        out = []
        with mock.patch.object(clipboard, "IS_WINDOWS", False):
            way = clipboard.copy_text("текст", write=out.append)
        self.assertEqual(way, "osc52")
        self.assertEqual(len(out), 1)
        self.assertIn("52;c;", out[0])

    def test_copy_text_windows_clip_backup(self):
        from unittest import mock

        from luna.ui import clipboard

        with mock.patch.object(clipboard, "IS_WINDOWS", True), \
                mock.patch.object(clipboard, "_clip_exe", lambda text: True):
            self.assertEqual(clipboard.copy_text("текст", write=lambda s: None),
                             "osc52+clip")
            self.assertEqual(clipboard.copy_text("текст"), "clip")
        with mock.patch.object(clipboard, "IS_WINDOWS", False):
            self.assertEqual(clipboard.copy_text("текст"), "none")

    def make_feed_app(self):
        app = make_app()
        app.copied = []
        app.copy_hook = app.copied.append
        app.on_agent_event({"type": "notice", "text": "альфа бета гамма"})
        app.draw(60, 16, 100.0)
        return app

    def test_drag_selection_copies_on_release(self):
        from luna.ui.keys import Mouse

        app = self.make_feed_app()
        app.handle_mouse(Mouse("press", 2, 1, button=0))    # строка 0, колонка 1
        app.handle_mouse(Mouse("move", 7, 1, button=0))     # тянем до колонки 6
        app.handle_mouse(Mouse("release", 7, 1, button=0))
        self.assertEqual(app.copied, ["льфа б"])

    def test_double_click_selects_word(self):
        from luna.ui.keys import Mouse

        app = self.make_feed_app()
        app.handle_mouse(Mouse("press", 9, 1, button=0))    # внутри слова «бета»
        app.handle_mouse(Mouse("release", 9, 1, button=0))
        app.handle_mouse(Mouse("press", 9, 1, button=0))    # второй клик — слово
        self.assertEqual(app.copied, ["бета"])

    def test_selection_highlight_reverse(self):
        from luna.ui import color
        from luna.ui.keys import Mouse

        app = self.make_feed_app()
        app.handle_mouse(Mouse("press", 2, 1, button=0))
        app.handle_mouse(Mouse("move", 5, 1, button=0))
        buf, _cursor = app.draw(60, 16, 100.0)
        attrs = buf.cells[0][2][3]
        self.assertTrue(attrs & color.REVERSE)
        plain = buf.cells[0][10][3]
        self.assertFalse(plain & color.REVERSE)

    def test_copy_last_answer_and_error(self):
        app = make_app()
        app.copied = []
        app.copy_hook = app.copied.append
        app.on_agent_event({"type": "agent.text", "text": "Готовый ответ"})
        app.on_agent_event({"type": "agent.text.done"})
        app.submit_text("/copy")
        self.assertEqual(app.copied, ["Готовый ответ"])
        app.on_agent_event({"type": "error", "message": "упало",
                            "detail": "подробности падения"})
        app.submit_text("/copy err")
        self.assertEqual(app.copied[-1], "упало\nподробности падения")
        notes = [e.text for e in app.entries if e.kind == "notice"]
        self.assertTrue(any("Скопировано" in t for t in notes))

    def test_copy_nothing_and_no_hook(self):
        app = make_app()
        app.copied = []
        app.copy_hook = app.copied.append
        app.submit_text("/copy")
        self.assertEqual(app.copied, [])
        app2 = make_app()  # без хука
        app2.on_agent_event({"type": "agent.text", "text": "ответ"})
        app2.submit_text("/copy")
        notes = [e.text for e in app2.entries if e.kind == "notice"]
        self.assertTrue(any("недоступен" in t for t in notes))

    def test_help_mentions_shift_drag(self):
        # Справка переехала в выбиралку /help (ЗАДАЧА-16); текст справки —
        # HELP_TEXT, собранный из реестра команд: подсказки в нём живут.
        from luna.ui.chat import HELP_TEXT

        app = make_app()
        app.submit_text("/help")
        self.assertIsNotNone(app.picker)
        self.assertIn("Shift+протяжка", HELP_TEXT)
        self.assertIn("/copy err", HELP_TEXT)


class ChatSnapshotTest(unittest.TestCase):
    def test_chat_frames_states(self):
        frames = snapshot.render_frames("chat", 100, 28, [0.0])
        labels = [label for label, _buf in frames]
        self.assertEqual(labels, ["стрим", "тул", "подтверждение", "вопрос"])
        texts = {label: buffer_text(buf) for label, buf in frames}
        self.assertIn("Готово", texts["стрим"])
        self.assertIn("pip install lxml", texts["тул"])
        self.assertIn("Enter — да", texts["подтверждение"])
        self.assertIn("Какой формат отчёта нужен?", texts["вопрос"])

    def test_html_lines_are_blocks_without_gaps(self):
        from luna.ui.screen import Buffer
        buf = Buffer(6, 2)
        buf.text(0, 0, "раз")
        page = snapshot.buffer_to_html(buf)
        self.assertIn('class="line"', page)
        self.assertIn("line-height: 1", snapshot._PAGE_CSS)
        self.assertIn("display: block", snapshot._PAGE_CSS)


if __name__ == "__main__":
    unittest.main()
