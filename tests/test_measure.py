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
            start_file = Path(tmp) / "start.txt"
            fake = mock.Mock()
            with mock.patch.object(measure.subprocess, "Popen", return_value=fake) as popen:
                proc, _t0 = measure._spawn_luna_console(tmp, marker, start_file)
            self.assertIs(proc, fake)
            kwargs = popen.call_args.kwargs
            self.assertEqual(kwargs["creationflags"], measure.CREATE_NEW_CONSOLE)
            self.assertEqual(kwargs["env"][measure.MARKER_ENV], str(marker))
            self.assertEqual(kwargs["env"][measure.START_ENV], str(start_file))
            self.assertEqual(kwargs["env"]["LUNA_HOME"], tmp)

    def test_start_run_picks_console_on_windows(self):
        fake = mock.Mock()
        seen = {}

        def fake_spawn(home, marker, start_file):
            seen["marker"] = marker
            seen["start_file"] = start_file
            return fake, 0.0

        with mock.patch.object(measure, "IS_WINDOWS", True), \
                mock.patch.object(measure, "_spawn_luna_console", fake_spawn):
            proc, _wait, _stop, _pid = measure._start_run("home")
        self.assertIs(proc, fake)
        self.assertEqual(seen["marker"].name, "first-frame.txt")
        self.assertEqual(seen["start_file"].name, "start.txt")


class StartMarkerTest(unittest.TestCase):
    """Метка старта внутри процесса (ЗАДАЧА-03): старт — не от Popen."""

    def test_process_writes_own_start_marker(self):
        import subprocess
        import sys

        with tempfile.TemporaryDirectory() as tmp:
            start_file = Path(tmp) / "start.txt"
            env = dict(os.environ, LUNA_START_FILE=str(start_file))
            proc = subprocess.Popen([sys.executable, "-m", "luna", "--version"],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    env=env)
            proc.communicate(timeout=30)
            self.assertEqual(proc.returncode, 0)
            got = measure._read_start_marker(start_file)
            self.assertIsNotNone(got)
            started, pid = got
            self.assertGreater(started, 0.0)
            self.assertEqual(pid, proc.pid)  # PID — самой Луны, не обёртки

    def test_read_marker_variants(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "start.txt"
            path.write_text("1000.5\n777\n", encoding="ascii")
            self.assertEqual(measure._read_start_marker(path), (1000.5, 777))
            path.write_text("1000.5\n", encoding="ascii")  # старый формат без PID
            self.assertEqual(measure._read_start_marker(path), (1000.5, None))
            path.write_bytes(b"garbage")
            self.assertIsNone(measure._read_start_marker(path))
            self.assertIsNone(measure._read_start_marker(Path(tmp) / "нет.txt"))

    def test_metric_pid_prefers_child_marker(self):
        self.assertEqual(measure._metric_pid(111, (1000.0, 222)), 222)
        self.assertEqual(measure._metric_pid(111, (1000.0, None)), 111)
        self.assertEqual(measure._metric_pid(111, None), 111)

    def test_wait_start_frame_counts_from_process_start(self):
        class Proc:
            def poll(self):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "first-frame.txt"
            start_file = Path(tmp) / "start.txt"
            marker.write_text(repr(1000.30), encoding="ascii")
            start_file.write_text("1000.10\n5\n", encoding="ascii")
            dt = measure._wait_start_frame(marker, start_file, 999.0, Proc(),
                                           timeout=1.0)
            # от метки процесса (1000.10), а не от Popen (999.0): 0.20, не 1.30
            self.assertAlmostEqual(dt, 0.20, places=3)


class MemoryCountersLayoutTest(unittest.TestCase):
    """Раскладка PROCESS_MEMORY_COUNTERS: первые поля — DWORD, не SIZE_T.

    Из-за c_size_t везде WorkingSetSize читался не со своего смещения,
    и замер памяти на Windows показывал 0,1 МБ (ЗАДАЧА-03).
    """

    def test_layout(self):
        import ctypes

        PMC = measure._memory_counters_class()
        fields = dict(PMC._fields_)
        self.assertIs(fields["cb"], ctypes.c_uint32)
        self.assertIs(fields["PageFaultCount"], ctypes.c_uint32)
        self.assertIs(fields["WorkingSetSize"], ctypes.c_size_t)
        self.assertEqual(PMC.WorkingSetSize.offset,
                         8 + ctypes.sizeof(ctypes.c_size_t))


if __name__ == "__main__":
    unittest.main()
