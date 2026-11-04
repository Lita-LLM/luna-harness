"""Headless-клиент: luna -p "задача" (ЗАДАЧА-04, расширено ЗАДАЧЕЙ-21).

Один ход без интерфейса:
- ответ агента — в stdout; ход (тулы, спутники, рой, Предохранитель) —
  в stderr, кратко, по строке на событие;
- --output json — один итоговый объект в stdout в конце (статус, ответ,
  расход, тулы, отказы); --output stream-json (и старый --json) — события
  шины docs/СОБЫТИЯ.md построчно JSON в stdout;
- коды выхода: 0 ok, 1 модель/сеть, 2 флаги/настройки/профиль, 3 лимит,
  4 отказ при fail_on_deny, 5 схема, 6 MCP, 130 прервано;
- клетка работника (--worker, docs/РАБОТНИК.md) — точный эфемерный запуск
  без проектных настроек и журнала, только с разрешёнными тулами;
- промпт можно дать аргументом или в stdin;
- сессии пишутся в журнал LUNA_HOME/sessions, как у чата: --continue и
  --resume <id> продолжают разговор с памятью модели;
- --max-usd и --max-turns — потолки именно этого запуска.

Вопросы Предохранителя без человека: подтверждать некому, поэтому всё,
что Guard решил спросить, отклоняется с понятной причиной в stderr.
Разрешить можно только явно: шаблонами --allow от человека в командной
строке или его настройкой headless.allow (синтаксис — как guard.rules:
«тул» или «тул:подстрока аргументов», fnmatch по имени тула). Жёсткие
блоки hard_block (необратимое, утечки секретов, локальная сеть, рой с
записью в проект и т.п.) не разрешаются ничем — даже шаблоном.
"""

from __future__ import annotations

import fnmatch
import json
import sys
import threading
from pathlib import Path


def _data_dir(settings, data):
    """Каталог данных для журналов сессий: явный, из настроек или общий."""
    from .core.paths import DataDir

    if data is not None:
        return data
    home = getattr(settings, "home_dir", None)
    if home is not None:
        return DataDir(Path(home))
    return DataDir()


def _allow_patterns(settings, cli_patterns) -> list:
    """Шаблоны разрешений: --allow человека + его настройка headless.allow."""
    patterns = [str(p).strip() for p in (cli_patterns or []) if str(p).strip()]
    if settings is not None:
        raw = str(settings.get("headless.allow", "") or "")
        for line in raw.replace(",", "\n").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                patterns.append(line)
    return patterns


def _pattern_matches(pattern: str, tool_name: str, args: dict) -> bool:
    tool_pattern, _, needle = pattern.partition(":")
    if not fnmatch.fnmatch(tool_name, tool_pattern.strip()):
        return False
    if needle:
        text = json.dumps(args or {}, ensure_ascii=False, default=str)
        return needle in text
    return True


def _brief_args(args: dict) -> str:
    text = json.dumps(args or {}, ensure_ascii=False, default=str)
    return text if len(text) <= 120 else text[:117] + "…"


