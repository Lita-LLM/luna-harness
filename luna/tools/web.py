# Перенесено из BENDER: bender/tools/web.py и bender/net/search.py (коммит 6552ed660bfabf5202569f9905212b77dbf6d31a), сокращено.
"""Веб-тулы (web_fetch, web_search) и человеческие: todo, ask_user.

Сеть ходит через ctx.web_fetcher, если задан (тесты), иначе HttpClient.
Проверки адресов (локальная сеть, секреты в URL) — дело Предохранителя,
он решает до исполнения; здесь только работа. Исключение — клетка
работника (ctx.cage): её сетевые границы web_fetch держит сам, на каждом
прыжке редиректа и в момент соединения, потому что Предохранитель видит
только первый адрес, а не то, куда сервер потом отправит.
"""

from __future__ import annotations

import functools
import html
import ipaddress
import re
import socket

from .registry import Tool, ToolResult, truncate_output


class CageRefused(Exception):
    """Клетка не пускает. Нарочно не OSError: urllib заворачивает OSError
    из connect в URLError, а HttpClient — в «нет сети», и причина бы
    потерялась."""


def _resolve(host: str) -> list:
    """Все адреса хоста строками. Отдельной функцией, чтобы тесты
    подменяли DNS, не трогая сеть."""
    try:
        return [str(ipaddress.ip_address(host.split("%", 1)[0]))]
    except ValueError:
        pass
    infos =socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    out = []
    for info in infos:
        ip = str(info[4][0]).split("%", 1)[0]
        if ip not in out:
            out.append(ip)
    return out


def _ip_reason(ip: str) -> str:
    """Пусто — публичный адрес; иначе почему нельзя."""
    try:
        addr = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return "не адрес: " + ip
    # ::ffff:127.0.0.1, 6to4 и Teredo несут IPv4 внутри — судим и по нему.
    if isinstance(addr, ipaddress.IPv6Address):
        teredo = addr.teredo
        for inner in (addr.ipv4_mapped, addr.sixtofour, teredo[1] if teredo else None):
            if inner is not None:
                reason = _ip_reason(str(inner))
                if reason:
                    return reason
    for attr, label in (("is_loopback", "петля"), ("is_private", "частная сеть"),
                        ("is_link_local", "локальный сегмент"),
                        ("is_multicast", "мультикаст"),
                        ("is_unspecified", "пустой адрес"),
                        ("is_reserved", "зарезервированный")):
        if getattr(addr, attr, False):
            return "адрес не публичный (" + label + "): " + str(addr)
    # CGNAT 100.64/10 и прочее, что не private, но и не глобальное.
    if not addr.is_global:
        return "адрес не публичный: " + str(addr)
    return ""


def _checked_ips(host: str) -> list:
    """Адреса хоста, если ВСЕ публичные; иначе CageRefused. Все — потому
    что при двух записях соединение может уйти на приватную."""
    try:
        ips = _resolve(host)
    except (OSError, UnicodeError) as exc:
        raise CageRefused("хост не разрешается: " + host + " (" + str(exc) + ")") from None
    if not ips:
        raise CageRefused("хост не разрешается: " + host)
    for ip in ips:
        reason = _ip_reason(ip)
        if reason:
            raise CageRefused(reason)
    return ips


_BAD_URL_CHARS = re.compile(r"[\x00-\x20\x7f\\]")


def _cage_url_reason(cage, url: str, *, dns: bool = True) -> str:
    """Полная проверка адреса для клетки; пусто — можно."""
    from urllib.parse import urlsplit

    # urlsplit и urllib.request по-разному понимают «\», «@» и управляющие
    # символы — такие адреса клетка не открывает, чтобы проверка и
    # соединение видели один и тот же хост.
    if _BAD_URL_CHARS.search(url):
        return "в адресе недопустимые символы"
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 — кривой порт бросает ValueError
    except ValueError:
        return "кривой адрес"
    if parts.scheme.lower() not in ("http", "https"):
        return "только http:// и https://"
    if "@" in parts.netloc:
        return "логин и пароль в адресе клетке запрещены"
    reason = cage.host_check(url)
    if reason or not dns:
        return reason
    try:
        _checked_ips((parts.hostname or "").rstrip("."))
    except CageRefused as exc:
        return str(exc)
    return ""


def _bracket(host: str) -> str:
    return "[" + host + "]" if ":" in host else host


