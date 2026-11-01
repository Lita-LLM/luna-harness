"""Тулы детей (ЗАДАЧА-11): assistant и crew.

assistant — временный полноценный агент: хозяин отдаёт задание, ассистент
работает до сдачи и отвечает только хозяину. По умолчанию ждём сдачи
прямо в туле; wait=false — параллельно, результат забирается действием
result. Модель по умолчанию — роль из agents.assistant_role (fast).

crew — спутники: постоянная команда сессии. Действия: create (имя, роль,
модель/провайдер, промпт роли, worktree, leader), task, say (письмо по
имени), ping/wake, sleep, dismiss, list, leader, save/load (состав
данными: LUNA_HOME/crews и .luna/crews проекта).

Права и правила живут в host.py; здесь только поверхность для модели.
"""

from __future__ import annotations

from ..tools.registry import Tool
from .host import SpawnError, host_for, identity_of
from .mail import MailError

_BUDGET_PROPS = {
    "steps": {"type": "integer", "description": "Лимит шагов на ход"},
    "tokens": {"type": "integer", "description": "Личный лимит токенов (0 — нет)"},
    "usd": {"type": "number", "description": "Личный лимит денег, USD (0 — нет)"},
    "seconds": {"type": "number", "description": "Личный лимит времени, секунд (0 — нет)"},
}


def _host(ctx):
    agent = getattr(ctx, "agent", None)
    if agent is None:
        return None, "Тул работает только внутри агента."
    return host_for(agent), None


def _budget_args(args: dict) -> dict:
    return {"steps": int(args.get("steps") or 0), "tokens": int(args.get("tokens") or 0),
            "usd": float(args.get("usd") or 0.0), "seconds": float(args.get("seconds") or 0.0)}


def _assistant(ctx, args: dict) -> str:
    host, err = _host(ctx)
    if err:
        return err
    action = str(args.get("action") or "run")
    try:
        if action == "list":
            rows = []
            for child in host.roster():
                if child.kind != "assistant":
                    continue
                rows.append(child.name + " — " + child.state +
                            "; токенов " + str(child.totals["tokens"]) +
                            "; $" + format(child.totals["cost"], ".4f") +
                            (("; сводка: " + child.summary) if child.summary else ""))
            return "\n".join(rows) if rows else "Ассистентов не было."
        if action == "result":
            child = host.find_child(str(args.get("name") or ""))
            if child is None or child.kind != "assistant":
                return "Нет такого ассистента: " + str(args.get("name") or "")
            if not child.done_event.is_set():
                return child.name + " ещё работает (" + child.state + ")."
            return child.name + " сдал работу:\n" + (child.summary or "(пусто)")
        if action in ("run", "start"):
            task = str(args.get("task") or "").strip()
            if not task:
                return "Нужно поле task — задание ассистенту."
            child = host.spawn(kind="assistant", name=str(args.get("name") or ""),
                               role="ассистент", model=str(args.get("model") or ""),
                               mode=str(args.get("mode") or ""), **_budget_args(args))
            wait = action == "run" and bool(args.get("wait", True))
            summary = host.run_assistant(child, task, identity_of(ctx.agent), wait=wait)
            notes = ("; ".join(child.spawn_notes) + "\n") if child.spawn_notes else ""
            if not wait:
                return notes + "Ассистент " + child.name + " запущен параллельно. " \
                    "Забрать результат: assistant action=result, name=" + child.name
            return notes + "Ассистент " + child.name + " сдал работу:\n" + \
                (summary or child.summary or "(пусто)")
        return "Неизвестное действие: " + action + " (run|start|result|list)"
    except (SpawnError, MailError) as exc:
        return "Нельзя: " + str(exc)


