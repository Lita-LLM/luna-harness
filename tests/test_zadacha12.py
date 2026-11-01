"""ЗАДАЧА-12: тул luna, защита настроек, горячая перезагрузка, совместимость."""
import json
import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from luna.core.errors import HumanError
from luna.core.settings import SCHEMA, Settings, is_human_only
from luna.guard.policy import Guard
from luna.mind import compat
from luna.tools.luna_tool import luna_tool, scrub, skill_tool
from luna.tools.registry import ToolContext


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.proj = self.root / "proj"
        self.home.mkdir()
        self.proj.mkdir()
        self.settings = Settings(home=self.home, cwd=self.proj)
        self.ctx = ToolContext(cwd=self.proj, settings=self.settings)
        patcher = mock.patch.object(compat, "_home", return_value=self.root / "user")
        patcher.start()
        self.addCleanup(patcher.stop)

    def luna(self, **args):
        return luna_tool().run(self.ctx, args)


class HumanOnlyTest(Base):
    def test_protection_fields_marked(self):
        for key in ("guard.enabled", "guard.rules", "agent.mode", "model.base_url",
                    "model.provider", "model.kind"):
            self.assertTrue(SCHEMA[key].human_only, key)
        self.assertFalse(is_human_only("ui.color"))
        self.assertTrue(is_human_only("guard.future_key"))

    def test_tool_refuses_protection_even_valid_value(self):
        for key, value in (("guard.enabled", "off"), ("agent.mode", "yolo"),
                           ("model.base_url", "http://evil.example")):
            res = self.luna(action="set", key=key, value=value)
            self.assertFalse(res.ok, key)
            self.assertIn("только человек", res.text)
        self.assertTrue(self.settings.get("guard.enabled"))
        self.assertEqual(self.settings.get("agent.mode"), "normal")

    def test_tool_sets_comfort_with_old_and_new(self):
        res = self.luna(action="set", key="ui.color", value="mono")
        self.assertTrue(res.ok, res.text)
        self.assertIn("auto → mono", res.text)
        self.assertEqual(Settings(home=self.home, cwd=self.proj).get("ui.color"), "mono")

    def test_tool_rejects_bad_values_and_unknown_keys(self):
        self.assertFalse(self.luna(action="set", key="ui.color", value="neon").ok)
        self.assertFalse(self.luna(action="set", key="no.such", value="1").ok)

    def test_reading_actions_work(self):
        for action in ("about", "settings", "paths", "manners", "compat", "version", "logs"):
            res = self.luna(action=action)
            self.assertTrue(res.ok, action + ": " + res.text)
        listing = self.luna(action="settings", prefix="guard.").text
        self.assertIn("[только человек]", listing)

    def test_changing_flag(self):
        tool = luna_tool()
        self.assertTrue(tool.is_mutating({"action": "set"}))
        self.assertTrue(tool.is_mutating({"action": "mcp_add"}))
        self.assertFalse(tool.is_mutating({"action": "settings"}))


class GuardTest(Base):
    def test_luna_changes_ask_even_in_yolo_and_auto(self):
        guard = Guard(self.settings)
        for mode in ("yolo", "auto", "normal"):
            d = guard.decide("luna", True, {"action": "set", "key": "ui.color"}, mode=mode)
            self.assertEqual(d.decision, "ask", mode)
        self.assertEqual(guard.decide("luna", False, {"action": "settings"}, mode="normal").decision,
                         "allow")

    def test_bypass_routes_ask(self):
        guard = Guard(self.settings)
        cases = [
            ("write", {"path": ".luna/settings.json", "content": "{}"}),
            ("edit", {"path": str(self.home / "settings.json")}),
            ("write", {"path": str(self.home / "mcp.json")}),
            ("bash", {"command": "luna set guard.enabled off"}),
            ("bash", {"command": "luna.cmd set agent.mode yolo"}),
        ]
        for tool, args in cases:
            d = guard.decide(tool, True, args, mode="yolo")
            self.assertEqual(d.decision, "ask", (tool, args))
        self.assertEqual(guard.decide("bash", True, {"command": "luna set ui.color mono"},
                                      mode="yolo").decision, "allow")
        self.assertEqual(guard.decide("write", True, {"path": "src/app.py"}, mode="yolo").decision,
                         "allow")

    def test_always_does_not_skip_luna_changes(self):
        guard = Guard(self.settings)
        guard.remember_always("high")
        d = guard.decide("luna", True, {"action": "set"}, mode="normal")
        self.assertEqual(d.decision, "ask")


class HotReloadTest(Base):
    def write_global(self, data):
        path = self.home / "settings.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        # mtime у быстрых записей может совпасть — сдвигаем явно
        st = path.stat()
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))

    def test_external_change_detected_once(self):
        self.assertEqual(self.settings.reload_if_changed(), [])
        self.write_global({"ui": {"color": "16"}})
        changes = self.settings.reload_if_changed()
        self.assertEqual([(c["key"], c["old"], c["value"]) for c in changes],
                         [("ui.color", "auto", "16")])
        self.assertEqual(self.settings.get("ui.color"), "16")
        self.assertEqual(self.settings.reload_if_changed(), [])

    def test_own_write_is_not_external(self):
        self.settings.set("ui.color", "mono")
        self.assertEqual(self.settings.reload_if_changed(), [])

    def test_broken_json_keeps_values_and_complains_once(self):
        self.write_global({"ui": {"color": "16"}})
        self.settings.reload_if_changed()
        path = self.home / "settings.json"
        path.write_text("{ сломано", encoding="utf-8")
        st = path.stat()
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))
        with self.assertRaises(HumanError):
            self.settings.reload_if_changed()
        self.assertEqual(self.settings.get("ui.color"), "16")
        self.assertEqual(self.settings.reload_if_changed(), [])

    def test_bus_event(self):
        events = []

        class Bus:
            def publish(self, ev):
                events.append(ev)

        self.settings.bus = Bus()
        self.write_global({"ui": {"splash": False}})
        self.settings.reload_if_changed()
        self.assertEqual(events[0]["type"], "settings.changed")
        self.assertEqual(events[0]["key"], "ui.splash")


