"""Снимки в HTML: буфер → ожидаемый HTML, разбор аргументов, концы в концы."""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
from pathlib import Path

from luna.core.errors import HumanError
from luna.devtools import snapshot
from luna.ui.screen import Buffer


class BufferToHtmlTest(unittest.TestCase):
    def test_small_buffer_expected_html(self):
        buf = Buffer(4, 1)
        buf.set(0, 0, "А", fg=(255, 0, 0), bg=(0, 0, 255))
        buf.set(1, 0, "Б", fg=(255, 0, 0), bg=(0, 0, 255))
        buf.set(2, 0, "<", fg=None, bg=None)
        page = snapshot.buffer_to_html(buf)
        # соседние ячейки одного стиля — один span на двоих
        self.assertEqual(page.count("<span"), 1)
        self.assertIn("color:#FF0000;background:#0000FF", page)
        self.assertIn("АБ", page)
        # спецсимволы экранируются, а не ломают страницу
        self.assertIn("&lt;", page)
        self.assertNotIn("><<", page)

    def test_wide_char_takes_one_place(self):
        buf = Buffer(4, 1)
        buf.text(0, 0, "🌕x")
        page = snapshot.buffer_to_html(buf)
        self.assertIn("🌕x", page)  # продолжение "" в HTML не попало мусором

    def test_frames_page_has_captions(self):
        frames = [("0.00 с", Buffer(6, 2)), ("0.50 с", Buffer(6, 2))]
        page = snapshot.frames_to_html(frames, "Тест")
        self.assertIn("кадр 1 из 2 · 0.00 с", page)
        self.assertIn("кадр 2 из 2 · 0.50 с", page)
        self.assertEqual(page.count('class="scr"'), 2)


class ParseArgsTest(unittest.TestCase):
    def test_full_args(self):
        opts = snapshot.parse_args(["splash", "--at", "0.3", "--frames", "5",
                                    "--every", "0.1", "--size", "120x30",
                                    "-o", "x.html"])
        self.assertEqual(opts["name"], "splash")
        self.assertEqual(opts["at"], 0.3)
        self.assertEqual(opts["frames"], 5)
        self.assertEqual(opts["cols"], 120)
        self.assertEqual(opts["rows"], 30)
        self.assertEqual(opts["out"], "x.html")

    def test_no_out_is_usage_error(self):
        with self.assertRaises(HumanError):
            snapshot.parse_args(["main"])

    def test_bad_screen_is_usage_error(self):
        with self.assertRaises(HumanError):
            snapshot.parse_args(["doctor", "-o", "x.html"])


class RunSnapshotTest(unittest.TestCase):
    def test_main_snapshot_writes_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "main.html"
            rc = snapshot.run_snapshot(["main", "--size", "30x8", "-o", str(out)])
            self.assertEqual(rc, 0)
            page = out.read_text(encoding="utf-8")
            self.assertIn('class="scr"', page)
            self.assertIn("контекст", page)  # строка состояния минимального экрана

    def test_splash_storyboard_writes_frames(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "splash.html"
            rc = snapshot.run_snapshot(["splash", "--frames", "3", "--every", "0.6",
                                        "--size", "40x12", "-o", str(out)])
            self.assertEqual(rc, 0)
            page = out.read_text(encoding="utf-8")
            self.assertEqual(page.count('class="scr"'), 3)

    def test_cp1252_stdout_survives_in_process_call(self):
        # Регрессия CI Windows по zadacha-03: run_snapshot зовут прямо
        # в процессе, в обход main(), а stdout — пайп с системной
        # кодировкой cp1252 и strict: «Снимок» не влезает в charmap.
        # Печатающий вход обязан сам переконфигурировать поток в UTF-8.
        raw = io.BytesIO()
        fake = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
        real_stdout = sys.stdout
        sys.stdout = fake
        try:
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / "main.html"
                rc = snapshot.run_snapshot(["main", "--size", "30x8", "-o", str(out)])
                wrote_file = out.exists()
        finally:
            sys.stdout = real_stdout
        self.assertEqual(rc, 0)
        self.assertTrue(wrote_file)
        fake.flush()
        printed = raw.getvalue().decode("utf-8")
        fake.detach()
        self.assertIn("Снимок", printed)


if __name__ == "__main__":
    unittest.main()
