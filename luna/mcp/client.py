# Перенесено из BENDER: bender/mcp/client.py (коммит 6552ed660bfabf5202569f9905212b77dbf6d31a), переписано синхронно и сокращено.
"""MCP без сторонних библиотек: JSON-RPC по stdio и Streamable HTTP.

У Бендера клиент асинхронный; ядро Луны синхронное, поэтому вместо asyncio —
процесс и поток-читатель с очередью. Осторожность та же, что у Бендера:

- нет автоматического повтора tools/call после обрыва: действие могло
  выполниться. Повтор допустим, только если запрос заведомо не ушёл
  (ClosedError.sent=False) или метод идемпотентен (tools/list и служебные);
- встречные запросы сервера без заявленных прав клиента отклоняются
  ошибкой (см. requests.py);
- размер одного ответа ограничен LIMIT.

Что не перенесено: обнаружение server/discover и версии протокола 2026 года
(у Луны — классический initialize), MRTR-раунды ввода и фоновые задачи MCP.
"""

from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request

from ..core.errors import HumanError
from .. import __version__
from .headers import header_value, parameter_headers
from .requests import client_capabilities, error_reply

VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_VERSION = "2025-06-18"
LIMIT = 2 * 1024 * 1024


class RpcError(HumanError):
    def __init__(self, error):
        self.rpc_code = error.get("code")
        super().__init__("mcp_rpc", "MCP-сервер отклонил запрос", "Проверь его настройки и выбранный тул.",
                         detail=str(error.get("message", ""))[:2000])


class ClosedError(HumanError):
    """Соединение оборвалось. sent=False — запрос не был отправлен и повтор безопасен."""

    def __init__(self, sent: bool, detail: str = ""):
        self.sent = sent
        super().__init__("mcp_closed", "MCP-сервер закрыл соединение",
                         "Действие могло выполниться; проверь результат до повторения." if sent
                         else "Запрос не ушёл; можно подключиться заново.",
                         detail=detail[:2000])


def _message(raw):
    if not isinstance(raw, dict) or raw.get("jsonrpc") != "2.0":
        raise HumanError("mcp_protocol", "Ответ сервера не похож на MCP", "Нужен JSON-RPC 2.0.")
    return raw


