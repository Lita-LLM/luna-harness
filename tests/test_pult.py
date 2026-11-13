"""Пульт ИИ: свой адрес → модели → роль; ключ скрытно в сейф; скан в пульте."""

import time
import unittest

from luna.core.secrets import Secrets
from luna.router.providers import ProviderRegistry
from luna.ui.keys import Paste

from tests.test_scan import _server
from tests.test_zadacha17 import key, make_app


def _wait(cond, sec=5.0):
    end = time.time() + sec
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def _pick(app, key_):
    item = next(i for i in app.picker.filtered() if i.key == key_)
    app._route_picker_action(("pick", item))


class PultTest(unittest.TestCase):
    def setUp(self):
        self.srv = _server({"/v1/models": (200, {"data": [{"id": "qwen3"}]})})
        self.addCleanup(self.srv.shutdown)
        self.url = "http://127.0.0.1:%d/v1" % self.srv.server_address[1]

    def test_svoy_adres_model_rol(self):
        app = make_app(self)
        app.submit_text("/пульт")
        self.assertEqual(app._picker_kind, "pult")
        _pick(app, "add")
        _pick(app, "custom")
        for ch in self.url:
            app.handle_key(key("char", ch))
        app.handle_key(key("enter"))
        reg = ProviderRegistry(app._data_dir().root / "providers.json")
        entry = reg.find_by_url(self.url)
        self.assertIsNotNone(entry)
        self.assertEqual(app._pult.screen, "provider")
        _pick(app, "models")
        self.assertTrue(_wait(lambda: app._pult._models.get(entry.name)))
        _pick(app, "m:qwen3")
        _pick(app, "r:fast")
        self.assertEqual(app.settings.get("model.fast"), entry.name + ":qwen3")
        # Esc — шаг назад, а не закрытие всего пульта
        app._route_picker_action(("close",))
        self.assertEqual(app._pult.screen, "main")
        app._route_picker_action(("close",))
        self.assertIsNone(app.picker)

    def test_klyuch_skryto_i_vstavkoy(self):
        app = make_app(self)
        app.submit_text("/providers")
        _pick(app, "add")
        _pick(app, "preset:openai")
        # облако без ключа — сразу спрашивает ключ скрытым полем
        self.assertEqual(app.picker.mode, "input")
        app.handle_paste(Paste("sk-секретик\n"))
        lines, _ = app.picker.render_block(60, 10)
        shown = "".join(t for line in lines for t, _r in line)
        self.assertNotIn("секретик", shown)
        self.assertIn("•", shown)
        self.assertEqual(app.editor.text, "")  # вставка не утекла в поле чата
        app.handle_key(key("enter"))
        self.assertEqual(Secrets(app._data_dir().secrets).get("provider:openai"),
                         "sk-секретик")
        feed = " ".join(e.text for e in app.entries)
        self.assertNotIn("секретик", feed)

    def test_rezultat_skana_v_pult(self):
        from luna.net import scan

        app = make_app(self)
        app.submit_text("/pult")
        found = scan.scan(plan=[("127.0.0.1", (self.srv.server_address[1],))], workers=2)
        app._pult.found = found
        app._pult._show("scan")
        _pick(app, "f:" + found[0].base_url)
        reg = ProviderRegistry(app._data_dir().root / "providers.json")
        self.assertIsNotNone(reg.find_by_url(found[0].base_url))
        self.assertEqual(app._pult.screen, "provider")
