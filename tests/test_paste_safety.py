"""Жирные вставки не вешают и не роняют Луну, ошибка одного события не
губит сессию, новые фразы на месте."""

import time
import unittest

from luna.ui import phrases
from luna.ui.chat import _is_file, _user_view
from luna.ui.errtrap import ErrorTrap
from luna.ui.keys import Key, KeyParser, Paste
from tests.test_carry_undo import TempHomeBase, make_app


class ParserPasteTest(unittest.TestCase):
    def test_big_bracketed_paste_byte_by_byte_is_linear(self):
        # Windows отдаёт ввод по символу: склейка строкой была квадратичной.
        body = "\r".join("строка %d" % i for i in range(20000))
        blob = ("\x1b[200~" + body + "\x1b[201~").encode()
        parser = KeyParser()
        started = time.monotonic()
        events = []
        for i in range(len(blob)):
            events += parser.feed(blob[i:i + 1])
        self.assertLess(time.monotonic() - started, 5.0)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].text, body.replace("\r", "\n"))

    def test_burst_without_bracketed_paste_is_a_paste(self):
        now = [100.0]
        parser = KeyParser(clock=lambda: now[0])
        self.assertEqual(parser.feed(b"one\rtwo\rthree"), [Paste("one\ntwo\nthree")])
        now[0] += 0.03  # хвост той же вставки следующим чтением
        self.assertEqual(parser.feed(b"four\r"), [Paste("four\n")])

    def test_typing_with_enter_stays_keys(self):
        parser = KeyParser(clock=lambda: 5.0)
        events = parser.feed(b"hi\r")
        self.assertEqual(events, [Key("h", char="h"), Key("i", char="i"), Key("enter")])


class ChatPasteTest(TempHomeBase):
    def test_huge_one_line_does_not_crash_or_hang(self):
        self.assertFalse(_is_file("x" * 5000))
        self.assertFalse(_is_file("a\x00b"))
        app = make_app(self)
        app.handle_paste(Paste("x" * 300_000))
        self.assertIn("символов]", app.editor.text)
        self.assertLess(len(app.editor.text), 100)
        app.submit_text()
        sent = [c for c in app.source.commands if c.get("type") == "user.message"]
        self.assertEqual(len(sent[-1]["text"]), 300_000)
        started = time.monotonic()
        for _ in range(3):
            app.draw(100, 30, time.monotonic() + 5)
        self.assertLess(time.monotonic() - started, 2.0)

    def test_user_message_folds_in_feed(self):
        text = "\n".join("строка " + str(i) for i in range(500))
        shown, more = _user_view(text, False)
        self.assertEqual(shown.count("\n"), 11)
        self.assertIn("ещё 488 строк", more)
        shown, more = _user_view(text, True)
        self.assertEqual((shown, more), (text, ""))
        self.assertEqual(_user_view("коротко", False), ("коротко", ""))


class SurviveErrorsTest(TempHomeBase):
    def test_bad_event_does_not_kill_the_rest(self):
        app = make_app(self)
        seen = []
        original = app.on_agent_event

        def flaky(ev):
            if ev.get("type") == "boom":
                raise RuntimeError("сломанное событие")
            seen.append(ev.get("type"))
            return original(ev)

        app.on_agent_event = flaky
        app.source.poll = lambda now=None: [{"type": "boom"},
                                            {"type": "notice", "text": "дальше"}]
        app.poll_source()
        self.assertEqual(seen, ["notice"])
        self.assertTrue(any(e.kind == "error" and "сломанное событие" in e.text
                            for e in app.entries))

    def test_trap_catch_dedupes_repeats(self):
        trap = ErrorTrap(self.tmp / "logs" / "t.log")
        trap.install()
        try:
            for _ in range(5):
                trap.catch("Отрисовка кадра", ValueError("кадр"))
            trap.catch("Обработка клавиши", KeyError("x"))
            events = trap.drain()
        finally:
            trap.uninstall()
        self.assertEqual(len(events), 2)
        self.assertIn("пережила", events[0]["message"])


class PhrasesTest(unittest.TestCase):
    def test_new_phrases(self):
        self.assertIn("Опять работа?", phrases.splash_phrases())
        dark = phrases.splash_phrases(newmoon=True)
        self.assertTrue(dark)
        self.assertFalse(set(dark) & set(phrases.splash_phrases()))
        self.assertNotEqual(phrases.spinner_phrases(True), phrases.spinner_phrases(False))
        self.assertTrue(phrases.event_phrase("end"))
        self.assertTrue(phrases.event_phrase("newmoon").startswith("Новолуние"))


if __name__ == "__main__":
    unittest.main()
