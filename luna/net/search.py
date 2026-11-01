"""Цепочка провайдеров web_search (ЗАДАЧА-19, заменяет раздел 5 ЗАДАЧИ-18).

Как у эталонов и без маскировки под браузер: OpenCode зовёт бесплатный
hosted MCP Exa, OpenClaw держит цепочку провайдеров с бесплатным
Parallel Search. У Луны — то же, своими силами и через свой MCP-клиент:

  searxng (свой, если задан web.searxng_url) → exa (MCP, без ключа) →
  parallel (MCP, без ключа) → duckduckgo (резерв) → brave / tavily /
  perplexity (официальные API по ключам из luna setup, сейф секретов).

Провайдеры — данные (PROVIDERS ниже): имя, заголовок для ответа, функция.
Провал или пустая выдача одного — следующий по цепочке; итог честный:
кто ответил, кто по пути отвалился и почему. Ключи агент не видит:
здесь они читаются из сейфа по именам «search:<провайдер>» и уходят
только в заголовки запросов провайдерам. Разбор фактов и лимитов —
docs/research/ПОИСК.md.
"""

from __future__ import annotations

import html
import json
import re
import urllib.parse

RESULT_CAP = 10


class SearchError(Exception):
    """Провайдер честно не смог: цепочка идёт дальше."""


class SkipProvider(Exception):
    """Провайдер не применим (нет адреса или ключа) — не ошибка."""


# --- общая сеть тулов: подмена в тестах — ctx.web_fetcher -------------------

def search_fetch(ctx, url: str) -> str:
    """Честный клиент: Луна представляется собой и не маскируется под браузер."""
    if ctx.web_fetcher is not None:
        return ctx.web_fetcher(url)
    from .http_client import HttpClient

    return HttpClient().get_text(url, headers={"Accept-Language": "ru,en;q=0.8"},
                                 timeout=20.0)


def http_json(ctx, method: str, url: str, headers: dict, payload=None) -> dict:
    """JSON-запрос провайдера; в тестах web_fetcher отдаёт готовый JSON."""
    if ctx.web_fetcher is not None:
        return json.loads(ctx.web_fetcher(url))
    from .http_client import HttpClient

    client = HttpClient()
    if method == "POST":
        return client.post_json(url, payload or {}, headers, timeout=25.0)
    return client.get_json(url, headers, timeout=25.0)


def secret(ctx, provider: str) -> str:
    """Ключ провайдера из сейфа («search:<провайдер>»); агента не касается."""
    if ctx.secrets is None:
        return ""
    try:
        return str(ctx.secrets.get("search:" + provider) or "")
    except Exception:
        return ""


def setting(ctx, key: str, default=""):
    if ctx.settings is None:
        return default
    return ctx.settings.get(key, default)


# --- hosted MCP (Exa и Parallel): наш MCP-клиент, Streamable HTTP -----------

def mcp_call(ctx, url: str, headers: dict, tool: str, arguments: dict) -> dict:
    """Один вызов MCP-тула; единственная сетевая точка MCP-провайдеров.

    Подмена в тестах: ctx.mcp_caller. Если сеть тулов подменена одним
    web_fetcher (старые тесты), MCP честно пропускается: fetcher говорит
    HTTP-текстом, а не JSON-RPC, — живую сеть при этом не дёргаем.
    """
    caller = getattr(ctx, "mcp_caller", None)
    if caller is not None:
        return caller(url, headers, tool, arguments)
    if ctx.web_fetcher is not None:
        raise SkipProvider("при подменённой сети MCP недоступен")
    from ..mcp.client import Client, HttpTransport

    client = Client(HttpTransport(url, headers), timeout=25.0)
    try:
        return client.call_tool(tool, arguments)
    finally:
        client.close()


def _mcp_text(ctx, url: str, headers: dict, tool: str, arguments: dict) -> str:
    from ..mcp.content import render_result

    result = mcp_call(ctx, url, headers, tool, arguments)
    if isinstance(result, dict) and result.get("structuredContent"):
        structured = result["structuredContent"]
        parsed = _results_from_json(structured)
        if parsed:
            return "\n".join(title + "\n" + href + "\n" + snippet
                             for title, href, snippet in parsed)
    text, _images, is_error = render_result(result)
    if is_error:
        raise SearchError(text.strip()[:200] or "MCP-провайдер вернул ошибку")
    return text


