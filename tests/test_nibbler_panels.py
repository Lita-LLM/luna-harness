"""ЗАДАЧА-14: Ниблер (дроссель, маршрутизация, мозг) и панели с наполнением.

Всё без терминала и сети: события — словари контракта, часы и мозг —
поддельные.
"""

import tempfile
import unittest
from pathlib import Path

from luna.core.bus import EventBus
from luna.core.settings import Settings
from luna.mind import manners
from luna.mind.nibbler import Nibbler
from luna.ui.anim import Scheduler
from luna.ui.chat import ChatApp
from luna.ui.keys import Key
from luna.ui.panels import PanelData, create_panel, find_panel, registered_panels


class FakeClock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, sec: float) -> None:
        self.t += sec


def buffer_text(buf) -> str:
    return "\n".join("".join(cell[0] for cell in row) for row in buf.cells)


def isolated_settings(case: unittest.TestCase, **overrides) -> Settings:
    """Настройки во временных каталогах: set() не должен трогать живой
    LUNA_HOME — иначе тесты отравляют друг друга через настоящий файл."""
    tmp = tempfile.TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    settings = Settings(home=Path(tmp.name) / "home", cwd=Path(tmp.name) / "work")
    for key, value in overrides.items():
        settings.set(key, value)
    return settings


def edit_call(nib, cid: str, path: str = "app.py") -> None:
    nib.observe({"type": "tool.call", "id": cid, "name": "edit",
                 "args": {"path": path}, "intent": "правлю"})
    nib.observe({"type": "tool.result", "id": cid, "ok": True,
                 "summary": path + ": +2 −1", "output": "+ строка\n"})


