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
                 ask_guard_fn=None, ask_user_fn=None, tools: dict | None = None):
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
                               output_dir=self._output_dir())
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
        return None

    def set_mode(self, mode: str) -> None:
        if mode in ("normal", "plan", "review", "auto", "yolo"):
            self.mode = mode
            self.publish({"type": "mode.changed", "mode": mode})

    def interrupt(self) -> None:
        self._interrupt.set()

    @property
    def _turn_active(self) -> bool:
        return getattr(self, "_active", False)

    def run_shell(self, command: str):
        """!команда мимо модели, но через Предохранитель, как любой bash."""
        tool = self.tools.get("bash")
        if tool is None:
            return None
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
        self.publish({"type": "usage", "model": result.model or self._model_label(),
                      "input": result.usage.input, "output": result.usage.output,
                      "cost": round(cost, 6), "context_pct": round(pct, 2)})

    # --- история и системный промпт ---

    def load_history(self, messages) -> None:
        """Принять историю прошлой сессии (продолжение, luna --continue).

        Сообщения — как их собрал core.sessions.build_history: без
        системного промпта, его _ensure_system_prompt вставит первым.
        Счётчик ходов продолжается с числа реплик пользователя.
        """
        self._history = [dict(m) for m in messages]
        self._turn = sum(1 for m in self._history if m.get("role") == "user")
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

    def _ensure_system_prompt(self) -> None:
        """Собрать промпт к первому ходу; дальше — пересобрать, если сменились
        режим или манера (тул manner и mode.set действуют со следующего хода)."""
        sig = self._current_prompt_sig()
        if self._started and sig == self._prompt_sig:
            return
        content = build_system_prompt(self.cwd, self.mode, settings=self.settings)
        if self._history and self._history[0].get("role") == "system":
            self._history[0] = {"role": "system", "content": content}
        else:
            self._history.insert(0, {"role": "system", "content": content})
        self._prompt_sig = sig

    def run_turn(self, text: str, attachments=None) -> str:
        """Один ход до финала. Возвращает причину turn.done."""
        self._active = True
        self._interrupt.clear()
        self._turn += 1
        turn = self._turn
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
        result = tool.run(self.ctx, final_args)
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