class CompatTest(Base):
    def test_instructions_imports_dedup_and_global_switch(self):
        (self.proj / "AGENTS.md").write_text("Правила проекта", encoding="utf-8")
        (self.proj / "CLAUDE.md").write_text("Правила проекта", encoding="utf-8")  # копия
        (self.proj / "notes.md").write_text("Вставка по импорту", encoding="utf-8")
        (self.proj / "GEMINI.md").write_text("Начало\n@notes.md\n", encoding="utf-8")
        (self.proj / ".cursorrules").write_text("Курсор", encoding="utf-8")
        user = self.root / "user" / ".claude"
        user.mkdir(parents=True)
        (user / "CLAUDE.md").write_text("Глобально", encoding="utf-8")

        found = compat.collect_instructions(self.proj, self.settings)
        texts = [t for _, t in found]
        self.assertEqual(texts.count("Правила проекта"), 1)
        self.assertIn("Начало\nВставка по импорту", texts)
        self.assertIn("Курсор", texts)
        self.assertIn("Глобально", texts)
        self.assertTrue(compat.is_global_instruction(found[-1][0]))

        self.settings.set("compat.global_instructions", False)
        texts = [t for _, t in compat.collect_instructions(self.proj, self.settings)]
        self.assertNotIn("Глобально", texts)

    def test_skills_listed_and_loaded(self):
        skill = self.proj / ".claude" / "skills" / "deploy"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\nname: deploy\ndescription: Выкатить на стенд\n---\nШаг 1. Собери.\n", encoding="utf-8")
        items = compat.list_skills(self.proj, self.settings)
        self.assertEqual([(i["name"], i["description"]) for i in items], [("deploy", "Выкатить на стенд")])
        self.assertIn("deploy — Выкатить на стенд", compat.skills_prompt_section(self.proj, self.settings))
        res = skill_tool().run(self.ctx, {"name": "deploy"})
        self.assertTrue(res.ok)
        self.assertIn("Шаг 1. Собери.", res.text)
        self.assertFalse(skill_tool().run(self.ctx, {"name": "нет"}).ok)

    def test_commands_with_arguments(self):
        cmds = self.proj / ".claude" / "commands" / "git"
        cmds.mkdir(parents=True)
        (cmds / "fix.md").write_text(
            "---\ndescription: Починить\n---\nПочини $1 в ветке $2. Всё: $ARGUMENTS\n!`git status`\n",
            encoding="utf-8")
        text = compat.resolve_command(self.proj, "git:fix", "сборку main", self.settings)
        self.assertIn("Почини сборку в ветке main. Всё: сборку main", text)
        self.assertIn("!`git status`", text)  # не исполняется, просто текст
        self.assertIsNone(compat.resolve_command(self.proj, "nope", "", self.settings))
        self.settings.set("compat.commands", False)
        self.assertIsNone(compat.resolve_command(self.proj, "git:fix", "", self.settings))


class ScrubTest(unittest.TestCase):
    def test_keys_removed(self):
        text = "key sk-abcdefghijklmnop1234 и api_key=supersecret и обычный текст"
        out = scrub(text)
        self.assertNotIn("sk-abcdefghijklmnop1234", out)
        self.assertNotIn("supersecret", out)
        self.assertIn("обычный текст", out)

    def test_known_secret_values_removed(self):
        class Secrets:
            def values(self):
                return ["коротыш42"]

        self.assertNotIn("коротыш42", scrub("пароль коротыш42 тут", Secrets()))


class PromptTest(Base):
    def test_self_section_mentions_tool_and_protection(self):
        from luna.mind.prompt import self_section

        text = self_section(self.settings)
        self.assertIn("тулом luna", text)
        self.assertIn("только человек", text)


if __name__ == "__main__":
    unittest.main()


class MannerAudienceTest(unittest.TestCase):
    def test_nibbler_manner_hidden_from_main_agent(self):
        from luna.mind import manners

        with TemporaryDirectory() as tmp:
            settings = Settings(home=tmp, cwd=tmp)
            main_ids = [m["id"] for m in manners.library(settings, tmp)]
            self.assertNotIn("nibbler", main_ids)
            self.assertIn("luna", main_ids)
            self.assertIsNotNone(manners.get(settings, "nibbler", tmp, audience="nibbler"))
            with self.assertRaises(HumanError):
                manners.resolve(settings, "nibbler", tmp)
            self.assertIn("nibbler", [m["id"] for m in manners.library(settings, tmp, audience=None)])


class LayoutStackTest(unittest.TestCase):
    def test_new_panels_share_space_equally(self):
        from luna.ui.panels import LayoutTree

        tree = LayoutTree()
        for pid in ("todo", "moon", "files", "nibbler"):
            tree.open(pid)
        self.assertEqual(tree.root.kind, "split")
        self.assertEqual([c.panel_id for c in tree.root.children], ["todo", "moon", "files", "nibbler"])
        self.assertEqual(tree.root.weights, [1.0] * 4)
