"""Headless-клиент: luna -p "задача" (ЗАДАЧА-04, расширено ЗАДАЧЕЙ-21).

Один ход без интерфейса:
- ответ агента — в stdout; ход (тулы, спутники, рой, Предохранитель) —
  в stderr, кратко, по строке на событие;
- --output json / stream-json (и старый --json) — события шины
  docs/СОБЫТИЯ.md построчно JSON в stdout;
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


def run_headless(prompt: str = "", *, json_mode: bool = False, mode: str = "",
                 model_ref: str = "", settings=None, secrets=None, router=None,
                 agent=None, out=None, err=None, stdin=None,
                 output: str = "", continue_last: bool = False,
                 resume_id: str = "", max_usd: float = 0.0,
                 max_turns: int = 0, allow=None, data=None,
                 cwd=None) -> int:
    """Выполнить один ход без интерфейса. Возвращает код выхода процесса.

    output: "" / "text" — ответ в stdout, ход в stderr; "json" и
    "stream-json" — события JSONL в stdout. json_mode — старый флаг
    --json, равен stream-json.
    """
    stream = out or sys.stdout
    err_stream = err or sys.stderr
    in_stream = stdin or sys.stdin
    output_mode = (output or ("stream-json" if json_mode else "text")).strip().lower()
    if output_mode == "json":
        output_mode = "stream-json"
    if output_mode not in ("text", "stream-json"):
        err_stream.write("Неизвестный --output: " + output_mode +
                         " (text|json|stream-json)\n")
        return 2
    if not (prompt or "").strip():
        try:
            prompt = in_stream.read() or ""
        except Exception:  # noqa: BLE001 — нет stdin: ниже честная ошибка
            prompt = ""
    prompt = (prompt or "").strip()
    if not prompt:
        err_stream.write("Нужен промпт: аргументом luna -p \"задача\" или в stdin.\n")
        return 2

    if agent is None:
        if router is None or settings is None:
            def_settings, def_secrets, def_router = _default_settings_secrets_router()
            settings = settings or def_settings
            secrets = secrets if secrets is not None else def_secrets
            router = router or def_router
    patterns = _allow_patterns(settings, allow)
    workdir = Path(cwd) if cwd is not None else Path.cwd()

    # --- сессия: журнал, как у чата, чтобы --continue/--resume жили ------
    journal = None
    history_state = None
    data_dir = _data_dir(settings, data)
    try:
        data_dir.ensure()
    except Exception:  # noqa: BLE001 — журнал не должен ронять headless
        data_dir = None
    if data_dir is not None:
        from .core.sessions import (SessionJournal, build_state, find_by_prefix,
                                     find_last, load_records)

        journal_path = None
        if resume_id:
            found = find_by_prefix(data_dir, resume_id)
            if found is None:
                err_stream.write("Сессии с id «" + str(resume_id) +
                                 "» не нашёл в " + str(data_dir.sessions) + ".\n")
                return 2
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
                        "reason": "headless: разрешено явно шаблоном «" +
                                  pattern + "» (--allow или headless.allow)"}
        return {"decision": "no",
                "reason": "headless: человека нет, подтверждение невозможно; "
                          "разрешить можно явно: --allow <шаблон> или "
                          "настройка headless.allow. Причина вопроса: " +
                          str(event.get("reason", ""))}

    if agent is None:
        from .mind.loop import Agent

        agent = Agent(settings=settings, secrets=secrets, router=router,
                      cwd=workdir, mode=mode, model_ref=model_ref,
                      session_id=(journal.session_id if journal else ""),
                      ask_guard_fn=ask_guard)
    else:
        if mode:
            agent.set_mode(mode)
        agent.ask_guard_fn = ask_guard
    if max_turns and int(max_turns) > 0:
        agent.max_steps_override = int(max_turns)
    if journal is not None:
        agent.journal = journal
        if history_state is not None:
            agent.load_history(history_state["history"],
                               turn=history_state["turn"],
                               marks=history_state["marks"])
        agent.listen(journal.append_event)
        journal.append_command({"type": "user.message", "text": prompt})

    state = {"cost": 0.0, "cost_exceeded": False, "wrote_text": False}

    def on_event(event: dict) -> None:
        if event.get("type") == "usage":
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
        if output_mode == "stream-json":
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
            stream.flush()
            return
        kind = event.get("type")
        if kind == "agent.text":
            stream.write(event.get("text", ""))
            stream.flush()
            state["wrote_text"] = True
            return
        line = _progress_line(event)
        if line is not None:
            err_stream.write(line + "\n")
            err_stream.flush()

    agent.listen(on_event)
    try:
        reason = agent.run_turn(prompt)
    finally:
        if journal is not None:
            journal.close()
    if output_mode == "text" and state["wrote_text"]:
        stream.write("\n")
        stream.flush()
    if state["cost_exceeded"]:
        return 2
    if max_turns and reason == "limit":
        return 2
    return 0 if reason in ("end", "limit", "interrupted") else 2