class StdioTransport:
    """Подпроцесс и построчный JSON-RPC по его stdin/stdout."""

    def __init__(self, argv: list, *, cwd, env: dict, limit: int = LIMIT):
        self.argv = list(argv)
        self.cwd = str(cwd)
        self.env = dict(env)
        self.limit = limit
        self.process: subprocess.Popen | None = None
        self._lines: queue.Queue = queue.Queue()
        self._reader: threading.Thread | None = None
        self.restarts = 0

    # --- жизненный цикл процесса ---

    def start(self) -> None:
        if self.alive():
            return
        self.close()
        try:
            proc = subprocess.Popen(
                self.argv, cwd=self.cwd, env=self.env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, start_new_session=os.name != "nt")
        except OSError as exc:
            raise HumanError("mcp_start", "Не удалось запустить MCP-сервер",
                             "Проверь программу, аргументы и рабочую папку.",
                             detail=str(exc)) from None
        self.process = proc
        self._lines = queue.Queue()
        self._reader = threading.Thread(target=self._pump, args=(proc, self._lines),
                                        name="luna-mcp-stdio", daemon=True)
        self._reader.start()

    def _pump(self, proc, out: queue.Queue) -> None:
        try:
            for line in proc.stdout:
                out.put(line)
        except (OSError, ValueError):
            pass
        finally:
            out.put(None)  # EOF: процесс завершился или закрыл вывод

    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def restart(self) -> None:
        self.restarts += 1
        self.close()
        self.start()

    def close(self) -> None:
        proc, self.process = self.process, None
        if proc is None:
            return
        if proc.poll() is None:
            try:
                if os.name != "nt":
                    os.killpg(proc.pid, signal.SIGTERM)
                else:
                    proc.terminate()
            except (ProcessLookupError, OSError):
                pass
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    if os.name != "nt":
                        os.killpg(proc.pid, signal.SIGKILL)
                    else:
                        proc.kill()
                except (ProcessLookupError, OSError):
                    pass
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        for stream in (proc.stdin, proc.stdout):
            try:
                if stream is not None:
                    stream.close()
            except (OSError, ValueError):
                pass

    # --- обмен ---

    def exchange(self, msg, timeout: float, on_progress=None, *, schema=None):  # noqa: ARG002 — общий интерфейс транспортов
        if self.process is None:
            self.start()
        if not self.alive():
            raise ClosedError(False, "процесс MCP-сервера завершился до запроса")
        try:
            self.process.stdin.write(json.dumps(msg, ensure_ascii=False).encode("utf-8") + b"\n")
            self.process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            raise ClosedError(False, str(exc)) from None
        if "id" not in msg:
            return None
        end = time.monotonic() + max(0.1, timeout)
        for _ in range(256):
            remaining = end - time.monotonic()
            if remaining <= 0:
                self.close()
                raise HumanError("mcp_timeout", "MCP-сервер не ответил вовремя",
                                 "Увеличь timeout сервера в mcp.json или проверь, не завис ли он.")
            try:
                line = self._lines.get(timeout=remaining)
            except queue.Empty:
                self.close()
                raise HumanError("mcp_timeout", "MCP-сервер не ответил вовремя",
                                 "Увеличь timeout сервера в mcp.json или проверь, не завис ли он.") from None
            if line is None:
                raise ClosedError(True, "процесс MCP-сервера закрыл вывод посреди ответа")
            if len(line) > self.limit:
                self.close()
                raise HumanError("mcp_size", "Слишком большой ответ MCP", "Сократи результат на стороне сервера.")
            try:
                received = _message(json.loads(line.decode("utf-8")))
            except (ValueError, UnicodeDecodeError) as exc:
                raise HumanError("mcp_protocol", "Ответ сервера не похож на MCP",
                                 "Нужен JSON-RPC 2.0 построчно.", detail=str(exc)[:500]) from None
            if received.get("id") == msg["id"] and "method" not in received:
                return received
            if "method" in received and "id" in received:
                self._reply(error_reply(received))
            elif received.get("method") == "notifications/progress" and on_progress:
                on_progress(str((received.get("params") or {}).get("message") or "MCP-сервер работает")[:500])
        raise HumanError("mcp_protocol", "Сервер прислал слишком много посторонних сообщений", "Проверь его журнал.")

    def _reply(self, msg) -> None:
        """Ответ на встречный запрос сервера; ошибка отправки здесь не главная."""
        try:
            if self.process is not None and self.process.stdin is not None:
                self.process.stdin.write(json.dumps(msg, ensure_ascii=False).encode("utf-8") + b"\n")
                self.process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass


