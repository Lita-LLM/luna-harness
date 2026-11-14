"""Что видит человек и что помнит модель: процент контекста без usage,
конверт пометок, «перечитать» после прилива и свёрнутое мышление."""

import unittest

from luna.core.envelope import compose
from luna.mind.compact import parse_reread
from luna.mind.loop import Agent
from luna.router.types import ChatResult, Usage
from tests.test_carry_undo import TempHomeBase, make_app


class ZeroUsageRouter:
    def chat(self, messages, tools=None, *, role="main", ref="", on_event=None):
        return ChatResult(model="fake", text="ответ " * 50, usage=Usage(0, 0))


class ContextPctTest(TempHomeBase):
    def test_bez_usage_otsenka_a_ne_nol(self):
        agent = Agent(settings=self.settings, secrets=self.secrets,
                      router=ZeroUsageRouter(), cwd=self.work)
        agent.publish = self.events.append
        agent.run_turn("привет " * 200)
        usage = [e for e in self.events if e.get("type") == "usage"]
        self.assertGreater(usage[-1]["context_pct"], 0)

    def test_zagruzka_istorii_publikuet_otsenku(self):
        agent = Agent(settings=self.settings, secrets=self.secrets,
                      router=ZeroUsageRouter(), cwd=self.work)
        agent.publish = self.events.append
        agent.load_history([{"role": "user", "content": "x" * 9000},
                            {"role": "assistant", "content": "ок"}])
        agent.publish_context()
        self.assertGreater(self.events[-1]["context_pct"], 0)


class EnvelopeTest(unittest.TestCase):
    def test_bez_pometok_tekst_kak_est(self):
        self.assertEqual(compose("привет", []), "привет")

    def test_pometki_otdelno_ot_cheloveka(self):
        text = compose("сделай", [{"from": "nibbler", "text": "тесты упали"},
                                  {"from": "bg", "text": "сборка готова"}])
        head, human = text.split("<человек>")
        self.assertIn("тесты упали", head)
        self.assertIn("не человек", head)
        self.assertEqual(human.strip(), "сделай\n</человек>")


class RereadTest(unittest.TestCase):
    def test_razdel_pererechitat(self):
        summary = ("- сделал правку\n\n**Перечитать:**\n- `luna/ui/chat.py` — лента\n"
                   "- docs/X.md\n- luna/ui/chat.py\n\nЕщё раздел")
        self.assertEqual(parse_reread(summary), ["luna/ui/chat.py", "docs/X.md"])

    def test_net_razdela(self):
        self.assertEqual(parse_reread("просто сводка"), [])


class TideAndFoldedTest(TempHomeBase):
    def test_priliv_pokazyvaet_svodku_po_kliku(self):
        app = make_app(self)
        app.on_agent_event({"type": "context.compacted", "before_pct": 80,
                            "after_pct": 20, "summarized_turns": [1, 2],
                            "summary": "человек попросил X", "reread": ["a.py"]})
        tide = [e for e in app.entries if e.kind == "tide"][-1]
        joined = feed(app)
        self.assertIn("перечитать: a.py", joined)
        self.assertNotIn("человек попросил X", joined)
        app._toggle_entry(tide)
        self.assertIn("человек попросил X", feed(app))


def feed(app) -> str:
    lines = app._feed_lines(100, app._clock() + 100)
    return "\n".join("".join(t for t, _f, _a in spans) for spans, _bg in lines)


class VaultTest(unittest.TestCase):
    def test_tuda_obratno_i_chuzhoy_parol(self):
        from luna.core import vault

        blob = vault.seal({"tape": [{"kind": "x", "t": "тайна"}]}, "пароль")
        self.assertNotIn("тайна".encode("utf-8"), blob)
        self.assertEqual(vault.unseal(blob, "пароль")["tape"][0]["t"], "тайна")
        with self.assertRaises(vault.VaultError):
            vault.unseal(blob, "не тот")
        broken = blob[:-40] + bytes([blob[-40] ^ 1]) + blob[-39:]
        with self.assertRaises(vault.VaultError):
            vault.unseal(broken, "пароль")


class KeepTest(TempHomeBase):
    def test_keep_i_open_v_novolunii(self):
        from unittest import mock

        from luna.ui.keys import Key

        def typed(app, text):
            for ch in text:
                app.handle_key(Key(ch, ch))
            app.handle_key(Key("enter"))

        app = make_app(self)
        app.tape = [{"kind": "command", "data": {"type": "user.message", "text": "секрет"}},
                    {"kind": "event", "data": {"type": "turn.start", "turn": 1}},
                    {"kind": "event", "data": {"type": "agent.text", "text": "ок"}},
                    {"kind": "event", "data": {"type": "turn.done", "turn": 1}}]
        target = self.tmp / "out" / "s1"
        from luna.ui import keep as keep_mod

        fake_nm = mock.Mock()
        fake_nm.active.return_value = True
        with mock.patch.object(keep_mod, "_nm", fake_nm):
            app.submit_text("/keep " + str(target))
            self.assertIsNotNone(app._secret)
            typed(app, "pw1")
            typed(app, "pw1")
            saved = target.with_name("s1.lunakeep")
            self.assertTrue(saved.exists())
            self.assertNotIn("pw1", "".join(app.editor.history))
            self.assertNotIn("секрет".encode("utf-8"), saved.read_bytes())
            app2 = make_app(self)
            app2.submit_text("/keep open " + str(saved))
            typed(app2, "pw1")
        users = [e.text for e in app2.entries if e.kind == "user"]
        self.assertEqual(users, ["секрет"])
        self.assertEqual(app2._kept_path, str(saved))

    def test_vne_novoluniya_ne_pishet(self):
        app = make_app(self)
        app.tape = [{"kind": "event", "data": {"type": "turn.start", "turn": 1}}]
        app.submit_text("/keep")
        self.assertIsNone(app._secret)


class InterleavedThinkingTest(TempHomeBase):
    def test_mysli_posle_tula_novoy_zapisyu(self):
        app = make_app(self)
        app.on_agent_event({"type": "agent.thinking", "text": "сначала"})
        app.on_agent_event({"type": "tool.call", "id": "t1", "name": "bash",
                            "args": {"command": "ls"}, "intent": ""})
        app.on_agent_event({"type": "agent.thinking", "text": "потом"})
        thinks = [e for e in app.entries if e.kind == "thinking"]
        self.assertEqual([t.text for t in thinks], ["сначала", "потом"])
        self.assertFalse(thinks[0].streaming)
        self.assertTrue(thinks[0].finished_at)


if __name__ == "__main__":
    unittest.main()