def _progress_line(event: dict) -> str | None:
    """Событие шины → одна короткая строка хода для stderr (или None)."""
    kind = event.get("type")
    if kind == "session.start":
        return ("сессия " + str(event.get("session", "")) + ": модель " +
                str(event.get("model", "")) + ", режим " + str(event.get("mode", "")))
    if kind == "turn.start":
        return "ход " + str(event.get("turn", ""))
    if kind == "tool.call":
        line = "тул " + str(event.get("name", "")) + ": " + _brief_args(event.get("args") or {})
        intent = str(event.get("intent") or "")
        if intent:
            line += " — " + intent
        return line
    if kind == "tool.result":
        return ("тул " + str(event.get("id", "")) + ": " +
                ("ok" if event.get("ok") else "ошибка") + " — " +
                str(event.get("summary", ""))[:160])
    if kind == "guard.ask":
        return ("Предохранитель спрашивает: " + str(event.get("tool", "")) +
                " — " + str(event.get("reason", "")))
    if kind == "guard.verdict":
        return ("Предохранитель: " + str(event.get("decision", "")) +
                " (" + str(event.get("by", "")) + ") — " +
                str(event.get("reason", "")))
    if kind == "agent.spawn":
        return ("ребёнок создан: " + str(event.get("kind", "")) + " " +
                str(event.get("name", "")) + " (" + str(event.get("model", "")) + ")")
    if kind == "agent.state":
        return ("ребёнок " + str(event.get("name", "")) + ": " +
                str(event.get("state", "")))
    if kind == "agent.message":
        return ("почта: " + str(event.get("from", "")) + " → " +
                str(event.get("to", "")))
    if kind == "agent.done":
        return ("ребёнок " + str(event.get("name", "")) + " сдал: " +
                str(event.get("summary", ""))[:160])
    if kind == "swarm.start":
        return ("рой " + str(event.get("swarm", "")) + ": " +
                str(event.get("total", "")) + " ячеек, звеньев " +
                str(event.get("links", "")) + ", папка " + str(event.get("dir", "")))
    if kind == "swarm.progress":
        return ("рой: сдали " + str(event.get("done", "")) + "/" +
                str(event.get("total", "")) + ", упали " +
                str(event.get("failed", "")) + ", $" +
                str(event.get("spent_usd", "")))
    if kind == "swarm.cells":
        cells = event.get("cells") or []
        counts: dict = {}
        for cell in cells:
            state = cell[1] if len(cell) > 1 else "?"
            counts[state] = counts.get(state, 0) + 1
        return "рой: состояния ячеек — " + ", ".join(
            k + " " + str(v) for k, v in sorted(counts.items()))
    if kind == "swarm.pool":
        return ("рой: адрес пула выведен — " + str(event.get("url", "")) +
                ": " + str(event.get("reason", ""))[:160])
    if kind == "swarm.done":
        line = ("рой завершён: сдали " + str(event.get("done", "")) + "/" +
                str(event.get("total", "")) + ", упали " +
                str(event.get("failed", "")) + ", пропущено " +
                str(event.get("skipped", "")) + ", $" +
                str(event.get("spent_usd", "")) + ", " +
                str(event.get("seconds", "")) + " с")
        if event.get("stopped_reason"):
            line += " — " + str(event["stopped_reason"])
        return line
    if kind == "budget.update":
        return ("бюджет детей: $" + str(event.get("spent_usd", "")) +
                " из $" + str(event.get("limit_usd", "")))
    if kind == "usage":
        return ("расход: вход " + str(event.get("input", "")) + ", выход " +
                str(event.get("output", "")) + ", $" + str(event.get("cost", "")))
    if kind == "notice":
        return "заметка: " + str(event.get("text", ""))
    if kind == "error":
        return "ошибка: " + str(event.get("message", ""))
    if kind == "turn.done":
        return "ход завершён: " + str(event.get("reason", ""))
    return None




# Коды выхода любого luna -p: внешний агент различает их без разбора текста.
EXIT_OK = 0
EXIT_ERROR = 1          # модель или сеть
EXIT_USAGE = 2          # флаги, настройки, профиль работника
EXIT_LIMIT = 3          # шаги, деньги, время
EXIT_DENIED = 4         # отказ Предохранителя при fail_on_deny
EXIT_SCHEMA = 5         # ответ так и не прошёл схему
EXIT_MCP = 6            # обязательный MCP-сервер не поднялся
EXIT_INTERRUPTED = 130  # Ctrl+C

_STATUS_CODES = {"ok": EXIT_OK, "error": EXIT_ERROR, "usage": EXIT_USAGE,
                 "limit": EXIT_LIMIT, "denied": EXIT_DENIED, "schema": EXIT_SCHEMA,
                 "mcp": EXIT_MCP, "interrupted": EXIT_INTERRUPTED}


def _min_nonzero(a, b):
    a, b = a or 0, b or 0
    if a and b:
        return min(a, b)
    return a or b


