"""ЗАДАЧА-22: нетбук и понятный рой.

Полоса прокрутки и тяга ленты — на подменённых событиях мыши и ручных
часах; замер кадра — на длинной ленте в буфер; Ниблер — только внутри
раскладки; рой — отказ на одном входе, параллельная смета, почта без
писем самому себе; luna -p — перевод строки после ответа.
"""

from __future__ import annotations

import io
import tempfile
import threading
import time
import unittest
from pathlib import Path

from luna.core.bus import EventBus
from luna.core.paths import DataDir
from luna.core.settings import Settings
from luna.ui.anim import Scheduler
from luna.ui.chat import ChatApp, Entry
from luna.ui.keys import Mouse


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


def mouse(kind: str, x: int, y: int) -> Mouse:
    """Мышь по клеткам 0-based: события SGR — 1-based."""
    return Mouse(kind, x + 1, y + 1, button=0)


def long_feed(app: ChatApp, pairs: int = 40) -> None:
    for i in range(pairs):
        app.entries.append(Entry("user", text="вопрос номер " + str(i)))
        app.entries.append(Entry("agent", text="ответ номер " + str(i) +
                                               " с пояснением в одну строку"))


def buffer_text(buf) -> str:
    return "\n".join("".join(ch for ch, _f, _b, _a in row).rstrip()
                     for row in buf.cells)


# --- раздел 1: полоса прокрутки ------------------------------------------------


class ScrollbarTest(unittest.TestCase):
    def test_drawn_at_feed_edge_and_pages_on_track_click(self):
        app = make_app(self)
        long_feed(app)
        buf, _ = app.draw(60, 16, app._clock())
        self.assertIsNotNone(app._sb_geom)
        total, feed_h, max_scroll, thumb_y, thumb_h = app._sb_geom
        self.assertGreater(total, feed_h)
        self.assertGreater(max_scroll, 0)
        # Полоса — в последней колонке ленты; ползунок у низа (мы внизу).
        chars = [buf.cells[y][59][0] for y in range(feed_h)]
        self.assertIn("█", chars)
        self.assertIn("│", chars)
        self.assertEqual(thumb_y + thumb_h, feed_h)
        # Клик по дорожке выше ползунка — страница к старому.
        app.handle_mouse(mouse("press", 59, 0))
        self.assertEqual(app.scroll, app._page)
        app.handle_mouse(mouse("release", 59, 0))
        # Клик по дорожке ниже ползунка — страница к новому.
        app.draw(60, 16, app._clock())
        _t, fh, _m, ty, th = app._sb_geom
        app.handle_mouse(mouse("press", 59, fh - 1))
        self.assertEqual(app.scroll, 0)

    def test_thumb_drag(self):
        app = make_app(self)
        long_feed(app)
        app.draw(60, 16, app._clock())
        _total, feed_h, max_scroll, thumb_y, thumb_h = app._sb_geom
        # Захват за середину ползунка и тяга на самый верх.
        grab_y = thumb_y + thumb_h // 2
        app.handle_mouse(mouse("press", 59, grab_y))
        self.assertIsNotNone(app._sb_drag)
        app.handle_mouse(mouse("move", 59, 0))
        app.handle_mouse(mouse("release", 59, 0))
        self.assertEqual(app.scroll, max_scroll)
        self.assertIsNone(app._sb_drag)

    def test_off_and_auto_short_feed(self):
        app = make_app(self, **{"ui.scrollbar": "off"})
        long_feed(app)
        app.draw(60, 16, app._clock())
        self.assertIsNone(app._sb_geom)
        app2 = make_app(self)  # auto + короткая лента — полосы нет
        app2.entries.append(Entry("user", text="коротко"))
        app2.draw(60, 16, app2._clock())
        self.assertIsNone(app2._sb_geom)
        app3 = make_app(self, **{"ui.scrollbar": "on"})  # on — есть всегда
        app3.entries.append(Entry("user", text="коротко"))
        app3.draw(60, 16, app3._clock())
        self.assertIsNotNone(app3._sb_geom)

    def test_scrollbar_at_feed_edge_not_screen_edge_with_panels(self):
        app = make_app(self)
        long_feed(app)
        app.toggle_panel("moon")
        # now — после анимации ширины колонки, иначе панели ещё «едут».
        buf, _ = app.draw(100, 24, app._clock() + 1.0)
        chat_w, _feed_h = app._feed_geom
        self.assertLess(chat_w, 100)
        self.assertIsNotNone(app._sb_geom)
        col = [buf.cells[y][chat_w - 1][0] for y in range(app._sb_geom[1])]
        self.assertTrue(any(c in "█│" for c in col))


