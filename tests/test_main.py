"""CLI: лёгкость --version (рендерер не импортируется), settings и set через subprocess."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(args, home, cwd=ROOT):
    env = dict(os.environ, LUNA_HOME=str(home))
    # Луна пишет в UTF-8; системная кодировка (на Windows — cp1251) вывод
    # не декодирует: кодировка задаётся явно, а не «как повезёт» (РЕВЬЮ-01, п. 2).
    return subprocess.run([sys.executable, "-m", "luna", *args],
                          capture_output=True, text=True, encoding="utf-8",
                          env=env, cwd=cwd, timeout=30)


class MainTest(unittest.TestCase):
    def test_version_is_light(self):
        code = ("import sys; from luna.__main__ import main; main(['--version']); "
                "bad = [m for m in sys.modules if m.startswith('luna.ui') "
                "and m != 'luna.ui']; "
                "assert not bad, bad")
        res = subprocess.run([sys.executable, "-c", code], capture_output=True,
                             text=True, encoding="utf-8", cwd=ROOT, timeout=30)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("0.0.1", res.stdout)

    def test_settings_and_set_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            res = run(["set", "ui.color", "256"], home)
            self.assertEqual(res.returncode, 0, res.stderr)
            data = json.loads((home / "settings.json").read_text(encoding="utf-8"))
            self.assertEqual(data["ui"]["color"], "256")
            res = run(["settings"], home)
            self.assertIn("ui.color", res.stdout)
            self.assertIn("глобально", res.stdout)
            res = run(["set", "ui.color", "millions"], home)
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("Хьюстон", res.stdout)

    def test_doctor_ok_without_tty(self):
        with tempfile.TemporaryDirectory() as tmp:
            res = run(["doctor"], Path(tmp) / "home")
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
            self.assertIn("Python", res.stdout)

    def test_commands_survive_cp1251_stdio(self):
        # РЕВЬЮ-01, раунд 2, п. 2: потоки ребёнка насильно в cp1251 (как
        # пайп на Windows) — main() обязан сам переключить вывод в UTF-8,
        # иначе ✓/✗ в doctor роняют команду UnicodeEncodeError-ом.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            for args in (["doctor"], ["settings"]):
                env = dict(os.environ, LUNA_HOME=str(home),
                           PYTHONIOENCODING="cp1251")
                res = subprocess.run([sys.executable, "-m", "luna", *args],
                                     capture_output=True, text=True, encoding="utf-8",
                                     env=env, cwd=ROOT, timeout=30)
                self.assertEqual(res.returncode, 0, res.stdout + res.stderr)


if __name__ == "__main__":
    unittest.main()
