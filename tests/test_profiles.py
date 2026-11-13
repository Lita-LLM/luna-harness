"""Профили моделей и «одна модель на все роли»."""

import unittest

from luna.router import profiles as P
from luna.router.providers import ProviderEntry, ProviderRegistry

from tests.test_zadacha17 import make_app


class ProfilesTest(unittest.TestCase):
    def setUp(self):
        self.app = make_app(self)
        self.root = self.app._data_dir().root
        reg = ProviderRegistry(self.root / "providers.json")
        reg.upsert(ProviderEntry("lab", "openai", "http://lab:1234/v1"))
        self.app.settings.set("model.main", "lab:big")
        self.app.settings.set("model.fast", "lab:small")

    def test_sohranit_i_primenit(self):
        self.app.open_profiles()
        ui = self.app._profiles
        ui.on_action(("input", "работа", "save"))
        self.assertIn("работа", ui.store().names())
        self.app.settings.set("model.main", "other:x")
        self.app.settings.set("model.fast", "")
        # Реестр почистили — профиль вернёт провайдера.
        ProviderRegistry(self.root / "providers.json").remove("lab")
        ui.apply("работа")
        self.assertEqual(self.app.settings.get("model.main"), "lab:big")
        self.assertEqual(self.app.settings.get("model.fast"), "lab:small")
        entry = ProviderRegistry(self.root / "providers.json").get("lab")
        self.assertEqual(entry.base_url, "http://lab:1234/v1")
        self.assertIn("Вернул в реестр", self.app.entries[-1].text)

    def test_udalenie_s_podtverzhdeniem(self):
        ui_store = P.ProfileStore(self.root / "model_profiles.json")
        ui_store.put("x", P.capture(self.app.settings,
                                    ProviderRegistry(self.root / "providers.json")))
        self.app.open_profiles()
        item = next(i for i in self.app.picker.items if i.key == "x")
        self.app._profiles.on_action(("action", "delete", item))
        self.assertIn("x", ui_store.names())
        self.app._profiles.on_action(("action", "delete", item))
        self.assertNotIn("x", ui_store.names())

    def test_odna_model_na_vse_roli(self):
        self.app.settings.set("model.guard", "lab:g")
        self.app._set_role_model("__all__", "lab:one")
        self.assertEqual(self.app.settings.get("model.main"), "lab:one")
        for role in ("fast", "guard", "vision"):
            self.assertEqual(self.app.settings.get("model." + role), "")

    def test_komanda_s_imenem(self):
        self.app._slash("/profile нет-такого")
        self.assertIn("нет", self.app.entries[-1].text)


if __name__ == "__main__":
    unittest.main()
