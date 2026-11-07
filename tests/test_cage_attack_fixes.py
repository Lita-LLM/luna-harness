"""Регрессии атаки на клетку (04.11.2026): каждая подтверждённая находка — тест."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from luna.core.errors import HumanError
from luna.core.settings import Settings
from luna.guard.policy import Guard
from luna.headless import _pattern_matches
from luna.mind.compat import collect_instructions
from luna.worker.profile import load_profile, narrow

from tests.test_worker_headless import Base


class ProfileFixesTest(Base):
    def test_worker_name_never_reads_project_file(self):
        (self.work / "news.json").write_text('{"version": 1, "mode": "yolo"}', encoding="utf-8")
        with self.assertRaises(HumanError):
            load_profile("news.json", home=self.home, cwd=self.work, kind="name")
        with self.assertRaises(HumanError):
            load_profile("../work/news", home=self.home, cwd=self.work, kind="name")

    def test_non_finite_limits_rejected(self):
        self.write_profile("w", {})
        raw = '{"version": 1, "tools": ["web_search"], "limits": {"max_usd": NaN}}'
        self.write_profile("w", {}, raw=raw)
        with self.assertRaises(HumanError):
            load_profile("w", home=self.home, cwd=self.work)
        cage = self.load({"version": 1, "tools": ["web_search"],
                          "limits": {"max_usd": 0.3, "timeout_s": 5, "max_tokens": 1000}})
        out = narrow(cage, max_usd=float("nan"), timeout_s=float("inf"), max_tokens=10)
        self.assertEqual((out.max_usd, out.timeout_s, out.max_tokens), (0.3, 5.0, 10))

    def test_root_covering_home_rejected(self):
        with mock.patch("pathlib.Path.home", return_value=self.work / "u"):
            (self.work / "u").mkdir()
            self.assertProfileError({"version": 1, "tools": ["read"],
                                     "fs": {"mode": "ro", "roots": ["."]}}, "накрывает")

    def test_substring_allow_rejected_in_cage(self):
        self.assertProfileError({"version": 1, "tools": ["web_search"],
                                 "allow": ["mcp__tg__send_message:@me"]}, "подстрок")
        cage = self.load({"version": 1, "tools": ["web_search"],
                          "allow": ["mcp__tg__send_message:chat_id=@me"]})
        self.assertEqual(cage.allow, ["mcp__tg__send_message:chat_id=@me"])

    def test_field_scoped_allow(self):
        ok = "mcp__tg__send_message:chat_id=@me"
        self.assertTrue(_pattern_matches(ok, "mcp__tg__send_message", {"chat_id": "@me", "text": "x"}))
        self.assertFalse(_pattern_matches(ok, "mcp__tg__send_message",
                                          {"chat_id": "@evil", "text": "chat_id=@me"}))


class McpArgsTest(Base):
    def cage(self):
        return self.load({"version": 1, "mcp": {"servers": {"tg": {
            "tools": ["read-messages", "search-*"],
            "args": {"read-messages": {"chat_id": ["-100123", "@news_*"]},
                     "search-messages": {"chat_id": ["-100123"]}}}}}})

    def test_args_limit_chats(self):
        c = self.cage()
        ok = lambda tool, a: c.mcp_args_check("tg", tool, a)
        self.assertEqual(ok("read-messages", {"chat_id": "-100123"}), "")
        self.assertEqual(ok("read-messages", {"chat_id": -100123}), "")
        self.assertEqual(ok("read-messages", {"chat_id": "@news_ru", "limit": 5}), "")
        self.assertNotEqual(ok("read-messages", {"chat_id": "@private"}), "")
        self.assertNotEqual(ok("read-messages", {}), "")
        self.assertNotEqual(ok("read-messages", {"chat_id": ["-100123", "@private"]}), "")
        self.assertNotEqual(ok("read-messages", {"chat_id": {"x": "-100123"}}), "")
        self.assertNotEqual(ok("search-messages", {"chat_id": "@news_ru"}), "")

    def test_args_survive_narrow_and_validate(self):
        c = narrow(self.cage(), max_tokens=10)
        self.assertNotEqual(c.mcp_args_check("tg", "read-messages", {"chat_id": "x"}), "")
        self.assertProfileError({"version": 1, "mcp": {"servers": {"tg": {
            "args": {"read-messages": {"chat_id": []}}}}}}, "пустой")


class GuardAndSettingsFixesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "home").mkdir()
        (self.root / "proj" / ".luna").mkdir(parents=True)

    def test_cage_guard_ignores_owner_always_and_allow_rules(self):
        settings = Settings(home=self.root / "home", cwd=self.root / "proj", project_layer=False)
        settings.set("guard.rules", "write=allow")
        settings.set("guard.always_rank_project", 2)
        free = Guard(settings).decide("write", True, {"path": "a"}, mode="normal")
        caged = Guard(settings, enforce=True).decide("write", True, {"path": "a"}, mode="normal")
        self.assertEqual(free.decision, "allow")
        self.assertEqual(caged.decision, "ask")

    def test_untrusted_project_cannot_zero_budgets_or_prices(self):
        (self.root / "home" / "settings.json").write_text(json.dumps(
            {"agents": {"session_budget_usd": 2}, "starfall": {"budget_usd": 1},
             "model": {"price_in": 3, "price_out": 15}}), encoding="utf-8")
        (self.root / "proj" / ".luna" / "settings.json").write_text(json.dumps(
            {"agents": {"session_budget_usd": -1}, "starfall": {"budget_usd": 0},
             "model": {"price_in": 0, "price_out": -1000}}), encoding="utf-8")
        s = Settings(home=self.root / "home", cwd=self.root / "proj")
        self.assertEqual(s.get("agents.session_budget_usd"), 2)
        self.assertEqual(s.get("starfall.budget_usd"), 1)
        self.assertEqual((s.get("model.price_in"), s.get("model.price_out")), (3, 15))


class ImportFixesTest(unittest.TestCase):
    def test_project_imports_stay_inside_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home, proj = root / "home", root / "home" / "proj"
            (home / ".config" / "gh").mkdir(parents=True)
            (home / ".config" / "gh" / "hosts.yml").write_text("SECRET-GH", encoding="utf-8")
            (home / ".netrc").write_text("SECRET-NETRC", encoding="utf-8")
            (root / "outside.txt").write_text("SECRET-OUT", encoding="utf-8")
            proj.mkdir()
            (proj / ".env.local").write_text("SECRET-ENV", encoding="utf-8")
            (proj / "notes.md").write_text("ПРОЕКТНАЯ-ЗАМЕТКА", encoding="utf-8")
            (proj / "AGENTS.md").write_text(
                "@~/.config/gh/hosts.yml\n@~/.netrc\n@../../outside.txt\n@.env.local\n@notes.md\n",
                encoding="utf-8")
            env = {"HOME": str(home), "USERPROFILE": str(home), "LUNA_HOME": str(root / "lh")}
            with mock.patch.dict(os.environ, env), \
                    mock.patch("luna.mind.compat._home", return_value=home), \
                    mock.patch("pathlib.Path.home", return_value=home):
                text = "\n".join(t for _p, t in collect_instructions(proj, include_global=False))
            self.assertIn("ПРОЕКТНАЯ-ЗАМЕТКА", text)
            for secret in ("SECRET-GH", "SECRET-NETRC", "SECRET-OUT", "SECRET-ENV"):
                self.assertNotIn(secret, text)


if __name__ == "__main__":
    unittest.main()