def _results_from_json(data) -> list:
    """Результаты из JSON-подобной выдачи: списки results/searches с
    полями title/name, url/link, excerpts/snippet/content/description."""
    items = []
    if isinstance(data, dict):
        for key in ("results", "searches", "items"):
            if isinstance(data.get(key), list):
                items = data[key]
                break
        if not items and (data.get("url") or data.get("title")):
            items = [data]
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or item.get("name") or "")
        href = str(item.get("url") or item.get("link") or "")
        snippet = item.get("snippet") or item.get("content") \
            or item.get("description") or ""
        if not snippet and isinstance(item.get("excerpts"), list):
            snippet = " ".join(str(x) for x in item["excerpts"][:2])
        if href:
            out.append((title, href, str(snippet)))
    return out


_URL_RE = re.compile(r"https?://[^\s<>\)\"']+")


def _results_from_text(text: str) -> list:
    """Результаты из текстовой выдачи MCP (формат Exa: Title:/URL: блоки;
    запасной путь — просто адреса с соседними строками)."""
    out = []
    blocks = re.split(r"\n(?=Title:\s)", text)
    for block in blocks:
        title_m = re.search(r"Title:\s*(.+)", block)
        url_m = re.search(r"URL:\s*(\S+)", block)
        if title_m and url_m:
            rest = block[url_m.end():]
            rest = re.sub(r"(?m)^(Published|Author|Highlights):.*$", "", rest)
            snippet = " ".join(rest.split())[:400]
            out.append((title_m.group(1).strip(), url_m.group(1).strip(),
                        snippet))
    if out:
        return out
    lines = [line.strip() for line in text.splitlines()]
    for i, line in enumerate(lines):
        match = _URL_RE.search(line)
        if not match:
            continue
        title = lines[i - 1] if i > 0 else ""
        snippet = lines[i + 1] if i + 1 < len(lines) else ""
        out.append((title, match.group(0), snippet))
    return out


# --- провайдеры ---------------------------------------------------------------

def provider_searxng(ctx, query: str) -> list:
    base = str(setting(ctx, "web.searxng_url", "") or "")
    if not base:
        raise SkipProvider("не задан web.searxng_url")
    url = base.rstrip("/") + "/search?format=json&q=" + urllib.parse.quote(query)
    data = json.loads(search_fetch(ctx, url))
    return [(str(r.get("title", "")), str(r.get("url", "")),
             str(r.get("content", "")))
            for r in data.get("results", []) if r.get("url")]


def provider_exa(ctx, query: str) -> list:
    url = str(setting(ctx, "web.exa_url", "") or "https://mcp.exa.ai/mcp")
    headers = {}
    key = secret(ctx, "exa")
    if key:
        headers["x-api-key"] = key
    text = _mcp_text(ctx, url, headers, "web_search_exa",
                     {"query": query, "objective": query, "numResults": 8})
    return _results_from_text(text)


def provider_parallel(ctx, query: str) -> list:
    url = str(setting(ctx, "web.parallel_url", "")
              or "https://search.parallel.ai/mcp")
    headers = {}
    key = secret(ctx, "parallel")
    if key:
        headers["Authorization"] = "Bearer " + key
    text = _mcp_text(ctx, url, headers, "web_search",
                     {"objective": query, "search_queries": [query]})
    parsed = []
    try:
        parsed = _results_from_json(json.loads(text))
    except (ValueError, TypeError):
        parsed = []
    return parsed or _results_from_text(text)


def _ddg_href(href: str) -> str:
    """Ссылки DuckDuckGo — через редирект /l/?uddg=…: достаём настоящий адрес."""
    href = html.unescape(href)
    if href.startswith("//"):
        href = "https:" + href
    parsed = urllib.parse.urlparse(href)
    if parsed.path.startswith("/l/"):
        target = urllib.parse.parse_qs(parsed.query).get("uddg")
        if target:
            return target[0]
    return href


def _clean(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text)).strip()


