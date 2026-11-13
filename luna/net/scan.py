"""Скан сети на API моделей: что вокруг отвечает как провайдер ИИ.

Два шага, оба параллельные:
1. TCP — какие порты открыты на целях (свой компьютер, имена из hosts,
   соседи по локальным подсетям /24).
2. HTTP — каждому открытому порту задаём вопросы провайдера
   (/v1/models, /models, /api/tags, /api/v1/models): кто ответил списком
   моделей или честным 401 — тот API.

Режимы: fast — широкий список типичных портов на всех целях; deep —
все 65535 портов, но только на именованных целях (свой компьютер и
hosts), иначе подсеть × весь диапазон — миллионы попыток.
Прокси не участвуют: смотрим свою сеть напрямую.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import os
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

# Порты серверов моделей и прокси, которые встречаются в дикой природе:
# Ollama, LM Studio, llama.cpp, vLLM, TGI, LocalAI, KoboldCpp, Jan,
# text-generation-webui, LiteLLM, OmniRoute, GPT4All и обычные веб-порты.
FAST_PORTS = (
    80, 443, 1234, 1337, 3000, 3001, 4000, 4001, 4891, 5000, 5001, 5005,
    5050, 5555, 6006, 7000, 7860, 7861, 8000, 8001, 8002, 8003, 8008, 8010,
    8080, 8081, 8082, 8088, 8090, 8100, 8188, 8443, 8880, 8888, 9000, 9090,
    9997, 10000, 11434, 11435, 12345, 1338, 18080, 20128, 30000, 39281,
    50051, 52415, 60000,
)
PROBE_PATHS = ("/v1/models", "/models", "/api/tags", "/api/v1/models")


@dataclass
class Found:
    base_url: str
    kind: str = "openai"
    models: list = field(default_factory=list)
    needs_key: bool = False
    host: str = ""
    port: int = 0
    latency_ms: float = 0.0
    hint: str = ""        # кто это, по ответу: ollama, lmstudio, сервер из заголовка
    aliases: list = field(default_factory=list)  # другие адреса того же сервиса

    @property
    def label(self) -> str:
        bits = [self.base_url]
        if self.hint:
            bits.append(self.hint)
        bits.append("нужен ключ" if self.needs_key else
                    "моделей: " + str(len(self.models)))
        return " · ".join(bits)


# --- цели ---------------------------------------------------------------------


def hosts_file_names() -> list:
    """Имена из системного hosts (кроме localhost) — узлы своей сети."""
    path = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/drivers/etc/hosts" \
        if sys.platform == "win32" else Path("/etc/hosts")
    names = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return names
    for line in text.splitlines():
        line = line.split("#", 1)[0].split()
        if len(line) < 2:
            continue
        for name in line[1:]:
            low = name.lower()
            if low in ("localhost", "localhost.localdomain", "ip6-localhost",
                       "ip6-loopback", "broadcasthost") or low in names:
                continue
            names.append(low)
    return names


def local_ipv4() -> list:
    """Свои IPv4-адреса (без петли): по ним строятся соседние /24."""
    found = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(info[4][0])
    except OSError:
        pass
    # Адрес исходящего интерфейса — UDP-«соединение» пакетов не шлёт.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            found.add(s.getsockname()[0])
    except OSError:
        pass
    return sorted(ip for ip in found if not ip.startswith(("127.", "169.254.")))


def subnet_hosts(ips: list) -> list:
    out = []
    for ip in ips:
        try:
            net = ipaddress.ip_network(ip + "/24", strict=False)
        except ValueError:
            continue
        if not net.is_private:
            continue  # чужие сети не сканируем
        out.extend(str(h) for h in net.hosts())
    return out


def targets(mode: str = "fast", extra: list | None = None,
            subnets: bool = True) -> list:
    """[(хост, порты)] для скана. Повторы адресов убраны."""
    named = ["127.0.0.1"] + hosts_file_names() + list(extra or [])
    plan, seen = [], set()
    all_ports = tuple(range(1, 65536))
    for host in named:
        if host in seen:
            continue
        seen.add(host)
        plan.append((host, all_ports if mode == "deep" else FAST_PORTS))
    resolved = set()
    for host in named:
        try:
            resolved.add(socket.gethostbyname(host))
        except OSError:
            pass
    if subnets:
        for host in subnet_hosts(local_ipv4()):
            if host in seen or host in resolved:
                continue
            seen.add(host)
            plan.append((host, FAST_PORTS))
    return plan


# --- шаг 1: TCP ---------------------------------------------------------------


def _open(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# --- шаг 2: HTTP --------------------------------------------------------------


def _get(host: str, port: int, path: str, tls: bool, timeout: float):
    conn_cls = http.client.HTTPSConnection if tls else http.client.HTTPConnection
    kwargs = {"timeout": timeout}
    if tls:
        import ssl

        # Свои узлы часто с самоподписанным сертификатом: тут мы только
        # узнаём, API ли это; ключи по этому соединению не шлём.
        kwargs["context"] = ssl._create_unverified_context()  # noqa: S323
    conn = conn_cls(host, port, **kwargs)
    try:
        conn.request("GET", path, headers={"Accept": "application/json",
                                           "User-Agent": "luna-scan"})
        resp = conn.getresponse()
        body = resp.read(512 * 1024)
        return resp.status, dict(resp.getheaders()), body
    finally:
        conn.close()


def _models_from(data) -> list:
    if isinstance(data, dict):
        items = data.get("data")
        if items is None:
            items = data.get("models")
        if isinstance(items, list):
            out = []
            for item in items:
                if isinstance(item, dict):
                    name = item.get("id") or item.get("name") or item.get("model") \
                        or item.get("key")
                    if name:
                        out.append(str(name))
                elif isinstance(item, str):
                    out.append(item)
            return out
    return []


def probe(host: str, port: int, timeout: float = 2.5):
    """Found, если на порту API моделей; иначе None."""
    tls_order = (True, False) if port in (443, 8443) else (False, True)
    for tls in tls_order:
        scheme = "https" if tls else "http"
        for path in PROBE_PATHS:
            started = time.perf_counter()
            try:
                status, headers, body = _get(host, port, path, tls, timeout)
            except (OSError, http.client.HTTPException, ValueError):
                break  # этой схемой порт не говорит — следующая схема
            ms = (time.perf_counter() - started) * 1000
            server = headers.get("Server") or headers.get("server") or ""
            try:
                data = json.loads(body.decode("utf-8", "replace") or "null")
            except ValueError:
                data = None
            root = scheme + "://" + host + ":" + str(port)
            if status == 200:
                models = _models_from(data)
                if not models and not (isinstance(data, dict) and
                                       ("data" in data or "models" in data)):
                    continue  # 200, но не список моделей — веб-страница, дальше
                if path == "/api/tags":
                    return Found(root + "/v1", "openai", models, False, host, port,
                                 ms, "ollama")
                if path == "/api/v1/models":
                    return Found(root + "/v1", "openai", models, False, host, port,
                                 ms, "lmstudio")
                base = root + ("/v1" if path.startswith("/v1") else "")
                return Found(base, "openai", models, False, host, port, ms, server[:30])
            if status in (401, 403) and isinstance(data, dict) and path == "/v1/models":
                # Честный отказ JSON-ом на пути провайдера — API за ключом.
                return Found(root + "/v1", "openai", [], True, host, port, ms, server[:30])
    return None


# --- весь скан ----------------------------------------------------------------


def scan(mode: str = "fast", *, extra_hosts=None, subnets: bool = True,
         workers: int = 1024, connect_timeout: float = 0.35,
         progress=None, cancel: threading.Event | None = None,
         plan: list | None = None) -> list:
    """Список Found по всей сети. progress(сделано, всего, найдено) — по ходу."""
    plan = plan if plan is not None else targets(mode, extra_hosts, subnets)
    # Имя → IP один раз на хост, а не на каждый из тысяч портов;
    # не резолвится — узел выключен или имени нет, его не стучим.
    with ThreadPoolExecutor(max_workers=64) as dns:
        ips = dict(zip([h for h, _p in plan],
                       dns.map(lambda h: _resolve_or_none(h), [h for h, _p in plan])))
    jobs = [(host, port) for host, ports in plan if ips.get(host) for port in ports]
    total = len(jobs)
    done = 0
    open_ports: list = []
    found: list = []

    def tick():
        if progress is not None:
            try:
                progress(done, total, len(found))
            except Exception:  # noqa: BLE001 — экран не роняет скан
                pass

    def check(job):
        return job, _open(ips[job[0]], job[1], connect_timeout)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        # Порциями: отмена срабатывает между ними, а миллион задач deep
        # не висит в памяти разом.
        chunk = max(256, workers * 8)
        for start in range(0, total, chunk):
            if cancel is not None and cancel.is_set():
                break
            for job, is_open in pool.map(check, jobs[start:start + chunk]):
                if is_open:
                    open_ports.append(job)
            done = min(total, start + chunk)
            tick()
        tick()
        probes = [pool.submit(probe, host, port) for host, port in open_ports]
        for fut in as_completed(probes):
            if cancel is not None and cancel.is_set():
                break
            try:
                result = fut.result()
            except Exception:  # noqa: BLE001
                result = None
            if result is not None:
                found.append(result)
                tick()
    return dedupe(found)


def _resolve_or_none(host: str):
    try:
        return socket.gethostbyname(host)
    except OSError:
        return None


def _resolve(host: str) -> str:
    try:
        return socket.gethostbyname(host)
    except OSError:
        return host


def dedupe(found: list) -> list:
    """Один сервис — одна строка: склеиваем адреса, ведущие в одно место.

    Свой компьютер виден как 127.0.0.1, свои IP, docker-имена и своё имя
    из hosts — это всё «localhost». У чужого узла имя из hosts лучше IP.
    """
    own = set(local_ipv4()) | {"127.0.0.1"}
    groups: dict = {}
    for item in found:
        ip = _resolve(item.host)
        key = ("self" if ip in own or ip.startswith("127.") else ip, item.port)
        groups.setdefault(key, []).append(item)
    out = []
    for (where, port), items in groups.items():
        def rank(f):
            if where == "self":
                return (0 if f.host in ("127.0.0.1", "localhost") else 1, f.host)
            is_ip = all(part.isdigit() for part in f.host.split("."))
            return (1 if is_ip else 0, f.host)
        items.sort(key=rank)
        best = items[0]
        if where == "self":
            best.base_url = best.base_url.replace("//" + best.host + ":", "//localhost:", 1)
            best.host = "localhost"
        best.aliases = sorted({f.host for f in items[1:]} - {best.host})
        # Модели и ключ — от самого полного ответа группы.
        richest = max(items, key=lambda f: len(f.models))
        best.models = richest.models
        best.needs_key = all(f.needs_key for f in items)
        out.append(best)
    out.sort(key=lambda f: (f.host != "localhost", f.host, f.port))
    return out
