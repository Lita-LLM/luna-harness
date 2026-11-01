# Перенесено из BENDER: bender/mind/loop.py (коммит 6552ed660bfabf5202569f9905212b77dbf6d31a), сокращено до синхронного ядра вехи 1.
"""Цикл агента: промпт → модель → Предохранитель → тулы → результат.

Наружу — только события из docs/СОБЫТИЯ.md, через шину и слушателей.
Секреты вычищаются из каждого события до публикации. Ход можно прервать
(interrupt), сообщения во время хода встают в очередь и уходят следующим
шагом, одинаковые вызовы подряд три раза — стоп (защита от зацикливания).
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

from ..core.secrets import redact, redact_obj
from ..guard.policy import Guard, denied_text
from ..tools.registry import WRITE_TOOLS, ToolContext, build_registry, truncate_output
from .prompt import build_system_prompt

LOOP_LIMIT = 3  # одинаковых вызовов подряд — и стоп


class Agent:
    def __init__(self, *, settings, secrets=None, router, cwd,
                 bus=None, session_id: str = "", mode: str = "",
                 model_ref: str = "", guard: Guard | None = None,
                 ask_guard_fn=None, ask_user_fn=None, tools: dict | None = None,
                 system_suffix: str = ""):
        self.settings = settings
        self.secrets = secrets
        self.router = router
        self.cwd = Path(cwd)
        self.bus = bus
        self.session_id = session_id or uuid.uuid4().hex[:12]
        self.mode = mode or (settings.get("agent.mode", "normal") if settings else "normal")
        self.model_ref = model_ref
        self.tools = tools if tools is not None else build_registry()
        self.guard = guard if guard is not None else Guard(settings, secrets, guard_judge=self._make_judge())
        self.ask_guard_fn = ask_guard_fn  # (event) -> {decision, args?} | None
        self.ctx = ToolContext(cwd=self.cwd, settings=settings, secrets=secrets,
                               ask_user_fn=self._ask_user,
                               output_dir=self._output_dir(), agent=self)
        self._system_suffix = system_suffix  # роль ребёнка (ЗАДАЧА-11): хвост промпта
        self._ask_user_fn = ask_user_fn  # (id, question, options) -> str | None
        self._listeners: list = []
        self._active = False
        self._queue: list[dict] = []
        self._interrupt = threading.Event()
        self._turn = 0
        self._started = False
        self._prompt_sig: tuple | None = None
        self._history: list[dict] = []
        self._total_in = 0
        self._total_out = 0
        self._last_sig = ""
        self._same_count = 0
        # Откат и сжатие (ЗАДАЧА-13): отметки ходов в истории — пары
        # (номер хода, индекс его сообщения user); журнал сессии может
        # поставить клиент (agent.journal = SessionJournal), тогда откат
        # и сжатие пишут в него записи своих видов (kind: rewind/compact).
        self._context_pct = 0.0
        self._turn_marks: list = []
        self._redo_stack: list = []
        self._compact_pending: str | None = None
        self.journal = None
        self._checkpoints = self._make_checkpoints()
        self.ctx.checkpoint = self._checkpoints

    def _make_checkpoints(self):
        """Стор чекпоинтов сессии; None — выключен настройкой или сломался."""
        if self.settings is not None and not self.settings.get("checkpoints.enabled", True):
            return None
        try:
            from ..core.checkpoints import CheckpointStore

            return CheckpointStore(self.session_id, cwd=self.cwd, settings=self.settings)
        except Exception:  # noqa: BLE001 — чекпоинты не должны мешать агенту жить
            return None

    # --- события ---

    def listen(self, fn) -> None:
        self._listeners.append(fn)

    def publish(self, event: dict) -> None:
        event = dict(event)
        event.setdefault("session", self.session_id)
        event.setdefault("ts", time.time())
        event = redact_obj(event, self.secrets)
        if self.bus is not None:
            self.bus.publish(event)
        for fn in list(self._listeners):
            try:
                fn(event)
            except Exception:  # noqa: BLE001 — слушатель не роняет агента
                pass

    # --- команды клиента (docs/СОБЫТИЯ.md) ---

    def handle_command(self, command: dict):
        kind = command.get("type")
        if kind == "user.message":
            text = command.get("text", "")
            # Почта команды (ЗАДАЧА-11): письма и отчёты детей — пометками
            # в начало хода хозяина, как замечания Ниблера.
            host = getattr(self, "_agents_host", None)
            if host is not None:
                notes = host.take_owner_notes()
                if notes:
                    text = notes + ("\n\n" + text if text else "")
            if self._turn_active:
                self._queue.append({"text": text, "attachments": command.get("attachments") or []})
                return None
            return self.run_turn(text, command.get("attachments") or [])
        if kind == "interrupt":
            self.interrupt()
            return None
        if kind == "mode.set":
            self.set_mode(command.get("mode", "normal"))
            return None
        if kind == "shell":
            return self.run_shell(command.get("command", ""))
        if kind == "undo":
            return self.undo()
        if kind == "rewind":
            return self.rewind_command(command.get("turn"), command.get("scope", "both"))
        if kind == "redo":
            return self.redo()
        if kind == "compact":
            return self.compact_now(str(command.get("hint", "") or ""))
        return None

    def set_mode(self, mode: str) -> None:
        if mode in ("normal", "plan", "review", "auto", "yolo"):
            self.mode = mode
            self.publish({"type": "mode.changed", "mode": mode})

    def interrupt(self) -> None:
        self._interrupt.set()
        # Esc хозяина гасит и всех его детей (ЗАДАЧА-11), а те — своих.
        host = getattr(self, "_agents_host", None)
        if host is not None:
            host.interrupt_all()
        # И текущий поток роя (ЗАДАЧА-15): Esc останавливает весь Звездопад.
        run = getattr(self, "_starfall_run", None)
        if run is not None:
            try:
                run.stop()
            except Exception:  # noqa: BLE001 — прерывание не падает
                pass

    @property
    def _turn_active(self) -> bool:
        return getattr(self, "_active", False)

    def run_shell(self, command: str):
        """!команда мимо модели, но через Предохранитель, как любой bash."""
        tool = self.tools.get("bash")
        if tool is None:
            return None
        if self._checkpoints is not None:
            self._checkpoints.begin_turn(self._turn)
        return self._execute_tool(tool, {"command": command}, "команда человека мимо модели",
                                  call_id="shell-" + uuid.uuid4().hex[:6])

    # --- охранник режима review ---

    def _make_judge(self):
        settings = self.settings
        if settings is None or not settings.get("model.guard", ""):
            return None
        router = self.router

        def judge(tool_name: str, args: dict, intent: str):
            prompt = (
                "Ты — модель-охранник. Тебе показывают только вызов тула, без диалога. "
                "Ответь одной строкой: ALLOW или DENY и короткую причину.\n"
                "Тул: " + tool_name + "\nАргументы: " +
                json.dumps(args, ensure_ascii=False, default=str)[:2000] +
                "\nНамерение агента: " + (intent or ""))
            try:
                result = router.chat([{"role": "user", "content": prompt}], None,
                                     role="guard", on_event=lambda kind, text: None)
            except Exception as exc:  # noqa: BLE001 — охранник упал: честнее спросить человека
                return False, "Охранник не ответил: " + str(exc)
            text = (result.text or "").strip()
            upper = text.upper()
            if upper.startswith("ALLOW"):
                return True, text
            if upper.startswith("DENY"):
                return False, text
            return False, "Охранник не дал однозначного ответа: " + text[:200]

        return judge

    # --- вопросы человеку ---

    def _ask_user(self, question: str, options) -> str | None:
        qid = "ask-" + uuid.uuid4().hex[:8]
        self.publish({"type": "ask.user", "id": qid, "question": question,
                      "options": list(options or [])})
        if self._ask_user_fn is None:
            return None
        return self._ask_user_fn(qid, question, list(options or []))

    # --- ход ---

    def _output_dir(self) -> Path:
        from ..core.paths import luna_home

        return luna_home() / "local" / "outputs"

    def _specs(self):
        specs = []
        for tool in self.tools.values():
            if self.mode == "plan" and tool.name in WRITE_TOOLS:
                continue  # в plan тулы записи модели даже не выдаются (ta)
            specs.append(tool.spec())
        return specs

    def _model_label(self) -> str:
        if self.model_ref:
            return self.model_ref
        if self.settings is not None:
            return self.settings.get("model.main", "") or ""
        return ""

    def _emit(self, kind: str, text: str) -> None:
        if kind == "thinking":
            self.publish({"type": "agent.thinking", "text": text})
        else:
            self.publish({"type": "agent.text", "text": text})

    def _usage_event(self, result) -> None:
        self._total_in += result.usage.input
        self._total_out += result.usage.output
        context_tokens = 128000
        price_in = 0.0
        price_out = 0.0
        if self.settings is not None:
            context_tokens = int(self.settings.get("model.context_tokens", 128000) or 128000)
            price_in = float(self.settings.get("model.price_in", 0.0) or 0.0)
            price_out = float(self.settings.get("model.price_out", 0.0) or 0.0)
        cost = (self._total_in * price_in + self._total_out * price_out) / 1_000_000.0
        pct = min(100.0, 100.0 * result.usage.input / context_tokens) if context_tokens else 0.0
        self._context_pct = pct
        self.publish({"type": "usage", "model": result.model or self._model_label(),
                      "input": result.usage.input, "output": result.usage.output,
                      "cost": round(cost, 6), "context_pct": round(pct, 2)})

    # --- история и системный промпт ---

    def _state_snapshot(self) -> dict:
        """История без системного промпта, номер хода и отметки ходов."""
        offset = 1 if self._history and self._history[0].get("role") == "system" else 0
        return {"history": [dict(m) for m in self._history[offset:]],
                "turn": self._turn,
                "marks": [[t, i - offset] for t, i in self._turn_marks if i >= offset]}

    def load_history(self, messages, *, turn=None, marks=None) -> None:
        """Принять историю прошлой сессии (продолжение, luna --continue).

        Сообщения — как их собрал core.sessions.build_history: без
        системного промпта, его _ensure_system_prompt вставит первым.
        Счётчик ходов продолжается с числа реплик пользователя.
        """
        self._history = [dict(m) for m in messages]
        if marks is None:
            marks, n = [], 0
            for i, m in enumerate(self._history):
                if m.get("role") == "user":
                    n += 1
                    marks.append((n, i))
        self._turn_marks = [(int(t), int(i)) for t, i in marks]
        self._turn = int(turn) if turn is not None else len(self._turn_marks)
        self._queue = []
        self._last_sig = ""
        self._same_count = 0
        self._prompt_sig = None  # промпт вставится в новую историю заново

    def _current_prompt_sig(self) -> tuple:
        """От чего зависит промпт вне истории: режим, манера и её текст.

        Хэш текста — чтобы правка файла активной манеры (тулом или в
        редакторе из чата) подхватывалась со следующего хода, а не только
        переключение на другую манеру.
        """
        manner = ""
        digest = ""
        if self.settings is not None:
            manner = str(self.settings.get("agent.manner", "") or "")
            if manner:
                try:
                    from .manners import active

                    entry = active(self.settings, self.cwd)
                    if entry is not None:
                        digest = str(hash(entry["text"]))
                except Exception:  # noqa: BLE001 — подпись не должна ронять ход
                    digest = ""
        return (self.mode, manner, digest)

    def _reload_settings(self) -> None:
        """Настройки поменяли руками или другой процесс — подхватить перед ходом."""
        reloader = getattr(self.settings, "reload_if_changed", None)
        if not callable(reloader):
            return
        try:
            changes = reloader()
        except Exception as exc:  # noqa: BLE001 — битый файл: прошлые значения остаются
            title = getattr(exc, "title", "") or str(exc)
            self.publish({"type": "notice", "level": "warn",
                          "text": title + " — работаю на прошлых значениях."})
            return
        if changes:
            # манера, инструкции — пересобрать промпт; agent.mode — режим по
            # умолчанию для новых сессий, текущий режим (Shift+Tab) не трогаем
            self._prompt_sig = None

    def _ensure_system_prompt(self) -> None:
        """Собрать промпт к первому ходу; дальше — пересобрать, если сменились
        режим или манера (тул manner и mode.set действуют со следующего хода)."""
        sig = self._current_prompt_sig()
        if self._started and sig == self._prompt_sig:
            return
        content = build_system_prompt(self.cwd, self.mode, settings=self.settings)
        if self._system_suffix:
            content = content + "\n\n" + self._system_suffix
        if self._history and self._history[0].get("role") == "system":
            self._history[0] = {"role": "system", "content": content}
        else:
            self._history.insert(0, {"role": "system", "content": content})
            # отметки ходов — индексы в истории, промпт сдвинул их на один
            self._turn_marks = [(t, i + 1) for t, i in self._turn_marks]
        self._prompt_sig = sig

    def run_turn(self, text: str, attachments=None) -> str:
        """Один ход до финала. Возвращает причину turn.done."""
        self._reload_settings()
        self._active = True
        self._interrupt.clear()
        self._turn += 1
        turn = self._turn
        self._redo_stack.clear()  # новый ход хоронит отмену отката
        if self._checkpoints is not None:
            self._checkpoints.begin_turn(turn)
        if not self._started:
            try:
                self._ensure_system_prompt()
            except Exception as exc:  # noqa: BLE001 — например, манера из настройки не найдена
                from ..core.errors import HumanError, unexpected

                err = exc if isinstance(exc, HumanError) else unexpected(exc, "сборка системного промпта")
                self.publish({"type": "error", "message": err.title,
                              "detail": err.detail or err.hint})
                self.publish({"type": "turn.done", "turn": turn, "reason": "error"})
                self._active = False
                return "error"
            self._started = True
            self.publish({"type": "session.start", "cwd": str(self.cwd),
                          "model": self._model_label(), "mode": self.mode})
        else:
            try:
                self._ensure_system_prompt()
            except Exception:  # noqa: BLE001 — пересборка не должна ронять ход: едем на старом промпте
                pass
        self.publish({"type": "turn.start", "turn": turn})
        user_msg: dict = {"role": "user", "content": text}
        if attachments:
            parts = [{"type": "text", "text": text}]
            for item in attachments:
                if isinstance(item, dict) and item.get("data"):
                    parts.append({"type": "image", "data": item["data"],
                                  "media_type": item.get("media_type", "image/png")})
            user_msg = {"role": "user", "content": parts}
        self._turn_marks.append((turn, len(self._history)))
        self._history.append(user_msg)
        max_steps = 30
        if self.settings is not None:
            max_steps = int(self.settings.get("agent.max_steps", 30) or 30)
        reason = "end"
        try:
            reason = self._steps(max_steps)
        except Exception as exc:  # noqa: BLE001 — ошибка хода — событие, не падение
            from ..core.errors import HumanError, unexpected

            err = exc if isinstance(exc, HumanError) else unexpected(exc, "ход агента")
            self.publish({"type": "error", "message": err.title, "detail": err.detail or err.hint})
            reason = "error"
        self.publish({"type": "turn.done", "turn": turn, "reason": reason})
        self._active = False
        self._after_turn()
        return reason

    def _steps(self, max_steps: int) -> str:
        for _step in range(max_steps):
            if self._interrupt.is_set():
                return "interrupted"
            result = self.router.chat(self._history, self._specs(),
                                      ref=self.model_ref, on_event=self._emit)
            self._usage_event(result)
            assistant: dict = {"role": "assistant", "content": result.text}
            if result.tool_calls:
                assistant["tool_calls"] = [
                    {"id": c.id, "name": c.name, "arguments": c.arguments, "intent": c.intent}
                    for c in result.tool_calls]
            self._history.append(assistant)
            self.publish({"type": "agent.text.done"})
            if not result.tool_calls:
                if self._queue:
                    nxt = self._queue.pop(0)
                    self._history.append({"role": "user", "content": nxt["text"]})
                    self.publish({"type": "notice", "level": "info",
                                  "text": "Сообщение из очереди ушло следующим шагом"})
                    continue
                return "end"
            stop = self._run_calls(result.tool_calls)
            if stop:
                return stop
            if self._interrupt.is_set():
                return "interrupted"
            if self._queue:
                nxt = self._queue.pop(0)
                self._history.append({"role": "user", "content": nxt["text"]})
                self.publish({"type": "notice", "level": "info",
                              "text": "Сообщение из очереди ушло следующим шагом"})
        self.publish({"type": "notice", "level": "warn",
                      "text": "Лимит шагов исчерпан: " + str(max_steps)})
        return "limit"

    def _run_calls(self, calls) -> str | None:
        """Выполнить вызовы шага; вернуть причину остановки хода или None."""
        for call in calls:
            sig = call.name + ":" + json.dumps(call.arguments, ensure_ascii=False,
                                               sort_keys=True, default=str)
            if sig == self._last_sig:
                self._same_count += 1
            else:
                self._last_sig = sig
                self._same_count = 1
            if self._same_count >= LOOP_LIMIT:
                note = ("Защита от зацикливания: вызов " + call.name +
                        " с теми же аргументами повторяется " + str(self._same_count) +
                        " раз подряд. Ход остановлен; объясни человеку, что ты делаешь не так.")
                self._history.append({"role": "tool", "tool_call_id": call.id,
                                      "name": call.name, "content": note})
                self.publish({"type": "notice", "level": "warn", "text": note})
                return "limit"
            tool = self.tools.get(call.name)
            if tool is None:
                self._history.append({"role": "tool", "tool_call_id": call.id, "name": call.name,
                                      "content": "Нет такого тула: " + call.name})
                continue
            outcome = self._execute_tool(tool, call.arguments, call.intent, call.id)
            if outcome == "stop":
                return "interrupted"
            if self._interrupt.is_set():
                return "interrupted"
        return None

    def _execute_tool(self, tool, args: dict, intent: str, call_id: str) -> str | None:
        """Предохранитель + исполнение одного вызова; события tool.* и guard.*."""
        self.publish({"type": "tool.call", "id": call_id, "name": tool.name,
                      "args": args, "intent": intent})
        decision = self.guard.decide(tool.name, tool.is_mutating(args), args, intent, self.mode)
        final_args = args
        if decision.decision == "ask":
            answer = self._ask_human(call_id, tool.name, args, intent, decision)
            verdict, by, reason, final_args = answer
            self.publish({"type": "guard.verdict", "id": call_id, "decision": verdict,
                          "by": by, "reason": reason})
            if verdict != "allow":
                self._deny(call_id, tool.name, reason)
                return "stop" if by == "human-stop" else None
        else:
            self.publish({"type": "guard.verdict", "id": call_id, "decision": decision.decision,
                          "by": decision.by, "reason": decision.reason})
            if decision.decision == "deny":
                self._deny(call_id, tool.name, decision.reason)
                return None
        if tool.name == "bash" and self._checkpoints is not None:
            self._checkpoints.note_bash(self.cwd)  # git-пометка хода до команды
        result = tool.run(self.ctx, final_args)
        if result.ok and tool.name in ("write", "edit") and self._checkpoints is not None:
            raw = final_args.get("path", "")
            path = Path(raw) if raw else None
            if path is not None:
                self._checkpoints.note_after(path if path.is_absolute() else self.cwd / path)
        limit = 8000
        if self.settings is not None:
            limit = int(self.settings.get("tools.output_limit", 8000) or 8000)
        output, _path = truncate_output(result.text, limit, self.ctx.output_dir, tool.name)
        output = redact(output, self.secrets)
        summary = output.splitlines()[0][:200] if output else ""
        self.publish({"type": "tool.result", "id": call_id, "ok": result.ok,
                      "summary": summary, "output": output})
        if result.data and result.data.get("todo_items") is not None:
            self.publish({"type": "todo", "items": result.data["todo_items"]})
        self._history.append({"role": "tool", "tool_call_id": call_id,
                              "name": tool.name, "content": output})
        return None

    def _deny(self, call_id: str, tool_name: str, reason: str) -> None:
        text = denied_text(reason)
        self.publish({"type": "tool.result", "id": call_id, "ok": False,
                      "summary": text, "output": text})
        self._history.append({"role": "tool", "tool_call_id": call_id,
                              "name": tool_name, "content": text})

    def _ask_human(self, call_id: str, tool_name: str, args: dict, intent: str,
                   decision) -> tuple[str, str, str, dict]:
        """guard.ask → ответ клиента. Нет канала — отказ с понятной причиной."""
        choices = ["yes", "no", "edit", "always", "stop"]
        self.publish({"type": "guard.ask", "id": call_id, "tool": tool_name, "args": args,
                      "intent": intent, "risk": decision.risk, "reason": decision.reason,
                      "choices": choices})
        if self.ask_guard_fn is None:
            return ("deny", "human", "Нет канала подтверждения (headless без терминала): "
                    "по умолчанию отказ", args)
        answer = self.ask_guard_fn({"type": "guard.ask", "id": call_id, "tool": tool_name,
                                    "args": args, "intent": intent, "risk": decision.risk,
                                    "reason": decision.reason, "choices": choices})
        if not isinstance(answer, dict):
            return ("deny", "human", "Человек не ответил", args)
        choice = answer.get("decision", "no")
        if choice == "yes":
            return ("allow", "human", "Человек разрешил", args)
        if choice == "always":
            self.guard.remember_always(decision.risk)
            return ("allow", "human", "Человек разрешил и сказал «дальше без вопросов»", args)
        if choice == "edit":
            new_args = answer.get("args")
            if not isinstance(new_args, dict):
                return ("deny", "human", "Правка без новых аргументов не принята", args)
            # Правленые аргументы проходят жёсткие блоки Предохранителя ещё раз:
            # режимный ask человек уже ответил, а необратимое и утечки обязаны
            # сработать и на новых данных; второй раз не спрашиваем — отказ.
            hard = self.guard.hard_block(tool_name, new_args)
            if hard is not None:
                return ("deny", hard.by, "Правленые аргументы не прошли Предохранитель: " +
                        hard.reason, args)
            return ("allow", "human", "Человек отредактировал и разрешил", new_args)
        if choice == "stop":
            return ("deny", "human-stop", "Человек отменил весь ход", args)
        return ("deny", "human", "Человек отказал", args)

    # --- откат: /undo, /rewind, /redo (ЗАДАЧА-13) ---

    def _journal(self, kind: str, data: dict) -> None:
        """Запись своего вида в журнал сессии, если клиент его поставил.

        К записи прикладывается снимок памяти после операции: продолжение
        (--continue) восстанавливает историю с него, а не пытается заново
        проиграть откат или сводку по событиям ленты.
        """
        if self.journal is not None:
            try:
                data = dict(data, state=self._state_snapshot())
                self.journal.append({"kind": kind, "data": data, "ts": time.time()})
            except Exception:  # noqa: BLE001 — журнал не роняет откат
                pass

    def turns_overview(self) -> list:
        """[(номер хода, первая строка сообщения человека)] — список для /rewind."""
        out = []
        for turn, idx in self._turn_marks:
            text = ""
            if 0 <= idx < len(self._history):
                content = self._history[idx].get("content")
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    text = " ".join(str(p.get("text", "")) for p in content
                                    if isinstance(p, dict))
            out.append((turn, text.split("\n")[0][:60]))
        return out

    def undo(self):
        """Откатить последний ход целиком: файлы и разговор."""
        return self.rewind_to(self._turn - 1, "both")

    def rewind_command(self, turn=None, scope: str = "both"):
        """Команда rewind: без номера — список ходов, с номером — откат."""
        if turn is None:
            overview = self.turns_overview()
            if not overview:
                self.publish({"type": "notice", "level": "info",
                              "text": "Ходов пока не было — откатывать нечего."})
                return None
            lines = ["Ходы сессии ( /rewind N — вернуть состояние после хода N;"
                     " можно добавить files, talk или both ):"]
            for number, preview in overview:
                lines.append("  " + str(number) + " · " + preview)
            self.publish({"type": "notice", "level": "info", "text": "\n".join(lines)})
            return None
        try:
            target = int(turn)
        except (TypeError, ValueError):
            self.publish({"type": "notice", "level": "warn",
                          "text": "Не понял номер хода: " + str(turn)})
            return None
        if scope not in ("files", "talk", "both"):
            scope = "both"
        return self.rewind_to(target, scope)

    def rewind_to(self, target: int, scope: str = "both"):
        """Откат к состоянию после хода target: файлы, разговор или оба.

        Файл, который человек правил руками после хода (содержимое не
        сошлось с записанным «после»), молча не затирается: спрашиваем
        через ask_user, а без канала — пропускаем и честно докладываем.
        """
        if self._active:
            self.publish({"type": "notice", "level": "warn",
                          "text": "Идёт ход — откат возможен, когда он закончится."})
            return None
        if target < 0 or target >= self._turn:
            note = "Откатывать нечего." if self._turn < 1 else \
                "Уже на ходе " + str(self._turn) + " — откат не нужен."
            self.publish({"type": "notice", "level": "info", "text": note})
            return None
        from_turn = self._turn
        report = {"restored": [], "failed": [], "no_snapshot": [],
                  "conflicts": [], "previous": {}, "notes": []}
        if scope in ("files", "both"):
            if self._checkpoints is None:
                report["notes"].append("чекпоинты выключены — файлы не откатываю")
            else:
                plan = self._checkpoints.plan_restore(target)
                overwrite = False
                if plan["conflicts"]:
                    overwrite = self._ask_conflicts([c["path"] for c in plan["conflicts"]])
                    if overwrite is None:
                        self.publish({"type": "notice", "level": "info",
                                      "text": "Откат отменён — ничего не трогаю."})
                        return None
                report = self._checkpoints.restore_to(target, overwrite_conflicts=overwrite)
        removed_history: list = []
        removed_marks: list = []
        cut_index = None
        if scope in ("talk", "both"):
            for turn_no, idx in self._turn_marks:
                if turn_no > target:
                    cut_index = idx
                    break
            if cut_index is not None:
                removed_history = self._history[cut_index:]
                removed_marks = [(t, i) for t, i in self._turn_marks if t > target]
                self._history = self._history[:cut_index]
                self._turn_marks = [(t, i) for t, i in self._turn_marks if t <= target]
                self._turn = target
                self._last_sig = ""
                self._same_count = 0
        if self._checkpoints is not None:
            # Линия времени раздвоилась: записи мёртвых ходов в манифесте
            # сбрасываем, иначе переиспользованные номера ходов смешают
            # снимки разных линий. Redo держит шаг в памяти агента.
            self._checkpoints.forget_after(target)
        changed = bool(report["restored"] or removed_history)
        if changed:
            self._redo_stack.append({
                "to_turn": target, "from_turn": from_turn, "scope": scope,
                "history_tail": removed_history, "marks_tail": removed_marks,
                "cut_index": cut_index,
                "file_prev": report["previous"],
                "file_restored": {p: self._checkpoints_state(p) for p in report["restored"]},
            })
            if len(self._redo_stack) > 5:
                self._redo_stack.pop(0)
        self._journal("rewind", {"to_turn": target, "from_turn": from_turn,
                                 "scope": scope, "files": report["restored"],
                                 "skipped": report["conflicts"] + report["failed"]})
        self.publish({"type": "session.rewound", "turn": target, "scope": scope,
                      "files": len(report["restored"])})
        parts = ["Откат к состоянию после хода " + str(target) +
                 " (был ход " + str(from_turn) + ")."]
        if scope in ("talk", "both") and removed_history:
            parts.append("Разговор: модель забыла ходы после " + str(target) + ".")
        if report["restored"]:
            parts.append("Файлов возвращено: " + str(len(report["restored"])) + ".")
        if report["conflicts"]:
            parts.append("Пропущены как правленые вручную: " +
                         ", ".join(report["conflicts"]) + ".")
        if report["failed"]:
            parts.append("Не удалось вернуть: " + ", ".join(report["failed"]) + ".")
        if report["no_snapshot"]:
            parts.append("Без снимка (файл больше лимита): " +
                         ", ".join(report["no_snapshot"]) + ".")
        for note in report["notes"]:
            parts.append(note + ".")
        if changed:
            parts.append("/redo — вернуть откат.")
        self.publish({"type": "notice", "level": "info", "text": " ".join(parts)})
        return None

    def _checkpoints_state(self, path):
        if self._checkpoints is None:
            return None
        return self._checkpoints.state_of(path)

    def _ask_conflicts(self, paths: list):
        """Спросить про файлы, правленые вручную. True — затирать, False —
        пропустить, None — отменить откат. Без канала — False (безопасно)."""
        if self._ask_user_fn is None:
            return False
        question = ("Файлы изменены вручную после хода агента: " +
                    ", ".join(paths) + ". Перезаписать их при откате?")
        options = ["Пропустить эти файлы", "Перезаписать и их", "Отменить откат"]
        answer = self._ask_user(question, options)
        if answer is None:
            return False
        if answer == options[1]:
            return True
        if answer == options[2]:
            return None
        return False

    def redo(self):
        """Вернуть последний откат (хотя бы на шаг): файлы и разговор."""
        if self._active:
            self.publish({"type": "notice", "level": "warn",
                          "text": "Идёт ход — redo возможен, когда он закончится."})
            return None
        if not self._redo_stack:
            self.publish({"type": "notice", "level": "info",
                          "text": "Возвращать нечего: отката не было."})
            return None
        entry = self._redo_stack[-1]
        scope = entry["scope"]
        restored_files: list = []
        skipped_files: list = []
        if scope in ("files", "both") and self._checkpoints is not None:
            todo = {}
            for path, prev in entry["file_prev"].items():
                left = entry["file_restored"].get(path)
                current = self._checkpoints.state_of(path)
                if left is not None and not self._same_state(current, left):
                    skipped_files.append(path)  # после отката файл трогали
                    continue
                todo[path] = prev
            done, failed = self._checkpoints.apply_states(todo)
            restored_files = done
            skipped_files += failed
        if scope in ("talk", "both") and entry["history_tail"]:
            cut = entry["cut_index"]
            if cut is not None and len(self._history) == cut:
                self._history = self._history + entry["history_tail"]
                self._turn_marks = self._turn_marks + entry["marks_tail"]
                self._turn = entry["from_turn"]
            else:
                skipped_files.append("(разговор изменился после отката)")
        self._redo_stack.pop()
        self._journal("redo", {"turn": entry["from_turn"], "scope": scope,
                               "files": restored_files})
        parts = ["Откат возвращён: снова состояние после хода " +
                 str(entry["from_turn"]) + "."]
        if restored_files:
            parts.append("Файлов переписано: " + str(len(restored_files)) + ".")
        if skipped_files:
            parts.append("Пропущено: " + ", ".join(skipped_files) + ".")
        self.publish({"type": "notice", "level": "info", "text": " ".join(parts)})
        return None

    @staticmethod
    def _same_state(a, b) -> bool:
        from ..core.checkpoints import same_state

        return same_state(a, b)

    # --- сжатие контекста: «прилив» (ЗАДАЧА-13) ---

    def _after_turn(self) -> None:
        """После хода: отложенное ручное сжатие или автопорог."""
        try:
            if self._compact_pending is not None:
                hint, self._compact_pending = self._compact_pending, None
                self._do_compact(hint)
                return
            if self.settings is None or not self.settings.get("compact.enabled", True):
                return
            threshold = int(self.settings.get("compact.threshold_pct", 80) or 80)
            if self._context_pct >= threshold:
                self._do_compact("")
        except Exception:  # noqa: BLE001 — сжатие не роняет агента
            pass

    def compact_now(self, hint: str = ""):
        """Команда /compact: вручную; во время хода — отложенно до его конца."""
        if self._active:
            self._compact_pending = hint
            self.publish({"type": "notice", "level": "info",
                          "text": "Прилив будет после хода."})
            return None
        self._do_compact(hint)
        return None

    def _do_compact(self, hint: str) -> None:
        if self.settings is not None and not self.settings.get("compact.enabled", True):
            self.publish({"type": "notice", "level": "info",
                          "text": "Сжатие выключено настройкой compact.enabled."})
            return
        from .compact import compact_history, history_chars

        keep = 2
        template = ""
        if self.settings is not None:
            keep = int(self.settings.get("compact.keep_turns", 2) or 2)
            template = str(self.settings.get("compact.prompt", "") or "")
        before_chars = history_chars(self._history)
        before_count = len(self._history)
        before_pct = round(self._context_pct, 1)
        try:
            outcome = compact_history(self.router, self._history, self._turn_marks,
                                      keep_turns=keep, hint=hint,
                                      prompt_template=template,
                                      todos=list(self.ctx.todos))
        except Exception as exc:  # noqa: BLE001 — модель сводки упала: история цела
            self.publish({"type": "notice", "level": "warn",
                          "text": "Прилив не удался (" + str(exc)[:120] +
                                  ") — история не тронута."})
            return
        if outcome is None:
            self.publish({"type": "notice", "level": "info",
                          "text": "Сжимать пока нечего: история короткая."})
            return
        self._history = outcome["history"]
        self._turn_marks = outcome["marks"]
        self._redo_stack.clear()
        after_chars = history_chars(self._history)
        after_pct = round(before_pct * after_chars / before_chars, 1) if before_chars else 0.0
        self._context_pct = after_pct
        self._journal("compact", {"before_pct": before_pct, "after_pct": after_pct,
                                  "summary": outcome["summary"],
                                  "summarized_turns": outcome["summarized"],
                                  "kept_turns": outcome["kept"]})
        self.publish({"type": "context.compacted", "before_pct": before_pct,
                      "after_pct": after_pct,
                      "before_messages": before_count,
                      "after_messages": len(self._history),
                      "summarized_turns": outcome["summarized"]})
        self.publish({"type": "notice", "level": "info",
                      "text": "Прилив: контекст сжат — было ~" + str(before_pct) +
                              "%, стало ~" + str(after_pct) + "% (сводкой заменено ходов: " +
                              str(len(outcome["summarized"])) + ")."})
