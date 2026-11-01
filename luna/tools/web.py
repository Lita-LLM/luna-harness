# Перенесено из BENDER: bender/tools/web.py и bender/net/search.py (коммит 6552ed660bfabf5202569f9905212b77dbf6d31a), сокращено.
"""Веб-тулы (web_fetch, web_search) и человеческие: todo, ask_user.

Сеть ходит через ctx.web_fetcher, если задан (тесты), иначе HttpClient.
Проверки адресов (локальная сеть, секреты в URL) — дело Предохранителя,
он решает до исполнения; здесь только работа.
"""

from __future__ import annotations

import html
import re
import urllib.parse

from .registry import Tool, ToolResult, truncate_output


def _fetch(ctx, url: str) -> str:
    if ctx.web_fetcher is not None:
        return ctx.web_fetcher(url)
    from ..net.http_client import HttpClient

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
    except Exception as exc:  # noqa: BLE001 — докладываем модели, не падаем
        return ToolResult(False, "Не удалось открыть страницу: " + str(exc))
    text = _html_to_text(raw) if "<" in raw[:200] else raw
    limit = int(ctx.settings.get("tools.output_limit", 8000)) if ctx.settings else 8000
    shown, _ = truncate_output(text, limit, ctx.output_dir, "web")
    return ToolResult(True, shown)


def _search_fetch(ctx, url: str) -> str:
    """Честный клиент: Луна представляется собой и не маскируется под браузер."""
    if ctx.web_fetcher is not None:
        return ctx.web_fetcher(url)
    from ..net.http_client import HttpClient

    return HttpClient().get_text(url, headers={"Accept-Language": "ru,en;q=0.8"}, timeout=20.0)


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
        out.append((_clean(title), _ddg_href(href), _clean(snippets[i]) if i < len(snippets) else ""))
    return out


def _parse_ddg_lite(raw: str) -> list:
    found = re.findall(r"<a[^>]*href=\"([^\"]+)\"[^>]*class='result-link'[^>]*>(.*?)</a>", raw, re.S)
    found += re.findall(r"<a[^>]*class='result-link'[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", raw, re.S)
    return [(_clean(t), _ddg_href(h), "") for h, t in found]


def _searxng(ctx, base: str, query: str) -> list:
    """Свой SearXNG (самохостинг): JSON-выдача, формат json должен быть включён."""
    import json

    url = base.rstrip("/") + "/search?format=json&q=" + urllib.parse.quote(query)
    data = json.loads(_search_fetch(ctx, url))
    return [(str(r.get("title", "")), str(r.get("url", "")), str(r.get("content", "")))
            for r in data.get("results", [])]


def _web_search(ctx, args) -> ToolResult:
    query = args.get("query", "")
    if not query.strip():
        return ToolResult(False, "Пустой запрос.")
    if ctx.settings is not None and not ctx.settings.get("web.enabled", True):
        return ToolResult(False, "Веб-тулы выключены настройкой web.enabled.")
    errors = []
    results: list = []
    searx = str(ctx.settings.get("web.searxng_url", "") or "") if ctx.settings is not None else ""
    engines = []
    if searx:
        engines.append(("SearXNG", lambda: _searxng(ctx, searx, query)))
    q = urllib.parse.quote(query)
    engines.append(("DuckDuckGo", lambda: _parse_ddg_html(
        _search_fetch(ctx, "https://html.duckduckgo.com/html/?q=" + q))))
    engines.append(("DuckDuckGo lite", lambda: _parse_ddg_lite(
        _search_fetch(ctx, "https://lite.duckduckgo.com/lite/?q=" + q))))
    for name, run in engines:
        try:
            results = run()
        except Exception as exc:  # noqa: BLE001 — пробуем следующий поисковик
            errors.append(name + ": " + str(exc)[:120])
            continue
        if results:
            break
        errors.append(name + ": пустая выдача (поисковик не отвечает ботам)")
    if not results:
        return ToolResult(False, "Поиск не дал результатов. " + "; ".join(errors) +
                          ". Надёжный путь — свой SearXNG: luna set web.searxng_url <адрес>. "
                          "Скажи об этом человеку; страницы по известным адресам открывает web_fetch.")
    out = []
    for title, href, snippet in results[:10]:
        out.append("- " + title + "\n  " + href + ("\n  " + snippet[:200] if snippet else ""))
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
    return Tool("web_search", "Поиск в вебе (свой SearXNG из web.searxng_url, иначе DuckDuckGo): заголовки, адреса, выдержки.",
                {"type": "object", "properties": {"query": {"type": "string"}},
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
