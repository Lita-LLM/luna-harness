# Собрано по образцу BENDER: bender/mcp/content.py и bender/mcp/manager.py (коммит 6552ed660bfabf5202569f9905212b77dbf6d31a), сокращено.
"""Результат tools/call → текст и картинки для цикла агента.

У Бендера content.py — каталог ресурсов и шаблонов (resources/prompts) с
отдельным одобрением хозяина; Луна его пока не переносит (своя веха позже).
Здесь живёт вторая половина той же темы — как ответ MCP-тула становится
результатом тула Луны: текстовые блоки склеиваются в текст, картинки уходят
отдельным списком в формате частей роутера ({"type": "image", "data": base64,
"media_type": ...}), чтобы модель со зрением могла их получить, ресурсы
показываются текстом с URI, остальное — честной пометкой, а не тихим мусором.
"""

from __future__ import annotations

import json

from ..core.errors import HumanError


def render_result(result: dict) -> tuple[str, list, bool]:
    """(текст, картинки, is_error) из результата tools/call."""
    if not isinstance(result, dict):
        raise HumanError("mcp_protocol", "Некорректный результат MCP-тула", "Проверь сервер.")
    content = result.get("content") or []
    if not isinstance(content, list):
        raise HumanError("mcp_protocol", "Некорректный результат MCP-тула", "Проверь сервер.")
    lines: list[str] = []
    images: list[dict] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text":
            lines.append(str(item.get("text", "")))
        elif kind == "image":
            data = item.get("data")
            if isinstance(data, str) and data:
                mime = str(item.get("mimeType") or "image/png")
                images.append({"type": "image", "data": data, "media_type": mime})
                lines.append("[картинка от MCP-сервера: " + mime + "]")
            else:
                lines.append("[картинка от MCP-сервера без данных]")
        elif kind == "resource":
            resource = item.get("resource") or {}
            uri = str(resource.get("uri", ""))
            if isinstance(resource.get("text"), str):
                lines.append(uri + "\n" + resource["text"])
            else:
                lines.append(uri + " · двоичный ресурс " + str(resource.get("mimeType", "")) +
                             "; base64 в текст не вставляется")
        elif kind == "audio":
            lines.append("[аудио от MCP-сервера: " + str(item.get("mimeType", "")) + "; в текст не вставляется]")
        else:
            lines.append(json.dumps(item, ensure_ascii=False))
    text = "\n".join(lines)
    if result.get("structuredContent") is not None:
        text += "\n" + json.dumps(result["structuredContent"], ensure_ascii=False)
    return text, images, bool(result.get("isError"))