def _check_output(text: str, schema: dict):
    from .core.jsonschema_lite import extract_json, validate

    obj, problem = extract_json(text)
    if problem:
        return None, [problem]
    return obj, list(validate(obj, schema) or [])


def _schema_retry_prompt(errors: list) -> str:
    return ("Ответ не прошёл проверку по JSON-схеме задания:\n" +
            "\n".join("- " + str(e) for e in errors[:20]) +
            "\nВерни заново только JSON по схеме — без пояснений и без ```.")


def _build_settings_secrets_router(settings, secrets, router, workdir: Path, caged: bool):
    from .core.paths import DataDir
    from .core.secrets import Secrets
    from .core.settings import Settings
    from .router.core import Router

    if settings is None:
        # Клетка не читает .luna/settings.json проекта: проект пишет
        # кто угодно, а клетку задаёт только хозяин.
        settings = (Settings(cwd=workdir, project_layer=False) if caged
                    else Settings(cwd=workdir))
    if secrets is None:
        home = getattr(settings, "home_dir", None)
        home_data = DataDir(Path(home)) if home is not None else DataDir()
        home_data.ensure()
        secrets = Secrets(home_data.secrets)
    return settings, secrets, router or Router(settings, secrets)


def run_headless(prompt: str = "", *, json_mode: bool = False, mode: str = "",
                 model_ref: str = "", settings=None, secrets=None, router=None,
                 agent=None, out=None, err=None, stdin=None,
                 output: str = "", continue_last: bool = False,
                 resume_id: str = "", max_usd: float = 0.0,
                 max_turns: int = 0, allow=None, data=None,
                 cwd=None, cage=None, check: bool = False,
                 timeout_s: float = 0.0, schema: dict | None = None,
                 fail_on_deny: bool = False) -> int:
    """Выполнить один ход без интерфейса. Возвращает код выхода процесса.

    output: "" / "text" — ответ в stdout, ход в stderr; "json" — один
    итоговый объект в stdout в самом конце; "stream-json" — события JSONL.
    json_mode — старый флаг --json, равен stream-json.

    cage — клетка работника (luna/worker/cage.py): проектные настройки не
    читаются, журнала нет (ephemeral), тулы и MCP — только разрешённые.
    check — только показать итоговую клетку, без вызова модели.
    """
    stream = out or sys.stdout
    err_stream = err or sys.stderr
    in_stream = stdin or sys.stdin
    output_mode = (output or ("stream-json" if json_mode else "text")).strip().lower()
    result = {"status": "ok", "result": "", "structured": None, "cost_usd": 0.0,
              "steps": 0, "tools_used": [], "denials": [], "error": None,
              "worker": (cage.name or None) if cage is not None else None}

    def finish(status: str, message: str = "") -> int:
        result["status"] = "error" if status == "usage" else status
        if message:
            result["error"] = message
            err_stream.write(message + "\n")
            err_stream.flush()
        if output_mode == "json":
            stream.write(json.dumps(result, ensure_ascii=False, default=str) + "\n")
            stream.flush()
        return _STATUS_CODES[status]

    if output_mode not in ("text", "json", "stream-json"):
        bad_mode, output_mode = output_mode, "text"
        return finish("usage", "Неизвестный --output: " + bad_mode +
                      " (text|json|stream-json)")
    if check and cage is None:
        return finish("usage", "--check показывает клетку работника: нужен --worker, "
                               "--worker-file или флаги клетки (--tools, --mcp, --fs …).")
    if cage is not None and (continue_last or resume_id):
        return finish("usage", "Работник в клетке эфемерен: --continue и --resume с ним "
                               "не работают. Каждый вызов — с чистого листа.")
    if not check:
        if not (prompt or "").strip():
            try:
                prompt = in_stream.read() or ""
            except Exception:  # noqa: BLE001 — нет stdin: ниже честная ошибка
                prompt = ""
        prompt = (prompt or "").strip()
        if not prompt:
            return finish("usage", "Нужен промпт: аргументом luna -p \"задача\" или в stdin.")

    workdir = Path(cwd) if cwd is not None else Path.cwd()
    retries = 1
    if cage is not None:
        from .worker.profile import MODE_ORDER

        max_usd = _min_nonzero(max_usd, cage.max_usd)
        max_turns = _min_nonzero(max_turns, cage.max_steps)
        timeout_s = _min_nonzero(timeout_s, cage.timeout_s)
        schema = cage.output_schema if cage.output_schema is not None else schema
        fail_on_deny = fail_on_deny or cage.fail_on_deny
        retries = int(cage.schema_retries)
        if mode in MODE_ORDER and cage.mode in MODE_ORDER:
            # Режим запуска не мягче режима клетки.
            mode = mode if MODE_ORDER.index(mode) < MODE_ORDER.index(cage.mode) else cage.mode
        else:
            mode = cage.mode
        model_ref = model_ref or cage.model

    if agent is None and (router is None or settings is None):
        try:
            settings, secrets, router = _build_settings_secrets_router(
                settings, secrets, router, workdir, cage is not None)
        except Exception as exc:  # noqa: BLE001 — кривые настройки — ошибка запуска, не трасса
            return finish("usage", "Не поднялись настройки: " + str(getattr(exc, "title", exc)))

    if cage is not None:
        # В клетке разрешает только хозяин: allow профиля и --allow вызова;
        # глобальная headless.allow рассчитана на полную Луну, не на работника.
        patterns = [str(p).strip() for p in list(cage.allow) + list(allow or []) if str(p).strip()]
    else:
        patterns = _allow_patterns(settings, allow)

    # --- сессия: журнал, как у чата, чтобы --continue/--resume жили ------
    journal = None
    history_state = None
    ephemeral = cage is not None and (cage.ephemeral or check)
    data_dir = None if ephemeral else _data_dir(settings, data)
    if data_dir is not None:
        try:
            data_dir.ensure()
        except Exception:  # noqa: BLE001 — журнал не должен ронять headless
            data_dir = None
    if data_dir is not None:
        from .core.sessions import (SessionJournal, build_state, find_by_prefix,
                                     find_last)

        journal_path = None
        if resume_id:
            found = find_by_prefix(data_dir, resume_id)
            if found is None:
                return finish("usage", "Сессии с id «" + str(resume_id) +
                              "» не нашёл в " + str(data_dir.sessions) + ".")
            journal_path = found["path"]
        elif continue_last:
            journal_path = find_last(data_dir, str(workdir))
        if journal_path is not None:
            journal, records = SessionJournal.resume(data_dir, journal_path)
            history_state = build_state(records)
        else:
            journal = SessionJournal.create(data_dir, str(workdir))

    # --- вопросы Предохранителя без человека ------------------------------
    def ask_guard(event: dict):
        tool_name = str(event.get("tool", ""))
        args = event.get("args") or {}
        guard = getattr(agent, "guard", None)
        if guard is not None:
            hard = guard.hard_block(tool_name, args)
            if hard is not None and hard.decision == "ask":
                return {"decision": "no",
                        "reason": "headless: жёсткий блок Предохранителя "
                                  "(hard_block) не разрешается ничем: " +
                                  hard.reason}
        for pattern in patterns:
            if _pattern_matches(pattern, tool_name, args):
                return {"decision": "yes",
                        "reason": "headless: разрешено явно шаблоном «" + pattern + "» (" +
                                  ("allow клетки или --allow" if cage is not None
                                   else "--allow или headless.allow") + ")"}
        return {"decision": "no",
                "reason": "headless: человека нет, подтверждение невозможно; "
                          "разрешить можно явно: --allow <шаблон>" +
                          (" или allow в профиле работника" if cage is not None
                           else " или настройка headless.allow") +
                          ". Причина вопроса: " + str(event.get("reason", ""))}

    state = {"cost": 0.0, "cost_exceeded": False, "wrote_text": False,
             "text_open": False,  # ответ в stdout не закрыт переводом строки
             "timeout": False, "denied": False, "step_text": [], "last_text": "",
             "names": {}, "last_error": ""}
    # В клетке и в --output json ответ печатаем целиком в конце: внешнему
    # агенту нужен ровно итог, без «сейчас поищу…» промежуточных шагов.
    buffer_answer = cage is not None or output_mode == "json"
    created = agent is None
    timer = None
    try:
        try:
            if agent is None:
                from .mind.loop import Agent

                extra = {"cage": cage} if cage is not None else {}
                agent = Agent(settings=settings, secrets=secrets, router=router,
                              cwd=workdir, mode=mode, model_ref=model_ref,
                              session_id=(journal.session_id if journal else ""),
                              ask_guard_fn=ask_guard, **extra)
            else:
                if mode:
                    agent.set_mode(mode)
                agent.ask_guard_fn = ask_guard
        except Exception as exc:  # noqa: BLE001 — клетка/MCP/настройки: ошибка запуска
            return finish("usage", "Агент не собрался: " + str(getattr(exc, "title", exc)) +
                          ((". " + exc.hint) if getattr(exc, "hint", "") else ""))
        if max_turns and int(max_turns) > 0:
            agent.max_steps_override = int(max_turns)

        def on_timeout() -> None:
            state["timeout"] = True
            err_stream.write("Потолок времени " + format(float(timeout_s), "g") +
                             " с исчерпан — ход прерван.\n")
            err_stream.flush()
            agent.interrupt()

        if timeout_s and float(timeout_s) > 0:
            timer = threading.Timer(float(timeout_s), on_timeout)
            timer.daemon = True
            timer.start()

        if cage is not None:
            # MCP поднимаем до хода: упавший обязательный сервер — код 6
            # сразу, а не модель, которая ищет несуществующий тул.
            visible = list(agent.tools.keys())
            failed = list(getattr(agent.tools, "mcp_required_failed", []) or [])
            mcp_errors = [str(e) for e in getattr(agent.tools, "mcp_errors", []) or []]
            if check:
                report = cage.describe(visible_tools=visible)
                report["mcp_errors"] = mcp_errors
                report["mcp_required_failed"] = failed
                stream.write(json.dumps(report, ensure_ascii=False, indent=1, default=str) + "\n")
                stream.flush()
                return EXIT_OK
            if failed:
                return finish("mcp", "Обязательный MCP-сервер не поднялся: " + ", ".join(failed) +
                              (" — " + "; ".join(mcp_errors)[:400] if mcp_errors else ""))
            if state["timeout"]:
                return finish("limit", "Время вышло ещё при подъёме MCP-серверов.")

        if journal is not None:
            agent.journal = journal
            if history_state is not None:
                agent.load_history(history_state["history"],
                                   turn=history_state["turn"],
                                   marks=history_state["marks"])
            agent.listen(journal.append_event)
            journal.append_command({"type": "user.message", "text": prompt})

        def on_event(event: dict) -> None:
            kind = event.get("type")
            if kind == "usage":
                cost = float(event.get("cost") or 0.0)
                if cost > state["cost"]:
                    state["cost"] = cost
                if max_usd and state["cost"] >= float(max_usd) and \
                        not state["cost_exceeded"]:
                    state["cost_exceeded"] = True
                    err_stream.write("Потолок --max-usd " + format(float(max_usd), ".4f") +
                                     " исчерпан (потрачено $" +
                                     format(state["cost"], ".4f") + ") — ход прерван.\n")
                    err_stream.flush()
                    agent.interrupt()
            elif kind == "agent.text":
                state["step_text"].append(str(event.get("text", "")))
            elif kind == "agent.text.done":
                result["steps"] += 1
                state["last_text"] = "".join(state["step_text"])
                state["step_text"] = []
            elif kind == "tool.call":
                name = str(event.get("name", ""))
                state["names"][event.get("id")] = name
                if name and name not in result["tools_used"]:
                    result["tools_used"].append(name)
            elif kind == "guard.verdict" and event.get("decision") not in ("allow", None):
                result["denials"].append({
                    "tool": state["names"].get(event.get("id"), ""),
                    "reason": str(event.get("reason", ""))})
                if fail_on_deny and not state["denied"]:
                    state["denied"] = True
                    err_stream.write("Отказ Предохранителя, а у работника fail_on_deny — "
                                     "ход прерван.\n")
                    err_stream.flush()
                    agent.interrupt()
            elif kind == "error":
                state["last_error"] = str(event.get("message", ""))
            if output_mode == "stream-json":
                stream.write(json.dumps(event, ensure_ascii=False) + "\n")
                stream.flush()
                return
            if kind == "agent.text":
                if buffer_answer:
                    return
                chunk = event.get("text", "")
                stream.write(chunk)
                stream.flush()
                if chunk:
                    state["wrote_text"] = True
                    state["text_open"] = not chunk.endswith("\n")
                return
            line = _progress_line(event)
            if line is not None:
                if state["text_open"]:
                    # Ответ обязан кончаться переводом строки ДО первой строки
                    # хода в stderr — иначе строка расхода «прилипает» к ответу
                    # в общем терминале (живой прогон тимлида, ЗАДАЧА-22).
                    stream.write("\n")
                    stream.flush()
                    state["text_open"] = False
                err_stream.write(line + "\n")
                err_stream.flush()

        agent.listen(on_event)

        def outcome(reason: str):
            """Причина хода → (статус, сообщение) или None, если ход удался."""
            if state["denied"]:
                return "denied", "Предохранитель отказал: " + (
                    result["denials"][0]["reason"] if result["denials"] else "")
            if state["timeout"]:
                return "limit", "Потолок времени исчерпан."
            if state["cost_exceeded"]:
                return "limit", "Потолок денег исчерпан."
            if reason == "limit":
                return "limit", "Потолок шагов исчерпан."
            if reason == "interrupted":
                return "interrupted", "Ход прерван."
            if reason == "error":
                return "error", "Ошибка хода: " + (state["last_error"] or "см. stderr")
            return None

        try:
            reason = agent.run_turn(prompt)
            bad = outcome(reason)
            answer = state["last_text"]
            if bad is None and schema is not None:
                obj, problems = _check_output(answer, schema)
                attempt = 0
                while problems and attempt < retries:
                    attempt += 1
                    err_stream.write("Ответ не прошёл схему (" + "; ".join(problems)[:300] +
                                     ") — прошу исправить, попытка " + str(attempt) + ".\n")
                    err_stream.flush()
                    reason = agent.run_turn(_schema_retry_prompt(problems))
                    bad = outcome(reason)
                    if bad is not None:
                        break
                    answer = state["last_text"]
                    obj, problems = _check_output(answer, schema)
                if bad is None and problems:
                    bad = ("schema", "Ответ так и не прошёл схему: " + "; ".join(problems)[:500])
                if bad is None:
                    result["structured"] = obj
        finally:
            if journal is not None:
                journal.close()
        result["result"] = answer
        result["cost_usd"] = round(state["cost"], 6)
        if output_mode == "text":
            if buffer_answer:
                if result["structured"] is not None:
                    stream.write(json.dumps(result["structured"], ensure_ascii=False) + "\n")
                elif answer and bad is None:
                    stream.write(answer + ("" if answer.endswith("\n") else "\n"))
                stream.flush()
            elif state["text_open"]:
                stream.write("\n")
                stream.flush()
        if bad is not None:
            return finish(*bad)
        return finish("ok")
    except KeyboardInterrupt:
        try:
            if agent is not None:
                agent.interrupt()
        except Exception:  # noqa: BLE001 — уходим в любом случае
            pass
        result["cost_usd"] = round(state["cost"], 6)
        return finish("interrupted", "Прервано (Ctrl+C).")
    finally:
        if timer is not None:
            timer.cancel()
        if agent is not None and (created or cage is not None):
            cleanup = getattr(agent, "cleanup", None)
            if callable(cleanup):
                try:
                    cleanup()
                except Exception:  # noqa: BLE001 — уборка не меняет код выхода
                    pass