class _CageConnectionMixin:
    """Соединение только с проверенным адресом: DNS решаем сами и
    подключаемся к тому IP, который проверили, — без окна для DNS
    rebinding между проверкой и connect. SNI и Host остаются именем."""

    def __init__(self, *args, cage=None, **kwargs):
        self._luna_cage = cage
        super().__init__(*args, **kwargs)
        # http.client кладёт socket.create_connection в атрибут экземпляра
        # прямо в __init__ — метод класса он бы перекрыл, ставим после.
        self._create_connection = self._luna_connect

    def _luna_connect(self, address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
                      source_address=None):
        host, port = address
        if self._luna_cage is not None:
            reason = self._luna_cage.host_check("http://" + _bracket(host) + "/")
            if reason:
                raise CageRefused(reason)
        last = None
        for ip in _checked_ips(host):
            try:
                return socket.create_connection((ip, port), timeout, source_address)
            except OSError as exc:
                last = exc
        raise last  # type: ignore[misc]


def _cage_opener(cage):
    """Opener urllib для клетки: без прокси из окружения (прокси на
    localhost обошёл бы проверку адресов), без file:/ftp:/data:, с
    проверкой каждого прыжка редиректа."""
    import http.client
    import urllib.request

    class Conn(_CageConnectionMixin, http.client.HTTPConnection):
        pass

    class ConnS(_CageConnectionMixin, http.client.HTTPSConnection):
        pass

    class HttpH(urllib.request.HTTPHandler):
        def http_open(self, req):
            return self.do_open(functools.partial(Conn, cage=cage), req)

    class HttpsH(urllib.request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(functools.partial(ConnS, cage=cage), req,
                                context=self._context)

    class Redirect(urllib.request.HTTPRedirectHandler):
        def _refuse(self, fp, newurl):
            reason = _cage_url_reason(cage, newurl)
            if reason:
                try:
                    fp.close()
                except Exception:  # noqa: BLE001
                    pass
                raise CageRefused("редирект на " + newurl[:200] + " — " + reason)

        def http_error_302(self, req, fp, code, msg, headers):
            # Проверяем до urllib: он сам режет file:/ftp:, но своей
            # ошибкой HTTPError, и модель не поняла бы, что это клетка.
            from urllib.parse import urljoin

            loc = headers.get("location") or headers.get("uri")
            if loc:
                self._refuse(fp, urljoin(req.full_url, loc))
            return super().http_error_302(req, fp, code, msg, headers)

        http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302

        def redirect_request(self, req, fp, code, msg, headers, newurl):
            self._refuse(fp, newurl)
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    opener = urllib.request.OpenerDirector()
    for handler in (urllib.request.ProxyHandler({}), urllib.request.UnknownHandler(),
                    HttpH(), HttpsH(), urllib.request.HTTPDefaultErrorHandler(),
                    Redirect(), urllib.request.HTTPErrorProcessor()):
        opener.add_handler(handler)
    return opener


def _fetch(ctx, url: str) -> str:
    cage = getattr(ctx, "cage", None)
    if cage is not None:
        reason = _cage_url_reason(cage, url)
        if reason:
            raise CageRefused(reason)
    if ctx.web_fetcher is not None:
        return ctx.web_fetcher(url)
    from ..net.http_client import HttpClient

    if cage is not None:
        return HttpClient(opener=_cage_opener(cage).open).get_text(url, timeout=30.0)
    return HttpClient().get_text(url, timeout=30.0)


def _html_to_text(raw: str) -> str:
    text = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", raw)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", text)).strip()


def _web_fetch(ctx, args) -> ToolResult:
    url = args.get("url", "")
    if not url.startswith(("http://", "https://")):
        return ToolResult(False, "Нужен адрес http:// или https://.")
    if ctx.settings is not None and not ctx.settings.get("web.enabled", True):
        return ToolResult(False, "Веб-тулы выключены настройкой web.enabled.")
    try:
        raw = _fetch(ctx, url)
    except CageRefused as exc:
        return ToolResult(False, "клетка: " + str(exc))
    except Exception as exc:  # noqa: BLE001 — докладываем модели, не падаем
        return ToolResult(False, "Не удалось открыть страницу: " + str(exc))
    text = _html_to_text(raw) if "<" in raw[:200] else raw
    limit = int(ctx.settings.get("tools.output_limit", 8000)) if ctx.settings else 8000
    shown, _ = truncate_output(text, limit, ctx.output_dir, "web")
    return ToolResult(True, shown)


