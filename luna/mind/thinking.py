"""Пересказ рассуждений быстрой ролью (ЗАДАЧА-19).

Пока главная модель думает, накопленные куски её рассуждений раз в
ui.thinking_summary_s секунд (и один раз по концу размышления)
уходят роли fast: та отвечает одной строкой по-русски — что модель
сейчас обдумывает. Строки публикуются событием thinking.summary и
копятся в записи «думает» в ленте чата; полный текст рассуждений —
по Ctrl+O.

Всё в отдельном потоке: основной стрим пересказ не задерживает ни на
секунду. Кусок перед отправкой обрезается (дешёвая роль, не нужен весь
монолог). Быстрая роль не настроена или провайдер не отдаёт reasoning —
пересказа просто нет: чат честно показывает «думает N с».
"""

from __future__ import annotations

import threading
import time

_CHUNK_CAP = 6000  # символов рассуждений в одном запросе пересказа
_LINE_CAP = 200    # символов в строке пересказа

_SYSTEM = ("Ты пересказываешь ход рассуждений другой модели для человека, "
           "который ждёт её ответа. Ответь ОДНОЙ строкой по-русски, не "
           "длиннее 160 знаков: что модель сейчас обдумывает. Без кавычек, "
           "без вступлений вроде «модель думает», сразу суть.")


class ThinkingSummarizer:
    """Наблюдает куски agent.thinking и зовёт fast-роль по таймеру."""

    def __init__(self, *, settings, router, publish, usage=None,
                 clock=time.monotonic):
        self.settings = settings
        self.router = router
        self.publish = publish
        self.usage = usage  # необязательный учёт расхода пересказа
        self._clock = clock
        self._lock = threading.Lock()
        self._buf = ""
        self._active = False
        self._inflight = False
        self._unavailable = False  # fast не настроена — больше не пробуем
        self._started_at = 0.0
        self._last_at = 0.0
        self._threads: list = []

    # --- выключатели и ритм ---

    def enabled(self) -> bool:
        if self._unavailable:
            return False
        if not self.settings.get("ui.show_thinking"):
            return False
        return str(self.settings.get("ui.thinking") or "") == "summary"

    def _interval(self) -> float:
        try:
            return max(1.0, float(self.settings.get("ui.thinking_summary_s")))
        except (TypeError, ValueError):
            return 15.0

    # --- вход из цикла агента ---

    def feed(self, text: str) -> None:
        """Очередной кусок рассуждений (стрим главной модели)."""
        if not text or not self.enabled():
            return
        now = self._clock()
        spawn = False
        with self._lock:
            if not self._active:
                self._active = True
                self._started_at = now
                self._last_at = now
            self._buf += text
            if (now - self._last_at >= self._interval()
                    and self._buf.strip() and not self._inflight):
                spawn = True
        if spawn:
            self._spawn()

    def flush(self) -> None:
        """Конец размышления шага: остаток — в последний пересказ шага."""
        with self._lock:
            pending = bool(self._active and self._buf.strip()
                           and not self._inflight)
            self._active = False
        if pending and self.enabled():
            self._spawn()

    # --- работа ---

    def _spawn(self) -> None:
        with self._lock:
            chunk = self._buf[-_CHUNK_CAP:]
            self._buf = ""
            self._inflight = True
            self._last_at = self._clock()
            seconds = max(0, int(self._clock() - self._started_at))
        thread = threading.Thread(target=self._run, args=(chunk, seconds),
                                  daemon=True, name="luna-thinking-summary")
        with self._lock:
            self._threads = [t for t in self._threads if t.is_alive()]
            self._threads.append(thread)
        thread.start()

    def _run(self, chunk: str, seconds: int) -> None:
        try:
            result = self.router.chat(
                [{"role": "system", "content": _SYSTEM},
                 {"role": "user", "content": chunk}],
                None, role="fast")
            line = (result.text or "").strip().splitlines()
            line = line[0].strip() if line else ""
            if len(line) > _LINE_CAP:
                line = line[:_LINE_CAP - 1] + "…"
            if self.usage is not None:
                try:
                    self.usage(result)
                except Exception:
                    pass  # учёт не должен ронять пересказ
            if line:
                self.publish({"type": "thinking.summary",
                              "text": line, "seconds": seconds})
        except Exception as exc:  # noqa: BLE001 — пересказ не критичен
            code = getattr(exc, "code", "")
            if code == "model_not_set":
                # Быстрой роли нет вовсе — дальше честно молчим.
                self._unavailable = True
        finally:
            with self._lock:
                self._inflight = False

    def wait_idle(self, timeout: float = 10.0) -> None:
        """Для тестов и аккуратного завершения: дождаться пересказов."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                alive = [t for t in self._threads if t.is_alive()]
            if not alive:
                return
            for thread in alive:
                thread.join(timeout=0.1)
