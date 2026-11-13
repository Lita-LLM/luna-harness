"""Клавиши ввода как у Клод Кода: слова, кольцо, Esc Esc, Ctrl+P/N."""

import unittest

from luna.ui.editor import TextEditor
from luna.ui.keys import Key

from tests.test_zadacha17 import make_app


def k(name, *mods, char=""):
    return Key(name, char=char, mods=frozenset(mods))


class FineWordsTest(unittest.TestCase):
    def test_ctrl_strelki_po_chastyam_puti(self):
        ed = TextEditor()
        ed.set_text("cat src/luna/ui.py")
        ed.word_left_fine()
        self.assertEqual(ed.text[ed.cursor:], "py")
        ed.word_left_fine()
        self.assertEqual(ed.text[ed.cursor:], ".py")
        ed.cursor = 0
        ed.word_right_fine()
        self.assertEqual(ed.cursor, 3)

    def test_udalenie_slov_i_koltso(self):
        ed = TextEditor()
        ed.set_text("один два три")
        ed.kill_word_back_fine()
        self.assertEqual(ed.text, "один два ")
        ed.cursor = 0
        ed.kill_word_forward()
        self.assertEqual(ed.text, " два ")
        ed.cursor = len(ed.text)
        ed.yank()
        self.assertTrue(ed.text.endswith("один"))
        self.assertTrue(ed.yank_pop())
        self.assertTrue(ed.text.endswith("три"))


class ChatKeysTest(unittest.TestCase):
    def test_ctrl_backspace_i_ctrl_strelki(self):
        app = make_app(self)
        app.editor.set_text("привет мир")
        app.handle_key(k("backspace", "ctrl"))
        self.assertEqual(app.editor.text, "привет ")
        app.handle_key(k("left", "ctrl"))
        self.assertEqual(app.editor.cursor, 0)

    def test_esc_esc_ubiraet_v_istoriyu(self):
        app = make_app(self)
        t = [100.0]
        app._clock = lambda: t[0]
        app.editor.set_text("набросок")
        app.handle_key(k("esc"))
        t[0] += 0.2
        app.handle_key(k("esc"))
        self.assertEqual(app.editor.text, "")
        app.handle_key(k("p", "ctrl"))
        self.assertEqual(app.editor.text, "набросок")


if __name__ == "__main__":
    unittest.main()
