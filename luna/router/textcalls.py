"""Вызовы тулов, которые модель написала текстом, а не штатным каналом.

Кривые модели (часто на прокси и локальных серверах без парсера
тул-коллов) выдают вызов прямо в текст или в мысли:
  <tool_call><function=read><parameter=path>a.py</parameter></function></tool_call>
  <tool_call>{"name": "read", "arguments": {"path": "a.py"}}</tool_call>
  <invoke name="read"><parameter name="path">a.py</parameter></invoke>
  <｜tool▁call▁begin｜>read<｜tool▁sep｜>{"path": "a.py"}<｜tool▁call▁end｜>
Различить, где модель говорит, а где думает, нельзя, но вызов узнаётся
по форме и по имени тула: берём только имена из списка тулов запроса,
поэтому рассуждение «вызову read» без разметки вызовом не станет.

Новый формат — ещё одна функция в PARSERS: (текст, имена) → [(начало,
конец, имя, аргументы)].
"""

from __future__ import annotations

import json
import re
import uuid

from .types import ToolCall


def _value(raw: str, schema: dict):
    """Значение параметра из текста: строки как есть, прочее — JSON, если выйдет."""
    # Формат Qwen ставит значение с новой строки: эти переводы — разметка.
    if raw.startswith("\n"):
        raw = raw[1:]
    if raw.endswith("\n"):
        raw = raw[:-1]
    if (schema or {}).get("type") == "string":
        return raw
    try:
        return json.loads(raw)
    except ValueError:
        return raw


# --- Qwen3-Coder и похожие: <function=имя><parameter=ключ>…</parameter> -------

_FN = re.compile(r"<function=([\w.\-]+)>")
_PARAM = re.compile(r"<parameter=([\w.\-]+)>(.*?)(?:</parameter>|(?=<parameter=)|(?=</function>)|\Z)",
                    re.S)
_FN_END = re.compile(r"</function>|</tool_call>|(?=<function=)|(?=<tool_call>)")


def _qwen_xml(text: str, names) -> list:
    out = []
    for m in _FN.finditer(text):
        name = m.group(1)
        if names and name not in names:
            continue
        end_m = _FN_END.search(text, m.end())
        body_end = end_m.start() if end_m else len(text)
        end = end_m.end() if end_m else len(text)
        if end_m and end_m.group(0) == "</function>":
            close = re.match(r"\s*</tool_call>", text[end:])
            if close:
                end += close.end()
        args = {key: raw for key, raw in _PARAM.findall(text[m.end():body_end])}
        start = m.start()
        # Обёртка <tool_call> перед функцией — часть вызова.
        before = text.rfind("<tool_call>", 0, start)
        if before != -1 and not text[before + len("<tool_call>"):start].strip():
            start = before
        out.append((start, end, name, args, "xml"))
    return out


# --- Hermes / Qwen2.5: <tool_call>{json}</tool_call> --------------------------

_HERMES = re.compile(r"<tool_call>\s*(\{.*?\})\s*(?:</tool_call>|\Z)", re.S)


def _hermes_json(text: str, names) -> list:
    out = []
    for m in _HERMES.finditer(text):
        try:
            data = json.loads(m.group(1))
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        name = str(data.get("name") or "")
        args = data.get("arguments", data.get("parameters", {}))
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {}
        if not name or (names and name not in names) or not isinstance(args, dict):
            continue
        out.append((m.start(), m.end(), name, args, "json"))
    return out


# --- XML в духе Клода: <invoke name="имя"><parameter name="ключ">…</parameter> --

_INVOKE = re.compile(r"<invoke\s+name=\"([\w.\-]+)\"\s*>(.*?)(?:</invoke>|\Z)", re.S)
_INV_PARAM = re.compile(r"<parameter\s+name=\"([\w.\-]+)\"\s*>(.*?)(?:</parameter>|\Z)", re.S)


def _invoke_xml(text: str, names) -> list:
    out = []
    for m in _INVOKE.finditer(text):
        name = m.group(1)
        if names and name not in names:
            continue
        args = {key: raw for key, raw in _INV_PARAM.findall(m.group(2))}
        start, end = m.start(), m.end()
        before = text.rfind("<function_calls>", 0, start)
        if before != -1 and not text[before + len("<function_calls>"):start].strip():
            start = before
        close = re.match(r"\s*</function_calls>", text[end:])
        if close:
            end += close.end()
        out.append((start, end, name, args, "xml"))
    return out


# --- DeepSeek: спецтокены ▁ (V3 с ```json и V3.1 с голым JSON) -----------------

_DS = re.compile(
    r"<｜tool▁call▁begin｜>\s*(?:function\s*<｜tool▁sep｜>\s*([\w.\-]+)|([\w.\-]+)\s*<｜tool▁sep｜>)"
    r"\s*(?:```(?:json)?\s*)?(\{.*?\})\s*(?:```\s*)?(?:<｜tool▁call▁end｜>|\Z)", re.S)
_DS_WRAP = re.compile(r"<｜tool▁calls▁(?:begin|end)｜>")


def _deepseek(text: str, names) -> list:
    out = []
    for m in _DS.finditer(text):
        name = m.group(1) or m.group(2)
        if names and name not in names:
            continue
        try:
            args = json.loads(m.group(3))
        except ValueError:
            continue
        if isinstance(args, dict):
            out.append((m.start(), m.end(), name, args, "json"))
    return out


PARSERS = (_qwen_xml, _hermes_json, _invoke_xml, _deepseek)


def _schemas(tools) -> dict:
    """имя тула → свойства его схемы (для типов значений)."""
    out = {}
    for spec in tools or []:
        if isinstance(spec, dict):
            fn = spec.get("function") or spec
            name, params = fn.get("name"), fn.get("parameters")
        else:
            name, params = getattr(spec, "name", ""), getattr(spec, "parameters", None)
        if name:
            out[name] = ((params or {}).get("properties") or {})
    return out


def extract(text: str, tools) -> tuple[list, str]:
    """([ToolCall], текст без разметки вызовов). Нечего найти — ([], text)."""
    if not text or "<" not in text:
        return [], text
    schemas = _schemas(tools)
    names = set(schemas)
    if not names:
        return [], text
    spans = []
    for parser in PARSERS:
        for found in parser(text, names):
            # Один кусок текста — один вызов: первый узнавший парсер прав.
            if any(found[0] < e and s < found[1] for s, e, *_ in spans):
                continue
            spans.append(found)
    if not spans:
        return [], text
    spans.sort(key=lambda s: s[0])
    calls, pieces, pos = [], [], 0
    from .adapters import _split_intent

    for start, end, name, args, how in spans:
        props = schemas.get(name, {})
        if how == "xml":
            args = {k: _value(v, props.get(k) or {}) for k, v in args.items()}
        args, intent = _split_intent(args)
        calls.append(ToolCall("call_txt_" + uuid.uuid4().hex[:12], name, args, intent))
        pieces.append(text[pos:start])
        pos = end
    pieces.append(text[pos:])
    rest = _DS_WRAP.sub("", "".join(pieces))
    return calls, rest.strip()


def rescue(result, tools) -> None:
    """Штатных вызовов нет — ищем написанные текстом: в ответе, потом в мыслях."""
    if result.tool_calls or not tools:
        return
    for field in ("text", "thinking"):
        calls, rest = extract(getattr(result, field), tools)
        if calls:
            result.tool_calls.extend(calls)
            setattr(result, field, rest)
            where = "в мыслях" if field == "thinking" else "в тексте"
            result.notes.append("Модель написала вызов тула " + where +
                                " — Луна его распознала: " +
                                ", ".join(c.name for c in calls))
            return