def _crew(ctx, args: dict) -> str:
    host, err = _host(ctx)
    if err:
        return err
    action = str(args.get("action") or "list")
    me = identity_of(ctx.agent)
    try:
        if action == "list":
            from .crewfile import list_crew_files

            text = host.describe()
            saved = list_crew_files(ctx.cwd) if getattr(ctx, "cwd", None) else []
            if saved:
                text += "\nСохранённые составы: " + ", ".join(saved)
            return text
        if action == "create":
            name = str(args.get("name") or "").strip()
            if not name:
                return "Нужно имя спутника (поле name)."
            child = host.spawn(kind="satellite", name=name,
                               role=str(args.get("role") or ""),
                               prompt=str(args.get("prompt") or ""),
                               model=str(args.get("model") or ""),
                               mode=str(args.get("mode") or ""),
                               worktree=bool(args.get("worktree")),
                               leader=bool(args.get("leader")),
                               **_budget_args(args))
            notes = (" (" + "; ".join(child.spawn_notes) + ")") if child.spawn_notes else ""
            return "Спутник " + child.name + " создан и спит" + \
                (" — лидер команды" if child.leader else "") + notes + \
                ". Задание: crew action=task, name=" + child.name
        target_name = str(args.get("name") or args.get("to") or "")
        child = host.find_child(target_name) if target_name else None
        if action in ("task", "say", "ping", "wake", "sleep", "dismiss", "leader") \
                and child is None:
            return "Нет такого спутника: " + target_name
        if action == "task":
            text = str(args.get("text") or "").strip()
            if not text:
                return "Нужно поле text — задание."
            if not (me["kind"] == "main" or me["id"] == child.parent_id or child.leader):
                return "Задания спутнику даёт хозяин, его родитель или лидер."
            child.host.assign_task(child, text, me)
            return "Задание ушло: " + child.name + " (" + child.state + ")"
        if action == "say":
            text = str(args.get("text") or "").strip()
            if not text:
                return "Нужно поле text — текст письма."
            return host.send_mail(me, child.name, text)
        if action in ("ping", "wake"):
            child.host.wake(child, me)
            return child.name + " пнут — проснётся."
        if action == "sleep":
            child.host.sleep(child)
            return child.name + " спит."
        if action == "dismiss":
            if not (me["kind"] == "main" or me["id"] == child.parent_id):
                return "Распустить спутника может хозяин или его родитель."
            child.host.dismiss(child)
            return child.name + " распущен."
        if action == "leader":
            for other in child.host.roster():
                if other.kind == "satellite":
                    other.leader = other is child
            child.host._persist_crew()
            return child.name + " — лидер команды: отчёты идут ему, он раздаёт работу."
        if action == "save":
            crew_name = str(args.get("crew") or "").strip()
            if not crew_name:
                return "Нужно поле crew — имя состава."
            path = host.save_crew(crew_name, str(args.get("where") or "home"))
            return "Состав сохранён: " + str(path)
        if action == "load":
            crew_name = str(args.get("crew") or "").strip()
            if not crew_name:
                return "Нужно поле crew — имя состава."
            notes = host.load_crew(crew_name)
            return "Команда «" + crew_name + "» загружена:\n" + "\n".join(notes)
        return "Неизвестное действие: " + action
    except (SpawnError, MailError, ValueError) as exc:
        return "Нельзя: " + str(exc)


def assistant_tool() -> Tool:
    return Tool(
        name="assistant",
        description="Временный полноценный агент-ассистент: отдать задание и получить "
                    "сводку. Работает автономно до сдачи, отвечает только тебе. "
                    "action=run — дождаться сдачи; start — параллельно (wait=false), "
                    "result — забрать сводку по имени; list — кто был. Модель по "
                    "умолчанию — быстрая роль (agents.assistant_role), можно указать "
                    "другую полем model. Бюджет: steps/tokens/usd/seconds + общий "
                    "кошелёк сессии.",
        parameters={"type": "object", "properties": dict(
            {"action": {"type": "string", "enum": ["run", "start", "result", "list"]},
             "task": {"type": "string", "description": "Задание ассистенту"},
             "name": {"type": "string", "description": "Имя (для result — чью сводку)"},
             "model": {"type": "string", "description": "Роль или ссылка провайдер:модель"},
             "mode": {"type": "string", "description": "Режим Предохранителя, не свободнее твоего"},
             "wait": {"type": "boolean", "description": "Для start: не ждать сдачи"}},
            **_BUDGET_PROPS),
            "required": ["action"]},
        handler=_assistant)


def crew_tool() -> Tool:
    return Tool(
        name="crew",
        description="Команда спутников — постоянных агентов сессии со своими ролями и "
                    "моделями (можно разных провайдеров). create — создать (name, role, "
                    "prompt роли, model, worktree=true — отдельный git worktree, "
                    "leader=true — лидер: ему идут отчёты, он раздаёт работу); task — "
                    "задание по имени; say — письмо по имени (спутники пишут друг другу "
                    "и пингуют: ping/wake); sleep — усыпить (спящий не тратит ничего); "
                    "dismiss — распустить; leader — назначить лидера; list — состав, "
                    "состояния и кошелёк; save/load — состав данными (crew=имя, "
                    "where=home|project). Спутники переживают --continue: вернутся спящими.",
        parameters={"type": "object", "properties": dict(
            {"action": {"type": "string",
                        "enum": ["create", "task", "say", "ping", "wake", "sleep",
                                 "dismiss", "leader", "list", "save", "load"]},
             "name": {"type": "string", "description": "Имя спутника"},
             "to": {"type": "string", "description": "Адресат письма (то же, что name)"},
             "text": {"type": "string", "description": "Задание или текст письма"},
             "role": {"type": "string", "description": "Роль спутника одной строкой"},
             "prompt": {"type": "string", "description": "Промпт роли спутника"},
             "model": {"type": "string", "description": "Роль или ссылка провайдер:модель"},
             "mode": {"type": "string", "description": "Режим, не свободнее твоего"},
             "worktree": {"type": "boolean", "description": "Отдельный git worktree"},
             "leader": {"type": "boolean", "description": "Сделать лидером команды"},
             "crew": {"type": "string", "description": "Имя сохранённого состава"},
             "where": {"type": "string", "enum": ["home", "project"],
                       "description": "Куда сохранять состав"}},
            **_BUDGET_PROPS),
            "required": ["action"]},
        handler=_crew)
