"""luna try brain: мозг на фальшивой модели, без сети и ключей.

Сценарий: модель пишет файл через тул write (режим auto), потом отвечает.
События печатаются одной строкой каждое — видно контракт docs/СОБЫТИЯ.md.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from ..core.stdio import ensure_utf8_stdio


def run_brain_try(out=None) -> int:
    ensure_utf8_stdio()
    stream = out or print
    from ..core.secrets import Secrets
    from ..core.settings import Settings
    from ..mind.loop import Agent
    from ..router.core import Router
    from .fake_llm import set_script

    with tempfile.TemporaryDirectory(prefix="luna-brain-") as tmp:
        root = Path(tmp)
        settings = Settings(home=root / "home", cwd=root)
        settings.set("model.provider", "fake")
        settings.set("model.kind", "fake")
        settings.set("model.main", "fake:script")
        secrets = Secrets(root / "home" / "secrets.json")
        router = Router(settings, secrets)
        set_script([
            {"tool_calls": [{"name": "write",
                             "arguments": {"path": "привет.txt", "content": "Луна пишет файл.\n"},
                             "intent": "проверить тул write на фальшивой модели"}]},
            {"text": "Готово: файл привет.txt записан."},
        ])
        agent = Agent(settings=settings, secrets=secrets, router=router, cwd=root, mode="auto")
        seen: list[str] = []
        agent.listen(lambda event: seen.append(event.get("type", "")))
        reason = agent.run_turn("Запиши файл привет.txt")
        content = (root / "привет.txt").read_text(encoding="utf-8")
        stream("События: " + " → ".join(seen))
        stream("Итог хода: " + reason)
        stream("Файл: " + content.strip())
    return 0
