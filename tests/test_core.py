"""Ядро: шина, идентификаторы, атомарная запись, json5, блокировка копии."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from luna.core.bus import EventBus
from luna.core.errors import HumanError
from luna.core.fsutil import atomic_write_text
from luna.core.ids import new_id
from luna.core.json5 import loads as json5_loads
from luna.core.run_lock import RunLock


class BusTest(unittest.TestCase):
    def test_listen_and_seq(self):
        bus = EventBus()
        seen = []
        bus.listen(seen.append)
        bus.publish({"type": "a"})
        bus.publish({"type": "b"})
        self.assertEqual([e["type"] for e in seen], ["a", "b"])
        self.assertEqual([e["seq"] for e in seen], [1, 2])
        self.assertIn("ts", seen[0])

    def test_listener_error_does_not_break_bus(self):
        bus = EventBus()
        bus.listen(lambda ev: 1 / 0)
        seen = []
        bus.listen(seen.append)
        bus.publish({"type": "x"})
        self.assertEqual(len(seen), 1)

    def test_queue_overflow_marker(self):
        async def run():
            bus = EventBus()
            bus.bind_loop(asyncio.get_running_loop())
            q = bus.subscribe(maxsize=2)
            for i in range(6):
                bus.publish({"type": "e", "i": i})
            out = []
            while not q.empty():
                out.append(q.get_nowait())
            return out

        out = asyncio.run(run())
        self.assertTrue(any(e.get("type") == "overflow" for e in out))


class IdsTest(unittest.TestCase):
    def test_format_and_monotonic(self):
        ids = [new_id() for _ in range(200)]
        self.assertTrue(all(len(i) == 26 for i in ids))
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(len(set(ids)), 200)


class FsutilTest(unittest.TestCase):
    def test_atomic_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub" / "file.txt"
            atomic_write_text(path, "раз\n")
            atomic_write_text(path, "два\n")
            self.assertEqual(path.read_text(encoding="utf-8"), "два\n")
            leftovers = [p for p in Path(tmp).rglob("*") if ".lntmp" in p.name]
            self.assertEqual(leftovers, [])


class Json5Test(unittest.TestCase):
    def test_lenient(self):
        data = json5_loads("{ // комментарий\n  a: 1, b: 'x', c: [1, 2,], }")
        self.assertEqual(data, {"a": 1, "b": "x", "c": [1, 2]})

    def test_plain_json(self):
        self.assertEqual(json5_loads('{"a": [true, null]}'), {"a": [True, None]})


class RunLockTest(unittest.TestCase):
    def test_second_lock_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = RunLock(tmp)
            try:
                with self.assertRaises(HumanError):
                    RunLock(tmp)
            finally:
                first.close()
            second = RunLock(tmp)  # после освобождения — можно
            second.close()


if __name__ == "__main__":
    unittest.main()
