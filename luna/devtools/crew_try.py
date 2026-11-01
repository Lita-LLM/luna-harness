"""Полигон команды: `luna try crew` (ЗАДАЧА-11).

Фальшивые модели, без сети. Сценарий по тексту задачи: хозяин создаёт
трёх спутников (один — лидер), лидер раздаёт работу письмом, спутники
переписываются и отчитываются, один упирается в личный бюджет токенов и
сдаёт частичный результат; рядом — один ассистент. В конце: состав,
сводки, почта хозяина и лента событий детей текстом.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from ..core.stdio import ensure_utf8_stdio
from .rewind_try import _make_agent

_WATCHED = ("agent.spawn", "agent.state", "agent.message", "agent.done", "budget.update")


def run_crew_try(argv=None) -> int:
    ensure_utf8_stdio()
    root = Path(tempfile.mkdtemp(prefix="luna-crew-try-"))
    owner, _work, settings = _make_agent(root)
    settings.set("model.main", "fake:echo")
    settings.set("model.fast", "fake:echo")

    from ..agents.host import host_for, identity_of
    from ..agents.tools import _assistant, _crew

    events: list[dict] = []
    owner.listen(events.append)
    host = host_for(owner)
    me = identity_of(owner)
    ctx = owner.ctx

    def crew(**args) -> str:
        print("» crew " + " ".join(str(k) + "=" + str(v) for k, v in args.items()))
        out = _crew(ctx, args)
        print("  " + out.replace("\n", "\n  "))
        return out

    print("== Луна собирает команду (фальшивые модели) ==")
    crew(action="create", name="Тимлид", role="координатор команды",
         model="fake:echo", leader=True)
    crew(action="create", name="Кодер1", role="пишет код", model="fake:echo")
    crew(action="create", name="Ревьюер", role="проверяет код",
         model="fake:echo", tokens=10)
    crew(action="task", name="Тимлид", text="Раздай работу команде")
    host.wait_idle(5.0)

    leader = host.find_child("Тимлид")
    coder = host.find_child("Кодер1")
    print("» Тимлид раздаёт работу письмом Кодеру1")
    print("  " + host.send_mail(leader.endpoint(), coder.name,
                                "Сделай фичу X и отчитайся мне"))
    host.wait_idle(5.0)

    crew(action="task", name="Ревьюер", text="Проверь код фичи X")
    host.wait_idle(5.0)

    print("» assistant: хозяин зовёт ассистента за сводкой")
    print("  " + _assistant(ctx, {"action": "run", "task": "Собери сводку по команде"}))

    print()
    print("== Состав и кошелёк ==")
    print(host.describe())
    print()
    print("== Почта хозяина (уйдёт пометками в его следующий ход) ==")
    notes = host.take_owner_notes()
    print(notes if notes else "(пусто)")
    print()
    print("== Сводки спутников ==")
    for child in host.roster():
        print(child.name + ": " + (child.summary or "(ещё не работал)"))
    print()
    print("== Лента событий детей ==")
    for ev in events:
        if ev.get("type") not in _WATCHED:
            continue
        print(_event_line(ev))
    print()
    print("Готово. Команда живёт в сессии; спутники переживут --continue спящими.")
    return 0


def _event_line(ev: dict) -> str:
    etype = ev.get("type")
    name = str(ev.get("name") or "")
    if etype == "agent.spawn":
        return "agent.spawn   " + name + " (" + str(ev.get("kind")) + ", модель " + \
            str(ev.get("model")) + ")"
    if etype == "agent.state":
        return "agent.state   " + name + " → " + str(ev.get("state"))
    if etype == "agent.message":
        return "agent.message " + str(ev.get("from")) + " → " + str(ev.get("to")) + \
            ": " + str(ev.get("text"))[:60]
    if etype == "agent.done":
        return "agent.done    " + name + ": " + str(ev.get("summary"))[:60]
    if etype == "budget.update":
        return "budget.update потрачено $" + format(float(ev.get("spent_usd") or 0), ".4f") + \
            ", темп $" + format(float(ev.get("rate_per_min_usd") or 0), ".4f") + "/мин"
    return str(etype)