# --- раздел 1: перетаскивание ленты --------------------------------------------


class DragScrollTest(unittest.TestCase):
    def test_vertical_drag_scrolls_feed(self):
        app = make_app(self)
        long_feed(app)
        app.draw(60, 16, app._clock())
        app.scroll = 10
        app.handle_mouse(mouse("press", 10, 5))
        app.handle_mouse(mouse("move", 10, 8))   # палец вниз на 3 строки
        self.assertEqual(app.scroll, 13)
        app.handle_mouse(mouse("move", 10, 9))
        self.assertEqual(app.scroll, 14)
        app.handle_mouse(mouse("release", 10, 9))
        self.assertIsNone(app.selection)

    def test_horizontal_drag_still_selects(self):
        app = make_app(self)
        copied = []
        app.copy_hook = copied.append
        app.entries.append(Entry("agent", text="альфа бета гамма"))
        app.draw(60, 16, app._clock())
        app.handle_mouse(mouse("press", 1, 0))
        app.handle_mouse(mouse("move", 6, 0))
        app.handle_mouse(mouse("release", 6, 0))
        self.assertEqual(copied, ["льфа б"])

    def test_long_press_selects(self):
        app = make_app(self)
        now = [1000.0]
        app._clock = lambda: now[0]
        copied = []
        app.copy_hook = copied.append
        app.entries.append(Entry("agent", text="альфа бета гамма"))
        app.draw(60, 16, now[0])
        app.handle_mouse(mouse("press", 1, 0))
        self.assertIsNone(app.selection)
        now[0] += 0.7  # держим дольше порога долгого нажатия
        app.draw(60, 16, now[0])
        self.assertIsNotNone(app.selection)
        app.handle_mouse(mouse("move", 6, 0))
        app.handle_mouse(mouse("release", 6, 0))
        self.assertEqual(copied, ["льфа б"])

    def test_drag_scroll_off_restores_press_selection(self):
        app = make_app(self, **{"ui.drag_scroll": False})
        app.entries.append(Entry("agent", text="альфа бета гамма"))
        for i in range(30):
            app.entries.append(Entry("agent", text="ещё строка " + str(i)))
        app.draw(60, 16, app._clock())
        app.handle_mouse(mouse("press", 1, 0))
        self.assertIsNotNone(app._sel_anchor)  # старое поведение: сразу якорь
        before = app.scroll
        app.handle_mouse(mouse("move", 2, 3))  # вертикальная тяга выделяет
        self.assertEqual(app.scroll, before)
        self.assertIsNotNone(app.selection)


# --- раздел 2: производительность ------------------------------------------------


