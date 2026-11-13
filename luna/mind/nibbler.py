"""Ниблер — компаньон-наблюдатель внизу экрана (docs/ДИЗАЙН.md, «Ниблер»).

Маленький наблюдатель за событиями главного агента: смотрит тулы, ошибки
и ходы, изредка говорит одну строку (событие nibbler.say). Часть замечаний —
эвристики без модели: третий раз правит один файл, контекст почти полон.
Остальное решает мозг — модель роли fast: её зовут редко (ошибка, конец
долгого или плохого хода, повторяющийся паттерн) и с дросселем — не чаще
nibbler.interval_sec секунд и nibbler.every_turns ходов.

Серьёзные замечания по умолчанию уходят главному агенту пометкой в
следующий ход (настройка nibbler.to_agent: serious | all | never) —
в этом отличие от /buddy: агент видит критику. Характер — манера
(nibbler.manner, механизм luna/mind/manners.py); модель сама решает,
промолчать ли: пустой ответ — молчание.

Ниблер работает по событиям, без таймеров и опросов, и никогда не роняет
хозяина: любая его ошибка — тишина.
"""

from __future__ import annotations

import json
import time
from collections import deque

CORE_PROMPT = """Ты — Ниблер, маленький наблюдатель в терминале агента «Луна». Ты видишь поток событий главного агента со стороны: вызовы тулов, ошибки, концы ходов. Твоя работа — изредка вводить человека в курс дела и критиковать агента, когда он петляет, врёт про «всё готово», забывает проверку или тратит лишнее.

Правила ответа:
- Ответь ОДНОЙ короткой строкой (до 140 знаков) — или пустым ответом, если сказать нечего. Молчание — норма.
- Серьёзное замечание (агент вредит, петляет, обманывает, теряет задачу) начни со знака «!».
- Никаких приветствий, пояснений и повторов того, что человек и так видит на экране."""

MAX_TEXT = 160
_DIGEST_LINES = 40
_EDIT_TOOLS = ("edit", "write")


def make_router_brain(settings, secrets=None):
    """Мозг Ниблера поверх роутера моделей (роль fast). Router строится лениво."""
    holder: dict = {}

    def brain(system: str, digest: str) -> str:
        if "router" not in holder:
            from ..core.paths import DataDir
            from ..core.secrets import Secrets
            from ..router.core import Router

            sec = secrets if secrets is not None else Secrets(DataDir().secrets)
            holder["router"] = Router(settings, sec)
        result = holder["router"].chat(
            [{"role": "system", "content": system},
             {"role": "user", "content": digest}], role="fast")
        return result.text or ""

    return brain