class NibblerTest(unittest.TestCase):
    def make(self, brain=None, clock=None, **overrides):
        settings = isolated_settings(self, **overrides)
        emitted: list = []
        clock = clock or FakeClock()
        nib = Nibbler(settings, emit=emitted.append, brain=brain, clock=clock)
        return nib, emitted, clock

    def test_repeat_edit_is_serious_and_goes_to_agent(self):
        nib, emitted, _clock = self.make()
        nib.observe({"type": "turn.start", "turn": 1})
        for i in range(3):
            edit_call(nib, "e" + str(i))
        self.assertEqual(len(emitted), 1)
        ev = emitted[0]
        self.assertEqual(ev["type"], "nibbler.say")
        self.assertEqual(ev["severity"], "serious")
        self.assertEqual(ev["to"], "agent")
        self.assertIn("app.py", ev["text"])
        notes = nib.take_notes()
        self.assertEqual(len(notes), 1)
        self.assertEqual(nib.take_notes(), [])

    def test_throttle_blocks_and_recovers(self):
        nib, emitted, clock = self.make()
        nib.observe({"type": "turn.start", "turn": 1})
        for i in range(3):
            edit_call(nib, "a" + str(i), "a.py")
        self.assertEqual(len(emitted), 1)
        # Дроссель закрыт: ни по секундам, ни по ходам.
        for i in range(3):
            edit_call(nib, "b" + str(i), "b.py")
        self.assertEqual(len(emitted), 1)
        clock.advance(61)
        nib.observe({"type": "turn.start", "turn": 2})
        nib.observe({"type": "turn.start", "turn": 3})
        for i in range(3):
            edit_call(nib, "c" + str(i), "c.py")
        self.assertEqual(len(emitted), 2)
        self.assertIn("c.py", emitted[1]["text"])

    def test_context_hot_is_minor_and_stays_with_human(self):
        nib, emitted, _clock = self.make(**{"nibbler.interval_sec": 0,
                                            "nibbler.every_turns": 0})
        nib.observe({"type": "usage", "context_pct": 95})
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0]["severity"], "minor")
        self.assertEqual(emitted[0]["to"], "human")
        self.assertEqual(nib.take_notes(), [])
        nib.observe({"type": "usage", "context_pct": 97})   # уже горячо: молчит
        self.assertEqual(len(emitted), 1)
        nib.observe({"type": "usage", "context_pct": 40})   # остыл
        nib.observe({"type": "usage", "context_pct": 96})   # снова говорит
        self.assertEqual(len(emitted), 2)

    def test_to_agent_never_and_all(self):
        nib, emitted, _c = self.make(**{"nibbler.to_agent": "never"})
        nib.observe({"type": "turn.start", "turn": 1})
        for i in range(3):
            edit_call(nib, "e" + str(i))
        self.assertEqual(emitted[0]["to"], "human")
        self.assertEqual(nib.take_notes(), [])

        nib2, emitted2, _c2 = self.make(**{"nibbler.to_agent": "all",
                                           "nibbler.interval_sec": 0,
                                           "nibbler.every_turns": 0})
        nib2.observe({"type": "usage", "context_pct": 95})
        self.assertEqual(emitted2[0]["to"], "agent")
        self.assertEqual(len(nib2.take_notes()), 1)

    def test_brain_silence_serious_and_failure(self):
        calls = []

        def silent(system, digest):
            calls.append(digest)
            return ""

        nib, emitted, _c = self.make(brain=silent)
        nib.observe({"type": "error", "message": "бум"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(emitted, [])

        def loud(system, digest):
            return "!Агент говорит «готово», а тесты красные"

        nib2, emitted2, _c2 = self.make(brain=loud)
        nib2.observe({"type": "error", "message": "бум"})
        self.assertEqual(len(emitted2), 1)
        self.assertEqual(emitted2[0]["severity"], "serious")
        self.assertNotIn("!", emitted2[0]["text"][:1])
        self.assertEqual(len(nib2.take_notes()), 1)

        def broken(system, digest):
            raise RuntimeError("модель недоступна")

        nib3, emitted3, _c3 = self.make(brain=broken)
        nib3.observe({"type": "error", "message": "бум"})  # не падает
        self.assertEqual(emitted3, [])

    def test_brain_called_rarely_and_on_long_turn(self):
        calls = []

        def brain(system, digest):
            calls.append(digest)
            return "Заметка от мозга"

        clock = FakeClock()
        nib, emitted, _c = self.make(brain=brain, clock=clock)
        nib.observe({"type": "error", "message": "раз"})
        nib.observe({"type": "error", "message": "два"})  # дроссель: мозг не дёргаем
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(emitted), 1)

        clock2 = FakeClock()
        nib2, emitted2, _c2 = self.make(brain=brain, clock=clock2)
        nib2.observe({"type": "turn.start", "turn": 1})
        clock2.advance(120)
        nib2.observe({"type": "turn.done", "turn": 1, "reason": "end"})
        self.assertEqual(len(calls), 2)
        self.assertIn("долгий ход", calls[-1])

    def test_default_manner_is_nibbler_character(self):
        entry = manners.get(isolated_settings(self), "nibbler", audience="nibbler")
        self.assertIsNotNone(entry)
        nib, _e, _c = self.make()
        prompt = nib._system_prompt()
        self.assertIn("Ниблер", prompt)
        self.assertIn("ехид", prompt.lower())


def feed_session(data: PanelData) -> None:
    """Сессия событиями: расход, тулы, Предохранитель, ошибка, Ниблер."""
    data.observe({"type": "session.start", "model": "fake-agent", "cwd": "/p"})
    data.observe({"type": "turn.start", "turn": 1})
    data.observe({"type": "agent.thinking", "text": "Думаю про сборку. "})
    data.observe({"type": "tool.call", "id": "t1", "name": "read",
                  "args": {"path": "app.py"}, "intent": "Читаю"})
    data.observe({"type": "tool.result", "id": "t1", "ok": True,
                  "summary": "120 строк", "output": "def handle(): ...\n"})
    data.observe({"type": "tool.call", "id": "t2", "name": "edit",
                  "args": {"path": "app.py"}, "intent": "Правлю"})
    data.observe({"type": "tool.result", "id": "t2", "ok": True,
                  "summary": "app.py: +12 −3",
                  "output": "+ добавлен стрим\n- старый вызов\n"})
    data.observe({"type": "guard.verdict", "id": "t2", "decision": "allow",
                  "by": "human", "reason": "человек разрешил"})
    data.observe({"type": "error", "message": "Хьюстон, у нас проблема",
                  "detail": "детали ошибки"})
    data.observe({"type": "usage", "model": "fake-agent", "input": 4200,
                  "output": 860, "cost": 0.004, "context_pct": 40})
    data.observe({"type": "usage", "model": "fake-agent", "input": 800,
                  "output": 100, "cost": 0.0061, "context_pct": 62})
    data.observe({"type": "nibbler.say", "severity": "minor", "to": "human",
                  "text": "Мелкая заметка"})
    data.observe({"type": "nibbler.say", "severity": "serious", "to": "agent",
                  "text": "Серьёзная заметка"})
    data.observe({"type": "turn.done", "turn": 1, "reason": "end"})


class _App:
    """Заглушка приложения для панелей: только то, что они читают."""

    def __init__(self, data: PanelData):
        self.panel_data = data
        self.glyph_mode = "emoji"
        self.todos = []


class PanelsDataTest(unittest.TestCase):
    def setUp(self):
        self.data = PanelData()
        feed_session(self.data)
        self.app = _App(self.data)

    def test_moon_panel_shows_context_tokens_cost(self):
        lines = create_panel("moon").lines(self.app, 30)
        joined = "\n".join(lines)
        self.assertIn("█", joined)                     # фаза полублоками
        self.assertIn("контекст: 62%", joined)
        self.assertIn("вход 5 000", joined)            # 4200 + 800
        self.assertIn("выход 960", joined)
        self.assertIn("$0.0061", joined)
        self.assertIn("fake-agent", joined)

    def test_limits_panel_honest_without_data(self):
        lines = create_panel("limits").lines(self.app, 30)
        self.assertIn("нет данных", "\n".join(lines))
        self.data.observe({"type": "usage", "limits": {"запросов в минуту": "60"}})
        lines = create_panel("limits").lines(self.app, 30)
        self.assertIn("запросов в минуту: 60", "\n".join(lines))

    def test_backside_panel_has_full_results_and_errors(self):
        panel = create_panel("backside")
        panel.height_hint = 100
        joined = "\n".join(panel.lines(self.app, 40))
        self.assertIn("edit", joined)
        self.assertIn("+ добавлен стрим", joined)      # полный результат тула
        self.assertIn("Предохранитель: allow", joined)
        self.assertIn("Ошибка: Хьюстон", joined)
        self.assertIn("Рассуждения", joined)

    def test_files_panel_counts_and_diff(self):
        panel = create_panel("files")
        joined = "\n".join(panel.lines(self.app, 40))
        self.assertIn("app.py", joined)
        self.assertIn("+12 −3", joined)
        self.assertIn("читал: 1", joined)
        self.assertTrue(panel.handle_key(self.app, Key("enter")))
        diff = "\n".join(panel.lines(self.app, 40))
        self.assertIn("дифф: app.py", diff)
        self.assertIn("+ добавлен стрим", diff)
        self.assertTrue(panel.handle_key(self.app, Key("enter")))
        self.assertIn("app.py", "\n".join(panel.lines(self.app, 40)))

    def test_nibbler_panel_history(self):
        joined = "\n".join(create_panel("nibbler").lines(self.app, 40))
        self.assertIn("Мелкая заметка", joined)
        self.assertIn("! Серьёзная заметка", joined)

    def test_registry_and_no_swarm_yet(self):
        # Название теста — из ЗАДАЧИ-14, когда роя ещё не было; ЗАДАЧА-15
        # рой построила: «Созвездие» теперь в реестре, смысл проверки —
        # что прежние панели на месте и рой нашёлся по своему алиасу.
        self.assertEqual(find_panel("Луна").panel_id, "moon")
        self.assertEqual(find_panel("лог").panel_id, "backside")
        self.assertEqual(find_panel("files").panel_id, "files")
        self.assertEqual(find_panel("рой").panel_id, "constellation")
        self.assertIn("constellation", registered_panels())


def make_app(case: unittest.TestCase):
    settings = isolated_settings(case)
    app = ChatApp(EventBus(), settings, Scheduler(), "emoji")
    return app


class ChatPanelsIntegrationTest(unittest.TestCase):
    def test_nibbler_line_and_note_reach_agent_command(self):
        app = make_app(self)
        clock = FakeClock()
        app._clock = clock
        app.on_agent_event({"type": "turn.start", "turn": 1})
        for i in range(3):
            cid = "e" + str(i)
            app.on_agent_event({"type": "tool.call", "id": cid, "name": "edit",
                                "args": {"path": "app.py"}, "intent": "правлю"})
            app.on_agent_event({"type": "tool.result", "id": cid, "ok": True,
                                "summary": "app.py: +2 −1", "output": "+ строка\n"})
        app.on_agent_event({"type": "turn.done", "turn": 1, "reason": "end"})
        self.assertIsNotNone(app._nibbler_line)
        buf, _cursor = app.draw(100, 30, clock.t + 1)
        self.assertIn("Ниблер:", buffer_text(buf))
        buf, _cursor = app.draw(100, 30, clock.t + 100)
        self.assertNotIn("Ниблер:", buffer_text(buf))
        app.submit_text("привет")
        cmd = app.sent_commands[-1]
        self.assertEqual(cmd["type"], "user.message")
        self.assertTrue(cmd["text"].startswith("[Ниблер:"))
        self.assertTrue(cmd["text"].endswith("привет"))
        user_entries = [e for e in app.entries if e.kind == "user"]
        self.assertEqual(user_entries[-1].text, "привет")

    def test_panel_command_menu_and_hotkeys(self):
        app = make_app(self)
        app.submit_text("/panel луна")
        self.assertIn("moon", app.layout)
        app.submit_text("/panel луна")
        self.assertNotIn("moon", app.layout)
        app.submit_text("/panels")
        # ЗАДАЧА-16: меню цифрами заменено общей выбиралкой.
        self.assertIsNotNone(app.picker)
        self.assertEqual(app._picker_kind, "panels")
        item = next(it for it in app.picker.items if it.key == "files")
        app.picker._sel_key = item.key
        app.handle_key(Key("enter"))
        self.assertIn("files", app.layout)
        app.handle_key(Key("m", char="m", mods=frozenset({"alt"})))
        self.assertIn("moon", app.layout)

    def test_panel_width_animation_and_snap(self):
        app = make_app(self)
        app.toggle_panel("moon")
        _from_w, t0 = app._panel_anchor
        mid = app._panel_width(120, t0 + 0.05)
        self.assertGreater(mid, 0)
        self.assertLess(mid, 40)
        self.assertEqual(app._panel_width(120, t0 + 0.3), 40)
        app.settings.set("ui.animations", False)
        app.toggle_panel("moon")
        self.assertEqual(app._panel_width(120, t0 + 0.35), 0)

    def test_panels_draw_with_data(self):
        app = make_app(self)
        feed_session(app.panel_data)
        app.layout.open("moon")
        app.layout.open("backside")
        buf, _cursor = app.draw(120, 38, 100.0)
        text = buffer_text(buf)
        self.assertIn("Луна", text)
        self.assertIn("Обратная сторона", text)
        self.assertIn("контекст: 62%", text)

    def test_snapshot_panels_screen(self):
        from luna.devtools.snapshot import render_frames

        frames = render_frames("panels", 120, 38, [0.0])
        self.assertEqual(len(frames), 1)
        text = buffer_text(frames[0][1])
        self.assertIn("Обратная сторона", text)
        self.assertIn("Файлы", text)
        self.assertIn("Ниблер:", text)


if __name__ == "__main__":
    unittest.main()
