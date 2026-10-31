"""Настройки: слои, источники, сохранение незнакомых полей, события, валидация."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from luna.core.bus import EventBus
from luna.core.errors import HumanError
from luna.core.settings import Settings


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.cwd = root / "proj"
        (self.cwd / ".luna").mkdir(parents=True)
        self.home.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, bus=None) -> Settings:
        return Settings(home=self.home, cwd=self.cwd, bus=bus)

    def test_defaults(self):
        s = self.make()
        self.assertEqual(s.get("ui.color"), "auto")
        self.assertEqual(s.get("ui.emoji"), "auto")
        self.assertEqual(s.parse_value("ui.emoji", "off"), "off")
        with self.assertRaises(HumanError):
            s.parse_value("ui.emoji", "sometimes")
        self.assertIs(s.get("ui.splash"), True)
        self.assertEqual(s.source("ui.splash"), "default")

    def test_layers_and_sources(self):
        (self.home / "settings.json").write_text(
            json.dumps({"ui": {"color": "256", "splash": False}}), encoding="utf-8")
        (self.cwd / ".luna" / "settings.json").write_text(
            json.dumps({"ui": {"color": "16"}}), encoding="utf-8")
        s = self.make()
        self.assertEqual(s.get("ui.color"), "16")       # проект сильнее
        self.assertEqual(s.source("ui.color"), "project")
        self.assertIs(s.get("ui.splash"), False)
        self.assertEqual(s.source("ui.splash"), "global")

    def test_set_preserves_unknown_fields(self):
        (self.home / "settings.json").write_text(
            json.dumps({"future": {"x": 1}, "ui": {"theme": "luna"}}), encoding="utf-8")
        s = self.make()
        s.set("ui.color", "mono")
        data = json.loads((self.home / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(data["future"], {"x": 1})      # незнакомое сохранилось
        self.assertEqual(data["ui"]["color"], "mono")
        self.assertEqual(s.unknown_keys(), ["future.x"])

    def test_set_publishes_event(self):
        bus = EventBus()
        seen = []
        bus.listen(seen.append)
        s = self.make(bus=bus)
        s.set("ui.splash", False)
        changed = [e for e in seen if e["type"] == "settings.changed"]
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0]["key"], "ui.splash")
        self.assertIs(changed[0]["value"], False)

    def test_validation(self):
        s = self.make()
        with self.assertRaises(HumanError):
            s.set("ui.color", "millions")
        with self.assertRaises(HumanError):
            s.set("ui.splash", "off")  # в API — bool, текст разбирает parse_value
        self.assertIs(s.parse_value("ui.splash", "off"), False)
        self.assertEqual(s.parse_value("ui.color", "256"), "256")
        with self.assertRaises(HumanError):
            s.parse_value("ui.nope", "1")

    def test_broken_file_is_human_error(self):
        (self.home / "settings.json").write_text("{не json", encoding="utf-8")
        with self.assertRaises(HumanError):
            self.make()


if __name__ == "__main__":
    unittest.main()