class Nibbler:
    """Наблюдатель событий агента. emit — куда класть nibbler.say."""

    def __init__(self, settings=None, *, emit=None, brain=None, clock=None,
                 threaded: bool = False):
        self.settings = settings
        self._emit = emit
        self._brain = brain
        # threaded: мозг зовётся в своём потоке, ответ забирает drain() из
        # главного цикла чата — иначе экран стоял, пока fast-модель думала.
        self._threaded = threaded
        self._thread = None
        self._ready: list = []
        self._clock = clock or time.time
        self.said: list[dict] = []
        self._notes: list[str] = []
        self._digest: deque = deque(maxlen=_DIGEST_LINES)
        self._turn = 0
        self._turn_started = 0.0
        self._last_say_at: float | None = None
        self._last_say_turn = -10 ** 9
        self._calls: dict = {}
        self._edit_counts: dict = {}
        self._remarked_files: set = set()
        self._last_sig = ""
        self._same_sig = 0
        self._ctx_hot = False
        self._request = ""        # последняя задача человека — без неё Ниблер слеп
        self._full: deque = deque()  # стенограмма целиком (nibbler.context=full)
        self._full_chars = 0
        self._answer: list = []   # текст агента текущего шага

    # --- настройки ---------------------------------------------------------

    def _get(self, key: str, default):
        if self.settings is None:
            return default
        value = self.settings.get(key)
        return default if value is None else value

    @property
    def _interval(self) -> float:
        return float(self._get("nibbler.interval_sec", 60) or 0)

    @property
    def _every_turns(self) -> int:
        return int(self._get("nibbler.every_turns", 2) or 0)

    @property
    def _long_turn(self) -> float:
        return float(self._get("nibbler.long_turn_sec", 90) or 0)

    # --- вход событий --------------------------------------------------------

    def observe(self, ev: dict) -> None:
        """Событие агента по контракту docs/СОБЫТИЯ.md. Никогда не падает."""
        try:
            self._observe(ev or {})
        except Exception:  # noqa: BLE001 — наблюдатель не роняет хозяина
            pass

    def _record(self, line: str) -> None:
        """Стенограмма с потолком nibbler.context_chars: старое уходит первым."""
        cap = int(self._get("nibbler.context_chars", 60000) or 60000)
        self._full.append(line)
        self._full_chars += len(line) + 1
        while self._full_chars > cap and len(self._full) > 1:
            self._full_chars -= len(self._full.popleft()) + 1

    def _transcribe(self, ev: dict) -> None:
        kind = ev.get("type")
        if kind == "user.message":
            self._record("ЧЕЛОВЕК: " + str(ev.get("text", "")))
        elif kind == "agent.text.done":
            pass  # текст шага пишется целиком в _observe
        elif kind == "agent.thinking":
            return
        elif kind == "tool.call":
            args = json.dumps(ev.get("args") or {}, ensure_ascii=False, default=str)
            self._record("ВЫЗОВ " + str(ev.get("name", "")) + " " + args[:4000] +
                         (" — " + str(ev.get("intent")) if ev.get("intent") else ""))
        elif kind == "tool.result":
            name = str(ev.get("name") or self._name_of(ev.get("id")) or "тул")
            body = str(ev.get("output") or ev.get("summary") or "")
            self._record("РЕЗУЛЬТАТ " + name + (" ok" if ev.get("ok") else " ОШИБКА") +
                         ":\n" + body[:6000])
        elif kind == "error":
            self._record("ОШИБКА: " + str(ev.get("message", "")) + " " + str(ev.get("detail") or ""))
        elif kind == "guard.verdict" and str(ev.get("decision")) == "deny":
            self._record("ПРЕДОХРАНИТЕЛЬ ОТКАЗАЛ: " + str(ev.get("reason", "")))
        elif kind == "turn.done":
            self._record("— конец хода: " + str(ev.get("reason", "")) + " —")

    def _observe(self, ev: dict) -> None:
        kind = ev.get("type")
        self._transcribe(ev)
        if kind == "user.message":
            text = " ".join(str(ev.get("text", "")).split())
            if text:
                self._request = text[:800]
                self._note_line("человек: " + text[:200])
            return
        if kind == "agent.text":
            self._answer.append(str(ev.get("text", "")))
            return
        if kind == "agent.text.done":
            full = "".join(self._answer)
            said = " ".join(full.split())
            self._answer = []
            if full.strip():
                self._record("АГЕНТ: " + full.strip())
            if said:
                self._note_line("агент сказал: " + said[:300])
            return
        if kind == "turn.start":
            self._turn += 1
            self._turn_started = self._clock()
            self._note_line("ход " + str(self._turn) + " начался")
        elif kind == "tool.call":
            self._on_tool_call(ev)
        elif kind == "tool.result":
            name = str(ev.get("name") or self._name_of(ev.get("id")) or "тул")
            ok = bool(ev.get("ok"))
            what = " ".join(str(ev.get("summary") or ev.get("output") or "").split())
            limit = 240 if not ok else 120
            self._note_line("тул " + name + ": " + ("ok" if ok else "ОШИБКА") +
                            (" — " + what[:limit] if what else ""))
            if not ok:
                self._consider("ошибка тула " + name)
        elif kind == "error":
            detail = " ".join(str(ev.get("detail") or "").split())
            self._note_line("ошибка: " + str(ev.get("message", ""))[:200] +
                            (" (" + detail[:200] + ")" if detail else ""))
            self._consider("ошибка хода")
        elif kind == "usage":
            pct = float(ev.get("context_pct") or 0)
            if pct >= 90 and not self._ctx_hot:
                self._ctx_hot = True
                self._say("Контекст заполнен на " + str(int(pct)) +
                          "% — скоро понадобится сжатие.", "minor", "context")
            elif pct < 85:
                self._ctx_hot = False
        elif kind == "guard.verdict":
            if str(ev.get("decision")) == "deny":
                self._note_line("Предохранитель отказал: " + str(ev.get("reason", ""))[:80])
        elif kind == "turn.done":
            self._on_turn_done(ev)

    def _on_tool_call(self, ev: dict) -> None:
        name = str(ev.get("name", ""))
        args = ev.get("args") or {}
        self._calls[str(ev.get("id", ""))] = name
        brief = name + " " + _args_brief(args)
        self._note_line("тул " + brief)
        sig = name + ":" + json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)
        if sig == self._last_sig:
            self._same_sig += 1
        else:
            self._last_sig, self._same_sig = sig, 1
        if self._same_sig == 2:
            self._consider("один и тот же вызов повторяется: " + brief)
        if name in _EDIT_TOOLS:
            path = str(args.get("path") or "")
            if path:
                count = self._edit_counts.get(path, 0) + 1
                self._edit_counts[path] = count
                if count >= 3 and path not in self._remarked_files:
                    # Помечаем «сказанным» только если реплика правда ушла:
                    # закрытый дроссель не должен съедать замечание навсегда.
                    if self._say("Агент правит файл " + path + " уже " + str(count) +
                                 "-й раз. Похоже, дело не в файле.", "serious", "repeat-edit"):
                        self._remarked_files.add(path)

    def _on_turn_done(self, ev: dict) -> None:
        reason = str(ev.get("reason", ""))
        duration = self._clock() - self._turn_started if self._turn_started else 0.0
        self._note_line("ход " + str(self._turn) + " кончился: " + reason +
                        ", " + str(int(duration)) + " с")
        if reason in ("error", "limit"):
            self._consider("ход кончился плохо: " + reason)
        elif duration >= self._long_turn and self._turn_started:
            self._consider("долгий ход: " + str(int(duration)) + " секунд")

    def _name_of(self, call_id) -> str:
        return self._calls.get(str(call_id), "")

    def _note_line(self, line: str) -> None:
        self._digest.append(line)

    # --- речь ------------------------------------------------------------------

    def _gate_open(self) -> bool:
        if self._last_say_at is None:
            return True
        if self._clock() - self._last_say_at < self._interval:
            return False
        return self._turn - self._last_say_turn >= self._every_turns

    def _consider(self, trigger: str) -> None:
        """Значимое событие: спросить мозг, если он есть и дроссель открыт."""
        if self._brain is None or not self._gate_open():
            return
        parts = []
        if str(self._get("nibbler.context", "full")) == "full" and self._full:
            parts.append("Стенограмма сессии (что видел главный агент; старое могло срезаться):\n"
                         + "\n".join(self._full))
        else:
            if self._request:
                parts.append("Задача человека: " + self._request)
            if self._digest:
                parts.append("Что происходило (свежее внизу):\n" + "\n".join(self._digest))
        parts.append("Повод высказаться: " + trigger + ".")
        digest = "\n\n".join(parts)
        if self._threaded:
            if self.busy():
                return  # прошлая мысль ещё не готова — эту пропускаем
            import threading

            system = self._system_prompt()

            def work():
                try:
                    self._ready.append(self._brain(system, digest))
                except Exception:  # noqa: BLE001 — мозг упал: молчим
                    pass

            self._thread = threading.Thread(target=work, daemon=True, name="luna-nibbler")
            self._thread.start()
            return
        try:
            answer = self._brain(self._system_prompt(), digest)
        except Exception:  # noqa: BLE001 — мозг упал: молчим
            return
        self._on_answer(answer)

    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def drain(self) -> None:
        """Главный цикл чата: готовые ответы мозга — в реплики (в своём потоке)."""
        while self._ready:
            try:
                self._on_answer(self._ready.pop(0))
            except Exception:  # noqa: BLE001
                pass

    def _on_answer(self, answer) -> None:
        parsed = _parse_answer(answer)
        if parsed is None:
            return
        text, severity = parsed
        self._say(text, severity, "model")

    def _say(self, text: str, severity: str, trigger: str) -> bool:
        if not self._gate_open():
            return False
        text = " ".join(str(text).split())[:MAX_TEXT]
        if not text:
            return False
        to_agent_mode = str(self._get("nibbler.to_agent", "serious"))
        routed = (to_agent_mode == "all"
                  or (to_agent_mode == "serious" and severity == "serious"))
        event = {"type": "nibbler.say", "text": text, "severity": severity,
                 "to": "agent" if routed else "human", "trigger": trigger}
        self._last_say_at = self._clock()
        self._last_say_turn = self._turn
        self.said.append(event)
        if routed:
            self._notes.append(text)
        if self._emit is not None:
            self._emit(dict(event))
        return True

    def take_notes(self) -> list:
        """Забрать пометки для главного агента (уйдут в его следующий ход)."""
        notes, self._notes = self._notes, []
        return notes

    # --- промпт ------------------------------------------------------------------

    def _system_prompt(self) -> str:
        parts = [CORE_PROMPT]
        mid = str(self._get("nibbler.manner", "nibbler") or "").strip()
        if mid:
            try:
                from . import manners

                entry = manners.get(self.settings, mid, audience="nibbler")
            except Exception:  # noqa: BLE001 — манеры нет: характер из ядра
                entry = None
            if entry:
                parts.append("Твой характер (манера «" + entry["name"] + "»):\n"
                             + entry["text"].strip())
        return "\n\n".join(parts)


def _args_brief(args: dict) -> str:
    for key in ("path", "command", "query", "url", "pattern"):
        value = args.get(key)
        if value:
            return str(value)[:60]
    return ""


def _parse_answer(answer) -> tuple | None:
    """Ответ мозга → (текст, серьёзность) или None — модель решила молчать."""
    if not answer:
        return None
    line = str(answer).strip().splitlines()[0].strip() if str(answer).strip() else ""
    if not line:
        return None
    severity = "minor"
    if line.startswith("!"):
        severity = "serious"
        line = line.lstrip("!").strip()
    if not line:
        return None
    return line[:MAX_TEXT], severity
