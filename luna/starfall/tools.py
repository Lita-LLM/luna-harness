"""Тулы роя для матери (ЗАДАЧА-15): cells и starfall.

cells — профили ячеек данными: list/show читают; create/edit правят
профили памяти сессии без подтверждения; save пишет профиль в
LUNA_HOME/cells на весь харнесс — это меняющее действие, его спрашивает
Предохранитель матери. Права профиля ⊆ прав матери: тул, которого у
матери нет, в профиль не попадёт уже при создании.

starfall — запуск роя одним действием: estimate (смета пробным прогоном
на 3 входах, не меняющее), run (меняющее: Предохранитель спрашивает один
раз, в аргументах — смета), status и resume (докрутить незаконченный
поток из его папки на диске). Потолки — настройки starfall.*: мать
решает в их пределах, выше потолка не прыгнуть.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from ..core.paths import luna_home
from ..tools.registry import Tool, ToolResult
from . import profiles as profiles_mod
from .engine import (RouterCellRunner, SwarmError, SwarmRun, estimate,
                     find_unfinished, resolve_inputs, resume, unfinished_from_records)
from .profiles import CellProfile

_SPAWN_BLOCK = ("assistant", "crew", "starfall", "cells")


def _mother_tools(ctx) -> dict:
    agent = getattr(ctx, "agent", None)
    tools = getattr(agent, "tools", None) if agent is not None else None
    if tools is None:
        return {}
    # dict.keys обходит ленивую догрузку MCP: для проверки прав хватит
    # встроенных тулов матери.
    return {name: None for name in dict.keys(tools)}


def _settings(ctx):
    return getattr(ctx, "settings", None)


def _check_subset(ctx, prof: CellProfile) -> str | None:
    """Права профиля ⊆ прав матери; строка — почему нельзя."""
    have = _mother_tools(ctx)
    missing = [name for name in prof.tools if name not in have]
    if missing:
        return ("у матери нет тулов " + ", ".join(missing) +
                " — права ячейки не могут быть шире прав матери")
    blocked = [name for name in prof.tools if name in _SPAWN_BLOCK]
    if blocked:
        return "служебные тулы " + ", ".join(blocked) + " ячейкам запрещены"
    return None


def _profile_line(prof: CellProfile) -> str:
    pool = (" пул×" + str(len(prof.pool))) if prof.pool else ""
    return (prof.caste + " — " + (prof.name or prof.caste) +
            " (" + profiles_mod.SOURCE_NAMES.get(prof.source, prof.source) + ")" +
            "; модель " + prof.model + pool +
            "; тулы: " + (", ".join(prof.tools) if prof.tools else "нет") +
            "; запись " + prof.write_mode + "; почта " + prof.mail +
            "; TTL " + format(prof.ttl_s, ".0f") + " с")


def _cells(ctx, args: dict) -> ToolResult:
    action = str(args.get("action") or "list")
    agent = getattr(ctx, "agent", None)
    settings = _settings(ctx)
    session = profiles_mod.session_profiles(agent) if agent is not None else {}
    try:
        if action == "list":
            profs = profiles_mod.library(settings, session)
            if not profs:
                return ToolResult(True, "Профилей нет.")
            return ToolResult(True, "\n".join(_profile_line(p) for p in profs))
        if action == "show":
            prof = profiles_mod.get_profile(str(args.get("caste") or ""), settings, session)
            if prof is None:
                return ToolResult(False, "Нет профиля «" + str(args.get("caste")) + "».")
            return ToolResult(True, _profile_line(prof) + "\n\n" + (prof.prompt or ""))
        if action in ("create", "edit"):
            data = dict(args.get("profile") or {})
            if args.get("caste") and not data.get("caste"):
                data["caste"] = args["caste"]
            prof = profiles_mod.validate_profile(CellProfile.from_dict(data))
            if prof.source == "builtin" and action == "edit" and prof.caste not in session:
                base = profiles_mod.get_profile(prof.caste, settings, session)
                if base is not None and base.source == "builtin":
                    pass  # правка встроенного для сессии — копия в памяти, файл не трогаем
            problem = _check_subset(ctx, prof)
            if problem:
                return ToolResult(False, "Нельзя: " + problem)
            prof.source = "session"
            session[prof.caste] = prof
            return ToolResult(True, "Профиль сессии обновлён: " + _profile_line(prof))
        if action == "save":
            caste = str(args.get("caste") or "")
            prof = profiles_mod.get_profile(caste, settings, session)
            if prof is None:
                return ToolResult(False, "Нет профиля «" + caste + "» — сначала create.")
            path = profiles_mod.save_home_profile(prof, settings)
            return ToolResult(True, "Профиль сохранён на весь харнесс: " + str(path))
        return ToolResult(False, "Неизвестное действие: " + action +
                          " (list|show|create|edit|save)")
    except ValueError as exc:
        return ToolResult(False, "Нельзя: " + str(exc))


def cells_tool() -> Tool:
    return Tool(
        name="cells",
        description="Профили ячеек роя данными (docs/РОЙ.md §5): list — все "
                    "профили (встроенные, свои из LUNA_HOME/cells, сессии); "
                    "show — профиль по касте с промптом; create/edit — создать "
                    "или изменить профиль в памяти сессии (без подтверждения; "
                    "поля profile: caste, name, description, model — роль или "
                    "ссылка, pool — адреса одной модели списком (адрес может "
                    "быть словарём {url, secret: «pool:<имя>», kind} — ключ "
                    "по имени берётся из сейфа, значение ключа профилю и "
                    "ячейкам не показывается), net — сеть для bash ячейки "
                    "(по умолчанию выключена изоляцией), prompt, tools, "
                    "write_mode: none|sandbox|worktree|project, ttl, mail, "
                    "steps, budget_usd, budget_tokens, can_spawn); save — "
                    "сохранить профиль сессии на весь харнесс (с подтверждением "
                    "человека). Права профиля не шире твоих: чужих тулов в "
                    "профиль не вписать. Профили — материал для starfall (рой "
                    "на много однотипных входов); одиночную задачу отдавай "
                    "assistant, не ячейке.",
        parameters={"type": "object", "properties": {
            "action": {"type": "string",
                       "enum": ["list", "show", "create", "edit", "save"]},
            "caste": {"type": "string", "description": "Каста профиля (rev, cls, …)"},
            "profile": {"type": "object", "description": "Поля профиля для create/edit"}},
            "required": ["action"]},
        handler=_cells,
        mutating_for=lambda args: str((args or {}).get("action")) == "save")


# --- starfall ---------------------------------------------------------------------


def _output_root(ctx) -> Path:
    settings = _settings(ctx)
    raw = ""
    if settings is not None:
        raw = str(settings.get("starfall.output_dir", "") or "")
    if raw:
        return Path(raw).expanduser()
    home = Path(settings.home_dir) if settings is not None else luna_home()
    return home / "local" / "starfall"


def _caps(ctx) -> dict:
    settings = _settings(ctx)

    def get(key, default):
        return settings.get(key, default) if settings is not None else default

    return {"enabled": bool(get("starfall.enabled", True)),
            "max_cells": int(get("starfall.max_cells", 100) or 100),
            "max_parallel": int(get("starfall.max_parallel", 8) or 8),
            "budget_usd": float(get("starfall.budget_usd", 1.0) or 0.0),
            "ttl_s": float(get("starfall.ttl_s", 300) or 300),
            "max_attempts": int(get("starfall.max_attempts", 3) or 3)}


def _make_runner(ctx, project_confirmed: bool) -> RouterCellRunner:
    agent = getattr(ctx, "agent", None)
    mother_tools = {}
    if agent is not None and getattr(agent, "tools", None) is not None:
        mother_tools = {tool.name: tool for tool in agent.tools.values()}
    return RouterCellRunner(
        router=getattr(agent, "router", None), settings=_settings(ctx),
        secrets=getattr(ctx, "secrets", None), mother_tools=mother_tools,
        guard=getattr(agent, "guard", None) if agent is not None else None,
        project_dir=Path(getattr(ctx, "cwd", Path.cwd())),
        project_confirmed=project_confirmed,
        cancel_event=getattr(agent, "_interrupt", None) if agent is not None else None)


def _plan(ctx, args: dict):
    """Общий разбор запуска: (профили, входы, параметры) или текст ошибки."""
    settings = _settings(ctx)
    agent = getattr(ctx, "agent", None)
    session = profiles_mod.session_profiles(agent) if agent is not None else {}
    castes = args.get("profiles") or args.get("profile") or "cls"
    if isinstance(castes, str):
        castes = [c.strip() for c in castes.split(",") if c.strip()]
    profs = []
    for caste in castes:
        prof = profiles_mod.get_profile(str(caste), settings, session)
        if prof is None:
            return None, "Нет профиля «" + str(caste) + "». Есть: " + \
                ", ".join(p.caste for p in profiles_mod.library(settings, session))
        problem = _check_subset(ctx, prof)
        if problem:
            return None, "Профиль " + prof.caste + ": " + problem
        ttl_cap = _caps(ctx)["ttl_s"]
        if prof.ttl_s > ttl_cap:
            prof = CellProfile.from_dict(prof.to_dict())
            prof.ttl_s = ttl_cap  # потолок starfall.ttl_s — выше нельзя
        profs.append(prof)
    inputs = resolve_inputs(args.get("inputs"), Path(getattr(ctx, "cwd", Path.cwd())))
    return (profs, inputs), None


def _stream_for(ctx) -> str:
    import datetime

    from .addressing import next_stream, stream_for_date

    agent = getattr(ctx, "agent", None)
    used = getattr(agent, "_starfall_streams", None) if agent is not None else None
    today = stream_for_date(datetime.date.today())
    if used is None:
        if agent is not None:
            try:
                agent._starfall_streams = [today]
            except Exception:  # noqa: BLE001
                pass
        return today
    nxt = next_stream(used[-1])
    used.append(nxt)
    return nxt


def _journal(ctx, data: dict) -> None:
    agent = getattr(ctx, "agent", None)
    journal = getattr(agent, "journal", None) if agent is not None else None
    if journal is not None:
        try:
            journal.append({"kind": "swarm", "data": data, "ts": time.time()})
        except Exception:  # noqa: BLE001 — журнал не роняет рой
            pass


def _starfall(ctx, args: dict) -> ToolResult:
    action = str(args.get("action") or "run")
    caps = _caps(ctx)
    agent = getattr(ctx, "agent", None)
    settings = _settings(ctx)
    try:
        if action == "status":
            rows = []
            for path in find_unfinished(_output_root(ctx)):
                rows.append("незакончен: " + path.name + " (" + str(path) + ")")
            records = []
            journal = getattr(agent, "journal", None) if agent is not None else None
            if journal is not None:
                records = getattr(journal, "records", []) or []
            last = unfinished_from_records(records)
            if last:
                rows.append("по журналу сессии незакончен поток: " +
                            str(last.get("swarm")) + " — " + str(last.get("dir")))
            return ToolResult(True, "\n".join(rows) if rows else
                              "Незаконченных потоков нет.")
        if action == "resume":
            target = str(args.get("swarm") or "").strip()
            root = _output_root(ctx)
            candidates = find_unfinished(root)
            path = None
            if target:
                direct = Path(target)
                path = direct if direct.is_dir() else root / target
            elif candidates:
                path = candidates[-1]
            if path is None or not path.is_dir():
                return ToolResult(False, "Не нашёл незаконченный поток: " +
                                  (target or "ни одного"))
            runner = _make_runner(ctx, bool(args.get("project_confirmed")))
            host = None
            if agent is not None:
                from ..agents.host import host_for

                host = host_for(agent)
            summary = resume(path, settings=settings,
                             publish=(agent.publish if agent is not None else None),
                             runner=runner,
                             interrupt_check=(lambda: agent._interrupt.is_set())
                             if agent is not None else None,
                             wallet=host.wallet if host is not None else None,
                             secrets=getattr(ctx, "secrets", None))
            _journal(ctx, {"swarm": summary["swarm"], "dir": summary["dir"],
                           "finished": True})
            return ToolResult(True, _summary_text(summary))
        # estimate и run: общий план
        plan, err = _plan(ctx, args)
        if err:
            return ToolResult(False, "Нельзя: " + err)
        profs, inputs = plan
        if not caps["enabled"]:
            return ToolResult(False, "Звездопад выключен настройкой starfall.enabled.")
        if action in ("estimate", "run"):
            # Рой на горсть входов — лишняя машина: дешевле assistant или
            # свои руки. Порог — настройка; человек может явно настоять.
            min_inputs = 3
            if settings is not None:
                min_inputs = int(settings.get("starfall.min_inputs", 3) or 3)
            if len(inputs) < min_inputs and not bool(args.get("force")):
                return ToolResult(False,
                                  "Рой на " + str(len(inputs)) + " вход(а) не "
                                  "запускаю: порог starfall.min_inputs=" +
                                  str(min_inputs) + ". Одна целая задача — "
                                  "assistant: {action: \"run\", task: \"…\"}; "
                                  "пара входов — сделай сама. Если человек ЯВНО "
                                  "настоял именно на рое — повтори вызов с "
                                  "force=true.")
        if len(inputs) > caps["max_cells"]:
            return ToolResult(False, "Входов " + str(len(inputs)) +
                              " — выше потолка starfall.max_cells=" +
                              str(caps["max_cells"]) + ". Потолок меняет человек "
                              "в настройках.")
        parallel = min(int(args.get("parallel") or caps["max_parallel"]),
                       caps["max_parallel"])
        if action == "estimate":
            runner = _make_runner(ctx, bool(args.get("project_confirmed")))
            with tempfile.TemporaryDirectory(prefix="luna-estimate-") as tmp:
                est = estimate(runner, profs[0], inputs,
                               str(args.get("template") or ""), settings,
                               parallel=parallel, tmp_dir=Path(tmp))
            return ToolResult(True,
                              "Смета (пробный прогон на " + str(est["samples"]) +
                              " входах, профиль " + est["profile"] + ", модель " +
                              est["model"] + "): одно — $" +
                              format(est["per_cost_usd"], ".4f") + " и " +
                              format(est["per_time_s"], ".2f") + " с; всего на " +
                              str(len(inputs)) + " входов — $" +
                              format(est["total_cost_usd"], ".4f") + " и ≈" +
                              format(est["total_time_s"], ".0f") + " с при параллельности " +
                              str(parallel) + ".")
        if action == "run":
            from ..agents.host import host_for

            spawner_ok = False
            if agent is not None:
                from ..agents.host import identity_of

                spawner_ok = identity_of(agent)["kind"] in ("main", "satellite")
                if getattr(agent, "_agents_child_id", "") and \
                        not getattr(agent, "_agents_can_spawn", True):
                    spawner_ok = False  # спутнику размножение запретили при создании
            if not spawner_ok:
                return ToolResult(False, "Рой зовут только полноценные агенты "
                                         "(главный и спутники с правом размножения).")
            budget = float(args.get("budget_usd") or caps["budget_usd"])
            if budget > caps["budget_usd"]:
                return ToolResult(False, "Бюджет $" + format(budget, ".2f") +
                                  " выше потолка starfall.budget_usd=$" +
                                  format(caps["budget_usd"], ".2f") + ".")
            mail = str(args.get("mail") or profs[0].mail or "tree")
            write_override = str(args.get("write_mode") or "")
            if write_override:
                profs = [CellProfile.from_dict(p.to_dict()) for p in profs]
                for prof in profs:
                    prof.write_mode = write_override
            if any(p.write_mode == "project" for p in profs) and \
                    not bool(args.get("project_confirmed")):
                return ToolResult(False, "Режим записи project — только с явным "
                                         "подтверждением человека: поставь "
                                         "project_confirmed=true после его «да» "
                                         "(подтверждение запуска покажет режим).")
            host = host_for(agent)
            run = SwarmRun(stream=_stream_for(ctx), profiles=profs, inputs=inputs,
                           template=str(args.get("template") or ""),
                           output_dir=_output_root(ctx), settings=settings,
                           publish=agent.publish, runner=_make_runner(
                               ctx, bool(args.get("project_confirmed"))),
                           parallel=parallel,
                           link_size=int(args.get("link_size") or 8),
                           budget_usd=budget, mail=mail,
                           collapse=str(args.get("collapse") or "auto"),
                           max_attempts=caps["max_attempts"],
                           interrupt_check=lambda: agent._interrupt.is_set(),
                           wallet=host.wallet,
                           secrets=getattr(ctx, "secrets", None))
            agent._starfall_run = run
            _journal(ctx, {"swarm": run.swarm_id, "dir": str(run.dir),
                           "finished": False, "total": len(inputs)})
            try:
                summary = run.run()
            finally:
                agent._starfall_run = None
            _journal(ctx, {"swarm": summary["swarm"], "dir": summary["dir"],
                           "finished": True})
            return ToolResult(True, _summary_text(summary))
        return ToolResult(False, "Неизвестное действие: " + action +
                          " (estimate|run|status|resume)")
    except SwarmError as exc:
        return ToolResult(False, "Нельзя: " + str(exc))


def _summary_text(summary: dict) -> str:
    head = ("Поток " + summary["swarm"] + ": сдали " + str(summary["done"]) +
            " из " + str(summary["total"]) + ", упали " + str(summary["failed"]) +
            ", не начаты " + str(summary["skipped"]) + ", звеньев " +
            str(summary["links"]) + ", $" + format(summary["spent_usd"], ".4f") +
            ", " + str(summary["tokens"]) + " токенов, " +
            format(summary["seconds"], ".1f") + " с.")
    if summary.get("stopped_reason"):
        head += " Остановлен: " + summary["stopped_reason"] + "."
    head += " Результаты и полная сводка: " + summary["dir"]
    if summary["total"] <= 50:
        return head + "\n\n" + "\n\n".join(summary["link_summaries"])
    return head + "\nСводки звеньев — в summary.md папки потока (в контекст не тащу простыню)."


def starfall_tool() -> Tool:
    return Tool(
        name="starfall",
        description="Звездопад: рой одноразовых ячеек по профилям (cells) на "
                    "массовую однотипную работу. КОГДА ЗВАТЬ РОЙ: входов МНОГО "
                    "и они ОДНОТИПНЫ — одна ячейка на вход, один шаблон на всех. "
                    "НЕ рой: одна целая задача — assistant; постоянная команда "
                    "ролей — crew. Порядок: сначала action=estimate — смета "
                    "пробным прогоном на 3 входах; потом action=run с той же "
                    "сметой в поле estimate — запуск одним действием "
                    "(подтверждение покажет смету, профили, режим записи). "
                    "Пример: {action: \"estimate\", profile: \"cls\", inputs: "
                    "{glob: \"src/*.py\"}, template: \"Классифицируй файл: "
                    "{input}\"} — затем тот же вызов с action=run и полем "
                    "estimate. Меньше starfall.min_inputs входов (по умолчанию "
                    "3) рой не запускается: 1–2 входа — assistant или сделай "
                    "сама; человек может явно настоять — тогда force=true. "
                    "inputs: список строк | {glob: маска} | {file_lines: файл} | "
                    "{file: файл} | {text: текст, chunk_chars: N}. profile — "
                    "каста или несколько через запятую. Потолки — настройки "
                    "starfall.* (ячеек, параллельности, бюджет, TTL). Итог — "
                    "сводка и папка с результатами; action=resume докручивает "
                    "незаконченный поток, status — что не закончено. Полное "
                    "руководство — скилл starfall (тул skill).",
        parameters={"type": "object", "properties": {
            "action": {"type": "string",
                       "enum": ["estimate", "run", "status", "resume"]},
            "inputs": {"description": "Входы НАСТОЯЩИМ JSON-массивом строк или объектом "
                                      "{glob|file_lines|file|text}, не строкой с JSON внутри. "
                                      "Один элемент массива — одна ячейка роя"},
            "profile": {"type": "string", "description": "Каста профиля (или несколько через запятую)"},
            "profiles": {"type": "array", "items": {"type": "string"},
                         "description": "Касты списком (альтернатива profile)"},
            "template": {"type": "string", "description": "Шаблон задания, {input} — место входа"},
            "parallel": {"type": "integer", "description": "Параллельность (не выше starfall.max_parallel)"},
            "link_size": {"type": "integer", "description": "Ячеек в звене (по умолчанию 8)"},
            "budget_usd": {"type": "number", "description": "Бюджет потока (не выше starfall.budget_usd)"},
            "mail": {"type": "string", "enum": ["none", "tree", "board", "neighbors"],
                     "description": "Схема почты роя"},
            "write_mode": {"type": "string",
                           "enum": ["none", "sandbox", "worktree", "project"],
                           "description": "Перекрыть режим записи профилей"},
            "collapse": {"type": "string", "enum": ["auto", "code", "model"],
                         "description": "Свёртка звеньев: кодом или ячейкой col"},
            "estimate": {"type": "string", "description": "Текст сметы из action=estimate — уйдёт в подтверждение запуска"},
            "force": {"type": "boolean",
                      "description": "Запустить рой вопреки порогу starfall.min_inputs — только если человек явно настоял"},
            "project_confirmed": {"type": "boolean",
                                  "description": "Человек явно подтвердил запись прямо в проект"},
            "swarm": {"type": "string", "description": "Для resume: id потока или путь его папки"}},
            "required": ["action"]},
        handler=_starfall,
        mutating_for=lambda args: str((args or {}).get("action")) in ("run", "resume"))
