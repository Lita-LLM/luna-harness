"""Headless-клиент: luna -p "задача".

Печатает ответ; --json — поток событий docs/СОБЫТИЯ.md построчно (JSONL).
Подтверждения без терминала: канала вопросов нет, Предохранитель по
умолчанию отказывает с понятным сообщением (решают режим и правила).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _default_settings_secrets_router():
    from .core.paths import DataDir
    from .core.secrets import Secrets
    from .core.settings import Settings
    from .router.core import Router

    data = DataDir()
    data.ensure()
    settings = Settings()
    secrets = Secrets(data.secrets)
    return settings, secrets, Router(settings, secrets)


def run_headless(prompt: str, *, json_mode: bool = False, mode: str = "",
                 model_ref: str = "", settings=None, secrets=None, router=None,
                 agent=None, out=None) -> int:
    """Выполнить один ход без интерфейса. Всё, кроме router/agent, — по умолчанию настоящее."""
    stream = out or sys.stdout
    if agent is None:
        if router is None or settings is None:
            def_settings, def_secrets, def_router = _default_settings_secrets_router()
            settings = settings or def_settings
            secrets = secrets if secrets is not None else def_secrets
            router = router or def_router
        from .mind.loop import Agent

        agent = Agent(settings=settings, secrets=secrets, router=router,
                      cwd=Path.cwd(), mode=mode, model_ref=model_ref)
    elif mode:
        agent.set_mode(mode)

    def on_event(event: dict) -> None:
        if json_mode:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
            stream.flush()
            return
        kind = event.get("type")
        if kind == "agent.text":
            stream.write(event.get("text", ""))
            stream.flush()
        elif kind == "error":
            stream.write("\nОшибка: " + event.get("message", "") + "\n")
            stream.flush()
        elif kind == "turn.done":
            stream.write("\n")
            stream.flush()

    agent.listen(on_event)
    reason = agent.run_turn(prompt)
    return 0 if reason in ("end", "limit", "interrupted") else 2
