# Перенесено из BENDER: bender/net/http_client.py (коммит 6552ed660bfabf5202569f9905212b77dbf6d31a), переписано синхронно на urllib.
"""HTTP-клиент на стандартной библиотеке: JSON-запросы и стрим строк для SSE.

Транспорт подменяем в тестах (фальшивая сеть): конструктор принимает opener —
функцию (Request, timeout) -> ответ с методами read/readline/headers/status.
Секреты в заголовках сюда приходят готовыми и никуда не логируются.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from typing import Iterator

# Шаг опроса чтения стрима: readline просыпается не реже раза в столько
# секунд и проверяет отмену — прерывание не зависит от того, умеет ли
# платформа будить чужой recv закрытием сокета (Windows — не умеет).
STREAM_POLL_SEC = 0.25


def _find_socket(resp):
    """Сокет под ответом urllib: идём по цепочке fp/raw/_sock вглубь.

    Нужен для shutdown и для короткого таймаута чтения стрима. Для
    несокетных ответов (подмена транспорта в тестах) — None."""
    stack = [resp]
    seen: set = set()
    while stack:
        obj = stack.pop()
        if obj is None or id(obj) in seen:
            continue
        seen.add(id(obj))
        if callable(getattr(obj, "shutdown", None)) and \
                callable(getattr(obj, "settimeout", None)):
            return obj
        for attr in ("fp", "raw", "_sock", "sock"):
            stack.append(getattr(obj, attr, None))
    return None


def _force_close(resp) -> None:
    """Закрыть ответ так, чтобы поток, висящий в readline(), проснулся.

    Одного close() мало (на Linux читающий поток спал дальше до конца
    ответа сервера) — нужен shutdown сокета. На Windows и этого мало
    (красный CI ЗАДАЧИ-18): надёжное пробуждение даёт не закрытие, а
    короткий таймаут чтения в stream_lines — опрос с проверкой отмены,
    а shutdown здесь остаётся ускорением, где срабатывает."""
    sock = _find_socket(resp)
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except Exception:  # noqa: BLE001 — закрытие не должно падать
            pass
    try:
        resp.close()
    except Exception:  # noqa: BLE001
        pass

from ..core.errors import HumanError


class HttpError(HumanError):
    """Ошибка HTTP: статус и тело ответа провайдера (без заголовков и ключей)."""

    def __init__(self, status: int, body: str, url: str = ""):
        detail = body[:2000] if body else ""
        super().__init__(
            "http_error",
            "Провайдер ответил ошибкой " + str(status),
            "Проверь ключ, модель и адрес провайдера (luna models, luna setup).",
            detail=detail,
            status=status,
        )
        self.http_status = status
        self.body = body or ""


def _read_body(resp) -> str:
    try:
        raw = resp.read()
    except Exception:  # noqa: BLE001 — тело ошибки читаем как получится
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return str(raw or "")


class HttpClient:
    def __init__(self, opener=None, timeout: float = 120.0):
        self._opener = opener or urllib.request.urlopen
        self.timeout = timeout
        # Точка отмены стримов (ЗАДАЧА-18): идущие потоковые ответы и
        # поколение отмены. cancel_active() закрывает ответы из другого
        # потока — readline() в потоке стрима тут же возвращает конец, и
        # адаптер честно собирает частичный результат, а не висит до
        # таймаута. Поколение нужно, чтобы отмена, пришедшая между
        # подключением и регистрацией ответа, тоже сработала.
        self._lock = threading.Lock()
        self._active: set = set()
        self._cancel_gen = 0

    def cancel_active(self) -> None:
        """Оборвать все идущие стримы этого клиента (прерывание хода)."""
        with self._lock:
            self._cancel_gen += 1
            actives = list(self._active)
        for resp in actives:
            _force_close(resp)

    def _request(self, url: str, data: bytes | None, headers: dict, timeout: float | None):
        req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
        try:
            return self._opener(req, timeout=timeout or self.timeout)
        except urllib.error.HTTPError as exc:
            raise HttpError(exc.code, _read_body(exc), url) from None
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise HumanError(
                "network",
                "Не удалось связаться с провайдером",
                "Проверь сеть и адрес провайдера (luna setup).",
                detail=str(exc),
                status=502,
            ) from None

    def post_json(self, url: str, payload: dict, headers: dict | None = None, timeout: float | None = None) -> dict:
        hdrs = {"Content-Type": "application/json", "Accept": "application/json"}
        hdrs.update(headers or {})
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        resp = self._request(url, body, hdrs, timeout)
        text = _read_body(resp)
        try:
            data = json.loads(text) if text else {}
        except ValueError:
            raise HumanError("provider_bad_json", "Провайдер вернул не JSON",
                             "Проверь адрес провайдера: это должен быть API, а не веб-страница.",
                             detail=text[:500]) from None
        return data if isinstance(data, dict) else {"data": data}

    def get_json(self, url: str, headers: dict | None = None, timeout: float | None = None) -> dict:
        hdrs = {"Accept": "application/json"}
        hdrs.update(headers or {})
        resp = self._request(url, None, hdrs, timeout)
        text = _read_body(resp)
        try:
            data = json.loads(text) if text else {}
        except ValueError:
            raise HumanError("provider_bad_json", "Провайдер вернул не JSON",
                             "Проверь адрес провайдера.", detail=text[:500]) from None
        return data if isinstance(data, dict) else {"data": data}

    def stream_lines(self, url: str, payload: dict, headers: dict | None = None,
                     timeout: float | None = None) -> Iterator[str]:
        """POST со стримом: отдаёт строки ответа по мере прихода (для SSE)."""
        hdrs = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        hdrs.update(headers or {})
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        read_timeout = timeout or self.timeout
        resp = self._request(url, body, hdrs, timeout)
        with self._lock:
            gen = self._cancel_gen
            self._active.add(resp)
        # Чтение — опросом: сокету короткий таймаут, readline просыпается
        # по TimeoutError и проверяет отмену и срок молчания. Подключение
        # при этом шло с полным таймаутом (TLS не страдает), короткий —
        # только на чтение уже открытого ответа.
        sock = _find_socket(resp)
        if sock is not None:
            try:
                sock.settimeout(STREAM_POLL_SEC)
            except Exception:  # noqa: BLE001 — не вышло: останемся на shutdown
                sock = None
        last_data = time.monotonic()
        try:
            while True:
                with self._lock:
                    if self._cancel_gen != gen:
                        break  # отмена пришла, пока стрим шёл или спал
                try:
                    raw = resp.readline()
                except TimeoutError:
                    if time.monotonic() - last_data > read_timeout:
                        raise  # молчание дольше таймаута — как было раньше
                    continue  # просто очередной шаг опроса
                except Exception:  # noqa: BLE001 — ответ закрыли из cancel_active
                    break
                if not raw:
                    break
                last_data = time.monotonic()
                if isinstance(raw, bytes):
                    yield raw.decode("utf-8", errors="replace")
                else:
                    yield str(raw)
        finally:
            with self._lock:
                self._active.discard(resp)
            _force_close(resp)

    def get_text(self, url: str, headers: dict | None = None, timeout: float | None = None,
                 limit: int = 2_000_000) -> str:
        """GET текста страницы (web_fetch); тело ограничено, чтобы не тащить лишнее."""
        hdrs = {"Accept": "text/html, text/plain, */*", "User-Agent": "Luna/1 (luna-harness)"}
        hdrs.update(headers or {})
        resp = self._request(url, None, hdrs, timeout)
        raw = resp.read(limit + 1)
        if isinstance(raw, bytes):
            return raw[:limit].decode("utf-8", errors="replace")
        return str(raw or "")[:limit]
