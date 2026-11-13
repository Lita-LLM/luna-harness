"""Настоящий агент как источник событий для чата (тот же интерфейс, что у FakeAgent).

Ход агента синхронный и блокирующий, а экран живёт в своём цикле кадров,
поэтому ход идёт в фоновом потоке, события копятся в очереди, а чат забирает
их в poll(). Подтверждения и вопросы агент ждёт на Event — пока человек не
ответит командой guard.answer / user.answer или не прервёт ход.
"""

from __future__ import annotations

import queue
import threading
import time
from pathlib import Path


class AgentSource:
    def __init__(self, *, settings=None, cwd: str = "", agent=None):
        self._settings = settings
        self._cwd = cwd or str(Path.cwd())
        self._agent = agent
        self._events: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None
        self._lock = threading.Lock()
        self._waits: dict[str, tuple[threading.Event, list]] = {}
        self._pending_history: list | None = None
        self._pending_state: dict = {}
        self._journal = None

    # --- интерфейс источника ------------------------------------------------

    def start(self) -> None:
        if self._agent is None:
            self._agent = self._build_agent()
        if self._pending_history is not None:
            self._agent.load_history(self._pending_history, **self._pending_state)
            self._pending_history = None
        self._agent.listen(self._events.put)
        model = getattr(self._agent, "_model_label", lambda: "")()
        if not model:
            self._events.put({"type": "notice", "level": "warn", "ts": time.time(),
                              "text": "Модель не настроена — открой /pult (пульт ИИ): найди провайдера сканом сети или добавь, выбери модель."})

    def has_pending(self) -> bool:
        return not self._events.empty() or self._busy()

    def poll(self, now: float | None = None) -> list:  # noqa: ARG002 — интерфейс как у FakeAgent
        out = []
        while True:
            try:
                out.append(self._events.get_nowait())
            except queue.Empty:
                return out

    def handle_command(self, cmd: dict) -> None:
        kind = cmd.get("type")
        if kind in ("guard.answer", "user.answer"):
            self._answer(cmd.get("id", ""), cmd)
            return
        if kind == "detach":
            detach = getattr(self._agent, "detach", None)
            if callable(detach):
                detach()
            return
        if kind == "interrupt":
            self._agent.interrupt()
            self._release_all({"decision": "stop", "text": ""})
            return
        if kind == "btw":
            # Вопрос в сторону идёт своим потоком (ЗАДАЧА-18): не ждёт
            # текущий ход и не мешает ему; историю агент не трогает.
            if self._agent is not None:
                threading.Thread(target=self._safe_btw, args=(cmd,),
                                 name="luna-btw", daemon=True).start()
            return
        if kind == "mode.set":
            self._agent.handle_command(cmd)
            return
        if kind in ("undo", "rewind", "redo", "compact"):
            # Откат посреди хода агент отклонит сам (notice), сжатие отложит
            # до конца хода; вне хода — в воркер: сводка ходит в модель.
            if self._busy():
                self._agent.handle_command(cmd)
            else:
                self._run_in_worker(cmd)
            return
        if kind in ("user.message", "shell"):
            if kind == "user.message" and self._busy():
                self._agent.handle_command(cmd)  # во время хода — в очередь агента
                return
            self._run_in_worker(cmd)

    def load_history(self, messages, *, turn=None, marks=None) -> None:
        """История прошлой сессии для агента (продолжение и переключение).

        Агент ещё не построен — запомнить и отдать при start(). Агент жив —
        пересобрать его: ход старой сессии мог ещё крутиться в потоке, и
        подменять историю под ним нельзя. Очередь событий при пересборке
        очищается: события прошлой сессии новой ленте не нужны.
        """
        self._pending_history = [dict(m) for m in messages]
        self._pending_state = {"turn": turn, "marks": marks}
        if self._agent is None:
            return
        self._agent.interrupt()
        self._release_all({"decision": "stop", "text": ""})
        if self._busy():
            self._worker.join(timeout=2.0)
        while True:
            try:
                self._events.get_nowait()
            except queue.Empty:
                break
        self._agent = self._build_agent()
        self._agent.load_history(self._pending_history, **self._pending_state)
        self._pending_history = None
        # Спутники прошлой сессии возвращаются спящими (ЗАДАЧА-11).
        try:
            if self._journal is not None:
                self._agent.journal = self._journal
            from ..agents.host import host_for

            host_for(self._agent).ensure_restored()
        except Exception:  # noqa: BLE001 — команда не мешает продолжению сессии
            pass
        self._agent.listen(self._events.put)

    def set_journal(self, journal) -> None:
        """Журнал сессии для записей отката и сжатия (kind: rewind/compact)."""
        self._journal = journal
        if self._agent is not None:
            self._agent.journal = journal

    def shutdown(self) -> None:
        """Перед горячей перезагрузкой: закрыть MCP-серверы агента и экипажа.

        Новый код поднимет свои; старые процессы иначе висели бы рядом
        (два юзербота на одном аккаунте мешают друг другу)."""
        agents = [self._agent]
        host = getattr(self._agent, "_agents_host", None)
        for child in list(getattr(host, "children", {}).values()):
            agents.append(getattr(child, "agent", None))
        for agent in agents:
            dropper = getattr(getattr(agent, "tools", None), "_drop_mcp", None)
            if callable(dropper):
                try:
                    dropper()
                except Exception:  # noqa: BLE001 — выход важнее
                    pass

    # --- внутреннее ---------------------------------------------------------

    def _busy(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    def _safe_btw(self, cmd: dict) -> None:
        try:
            self._agent.handle_command(cmd)
        except Exception as exc:  # noqa: BLE001 — btw не роняет экран
            self._events.put({"type": "error", "ts": time.time(),
                              "message": "Вопрос в сторону упал: " + str(exc),
                              "detail": repr(exc)})

    def _run_in_worker(self, cmd: dict) -> None:
        def work() -> None:
            try:
                self._agent.handle_command(cmd)
                # delivery=queue/interrupt (ЗАДАЧА-18): отложенные сообщения
                # уходят отдельными ходами следом, в этом же потоке.
                take = getattr(self._agent, "take_deferred", None)
                while take is not None:
                    deferred = take()
                    if not deferred:
                        break
                    for item in deferred:
                        self._agent.run_turn(item["text"],
                                             item.get("attachments") or [])
            except Exception as exc:  # noqa: BLE001 — падение хода не роняет экран
                self._events.put({"type": "error", "ts": time.time(),
                                  "message": "Ход упал: " + str(exc), "detail": repr(exc)})
                self._events.put({"type": "turn.done", "ts": time.time(), "reason": "error"})

        if self._busy():
            self._worker.join(timeout=0.1)
        self._worker = threading.Thread(target=work, name="luna-agent-turn", daemon=True)
        self._worker.start()

    def _wait_answer(self, wait_id: str) -> dict | None:
        ev = threading.Event()
        box: list = []
        with self._lock:
            self._waits[wait_id] = (ev, box)
        ev.wait()
        with self._lock:
            self._waits.pop(wait_id, None)
        return box[0] if box else None

    def _answer(self, wait_id: str, cmd: dict) -> None:
        with self._lock:
            pair = self._waits.get(wait_id)
        if pair is None:
            return
        ev, box = pair
        box.append(cmd)
        ev.set()

    def _release_all(self, answer: dict) -> None:
        with self._lock:
            pairs = list(self._waits.values())
        for ev, box in pairs:
            if not box:
                box.append(answer)
            ev.set()

    def _ask_guard(self, event: dict):
        return self._wait_answer(event.get("id", ""))

    def _ask_user(self, qid: str, question: str, options):  # noqa: ARG002
        ans = self._wait_answer(qid)
        if not ans or ans.get("decision") == "stop":
            return None
        return ans.get("text")

    def _build_agent(self):
        from ..core.paths import DataDir
        from ..core.secrets import Secrets
        from ..core.settings import Settings
        from ..router.core import Router
        from .loop import Agent

        data = DataDir()
        data.ensure()
        settings = self._settings or Settings()
        secrets = Secrets(data.secrets)
        router = Router(settings, secrets)
        agent = Agent(settings=settings, secrets=secrets, router=router, cwd=Path(self._cwd),
                      ask_guard_fn=self._ask_guard, ask_user_fn=self._ask_user)
        agent.journal = self._journal
        return agent