def _parse_ddg_html(raw: str) -> list:
    found = re.findall(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', raw, re.S)
    snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', raw, re.S)
    out = []
    for i, (href, title) in enumerate(found):
        out.append((_clean(title), _ddg_href(href),
                    _clean(snippets[i]) if i < len(snippets) else ""))
    return out


def _parse_ddg_lite(raw: str) -> list:
    found = re.findall(r"<a[^>]*href=\"([^\"]+)\"[^>]*class='result-link'[^>]*>(.*?)</a>", raw, re.S)
    found += re.findall(r"<a[^>]*class='result-link'[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", raw, re.S)
    return [(_clean(t), _ddg_href(h), "") for h, t in found]


def provider_duckduckgo(ctx, query: str) -> list:
    q = urllib.parse.quote(query)
    results = _parse_ddg_html(
        search_fetch(ctx, "https://html.duckduckgo.com/html/?q=" + q))
    if results:
        return results
    return _parse_ddg_lite(
        search_fetch(ctx, "https://lite.duckduckgo.com/lite/?q=" + q))


def provider_brave(ctx, query: str) -> list:
    key = secret(ctx, "brave")
    if not key:
        raise SkipProvider("нет ключа (luna setup)")
    url = ("https://api.search.brave.com/res/v1/web/search?count=8&q="
           + urllib.parse.quote(query))
    data = http_json(ctx, "GET", url, {"X-Subscription-Token": key,
                                       "Accept": "application/json"})
    results = (data.get("web") or {}).get("results") or []
    return [(str(r.get("title", "")), str(r.get("url", "")),
             str(r.get("description", "")))
            for r in results if r.get("url")]


def provider_tavily(ctx, query: str) -> list:
    key = secret(ctx, "tavily")
    if not key:
        raise SkipProvider("нет ключа (luna setup)")
    data = http_json(ctx, "POST", "https://api.tavily.com/search",
                     {"Content-Type": "application/json"},
                     {"api_key": key, "query": query, "max_results": 8})
    return _results_from_json(data)


def provider_perplexity(ctx, query: str) -> list:
    key = secret(ctx, "perplexity")
    if not key:
        raise SkipProvider("нет ключа (luna setup)")
    data = http_json(ctx, "POST", "https://api.perplexity.ai/search",
                     {"Authorization": "Bearer " + key,
                      "Content-Type": "application/json"},
                     {"query": query, "max_results": 8})
    return _results_from_json(data)


def provider_native(ctx, query: str) -> list:
    """Родной поиск провайдера модели (web.native_search): умеем честно
    только OpenRouter — та же главная модель с суффиксом :online."""
    agent = ctx.agent
    router = getattr(agent, "router", None)
    if agent is None or router is None:
        raise SkipProvider("нет агента с роутером")
    ref = str(setting(ctx, "model.main", "") or "")
    cfg = None
    try:
        cfg, _model = router.resolve(ref)
    except Exception:
        cfg = None
    if cfg is None or cfg.name != "openrouter":
        name = cfg.name if cfg is not None else "?"
        raise SearchError("родной поиск умею включать только для OpenRouter "
                          "(:online); провайдер главной модели: " + name)
    parts = ref.split(":", 1)
    online_ref = parts[0] + ":" + parts[1] + ":online" if len(parts) == 2 \
        else ref + ":online"
    prompt = ("Найди в вебе и перечисли источники по запросу: " + query
              + "\nВерни список: заголовок, адрес и короткая выдержка "
              "по каждому источнику.")
    result = router.chat([{"role": "user", "content": prompt}], None,
                         ref=online_ref)
    return [("Родной поиск провайдера (" + online_ref + ")", "",
             (result.text or "").strip()[:2000])]


# Провайдеры — данные: имя → (подпись в ответе, функция).
PROVIDERS = {
    "searxng": ("SearXNG", provider_searxng),
    "exa": ("Exa (MCP)", provider_exa),
    "parallel": ("Parallel Search (MCP)", provider_parallel),
    "duckduckgo": ("DuckDuckGo", provider_duckduckgo),
    "brave": ("Brave Search", provider_brave),
    "tavily": ("Tavily", provider_tavily),
    "perplexity": ("Perplexity", provider_perplexity),
    "native": ("родной поиск провайдера", provider_native),
}

DEFAULT_ORDER = "searxng,exa,parallel,duckduckgo,brave,tavily,perplexity"


def chain_search(ctx, query: str):
    """(результаты, подпись источника, ошибки по пути, пропуски).

    Идём по web.search_providers; web.native_search добавляет родной
    поиск последним резервом. Пустая выдача — тоже повод идти дальше.
    """
    order_text = str(setting(ctx, "web.search_providers", "")
                     or DEFAULT_ORDER)
    order = [name.strip().lower() for name in order_text.split(",")
             if name.strip()]
    if setting(ctx, "web.native_search", False) and "native" not in order:
        order.append("native")
    errors: list = []
    skipped: list = []
    for name in order:
        entry = PROVIDERS.get(name)
        if entry is None:
            skipped.append(name + ": не знаю такого провайдера")
            continue
        label, run = entry
        try:
            results = run(ctx, query)
        except SkipProvider as exc:
            skipped.append(name + ": " + str(exc))
            continue
        except Exception as exc:  # noqa: BLE001 — следующий по цепочке
            errors.append(label + ": " + str(exc)[:160])
            continue
        if results:
            return results[:RESULT_CAP], label, errors, skipped
        errors.append(label + ": пустая выдача")
    return [], "", errors, skipped
