"""luna try undo и luna try compact: откат и прилив на фальшивой модели.

undo: во временном каталоге агент пишет файл, правит его и создаёт второй,
потом /undo возвращает файлы и разговор на ход назад, а /redo — обратно.
compact: пять фальшивых ходов сжимаются командой /compact — печатаем
было/стало и событие context.compacted. Сети и ключей не нужно.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from ..core.stdio import ensure_utf8_stdio


def _make_agent(root: Path):
    from ..core.secrets import Secrets
    from ..core.settings import Settings
    from ..mind.loop import Agent
    from ..router.core import Router

    work = root / "work"
    work.mkdir(parents=True, exist_ok=True)
    settings = Settings(home=root / "home", cwd=work)
    settings.set("model.provider", "fake")
    settings.set("model.kind", "fake")
    settings.set("model.main", "fake:script")
    secrets = Secrets(root / "home" / "secrets.json")
    router = Router(settings, secrets)
    agent = Agent(settings=settings, secrets=secrets, router=router,
                  cwd=work, mode="auto")
    return agent, work, settings


class _Home:
    """Временный LUNA_HOME на время сценария (чекпоинты живут там)."""

    def __init__(self, root: Path):
        self._root = root
        self._old = os.environ.get("LUNA_HOME")

    def __enter__(self):
        os.environ["LUNA_HOME"] = str(self._root / "home")
        return self

    def __exit__(self, *exc):
        if self._old is None:
            os.environ.pop("LUNA_HOME", None)
        else:
            os.environ["LUNA_HOME"] = self._old


def run_undo_try(out=None) -> int:
    ensure_utf8_stdio()
    stream = out or print
    from .fake_llm import set_script

    with tempfile.TemporaryDirectory(prefix="luna-undo-") as tmp:
        root = Path(tmp)
        with _Home(root):
            agent, work, _settings = _make_agent(root)
            set_script([
                {"tool_calls": [{"name": "write",
                                 "arguments": {"path": "a.txt", "content": "версия 1\n"},
                                 "intent": "создать файл"}]},
                {"text": "Записал a.txt."},
                {"tool_calls": [
                    {"name": "edit",
                     "arguments": {"path": "a.txt", "old": "версия 1", "new": "версия 2"},
                     "intent": "обновить файл"},
                    {"name": "write",
                     "arguments": {"path": "b.txt", "content": "файл b\n"},
                     "intent": "второй файл"}]},
                {"text": "Обновил a.txt и создал b.txt."},
            ])
            agent.run_turn("Создай a.txt")
            agent.run_turn("Обнови a.txt и добавь b.txt")
            a = work / "a.txt"
            b = work / "b.txt"
            stream("После двух ходов: a.txt = " + a.read_text(encoding="utf-8").strip() +
                   ", b.txt существует: " + str(b.exists()))
            agent.handle_command({"type": "undo"})
            ok_undo = a.read_text(encoding="utf-8") == "версия 1\n" and not b.exists()
            stream("После /undo: a.txt = " + a.read_text(encoding="utf-8").strip() +
                   ", b.txt существует: " + str(b.exists()) +
                   ", ходов помнит: " + str(len(agent.turns_overview())))
            agent.handle_command({"type": "redo"})
            ok_redo = a.read_text(encoding="utf-8") == "версия 2\n" and b.exists()
            stream("После /redo: a.txt = " + a.read_text(encoding="utf-8").strip() +
                   ", b.txt существует: " + str(b.exists()))
            stream("Чекпоинты сессии: " + str(root / "home" / "checkpoints"))
            stream("Итог: undo " + ("OK" if ok_undo else "ПРОВАЛ") +
                   ", redo " + ("OK" if ok_redo else "ПРОВАЛ"))
            return 0 if ok_undo and ok_redo else 1


def run_compact_try(out=None) -> int:
    ensure_utf8_stdio()
    stream = out or print
    from .fake_llm import set_script

    with tempfile.TemporaryDirectory(prefix="luna-compact-") as tmp:
        root = Path(tmp)
        with _Home(root):
            agent, _work, settings = _make_agent(root)
            settings.set("model.context_tokens", 1000)  # окно маленькое — проценты видны
            steps = []
            for n in range(1, 6):
                steps.append({"text": "Ход " + str(n) + ": обсуждаем детали проекта. " +
                                      "Подробности хода номер " + str(n) + ". " * 8,
                              "usage": {"input": 300, "output": 20}})
            steps.append({"text": "Цель — проверить прилив. Сделано: пять ходов "
                                  "обсуждений. Открытых вопросов нет."})
            set_script(steps)
            events: list = []
            agent.listen(events.append)
            for n in range(1, 6):
                agent.run_turn("Сообщение хода " + str(n))
            before = len(agent._history)
            agent.handle_command({"type": "compact", "hint": "сохрани цель проверки"})
            after = len(agent._history)
            compacted = [e for e in events if e.get("type") == "context.compacted"]
            notices = [e.get("text", "") for e in events
                       if e.get("type") == "notice" and "Прилив" in e.get("text", "")]
            stream("Сообщений в истории: было " + str(before) + ", стало " + str(after))
            if compacted:
                ev = compacted[-1]
                stream("Событие context.compacted: было ~" + str(ev.get("before_pct")) +
                       "%, стало ~" + str(ev.get("after_pct")) + "%")
            if notices:
                stream(notices[-1])
            stream("Итог: " + ("OK" if compacted and after < before else "ПРОВАЛ"))
            return 0 if compacted and after < before else 1
