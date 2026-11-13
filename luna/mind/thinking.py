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


def _pieces(text: str, cap: int) -> list:
    """Весь текст — кусками не длиннее cap, режем по пробелу у границы."""
    out = []
    while len(text) > cap:
        cut = text.rfind(" ", cap // 2, cap)
        cut = cut if cut > 0 else cap
        out.append(text[:cut])
        text = text[cut:]
    if text.strip():
        out.append(text)
    return out


class ThinkingSummarizer:
    """Наблюдает куски agent.thinking и зовёт fast-роль по таймеру.

    Пересказывается ВСЁ: накопленное режется на куски не длиннее
    _CHUNK_CAP и встаёт в очередь; одновременно идут до
    ui.thinking_summary_parallel пересказов, строки публикуются строго по
    порядку кусков. Раньше брался только хвост в 6000 знаков, а остаток
    на конце размышления терялся, если прошлый пересказ ещё шёл.
    """

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
        self._unavailable = False  # fast не настроена — больше не пробуем
        self._started_at = 0.0
        self._last_at = 0.0
        self._threads: list = []
        self._queue: list = []      # (номер, кусок, секунды) — ждут слота
        self._running = 0
        self._next_seq = 0
        self._pub_seq = 0           # следующий номер к публикации
        self._done: dict = {}       # номер → (строка, секунды, блок)
        # Номер блока мыслей (между flush): строка пересказа помечается им,
        # и чат крепит её к своей записи, даже если пришла после следующей.
        self.block = 1

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

    def _parallel(self) -> int:
        try:
            return max(1, int(self.settings.get("ui.thinking_summary_parallel") or 2))
        except (TypeError, ValueError):
            return 2

    # --- вход из цикла агента ---

    def feed(self, text: str) -> None:
        """Очередной кусок рассуждений (стрим главной модели)."""
        if not text or not self.enabled():
            return
        now = self._clock()
        due = False
        with self._lock:
            if not self._active:
                self._active = True
                self._started_at = now
                self._last_at = now
            self._buf += text
            # Таймер — или кусок уже набрался: быстрый поток мыслей не ждёт.
            if self._buf.strip() and (now - self._last_at >= self._interval()
                                      or len(self._buf) >= _CHUNK_CAP):
                due = True
        if due:
            self._cut()

    def flush(self) -> None:
        """Конец размышления шага: остаток — в последний пересказ шага."""
        with self._lock:
            self._active = False
        if self.enabled():
            self._cut()
        with self._lock:
            self.block += 1

    # --- работа ---

    def _cut(self) -> None:
        with self._lock:
            pieces = _pieces(self._buf, _CHUNK_CAP)
            self._buf = ""
            self._last_at = self._clock()
            seconds = max(0, int(self._clock() - self._started_at))
            for piece in pieces:
                self._queue.append((self._next_seq, piece, seconds, self.block))
                self._next_seq += 1
        self._pump()

    def _pump(self) -> None:
        fresh = []
        with self._lock:
            self._threads = [t for t in self._threads if t.is_alive()]
            while self._queue and self._running < self._parallel():
                thread = threading.Thread(target=self._run, args=self._queue.pop(0),
                                          daemon=True, name="luna-thinking-summary")
                self._running += 1
                self._threads.append(thread)
                fresh.append(thread)
        for thread in fresh:
            thread.start()

    def _run(self, seq: int, chunk: str, seconds: int, block: int = 0) -> None:
        line = ""
        try:
            if self._unavailable:
                return
            result = self.router.chat(
                [{"role": "system", "content": _SYSTEM},
                 {"role": "user", "content": chunk}],
                None, role="fast")
            lines = (result.text or "").strip().splitlines()
            line = lines[0].strip() if lines else ""
            if len(line) > _LINE_CAP:
                line = line[:_LINE_CAP - 1] + "…"
            if self.usage is not None:
                try:
                    self.usage(result)
                except Exception:
                    pass  # учёт не должен ронять пересказ
        except Exception as exc:  # noqa: BLE001 — пересказ не критичен
            if getattr(exc, "code", "") == "model_not_set":
                # Быстрой роли нет вовсе — дальше честно молчим.
                self._unavailable = True
                with self._lock:
                    self._queue.clear()
        finally:
            ready = []
            with self._lock:
                self._running -= 1
                self._done[seq] = (line, seconds, block)
                while self._pub_seq in self._done:
                    ready.append(self._done.pop(self._pub_seq))
                    self._pub_seq += 1
            for text, secs, blk in ready:
                if text:
                    self.publish({"type": "thinking.summary",
                                  "text": text, "seconds": secs, "block": blk})
            self._pump()

    def wait_idle(self, timeout: float = 10.0) -> None:
        """Для тестов и аккуратного завершения: дождаться пересказов."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                alive = [t for t in self._threads if t.is_alive()]
                queued = bool(self._queue)
            if not alive and not queued:
                return
            for thread in alive:
                thread.join(timeout=0.1)
