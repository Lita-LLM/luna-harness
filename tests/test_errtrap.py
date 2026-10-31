"""Ловушка ошибок полноэкранного режима (ЗАДАЧА-06, п. 2): исключения
фоновых потоков и stderr уходят в лог и в ленту событиями, а не на экран."""

from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from luna.ui.errtrap import ErrorTrap


class ErrorTrapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = Path(self.tmp.name) / "logs" / "chat-test.log"
        self.prev_sys_hook = sys.excepthook
        self.prev_thread_hook = threading.excepthook
        self.prev_stderr = sys.stderr
        self.trap = ErrorTrap(self.log)
        self.trap.install()
        self.addCleanup(self.trap.uninstall)

    def test_thread_exception_goes_to_queue_and_log(self):
        def boom():
            raise ValueError("бум в потоке")

        t = threading.Thread(target=boom, name="luna-test-worker")
        t.start()
        t.join(timeout=5)
        self.assertFalse(t.is_alive())
        events = self.trap.drain()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "error")
        self.assertIn("luna-test-worker", events[0]["message"])
        self.assertIn("бум в потоке", events[0]["message"])
        self.assertIn("ValueError", events[0]["detail"])
        text = self.log.read_text(encoding="utf-8")
        self.assertIn("ValueError: бум в потоке", text)
        self.assertIn("Traceback", text)

    def test_stderr_redirected_to_log(self):
        self.assertIs(sys.stderr, self.trap._file)
        print("сырой вывод мимо экрана", file=sys.stderr)
        sys.stderr.flush()
        self.trap.uninstall()
        self.assertIsNot(sys.stderr, self.trap._file)
        text = self.log.read_text(encoding="utf-8")
        self.assertIn("сырой вывод мимо экрана", text)

    def test_uninstall_restores_hooks(self):
        self.trap.uninstall()
        self.assertIs(sys.excepthook, self.prev_sys_hook)
        self.assertEqual(threading.excepthook, self.prev_thread_hook)
        self.assertIs(sys.stderr, self.prev_stderr)
        # повторная установка после снятия работает
        self.trap.install()
        self.assertTrue(self.trap._installed)

    def test_drain_empty(self):
        self.assertEqual(self.trap.drain(), [])
        self.assertFalse(self.trap.pending)


if __name__ == "__main__":
    unittest.main()