def _web_search(ctx, args) -> ToolResult:
    """Поиск цепочкой провайдеров (net/search.py): свой SearXNG, бесплатные
    hosted MCP Exa и Parallel без ключа, резерв DuckDuckGo, дальше —
    официальные API по ключам из luna setup. Единый формат ответа:
    заголовок, адрес, выдержка и подпись источника."""
    query = args.get("query", "")
    if not query.strip():
        return ToolResult(False, "Пустой запрос.")
    if ctx.settings is not None and not ctx.settings.get("web.enabled", True):
        return ToolResult(False, "Веб-тулы выключены настройкой web.enabled.")
    from ..net.search import chain_search

    results, label, errors, skipped = chain_search(ctx, query, str(args.get("provider") or ""))
    if not results:
        parts = ["Поиск не дал результатов."]
        if errors:
            parts.append("Ошибки: " + "; ".join(errors) + ".")
        if skipped:
            parts.append("Пропущены: " + "; ".join(skipped) + ".")
        parts.append("Надёжный путь — свой SearXNG: luna set web.searxng_url "
                     "<адрес>, или ключи поиска через luna setup. Скажи об "
                     "этом человеку; страницы по известным адресам "
                     "открывает web_fetch.")
        return ToolResult(False, " ".join(parts))
    out = ["Поиск: «" + query + "» · источник: " + label]
    for title, href, snippet in results:
        line = "- " + (title or href)
        if href:
            line += "\n  " + href
        if snippet:
            line += "\n  " + snippet[:300]
        out.append(line)
    if errors:
        out.append("(по пути отвалились: " + "; ".join(errors) + ")")
    return ToolResult(True, "\n".join(out))


def _todo(ctx, args) -> ToolResult:
    items = args.get("items") or []
    cleaned = []
    for item in items:
        if isinstance(item, dict) and item.get("text"):
            status = item.get("status") or "pending"
            if status not in ("pending", "active", "done"):
                status = "pending"
            cleaned.append({"text": str(item["text"]), "status": status})
    ctx.todos = cleaned
    lines = []
    marks = {"pending": "[ ]", "active": "[~]", "done": "[x]"}
    for item in cleaned:
        lines.append(marks[item["status"]] + " " + item["text"])
    return ToolResult(True, "\n".join(lines) if lines else "(план пуст)",
                      data={"todo_items": cleaned})


def _ask_user(ctx, args) -> ToolResult:
    question = args.get("question", "")
    options = args.get("options") or []
    if ctx.ask_user_fn is None:
        return ToolResult(False, "Спросить человека сейчас нельзя: у этого клиента нет "
                                 "канала вопросов. Продолжай без ответа или закончи ход.")
    answer = ctx.ask_user_fn(question, options)
    if answer is None:
        return ToolResult(False, "Человек не ответил.")
    return ToolResult(True, str(answer))


def web_fetch_tool() -> Tool:
    return Tool("web_fetch", "Открыть страницу и вернуть её текст.",
                {"type": "object", "properties": {"url": {"type": "string"}},
                 "required": ["url"]}, _web_fetch)


def web_search_tool() -> Tool:
    return Tool("web_search", "Поиск в вебе цепочкой провайдеров (порядок — web.search_providers): свой SearXNG, бесплатные Exa и Parallel без ключа, DuckDuckGo, ключные Brave/Tavily/Perplexity. Ответ: заголовки, адреса, выдержки и источник. Можно выбрать движок сама полем provider (searxng, exa, parallel, duckduckgo, brave, tavily, perplexity, native; несколько — через запятую): например, если выдача пустая или нужен другой взгляд. Без provider — обычная цепочка.",
                {"type": "object", "properties": {"query": {"type": "string"},
                                                  "provider": {"type": "string", "description": "Необязательно: движок или несколько через запятую"}},
                 "required": ["query"]}, _web_search)


def todo_tool() -> Tool:
    return Tool("todo", "Задать или обновить план работы целиком: список пунктов со статусами "
                        "pending/active/done.",
                {"type": "object", "properties": {
                    "items": {"type": "array", "items": {
                        "type": "object", "properties": {
                            "text": {"type": "string"}, "status": {"type": "string"}}}}},
                 "required": ["items"]}, _todo)


def ask_user_tool() -> Tool:
    return Tool("ask_user", "Спросить человека и дождаться ответа. options — варианты, если уместны.",
                {"type": "object", "properties": {
                    "question": {"type": "string"},
                    "options": {"type": "array", "items": {"type": "string"}}},
                 "required": ["question"]}, _ask_user)
