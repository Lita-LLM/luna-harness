"""Новолуние: без следов, каждая модель — с подтверждения человека."""

import os
import unittest
from pathlib import Path

from luna.core import newmoon
from luna.core.errors import HumanError
from luna.core.paths import DataDir
from luna.core.settings import Settings
from luna.router.core import Router
from luna.router.providers import ProviderEntry, ProviderRegistry

from tests.test_zadacha17 import make_app


class NewmoonCase(unittest.TestCase):
    def setUp(self):
        self._env = {k: os.environ.get(k) for k in (newmoon.ENV, newmoon.ENV_DIR)}
        self.scratch = newmoon.enable()
        newmoon.forget_all()

    def tearDown(self):
        newmoon.wipe()
        newmoon.forget_all()
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class NoTracesTest(NewmoonCase):
    def test_puti_vo_vremennuyu_papku_i_stirayutsya(self):
        data = DataDir()
        self.assertTrue(str(data.sessions).startswith(str(self.scratch)))
        self.assertTrue(str(data.logs).startswith(str(self.scratch)))
        (data.local / "x.txt").write_text("след", encoding="utf-8")
        newmoon.wipe()
        self.assertFalse(Path(self.scratch).exists())

    def test_nastroyki_tolko_v_pamyati(self):
        home = Path(self.scratch) / "home"
        settings = Settings(home=home, cwd=home)
        settings.ephemeral = True
        settings.set("ui.theme", "newmoon")
        self.assertEqual(settings.get("ui.theme"), "newmoon")
        self.assertFalse((home / "settings.json").exists())

    def test_luna_znaet_o_rezhime(self):
        from luna.mind.prompt import build_system_prompt

        text = build_system_prompt(Path(self.scratch))
        self.assertIn("Новолуние", text)


class GateTest(NewmoonCase):
    def _router(self):
        home = Path(self.scratch) / "home"
        reg = ProviderRegistry(home / "providers.json")
        # Адрес в никуда: если шлагбаум не сработает, тест упадёт на сети.
        reg.upsert(ProviderEntry(name="tyomnyy", base_url="http://127.0.0.1:9/v1"))
        settings = Settings(home=home, cwd=home)
        settings.ephemeral = True
        settings.set("model.main", "tyomnyy:qwen")
        return Router(settings, None, registry=reg)

    def test_bez_podtverzhdeniya_ni_odnogo_zaprosa(self):
        router = self._router()
        with self.assertRaises(HumanError) as ctx:
            router.chat([{"role": "user", "content": "привет"}])
        self.assertEqual(ctx.exception.code, "newmoon_unconfirmed")
        self.assertEqual(ctx.exception.data["model"], "qwen")

    def test_podtverzhdyonnoe_propuskaet(self):
        router = self._router()
        newmoon.confirm("http://127.0.0.1:9/v1", "qwen")
        with self.assertRaises(Exception) as ctx:
            router.chat([{"role": "user", "content": "привет"}])
        # дальше шлагбаума — уже сеть (порт 9 закрыт), а не отказ режима
        self.assertNotEqual(getattr(ctx.exception, "code", ""), "newmoon_unconfirmed")


class ChatTest(NewmoonCase):
    def test_soobshchenie_zhdyot_podtverzhdeniya(self):
        app = make_app(self)
        app.settings.ephemeral = True
        reg = ProviderRegistry(app._data_dir().root / "providers.json")
        reg.upsert(ProviderEntry(name="tyomnyy", base_url="http://127.0.0.1:9/v1"))
        app.settings.set("model.main", "tyomnyy:qwen")
        app.submit_text("коварный план")
        self.assertEqual(app._picker_kind, "newmoon")
        sent = [c for c in app.sent_commands if c.get("type") == "user.message"]
        self.assertEqual(sent, [])
        item = next(i for i in app.picker.items if i.key == "__all__")
        app._route_picker_action(("pick", item))
        sent = [c for c in app.sent_commands if c.get("type") == "user.message"]
        self.assertEqual(len(sent), 1)
        self.assertTrue(newmoon.is_confirmed("http://127.0.0.1:9/v1", "qwen"))

    def test_zametka_v_pamyat_ne_pishetsya(self):
        app = make_app(self)
        app.submit_text("# секретная заметка")
        self.assertFalse((Path(app.cwd) / "AGENTS.md").exists())

    def test_komanda_vne_rezhima_perezapuskaet(self):
        os.environ.pop(newmoon.ENV, None)
        app = make_app(self)
        app.submit_text("/новолуние")
        self.assertEqual(app.relaunch, "newmoon")
        self.assertTrue(app.done)