class PerfTest(unittest.TestCase):
    ANSWER = ("Отвечаю по пунктам:\n\n"
              "- первое — **важное** про сборку и тесты,\n"
              "- второе — `код` и пути вида luna/ui/chat.py,\n"
              "- третье — длинная строка пояснения, которая не влезает "
              "в ширину экрана и честно переносится на следующую.\n\n"
              "Итого: всё собралось, тесты зелёные, можно двигаться дальше.")

    def test_frame_cost_on_long_feed(self):
        app = make_app(self)
        for i in range(250):
            app.entries.append(Entry("user", text="вопрос " + str(i)))
            app.entries.append(Entry("agent", text=self.ANSWER))
        now = app._clock()
        app.draw(100, 30, now)  # прогрев кэша строк записей
        app.draw(100, 30, now)
        self.assertGreaterEqual(len(app._feed_texts), 1800)
        samples = []
        for _ in range(5):
            t0 = time.perf_counter()
            app.draw(100, 30, now)
            samples.append((time.perf_counter() - t0) * 1000)
        samples.sort()
        median = samples[len(samples) // 2]
        # До кэша кадр на такой ленте стоил ~275 мс; цель — заметно ниже 100.
        self.assertLess(median, 100.0)

    def test_auto_low_engages_and_warns_once(self):
        app = make_app(self)
        app.entries.append(Entry("user", text="привет"))
        for _ in range(30):
            app._note_frame_time(0.1)  # стабильно долгие кадры
        self.assertTrue(app._low_perf())
        app.draw(60, 16, app._clock())
        notes = [e for e in app.entries
                 if e.kind == "notice" and "слабый режим" in e.text]
        self.assertEqual(len(notes), 1)
        app.draw(60, 16, app._clock())
        notes = [e for e in app.entries
                 if e.kind == "notice" and "слабый режим" in e.text]
        self.assertEqual(len(notes), 1)  # предупреждение — один раз

    def test_perf_modes(self):
        low = make_app(self, **{"ui.perf": "low"})
        self.assertTrue(low._low_perf())
        full = make_app(self, **{"ui.perf": "full"})
        for _ in range(40):
            full._note_frame_time(0.2)
        self.assertFalse(full._low_perf())  # full — слабый режим никогда сам


# --- раздел 3: Ниблер внутри раскладки -------------------------------------------


class NibblerLayoutTest(unittest.TestCase):
    def _app_with_long_nibbler(self, **overrides) -> ChatApp:
        app = make_app(self, **overrides)
        app.on_agent_event({"type": "session.start", "cwd": "/tmp",
                            "model": "fake", "mode": "normal"})
        app.submit_text("почини сборку")
        app.on_agent_event({"type": "nibbler.say", "severity": "serious",
                            "to": "human",
                            "text": "очень длинное наблюдение ниблера " * 8})
        return app

    def _assert_inside(self, app: ChatApp, cols: int, rows: int) -> None:
        # now — после анимации ширины колонки панелей.
        buf, _ = app.draw(cols, rows, app._clock() + 1.0)
        chat_w, _feed_h = app._feed_geom
        text = buffer_text(buf)
        self.assertIn("Ниблер:", text)
        for row in buf.cells:
            line = "".join(ch for ch, _f, _b, _a in row)
            if "Ниблер" in line or "наблюдение" in line:
                # Правее границы ленты реплики быть не должно.
                self.assertNotIn("наблюдение", line[chat_w:])
                self.assertNotIn("Ниблер", line[chat_w:])

    def test_panels_open(self):
        app = self._app_with_long_nibbler()
        app.toggle_panel("moon")
        app.toggle_panel("files")
        self._assert_inside(app, 80, 24)
        buf, _ = app.draw(80, 24, app._clock() + 1.0)
        self.assertIn("Луна", buffer_text(buf))  # панель цела и читается

    def test_panels_closed_narrow_and_low(self):
        app = self._app_with_long_nibbler()
        self._assert_inside(app, 50, 12)
        self._assert_inside(app, 120, 40)


# --- раздел 4: рой, понятный модели ----------------------------------------------


def make_owner(root: Path):
    """Владелец-агент на фальшивой модели — как в tests/test_starfall.py."""
    from luna.core.secrets import Secrets
    from luna.mind.loop import Agent
    from luna.router.core import Router

    work = root / "work"
    work.mkdir(parents=True, exist_ok=True)
    settings = Settings(home=root / "home", cwd=work)
    settings.set("model.provider", "fake")
    settings.set("model.kind", "fake")
    settings.set("model.main", "fake:echo")
    settings.set("model.fast", "fake:echo")
    secrets = Secrets(root / "home" / "secrets.json")
    agent = Agent(settings=settings, secrets=secrets,
                  router=Router(settings, secrets), cwd=work, mode="auto")
    return agent, settings, work


class SwarmGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        from luna.starfall.tools import _starfall

        self._starfall = _starfall
        self.agent, self.settings, self.work = make_owner(self.root)
        self.settings.set("starfall.output_dir", str(self.root / "sf"))
        self.ctx = self.agent.ctx

    def test_one_input_refused_with_hint(self):
        out = self._starfall(self.ctx, {"action": "run", "profile": "cls",
                                        "inputs": ["один"]})
        self.assertFalse(out.ok)
        self.assertIn("min_inputs", out.text)
        self.assertIn("assistant", out.text)     # подсказка: одна задача — туда
        self.assertIn("force=true", out.text)    # человек может настоять
        est = self._starfall(self.ctx, {"action": "estimate", "profile": "cls",
                                        "inputs": ["раз", "два"]})
        self.assertFalse(est.ok)                 # смета — тоже не на горсть

    def test_force_and_threshold_setting(self):
        out = self._starfall(self.ctx, {"action": "run", "profile": "cls",
                                        "inputs": ["один"], "force": True})
        self.assertTrue(out.ok, out.text)
        self.settings.set("starfall.min_inputs", 5)
        out = self._starfall(self.ctx, {"action": "run", "profile": "cls",
                                        "inputs": ["а", "б", "в", "г"]})
        self.assertFalse(out.ok)
        out = self._starfall(self.ctx, {"action": "run", "profile": "cls",
                                        "inputs": ["а", "б", "в", "г", "д"]})
        self.assertTrue(out.ok, out.text)

    def test_bad_inputs_answer_with_example(self):
        out = self._starfall(self.ctx, {"action": "run", "profile": "cls",
                                        "inputs": {"странное": 1},
                                        "force": True})
        self.assertFalse(out.ok)
        self.assertIn("glob", out.text)          # пример правильной формы
        self.assertIn("file_lines", out.text)


class SwarmEngine22Test(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_estimate_samples_run_in_parallel(self):
        from luna.starfall.engine import estimate
        from luna.starfall.profiles import get_profile

        lock = threading.Lock()
        state = {"now": 0, "peak": 0}

        def slow_runner(profile, task_text, meta):
            with lock:
                state["now"] += 1
                state["peak"] = max(state["peak"], state["now"])
            time.sleep(0.3)
            with lock:
                state["now"] -= 1
            return "ok", 10, 5

        started = time.monotonic()
        est = estimate(slow_runner, get_profile("cls"),
                       ["а", "б", "в", "г"], "", None, tmp_dir=self.root)
        wall = time.monotonic() - started
        self.assertEqual(est["samples"], 3)
        self.assertGreaterEqual(state["peak"], 2)  # пробы реально параллельны
        self.assertLess(wall, 0.75)                # по очереди было бы ≥0,9 с

    def test_tree_mail_has_no_self_letters(self):
        from luna.starfall.engine import SwarmRun
        from luna.starfall.profiles import get_profile

        def stub(profile, task_text, meta):
            return "готово", 10, 5

        events = []
        run = SwarmRun(stream="leo", profiles=[get_profile("cls")],
                       inputs=["а", "б", "в"], output_dir=self.root / "out",
                       runner=stub, parallel=3, link_size=8,
                       budget_usd=1.0, mail="tree", publish=events.append)
        summary = run.run()
        self.assertEqual(summary["done"], 3)
        mails = [e for e in events if e.get("type") == "agent.message"]
        self.assertTrue(mails)  # почта звеньев хозяину и внутри звена есть
        for mail_event in mails:
            self.assertNotEqual(mail_event.get("from"), mail_event.get("to"),
                                "письмо самому себе: " + str(mail_event))


# --- раздел 4: luna -p — перевод строки после ответа -------------------------------


class HeadlessNewlineTest(unittest.TestCase):
    def test_answer_closed_before_progress_lines(self):
        from luna.core.secrets import Secrets
        from luna.devtools.fake_llm import set_script
        from luna.headless import run_headless
        from luna.router.core import ProviderConfig, Router

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        work = root / "work"
        work.mkdir(parents=True)
        settings = Settings(home=root / "home", cwd=work)
        settings.set("model.provider", "fake")
        settings.set("model.kind", "fake")
        settings.set("model.main", "fake:echo")
        secrets = Secrets(root / "home" / "secrets.json")
        providers = {"fake": ProviderConfig("fake", "fake", "", None)}
        router = Router(settings, secrets, providers=providers)
        set_script([])
        self.addCleanup(lambda: set_script([]))

        log = []

        class Rec(io.StringIO):
            def __init__(self, tag):
                super().__init__()
                self.tag = tag

            def write(self, s):
                if s:
                    log.append((self.tag, s))
                return super().write(s)

        out, err = Rec("out"), Rec("err")
        code = run_headless("привет", settings=settings, secrets=secrets,
                            router=router, cwd=work, out=out, err=err)
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue(), "Эхо: привет\n")
        # Перевод строки в stdout записан ДО первой строки хода в stderr.
        newline_at = next(i for i, (tag, s) in enumerate(log)
                          if tag == "out" and s == "\n")
        progress_at = next(i for i, (tag, s) in enumerate(log)
                           if tag == "err" and "ход завершён" in s)
        self.assertLess(newline_at, progress_at)


if __name__ == "__main__":
    unittest.main()
