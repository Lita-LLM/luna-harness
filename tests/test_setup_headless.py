"""setup, models, headless и сейф секретов — на фальшивом провайдере."""

from __future__ import annotations

import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from luna.core.secrets import Secrets
from luna.core.settings import Settings
from luna.headless import run_headless
from luna.router.core import ProviderConfig, Router
from luna.setup import run_models


def fake_router(settings, secrets) -> Router:
    providers = {"fake": ProviderConfig("fake", "fake", "", None)}
    return Router(settings, secrets, providers=providers)


class SecretsTest(unittest.TestCase):
    def test_roundtrip_mask_and_perms(self):
        tmp = Path(tempfile.mkdtemp(prefix="luna-secrets-"))
        secrets = Secrets(tmp / "secrets.json")
        secrets.set("provider:openai", "sk-1234567890abcd")
        self.assertEqual(secrets.get("provider:openai"), "sk-1234567890abcd")
        masked = secrets.masked("provider:openai")
        self.assertNotIn("1234567890", masked)
        self.assertIn("sk-1", masked)
        if os.name == "posix":
            mode = stat.S_IMODE((tmp / "secrets.json").stat().st_mode)
            self.assertEqual(mode, 0o600)
        self.assertIn("sk-1234567890abcd", secrets.values())


class SetupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="luna-setup-"))
        self.settings = Settings(home=self.tmp / "home", cwd=self.tmp)
        self.secrets = Secrets(self.tmp / "home" / "secrets.json")
        self.outputs: list[str] = []

    def test_models_command_reports_roles_and_liveness(self):
        self.settings.set("model.provider", "fake")
        self.settings.set("model.kind", "fake")
        self.settings.set("model.main", "fake:echo")
        self.secrets.set("provider:fake", "sk-models-secret-333")
        code = run_models(settings=self.settings, secrets=self.secrets,
                          router=fake_router(self.settings, self.secrets),
                          out=self.outputs.append)
        self.assertEqual(code, 0)
        joined = "\n".join(self.outputs)
        self.assertIn("fake:echo", joined)
        self.assertIn("моделей в каталоге", joined)
        self.assertNotIn("sk-models-secret-333", joined)


class HeadlessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="luna-headless-"))
        self.settings = Settings(home=self.tmp / "home", cwd=self.tmp)
        self.settings.set("model.provider", "fake")
        self.settings.set("model.kind", "fake")
        self.settings.set("model.main", "fake:echo")
        self.secrets = Secrets(self.tmp / "home" / "secrets.json")

    def test_headless_plain_prints_answer(self):
        stream = io.StringIO()
        code = run_headless("привет", settings=self.settings, secrets=self.secrets,
                            router=fake_router(self.settings, self.secrets), out=stream)
        self.assertEqual(code, 0)
        self.assertIn("Эхо: привет", stream.getvalue())

    def test_headless_json_is_jsonl_events(self):
        stream = io.StringIO()
        code = run_headless("привет", json_mode=True, settings=self.settings,
                            secrets=self.secrets,
                            router=fake_router(self.settings, self.secrets), out=stream)
        self.assertEqual(code, 0)
        events = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
        types = [e["type"] for e in events]
        self.assertEqual(types[0], "session.start")
        self.assertIn("agent.text", types)
        self.assertEqual(types[-1], "turn.done")
        for event in events:
            self.assertIn("session", event)
            self.assertIn("ts", event)


if __name__ == "__main__":
    unittest.main()