class HttpTransport:
    """Streamable HTTP: один POST на сообщение, ответ — JSON или SSE-поток."""

    def __init__(self, url: str, headers: dict, *, opener=None, limit: int = LIMIT):
        self.url = url
        self.headers = dict(headers)
        self.limit = limit
        self._opener = opener or urllib.request.urlopen
        self.session = None
        self.protocol_version = None

    def alive(self) -> bool:
        return True  # HTTP без состояния процесса; сессия проверяется ответами

    def restart(self) -> None:
        self.session = None

    def close(self) -> None:
        self.session = None

    def _post(self, msg, timeout: float, headers: dict):
        body = json.dumps(msg, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
        try:
            return self._opener(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read(2000).decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001 — тело ошибки читаем как получится
                detail = ""
            raise HumanError("mcp_http", "MCP-сервер ответил HTTP " + str(exc.code),
                             "Проверь адрес, доступ и секреты; редиректы автоматически не выполняются.",
                             detail=detail) from None
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise HumanError("mcp_transport", "Не удалось связаться с MCP-сервером",
                             "Проверь адрес и доступность. Действие могло выполниться; перед повтором проверь результат.",
                             detail=str(exc)[:2000]) from None

    def exchange(self, msg, timeout: float, on_progress=None, *, schema=None):
        headers = {key: value for key, value in self.headers.items()
                   if not key.lower().startswith("mcp-") and key.lower() != "accept"}
        headers["Accept"] = "application/json, text/event-stream"
        headers["Content-Type"] = "application/json"
        if self.protocol_version:
            headers["MCP-Protocol-Version"] = self.protocol_version
        headers["Mcp-Method"] = msg["method"]
        params = msg.get("params") or {}
        if msg["method"] == "tools/call":
            headers["Mcp-Name"] = header_value(params.get("name", ""))
            if schema is not None:
                headers.update(parameter_headers(schema, params.get("arguments") or {}))
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        resp = self._post(msg, timeout, headers)
        with resp:
            if msg["method"] == "initialize":
                self.session = resp.headers.get("mcp-session-id") if resp.headers else None
            if "id" not in msg:
                resp.read(self.limit + 1)
                return None
            ctype = ""
            if resp.headers is not None:
                ctype = (resp.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
            if ctype == "text/event-stream":
                return self._from_sse(resp, msg, timeout, on_progress)
            raw = resp.read(self.limit + 1)
            if len(raw) > self.limit:
                raise HumanError("mcp_size", "Слишком большой ответ MCP", "Сократи результат на стороне сервера.")
            if not raw:
                raise ClosedError(True, "пустой ответ HTTP")
            try:
                return _message(json.loads(raw.decode("utf-8")))
            except (ValueError, UnicodeDecodeError) as exc:
                raise HumanError("mcp_protocol", "MCP-сервер не прислал JSON или SSE",
                                 "Проверь адрес MCP-эндпоинта.", detail=str(exc)[:500]) from None

    def _from_sse(self, resp, msg, timeout: float, on_progress):
        from ..net.sse import parse_sse_lines

        def lines():
            total = 0
            while True:
                raw = resp.readline()
                if not raw:
                    return
                total += len(raw)
                if total > self.limit:
                    raise HumanError("mcp_size", "Слишком большой поток MCP", "Сократи результат на стороне сервера.")
                yield raw.decode("utf-8", errors="replace")

        for event in parse_sse_lines(lines()):
            if not event.data:
                continue
            received = _message(json.loads(event.data))
            if received.get("id") == msg["id"] and "method" not in received:
                return received
            if "method" in received and "id" in received:
                self._post_detached(error_reply(received), timeout)
            elif received.get("method") == "notifications/progress" and on_progress:
                on_progress(str((received.get("params") or {}).get("message") or "MCP-сервер работает")[:500])
        raise ClosedError(True, "поток MCP оборвался без результата")

    def _post_detached(self, msg, timeout: float) -> None:
        """Ответ на встречный запрос сервера отдельным POST; ошибка здесь не главная."""
        headers = {key: value for key, value in self.headers.items()
                   if not key.lower().startswith("mcp-")}
        headers["Accept"] = "application/json, text/event-stream"
        headers["Content-Type"] = "application/json"
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        try:
            resp = self._post(msg, timeout, headers)
            with resp:
                resp.read(self.limit + 1)
        except HumanError:
            pass


class Client:
    """Сессия MCP поверх транспорта: initialize, tools/list, tools/call."""

    def __init__(self, transport, *, timeout: float = 30.0):
        self.transport = transport
        self.timeout = float(timeout)
        self.version = None
        self.info: dict = {}
        self.capabilities: dict = {}
        self._next = 0
        self._lock = threading.Lock()

    # --- базовый обмен ---

    def request(self, method: str, params=None, *, notify: bool = False,
                progress=None, schema=None, idempotent: bool = False):
        """Один запрос с одним возможным переподключением.

        Переподключение (перезапуск транспорта + initialize) делается, если
        запрос не ушёл (ClosedError.sent=False) или метод идемпотентен;
        tools/call после ушедшего запроса не повторяется никогда.
        """
        with self._lock:
            for attempt in (0, 1):
                try:
                    return self._once(method, params, notify=notify, progress=progress, schema=schema)
                except ClosedError as exc:
                    if notify or attempt == 1 or (exc.sent and not idempotent):
                        raise
                    self._reconnect()
            raise AssertionError("unreachable")

    def _reconnect(self) -> None:
        self.version = None
        self.capabilities = {}
        self.transport.restart()
        self._handshake()

    def _once(self, method, params=None, *, notify: bool = False, progress=None, schema=None):
        self._next += 1
        mid = "luna-" + str(self._next)
        msg = {"jsonrpc": "2.0", "method": method, "params": dict(params or {})}
        if not notify:
            msg["id"] = mid
        try:
            response = self.transport.exchange(msg, self.timeout, progress, schema=schema)
        except HumanError:
            raise
        except (OSError, ValueError) as exc:
            raise HumanError("mcp_transport", "Не удалось получить ответ MCP-сервера",
                             "Проверь его запуск и доступность. Действие могло выполниться; перед повтором проверь результат.",
                             detail=str(exc)[:2000]) from None
        if notify:
            return {}
        if not isinstance(response, dict) or response.get("id") != mid:
            raise HumanError("mcp_protocol", "Ответ MCP относится к другому запросу", "Переподключи сервер.")
        if "error" in response:
            if not isinstance(response["error"], dict):
                raise HumanError("mcp_protocol", "Некорректная ошибка MCP", "Проверь сервер.")
            raise RpcError(response["error"])
        result = response.get("result")
        if not isinstance(result, dict):
            raise HumanError("mcp_protocol", "В ответе MCP нет результата", "Проверь сервер.")
        return result

    # --- сессия ---

    def connect(self) -> None:
        """Поднять транспорт (если мёртв) и выполнить initialize."""
        with self._lock:
            if self.version and self.transport.alive():
                return
            if not self.transport.alive():
                self.transport.restart()
            self._handshake()

    def _handshake(self) -> None:
        initialized = self._once("initialize", {
            "protocolVersion": DEFAULT_VERSION,
            "capabilities": client_capabilities(),
            "clientInfo": {"name": "luna", "version": __version__},
        })
        version = initialized.get("protocolVersion")
        if version not in VERSIONS:
            raise HumanError("mcp_version", "Версия MCP-сервера не поддерживается", str(version))
        self.version = version
        self.info = initialized.get("serverInfo") or {}
        self.capabilities = initialized.get("capabilities") or {}
        if isinstance(self.transport, HttpTransport):
            self.transport.protocol_version = version
        self._once("notifications/initialized", notify=True)

    def tools(self) -> list:
        """Полный каталог тулов с ограниченной пагинацией (как у Бендера)."""
        self.connect()
        out, cursor, seen = [], None, set()
        for _page in range(20):
            result = self.request("tools/list", {"cursor": cursor} if cursor else {},
                                  idempotent=True)
            page = result.get("tools")
            if not isinstance(page, list) or any(not isinstance(t, dict) for t in page):
                raise HumanError("mcp_tools", "Некорректный каталог MCP", "Проверь сервер: tools/list.")
            out.extend(page)
            if len(out) > 200:
                raise HumanError("mcp_tools", "Слишком большой каталог MCP", "Не более 200 тулов на сервер.")
            cursor = result.get("nextCursor")
            if not cursor:
                return out
            if not isinstance(cursor, str) or len(cursor) > 8192 or cursor in seen:
                break
            seen.add(cursor)
        raise HumanError("mcp_tools", "Слишком большой или зацикленный каталог MCP", "Не более 20 страниц: tools/list.")

    def call_tool(self, name: str, arguments: dict, *, progress=None, schema=None) -> dict:
        self.connect()
        return self.request("tools/call", {"name": name, "arguments": arguments or {}},
                            progress=progress, schema=schema)

    def close(self) -> None:
        self.transport.close()
        self.version = None
        self.capabilities = {}
