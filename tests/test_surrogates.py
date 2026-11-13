"""Смайлики вне BMP: половинки UTF-16 не роняют ни ввод, ни вывод."""

import io
import unittest

from luna.ui.term import fix_surrogates

HI, LO = chr(0xD83D), chr(0xDE08)


class SurrogateTest(unittest.TestCase):
    def test_para_skleivaetsya(self):
        self.assertEqual(fix_surrogates(HI + LO), "\U0001F608")
        self.assertEqual(fix_surrogates(HI + LO + HI + LO), "\U0001F608" * 2)

    def test_odinochka_zamenyaetsya(self):
        self.assertEqual(fix_surrogates("a" + HI + "b"), "a�b")

    def test_vyvod_ne_uhodit_v_nikuda(self):
        from luna.ui.term import Terminal

        raw = io.BytesIO()
        out = io.TextIOWrapper(raw, encoding="utf-8")
        term = Terminal.__new__(Terminal)
        term.out = out
        term.write("ок " + HI + LO + " " + HI)
        term.write("дальше")
        out.flush()
        text = raw.getvalue().decode("utf-8")
        self.assertIn("\U0001F608", text)
        self.assertIn("дальше", text)


if __name__ == "__main__":
    unittest.main()
