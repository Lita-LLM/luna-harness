"""Горячая перезагрузка и обновления в чате (без сети и без git)."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from luna.core import hotreload, newmoon

from tests.test_zadacha17 import make_app


class PurgeTest(unittest.TestCase):
    """Выгрузка на подставном пакете: настоящий luna в тестах не трогаем."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        pkg = Path(tmp.name) / "lunapkg_hr"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
        self.pkg = pkg
        sys.path.insert(0, tmp.name)
        self.addCleanup(sys.path.remove, tmp.name)
        patcher = mock.patch.object(hotreload, "PACKAGE", "lunapkg_hr")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(lambda: [sys.modules.pop(n, None) for n in list(sys.modules)
                                 if n.startswith("lunapkg_hr")])

    def test_svezhij_kod_i_otkat(self):
        import importlib

        old = importlib.import_module("lunapkg_hr.mod")
        (self.pkg / "mod.py").write_text("VALUE = 22  # другой размер: .pyc сверяется по mtime+size\n", encoding="utf-8")
        saved = hotreload.purge()
        self.assertNotIn("lunapkg_hr.mod", sys.modules)
        new = importlib.import_module("lunapkg_hr.mod")
        self.assertEqual(new.VALUE, 22)
        hotreload.restore(saved)
        self.assertIs(sys.modules["lunapkg_hr.mod"], old)


class SnapshotTest(unittest.TestCase):
    def test_lenta_v_pamyati_bez_zhurnala(self):
        app = make_app(self)
        self.assertIsNone(app.journal)
        app.on_agent_event({"type": "notice", "text": "привет"})
        app.send_command({"type": "user.message", "text": "сделай"})
        kinds = [r["kind"] for r in app.tape]
        self.assertIn("event", kinds)
        self.assertIn("command", kinds)
        app.editor.set_text("черновик")
        app.settings.set("ui.theme", "amoled", layer="session")
        snap = app.updater.snapshot()
        self.assertEqual(snap["draft"], "черновик")
        self.assertEqual(snap["settings_session"].get("ui.theme"), "amoled")
        self.assertTrue(snap["tape"])

    def test_reload_zhdyot_konca_hoda(self):
        app = make_app(self)
        app.busy = True
        app.updater.reload_command()
        self.assertFalse(app.done)
        self.assertTrue(app.updater.pending_reload)
        app.busy = False
        app.updater.tick()
        self.assertTrue(app.done)
        self.assertEqual(app.relaunch, "reload")
        self.assertIsNotNone(app.handoff)

    def test_podtverzhdeniya_perezhivayut(self):
        newmoon.forget_all()
        newmoon.confirm("http://x:1/v1", "m")
        keys = newmoon.confirmed_keys()
        newmoon.forget_all()
        newmoon.restore_confirmed(keys)
        self.assertTrue(newmoon.is_confirmed("http://x:1/v1", "m"))
        newmoon.forget_all()


class UpdateFlowTest(unittest.TestCase):
    def _info(self, behind):
        from luna.diagnostics.update import UpdateInfo

        return UpdateInfo("main", "abc1234", behind, 0, ["def5678 новое"] * behind)

    def _wait(self, app):
        app.updater._job.join(5)
        app.updater.tick()

    def test_check_pokazyvaet_metku(self):
        app = make_app(self)
        with mock.patch("luna.diagnostics.update.check", return_value=self._info(2)):
            app.updater.update_command("check")
            self._wait(app)
        self.assertEqual(app.updater.available, 2)
        self.assertFalse(app.done)

    def test_install_primenyaet(self):
        app = make_app(self)
        with mock.patch("luna.diagnostics.update.check", return_value=self._info(1)), \
                mock.patch("luna.diagnostics.update.install", return_value="def5678"):
            app.updater.update_command()
            self._wait(app)
        self.assertTrue(app.done)
        self.assertEqual(app.relaunch, "reload")

    def test_v_novolunii_fon_ne_hodit(self):
        app = make_app(self)
        with mock.patch.object(newmoon, "active", return_value=True):
            self.assertEqual(app.updater._mode(), "off")


if __name__ == "__main__":
    unittest.main()
