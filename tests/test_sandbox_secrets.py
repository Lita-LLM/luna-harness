"""Сшивка тимлида к ЗАДАЧЕ-21: bwrap прячет от ячейки сейф Луны и ключи хозяина."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from luna.starfall.sandbox import _hide_secrets_args


class HideSecretsTest(unittest.TestCase):
    def test_hides_luna_vault_and_ssh(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            lh = Path(tmp) / "lh"
            (home / ".ssh").mkdir(parents=True)
            (home / ".netrc").write_text("x")
            (lh / "sessions").mkdir(parents=True)
            (lh / "secrets.json").write_text("{}")
            cell = lh / "starfall" / "cell-1"
            cell.mkdir(parents=True)
            env = {"LUNA_HOME": str(lh), "HOME": str(home), "USERPROFILE": str(home)}
            with mock.patch.dict(os.environ, env):
                args = _hide_secrets_args(str(cell))
            joined = " ".join(args)
            self.assertIn(str(home / ".ssh"), joined)
            self.assertIn(str(home / ".netrc"), joined)
            self.assertIn(str(lh / "secrets.json"), joined)
            self.assertIn(str(lh / "sessions"), joined)
            # Папка ячейки внутри LUNA_HOME — сам LUNA_HOME целиком не прячем.
            self.assertNotIn("--tmpfs " + str(lh) + " ", joined + " ")


if __name__ == "__main__":
    unittest.main()
