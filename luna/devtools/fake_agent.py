"""Фальшивый агент: источник событий контракта docs/СОБЫТИЯ.md без ядра.

Проигрывает сценарии с реалистичным темпом: стрим текста и рассуждений
кусками, тулы с выводом, подтверждение, вопрос, todo, рост context_pct,
ошибка. Отвечает на команды клиента тем же контрактом.

Интерфейс источника событий (то самое одно место подмены для тимлида):
    start() -> None                  — открыть сессию (события заберёт poll)
    handle_command(cmd: dict)        — команда контракта (user.message, ...)
    poll(now: float | None) -> list  — события, которым пришло время
    has_pending() -> bool            — есть запланированные события/ожидание
    busy -> bool                     — идёт ход

Ключевые слова в сообщении выбирают сценарий: «подтверди» — с guard.ask,
«вопрос» — с ask.user, «ошибка» — с ошибкой, «долго» — длинный вывод тула,
всё остальное — обычный ход с планом, рассуждениями, тулом и ответом.
"""

from __future__ import annotations

import time

_CHUNK_PAUSE = 0.45


class FakeAgent:
    def __init__(self, *, model: str = "fake-agent", cwd: str = "",
                 clock=None, session: str = "fake-session"):
        self.model = model
        self.cwd = cwd
        self.session = session
        self._clock = clock or time.monotonic
        self.mode = "normal"
        self.busy = False
        self.turn = 0
        self.context_pct = 0
        self.cost = 0.0
        self._queue: list = []        # сообщения, пришедшие во время хода
        self._pending: list = []      # (время, событие | callable)
        self._waiting_guard: dict | None = None
        self._waiting_ask: dict | None = None
        self._started = False
        self.history: list = []       # принятая история прошлой сессии

    # --- интерфейс источника ------------------------------------------------

    def load_history(self, messages, **_state) -> None:
        """История прошлой сессии: фальшивому агенту она нужна только для
        проверок интерфейса источника (сценарии от неё не зависят)."""
        self.history = [dict(m) for m in messages]

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        now = self._clock()
        self._at(now, {"type": "session.start", "cwd": self.cwd,
                       "model": self.model, "mode": self.mode})
        self._at(now, {"type": "notice", "level": "info",
                       "text": "Фальшивый агент: сценарии — обычное сообщение, "
                               "«подтверди …», «вопрос …», «ошибка …», «долго …», "
                               "«петля …» (Ниблер заметит). Панели: /panels."})

    def has_pending(self) -> bool:
        return bool(self._pending) or self._waiting_guard is not None \
            or self._waiting_ask is not None

    def poll(self, now: float | None = None) -> list:
        now = self._clock() if now is None else now
        out = []
        while self._pending and self._pending[0][0] <= now:
            _when, payload = self._pending.pop(0)
            if callable(payload):
                produced = payload()
                if isinstance(produced, dict):
                    out.append(self._stamp(produced))
            else:
                out.append(self._stamp(payload))
        return out

    # --- команды ---------------------------------------------------------------

    def handle_command(self, cmd: dict) -> None:
        kind = cmd.get("type")
        now = self._clock()
        if kind == "user.message":
            if self.busy:
                self._queue.append(cmd)
                self._at(now, {"type": "notice", "level": "info",
                               "text": "В очереди: уйдёт следующим шагом."})
                return
            self._begin_turn(cmd)
        elif kind == "interrupt":
            if self.busy:
                self._pending.clear()
                self._waiting_guard = None
                self._waiting_ask = None
                self.busy = False
                self._at(now, {"type": "turn.done", "turn": self.turn,
                               "reason": "interrupted"})
                self._at(now, {"type": "notice", "level": "info",
                               "text": "Возвращаемся на орбиту."})
                self._drain_queue()
        elif kind == "guard.answer":
            self._guard_answer(cmd)
        elif kind == "user.answer":
            self._user_answer(cmd)
        elif kind == "mode.set":
            self.mode = cmd.get("mode", self.mode)
            self._at(now, {"type": "mode.changed", "mode": self.mode})
        elif kind == "btw":
            # Вопрос «в сторону»: отвечаем отдельным событием, ход не трогаем.
            question = str(cmd.get("text", ""))
            self._at(now + 0.3, {"type": "btw.answer",
                                 "text": "В сторону, не сбивая ход: на «"
                                         + question + "» — фальшивый ответ."})
        elif kind == "shell":
            command = cmd.get("command", "")
            self._at(now, {"type": "notice", "level": "info",
                           "text": "Оболочка (фальшиво): " + command + " — выполнено."})

    # --- внутренности -------------------------------------------------------------

    def _stamp(self, ev: dict) -> dict:
        ev = dict(ev)
        ev.setdefault("session", self.session)
        ev.setdefault("ts", time.time())
        return ev

    def _at(self, when: float, payload) -> None:
        self._pending.append((when, payload))
        self._pending.sort(key=lambda item: item[0])

    def _after(self, delay: float, payload) -> None:
        self._at(self._clock() + delay, payload)

    def _drain_queue(self) -> None:
        if self._queue and not self.busy:
            self._begin_turn(self._queue.pop(0))

    def _begin_turn(self, cmd: dict) -> None:
        text = cmd.get("text", "")
        self.turn += 1
        self.busy = True
        turn = self.turn
        t = self._clock()
        self._at(t, {"type": "turn.start", "turn": turn})
        low = text.lower()
        if "ошибка" in low:
            self._at(t + 0.6, {"type": "error",
                                "message": "фальшивая ошибка по сценарию",
                                "detail": "так выглядит HumanError в ленте"})
            self._finish_at(t + 0.9, "error")
            return
        if "вопрос" in low:
            self._ask_scenario(t, turn)
            return
        if "подтверди" in low:
            self._guard_scenario(t, turn)
            return
        if "петля" in low:
            self._loop_scenario(t, turn)
            return
        self._plain_scenario(t, turn, long_output="долго" in low)

    def _loop_scenario(self, t: float, turn: int) -> None:
        """Петля правок одного файла: Ниблер такое замечает (ЗАДАЧА-14)."""
        self._stream(t + 0.3, "agent.thinking", "Чиню обработчик, попытка за попыткой. ")
        self._at(t + 1.4, {"type": "tool.call", "id": "loop-r", "name": "read",
                            "args": {"path": "app.py"},
                            "intent": "Читаю обработчик перед правками"})
        self._at(t + 1.7, {"type": "tool.result", "id": "loop-r", "ok": True,
                            "summary": "120 строк", "output": "def handle(): ...\n"})
        at = t + 2.0
        for i in range(3):
            cid = "loop-" + str(i + 1)
            self._at(at, {"type": "tool.call", "id": cid, "name": "edit",
                          "args": {"path": "app.py"},
                          "intent": "Правлю обработчик, попытка " + str(i + 1)})
            self._at(at + 0.4, {"type": "tool.result", "id": cid, "ok": True,
                                "summary": "app.py: +2 −1",
                                "output": "+ новая строка " + str(i + 1) + "\n- старая строка\n"})
            at += 0.8
        self._stream(at, "agent.text", "Готово, обработчик поправлен.")
        self._at(at + 1.2, {"type": "agent.text.done"})
        self._usage_at(at + 1.3, 12, 0.006)
        self._finish_at(at + 1.5, "end")

    def _finish_at(self, when: float, reason: str) -> None:
        self._at(when, {"type": "turn.done", "turn": self.turn, "reason": reason})

        def clear() -> None:
            self.busy = False
            self._drain_queue()

        self._at(when + 0.01, clear)

    def _usage_at(self, when: float, add_pct: int, add_cost: float) -> None:
        self.context_pct = min(100, self.context_pct + add_pct)
        self.cost = round(self.cost + add_cost, 4)
        self._at(when, {"type": "usage", "model": self.model,
                        "input": 1200 + self.turn * 300, "output": 240,
                        "cost": self.cost, "context_pct": self.context_pct})

    def _plain_scenario(self, t: float, turn: int, long_output: bool = False) -> None:
        self._at(t + 0.2, {"type": "todo", "items": [
            {"text": "Понять задачу", "status": "done"},
            {"text": "Выполнить работу", "status": "active"},
            {"text": "Проверить результат", "status": "pending"},
        ]})
        think = "Сверяюсь со звёздами и смотрю, что уже есть под рукой. "
        self._stream(t + 0.4, "agent.thinking", think)
        answer_head = "## Готово\n\nСделал по шагам:\n\n"
        self._stream(t + 1.6, "agent.text", answer_head)
        self._at(t + 2.5, {"type": "agent.text.done"})
        tool_at = t + 2.6
        self._at(tool_at, {"type": "tool.call", "id": "tool-1", "name": "bash",
                            "args": {"command": "pip install lxml"},
                            "intent": "Ставлю зависимость для разбора разметки"})
        lines = 24 if long_output else 4
        for i in range(lines):
            self._at(tool_at + 0.3 + i * 0.12,
                     {"type": "tool.output", "id": "tool-1",
                      "text": "строка вывода " + str(i + 1) + "\n"})
        result_at = tool_at + 0.6 + lines * 0.12
        self._at(result_at, {"type": "tool.result", "id": "tool-1", "ok": True,
                             "summary": "установлено за 1,2 с",
                             "output": "Successfully installed lxml-5.3.0\n"})
        self._at(result_at + 0.2, {"type": "tool.call", "id": "tool-2",
                                   "name": "edit",
                                   "args": {"path": "app.py"},
                                   "intent": "Правлю обработчик по итогам запуска"})
        self._at(result_at + 0.7, {"type": "tool.result", "id": "tool-2",
                                   "ok": True, "summary": "app.py: +12 −3",
                                   "output": "+ добавлен стрим\n- старый вызов\n"})
        tail = ("Дальше — **проверка**: `python -m unittest -q`.\n\n"
                "- вывод тула сворачивается\n"
                "- правки видны мини-диффом\n")
        self._stream(result_at + 1.0, "agent.text", tail)
        done_at = result_at + 1.0 + 0.5 * (len(tail) // 24 + 1)
        self._at(done_at, {"type": "agent.text.done"})
        self._usage_at(done_at + 0.1, 9, 0.0042)
        self._at(done_at + 0.2, {"type": "todo", "items": [
            {"text": "Понять задачу", "status": "done"},
            {"text": "Выполнить работу", "status": "done"},
            {"text": "Проверить результат", "status": "active"},
        ]})
        self._finish_at(done_at + 0.3, "end")

    def _guard_scenario(self, t: float, turn: int) -> None:
        self._stream(t + 0.3, "agent.thinking", "Команда меняет систему — спрошу человека. ")
        self._at(t + 1.2, {"type": "tool.call", "id": "tool-g", "name": "bash",
                           "args": {"command": "rm -rf build/"},
                           "intent": "Чищу каталог сборки перед пересборкой"})

        def ask() -> None:
            self._waiting_guard = {"id": "guard-1"}

        self._at(t + 1.5, ask)
        self._at(t + 1.5, {"type": "guard.ask", "id": "guard-1", "tool": "bash",
                           "args": {"command": "rm -rf build/"},
                           "intent": "Чищу каталог сборки перед пересборкой",
                           "risk": "high", "reason": "необратимое удаление",
                           "choices": ["yes", "no", "edit", "always", "stop"]})

    def _guard_answer(self, cmd: dict) -> None:
        if self._waiting_guard is None:
            return
        self._waiting_guard = None
        now = self._clock()
        decision = cmd.get("decision", "no")
        self._at(now, {"type": "guard.verdict", "id": cmd.get("id", "guard-1"),
                       "decision": decision, "by": "human", "reason": ""})
        if decision in ("yes", "always", "edit"):
            args = cmd.get("args") or {"command": "rm -rf build/"}
            command = args.get("command", "")
            note = "Выполняю: " + command
            self._at(now + 0.2, {"type": "notice", "level": "info", "text": note})
            self._at(now + 0.4, {"type": "tool.result", "id": "tool-g", "ok": True,
                                 "summary": "выполнено", "output": ""})
            self._stream(now + 0.6, "agent.text", "Готово, сборка чиста.")
            self._at(now + 1.6, {"type": "agent.text.done"})
            if decision == "always":
                self._at(now + 1.7, {"type": "notice", "level": "info",
                                     "text": "Дальше без вопросов на этом уровне риска."})
            self._finish_at(now + 1.8, "end")
        elif decision == "stop":
            self._at(now + 0.2, {"type": "notice", "level": "info",
                                 "text": "Ход остановлен человеком."})
            self._finish_at(now + 0.3, "interrupted")
        else:
            self._at(now + 0.2, {"type": "tool.result", "id": "tool-g", "ok": False,
                                 "summary": "НЕ ВЫПОЛНЕНО: человек отказал",
                                 "output": ""})
            self._stream(now + 0.4, "agent.text", "Понял, не трогаю. Обходных путей не ищу.")
            self._at(now + 1.2, {"type": "agent.text.done"})
            self._finish_at(now + 1.4, "end")

    def _ask_scenario(self, t: float, turn: int) -> None:
        self._stream(t + 0.3, "agent.text", "Уточню один момент. ")

        def ask() -> None:
            self._waiting_ask = {"id": "ask-1"}

        self._at(t + 0.9, ask)
        self._at(t + 0.9, {"type": "ask.user", "id": "ask-1",
                           "question": "Какой формат отчёта нужен?",
                           "options": ["Короткий", "Подробный", "Только цифры"]})

    def _user_answer(self, cmd: dict) -> None:
        if self._waiting_ask is None:
            return
        self._waiting_ask = None
        now = self._clock()
        answer = cmd.get("text", "")
        reply = "Принято: " + answer + ". Делаю отчёт в этом формате."
        self._stream(now + 0.2, "agent.text", reply)
        self._at(now + 1.0, {"type": "agent.text.done"})
        self._finish_at(now + 1.2, "end")

    def _stream(self, start: float, kind: str, text: str) -> None:
        """Разрезать текст на куски и разложить по времени — как настоящий стрим."""
        step = 24
        when = start
        for i in range(0, len(text), step):
            chunk = text[i:i + step]
            self._at(when, {"type": kind, "text": chunk})
            when += _CHUNK_PAUSE
