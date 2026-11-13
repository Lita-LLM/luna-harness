"""Ctrl+B: идущая команда bash уходит в фон, вывод — в лог, конец — пометкой."""

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from luna.tools.registry import ToolContext
from luna.tools.shell import _bash, _decode

SCRIPT = ("import time,sys\n"
          "print('start', flush=True)\n"
          "time.sleep(1.0)\n"
          "print('finish', flush=True)\n")


class DetachTest(unittest.TestCase):
    def test_v_fon_i_pometka(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "slow.py"
            script.write_text(SCRIPT, encoding="utf-8")
            done = []
            finished = threading.Event()

            def on_done(pid, command, code, log):
                done.append((pid, code, log))
                finished.set()

            ctx = ToolContext(cwd=Path(tmp), output_dir=Path(tmp))
            ctx.detach_event = threading.Event()
            ctx.on_background_done = on_done
            # «Человек» жмёт Ctrl+B через 0,4 с после старта.
            threading.Timer(0.4, ctx.detach_event.set).start()
            started = time.monotonic()
            res = _bash(ctx, {"command": '"' + sys.executable + '" "' + str(script) + '"'})
            self.assertTrue(res.ok)
            self.assertLess(time.monotonic() - started, 0.9)  # ход не ждал конца
            self.assertIn("Ctrl+B", res.text)
            self.assertTrue(finished.wait(10))
            pid, code, log = done[0]
            self.assertEqual(code, 0)
            text = _decode(Path(log).read_bytes())  # кодировку вывода знает тул
            self.assertIn("start", text)
            self.assertIn("finish", text)
            self.assertIn(pid, ctx.background)

    def test_obychnaya_komanda_kak_ran_she(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = ToolContext(cwd=Path(tmp))
            ctx.detach_event = threading.Event()
            res = _bash(ctx, {"command": "echo привет"})
            self.assertTrue(res.ok)
            self.assertIn("привет", res.text)


if __name__ == "__main__":
    unittest.main()
