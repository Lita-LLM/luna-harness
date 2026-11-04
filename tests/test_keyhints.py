"""Кликабельные подсказки клавиш: разбор строк и клик вместо клавиши."""

from __future__ import annotations

import unittest

from luna.ui.keyhints import find_key_hints, parse_key

from tests.test_zadacha20 import make_app, mouse


def labels(text: str) -> list:
    return [(text[a:b].split(" — ")[0], k.label) for a, b, k in find_key_hints(text)]


class ParseTest(unittest.TestCase):
    def test_keys(self):
        self.assertEqual(parse_key("Alt+S").label, "alt+s")
        self.assertEqual(parse_key("Shift+Tab").label, "shift+tab")
        self.assertEqual(parse_key("F6").label, "f6")
        self.assertEqual(parse_key("N").label, "N")
        self.assertEqual(parse_key("↑↓/Tab").label, "tab")
        self.assertEqual(parse_key("Esc/F6").label, "esc")
        for not_key in ("печатайте", "↑↓", "↑ на пустом поле", ""):
            self.assertIsNone(parse_key(not_key), not_key)

    def test_hint_line(self):
        self.assertEqual(
            labels("печатайте — поиск · Enter — выполнить · Esc — закрыть"),
            [("Enter", "enter"), ("Esc", "esc")])
        self.assertEqual(labels("в заначке: x (Ctrl+S — вернуть)"),
                         [("Ctrl+S", "ctrl+s")])
        self.assertEqual(labels("правка: Enter — отправить, Esc — отмена"),
                         [("Enter", "enter"), ("Esc", "esc")])
        self.assertEqual(labels("просто текст с тире — без клавиши"), [])


class ClickTest(unittest.TestCase):
    def key_zone(self, app, label):
        return next(z for z in reversed(app.zones)
                    if z[4][0] == "key" and z[4][1].label == label)

    def test_click_esc_closes_picker(self):
        app = make_app(self)
        app.open_help_picker()
        app.draw(120, 30, app._clock())
        zone = self.key_zone(app, "esc")
        app.handle_mouse(mouse("press", zone[0], zone[1]))
        self.assertIsNone(app.picker)

    def test_click_guard_answer_and_hover(self):
        app = make_app(self)
        app.on_agent_event({"type": "guard.ask", "id": "g1", "tool": "bash",
                            "args": {"command": "ls"}, "intent": "", "risk": "low",
                            "reason": "", "choices": []})
        app.draw(120, 30, app._clock())
        zone = self.key_zone(app, "n")
        app.handle_mouse(mouse("move", zone[0], zone[1]))
        buf, _cur = app.draw(120, 30, app._clock())
        self.assertTrue(buf.cell(zone[0], zone[1])[3] & 8)  # подчёркнут
        app.handle_mouse(mouse("press", zone[0], zone[1]))
        answers = [c for c in app.sent_commands if c["type"] == "guard.answer"]
        self.assertEqual(answers[0]["decision"], "no")
        app.draw(120, 30, app._clock())
        self.assertFalse(any(z[4][0] == "key" and z[4][1].label == "n"
                             for z in app.zones))

    def test_setting_off(self):
        app = make_app(self, **{"ui.click_hints": False})
        app.open_help_picker()
        app.draw(120, 30, app._clock())
        self.assertFalse(any(z[4][0] == "key" for z in app.zones))


if __name__ == "__main__":
    unittest.main()
