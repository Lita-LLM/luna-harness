"""Круглый диск луны и кривые плавности (ЗАДАЧА-03)."""

from __future__ import annotations

import unittest

from luna.ui import anim, color
from luna.ui.disc import EARTHSHINE, MOON_TOP, draw_disc
from luna.ui.screen import Buffer

SKY = (11, 13, 23)


def _buf(cols=41, rows=21):
    buf = Buffer(cols, rows)
    buf.clear(bg=SKY)
    return buf


def _lum(rgb) -> float:
    return 0.3 * rgb[0] + 0.59 * rgb[1] + 0.11 * rgb[2]


class EaseTest(unittest.TestCase):
    def test_endpoints_and_shape(self):
        for fn in (anim.ease_in_out, anim.ease_out, anim.ease_in, anim.fade):
            self.assertEqual(fn(0.0), 0.0)
            self.assertEqual(fn(1.0), 1.0)
            self.assertEqual(fn(-1.0), 0.0)  # зажим вне диапазона
            self.assertEqual(fn(2.0), 1.0)
        self.assertEqual(anim.ease_in_out(0.5), 0.5)
        self.assertGreater(anim.ease_out(0.5), 0.5)  # быстрый старт
        self.assertLess(anim.ease_in(0.5), 0.5)      # мягкий разгон

    def test_monotonic(self):
        prev = -1.0
        for i in range(21):
            v = anim.ease_in_out(i / 20)
            self.assertGreaterEqual(v, prev)
            prev = v


class PhraseWheelTest(unittest.TestCase):
    def test_fade_in_hold_out_and_switch(self):
        wheel = anim.PhraseWheel(["раз", "два"], period=2.0,
                                 fade_seconds=0.4, started=100.0)
        text, alpha = wheel.text_alpha(100.0)
        self.assertEqual((text, alpha), ("раз", 0.0))       # выходит из фона
        text, alpha = wheel.text_alpha(100.8)
        self.assertEqual((text, alpha), ("раз", 1.0))       # держится
        text, alpha = wheel.text_alpha(101.9)
        self.assertEqual(text, "раз")
        self.assertLess(alpha, 0.2)                          # уходит в фон
        text, alpha = wheel.text_alpha(102.8)
        self.assertEqual((text, alpha), ("два", 1.0))       # следующая фраза


class DiscTest(unittest.TestCase):
    def test_full_moon_center_is_moon_color(self):
        buf = _buf()
        draw_disc(buf, 20.0, 10.0, 8.0, 1.0)
        ch, fg, bg, _ = buf.cell(20, 10)
        self.assertIsNotNone(bg)
        self.assertGreater(_lum(bg), _lum(SKY) + 60)  # центр — яркая луна

    def test_far_cells_untouched(self):
        buf = _buf()
        draw_disc(buf, 20.0, 10.0, 8.0, 1.0)
        self.assertEqual(buf.cell(0, 0), (" ", None, SKY, 0))  # угол — небо как было

    def test_edge_is_smooth_not_stepped(self):
        buf = _buf()
        draw_disc(buf, 20.0, 10.0, 8.0, 1.0)
        # вдоль строки через центр: у края диска — несколько разных
        # промежуточных яркостей (смешение по покрытию), а не один скачок
        shades = set()
        for x in range(8, 33):
            cell = buf.cell(x, 10)
            if cell[2] is not None:
                shades.add(cell[2])
        self.assertGreater(len(shades), 6)
        # и на самом краю есть полублок: верх и низ ячейки разного цвета
        halves = [buf.cell(x, 10) for x in range(41)]
        self.assertTrue(any(c[0] == "▀" and c[1] != c[2] for c in halves))

    def test_terminator_soft_and_earthshine(self):
        buf = _buf()
        draw_disc(buf, 20.0, 10.0, 8.0, 0.5)  # половина фазы: справа свет
        right = buf.cell(24, 10)[2]
        left = buf.cell(16, 10)[2]
        self.assertGreater(_lum(right), _lum(left) + 40)
        self.assertGreater(_lum(left), _lum(SKY))  # тёмная сторона видна: пепел
        self.assertLess(_lum(left), _lum(EARTHSHINE) + 25)
        # между ними — переход не в одну клетку: есть промежуточные яркости
        mid = buf.cell(20, 10)[2]
        self.assertLess(_lum(left), _lum(mid))
        self.assertLess(_lum(mid), _lum(right))

    def test_new_moon_is_dark_disc(self):
        buf = _buf()
        draw_disc(buf, 20.0, 10.0, 8.0, 0.0)
        center = buf.cell(20, 10)[2]
        self.assertLess(_lum(center), _lum(MOON_TOP) - 80)
        self.assertGreater(_lum(center), _lum(SKY))  # силуэт, а не дыра

    def test_halo_fades_into_sky(self):
        buf = _buf()
        draw_disc(buf, 20.0, 10.0, 8.0, 1.0)
        # клетка сразу за диском светлее неба, дальше — слабее, потом небо
        near = _lum(buf.cell(29, 10)[2])
        far = _lum(buf.cell(31, 10)[2])
        self.assertGreater(near, far)
        self.assertGreaterEqual(far, _lum(SKY))

    def test_ascii_mode_has_no_halfblocks(self):
        buf = _buf()
        draw_disc(buf, 20.0, 10.0, 8.0, 1.0, mode="ascii")
        chars = {buf.cell(x, y)[0] for y in range(21) for x in range(41)}
        self.assertNotIn("▀", chars)
        self.assertGreater(_lum(buf.cell(20, 10)[2]), _lum(SKY) + 40)

    def test_small_radius_and_offscreen_do_not_crash(self):
        buf = _buf(10, 5)
        draw_disc(buf, -2.0, -1.0, 3.0, 0.7)   # частично за экраном
        draw_disc(buf, 5.0, 2.0, 0.0, 1.0)     # нулевой радиус — no-op
        page = color.hex_of(buf.cell(0, 0)[2] or SKY)
        self.assertTrue(page.startswith("#"))


if __name__ == "__main__":
    unittest.main()
