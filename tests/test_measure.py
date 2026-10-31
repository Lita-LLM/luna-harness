"""Замеры: протокол метки первого кадра и платформенный спавн (РЕВЬЮ-01, п. 3).

Живой Windows здесь нет, поэтому Windows-путь проверяется по частям:
протокол метки честно гоняется через настоящий Screen, спавн — на подменённом
Popen (флаг консоли и переменные окружения), ветвление — на подменённом
IS_WINDOWS. Сам замер в новой консоли — только на живой Windows.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from luna.devtools import measure
from luna.ui.screen import Buffer, Screen


class MarkerProtocolTest(unittest.TestCase):
    def test_screen_writes_marker_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "first-frame.txt"
            frames: list = []
            with mock.patch.dict(os.environ, {measure.MARKER_ENV: str(marker)}):
                screen = Screen(frames.append, "truecolor", 8, 2)
                buf = Buffer(8, 2)
                buf.text(0, 0, "раз")
                screen.present(buf)
                self.assertTrue(marker.exists())
                first = marker.read_text(encoding="ascii")
                float(first)  # в файле — число (монотонные часы ребёнка)
                buf.set(0, 0, "Д")
                screen.present(buf)
                self.assertEqual(marker.read_text(encoding="ascii"), first)

    def test_no_marker_without_env(self):
        env = {k: v for k, v in os.environ.items() if k != measure.MARKER_ENV}
        with mock.patch.dict(os.environ, env, clear=True):
            screen = Screen([].append, "truecolor", 8, 2)
            buf = Buffer(8, 2)
            buf.text(0, 0, "раз")
            self.assertIn("раз", screen.present(buf))  # и никакой магии вокруг

    def test_wait_reads_marker_timestamp(self):
        class Proc:
            def poll(self):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "first-frame.txt"
            marker.write_text(repr(1000.25), encoding="ascii")
            dt = measure._wait_first_frame_marker(marker, 1000.0, Proc(), timeout=1.0)
            self.assertAlmostEqual(dt, 0.25, places=3)

    def test_wait_gives_up_when_process_died(self):
        class Proc:
            def poll(self):
                return 1

        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "first-frame.txt"
            self.assertIsNone(
                measure._wait_first_frame_marker(marker, 0.0, Proc(), timeout=1.0))


class WindowsSpawnTest(unittest.TestCase):
    def test_console_spawn_passes_flag_and_marker_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "first-frame.txt"
            fake = mock.Mock()
            with mock.patch.object(measure.subprocess, "Popen", return_value=fake) as popen:
                proc, _t0 = measure._spawn_luna_console(tmp, marker)
            self.assertIs(proc, fake)
            kwargs = popen.call_args.kwargs
            self.assertEqual(kwargs["creationflags"], measure.CREATE_NEW_CONSOLE)
            self.assertEqual(kwargs["env"][measure.MARKER_ENV], str(marker))
            self.assertEqual(kwargs["env"]["LUNA_HOME"], tmp)

    def test_start_run_picks_console_on_windows(self):
        fake = mock.Mock()
        seen = {}

        def fake_spawn(home, marker):
            seen["marker"] = marker
            return fake, 0.0

        with mock.patch.object(measure, "IS_WINDOWS", True), \
                mock.patch.object(measure, "_spawn_luna_console", fake_spawn):
            proc, _wait, _stop = measure._start_run("home")
        self.assertIs(proc, fake)
        self.assertEqual(seen["marker"].name, "first-frame.txt")


if __name__ == "__main__":
    unittest.main()
