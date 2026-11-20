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


_DECODER = json.JSONDecoder()


def _json_at(text: str, pos: int, with_end: bool = False):
    """JSON-значение с позиции pos (вложенные скобки — честно, не регэкспом)."""
    try:
        value, end = _DECODER.raw_decode(text, pos)
    except ValueError:
        return (None, pos) if with_end else None
    return (value, end) if with_end else value


def _name(raw: str, names):
    """Имя тула из текста или None. functions.read и read:0 — тоже read."""
    raw = (raw or "").strip()
    if raw.startswith("functions."):
        raw = raw[len("functions."):]
    raw = raw.split(":", 1)[0]
    if not raw:
        return None
    if not names or raw in names:
        return raw
    # Слабые модели калечат имя: съедают подчёркивания (list_processes →
    # listprocesses), меняют регистр, отбрасывают префикс MCP-сервера.
    # Берём, только если подходит ровно один тул.
    want = _squash(raw)
    if len(want) < 3:
        return None
    exact = [n for n in names if _squash(n) == want]
    if len(exact) == 1:
        return exact[0]
    tail = [n for n in names if _squash(n.rsplit("__", 1)[-1]) == want]
    return tail[0] if len(tail) == 1 else None


def _squash(name: str) -> str:
    return re.sub(r"[^0-9a-zа-яё]", "", name.lower())


def _args_of(raw):
    """Аргументы вызова: dict, JSON-строка с dict или None (тогда это не вызов)."""
    if raw is None:
        return {}
    if isinstance(raw, str):
        if not raw.strip():
            return {}
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    return raw if isinstance(raw, dict) else None


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
        name = _name(m.group(1), names)
        if not name:
            continue
        end_m = _FN_END.search(text, m.end())
        body_end = end_m.start() if end_m else len(text)
        end = end_m.end() if end_m else len(text)
        if end_m and end_m.group(0) == "</function>":
            close = re.match(r"\s*</tool_call>", text[end:])
            if close:
                end += close.end()
        body = text[m.end():body_end]
        args = {key: raw for key, raw in _PARAM.findall(body)}
        how = "xml"
        if not args and body.strip().startswith("{"):
            # Llama 3.1: <function=имя>{json}</function> — тот же тег, тело JSON.
            data = _json_at(body, body.index("{"))
            if isinstance(data, dict):
                args, how = data, "json"
        start = m.start()
        # Обёртка <tool_call> перед функцией — часть вызова.
        before = text.rfind("<tool_call>", 0, start)
        if before != -1 and not text[before + len("<tool_call>"):start].strip():
            start = before
        out.append((start, end, name, args, how))
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
        name = _name(str(data.get("name") or ""), names)
        args = _args_of(data.get("arguments", data.get("parameters", {})))
        if not name or args is None:
            continue
        out.append((m.start(), m.end(), name, args, "json"))
    return out


# --- XML в духе Клода: <invoke name="имя"><parameter name="ключ">…</parameter> --

_INVOKE = re.compile(r"<invoke\s+name=\"([\w.\-]+)\"\s*>(.*?)(?:</invoke>|\Z)", re.S)
_INV_PARAM = re.compile(r"<parameter\s+name=\"([\w.\-]+)\"\s*>(.*?)(?:</parameter>|\Z)", re.S)


def _invoke_xml(text: str, names) -> list:
    out = []
    for m in _INVOKE.finditer(text):
        name = _name(m.group(1), names)
        if not name:
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
        name = _name(m.group(1) or m.group(2), names)
        if not name:
            continue
        try:
            args = json.loads(m.group(3))
        except ValueError:
            continue
        if isinstance(args, dict):
            out.append((m.start(), m.end(), name, args, "json"))
    return out


# --- GLM-4.5/4.6: <tool_call>имя<arg_key>к</arg_key><arg_value>з</arg_value> ---
#     и GLM-4: <tool_call>имя\n{json}</tool_call>

_GLM = re.compile(r"<tool_call>\s*([\w.\-:]+)\s*((?:<arg_key>.*?</arg_key>\s*<arg_value>.*?"
                  r"(?:</arg_value>|\Z)\s*)+)(?:</tool_call>|\Z)", re.S)
_GLM_PAIR = re.compile(r"<arg_key>(.*?)</arg_key>\s*<arg_value>(.*?)(?:</arg_value>|\Z)", re.S)
_GLM_JSON = re.compile(r"<tool_call>\s*([\w.\-:]+)\s*\n\s*(?=\{)")


def _glm(text: str, names) -> list:
    out = []
    for m in _GLM.finditer(text):
        name = _name(m.group(1), names)
        if name:
            args = {k.strip(): v for k, v in _GLM_PAIR.findall(m.group(2))}
            out.append((m.start(), m.end(), name, args, "xml"))
    for m in _GLM_JSON.finditer(text):
        name = _name(m.group(1), names)
        args, end = _json_at(text, m.end(), with_end=True)
        if not name or not isinstance(args, dict):
            continue
        close = re.match(r"\s*</tool_call>", text[end:])
        out.append((m.start(), end + (close.end() if close else 0), name, args, "json"))
    return out


# --- Kimi K2: <|tool_call_begin|>functions.имя:0<|tool_call_argument_begin|>{…} --

_KIMI = re.compile(r"<\|tool_call_begin\|>\s*([\w.\-:]+)\s*<\|tool_call_argument_begin\|>\s*")
_KIMI_END = re.compile(r"\s*<\|tool_call_end\|>")


def _kimi(text: str, names) -> list:
    out = []
    for m in _KIMI.finditer(text):
        name = _name(m.group(1), names)
        args, end = _json_at(text, m.end(), with_end=True)
        if not name or not isinstance(args, dict):
            continue
        close = _KIMI_END.match(text, end)
        out.append((m.start(), close.end() if close else end, name, args, "json"))
    return out


# --- Mistral: [TOOL_CALLS][{…}, …] и новое [TOOL_CALLS]имя[ARGS]{…} -----------

_MISTRAL = re.compile(r"\[TOOL_CALLS\]\s*")
_MISTRAL_ARGS = re.compile(r"([\w.\-:]+)\s*(?:\[CALL_ID\]\s*\w+\s*)?\[ARGS\]\s*")


def _mistral(text: str, names) -> list:
    out = []
    for m in _MISTRAL.finditer(text):
        named = _MISTRAL_ARGS.match(text, m.end())
        if named:
            name = _name(named.group(1), names)
            args, end = _json_at(text, named.end(), with_end=True)
            if name and isinstance(args, dict):
                out.append((m.start(), end, name, args, "json"))
            continue
        items, end = _json_at(text, m.end(), with_end=True)
        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, list):
            continue
        # Список вызовов — один кусок текста: первый вызов забирает весь
        # кусок, остальные получают пустые отрезки в его конце.
        pos = m.start()
        for item in items:
            found = _shape(item, names)
            if found:
                out.append((pos, end, found[0], found[1], "json"))
                pos = end
    return out


# --- gpt-oss (Harmony): to=functions.имя <|constrain|>json<|message|>{…}<|call|> --

_HARMONY = re.compile(r"(?:<\|start\|>\s*assistant\s*)?(?:<\|channel\|>\s*\w+\s*)?"
                      r"to=([\w.\-]+)[^<{]*(?:<\|constrain\|>\s*\w*\s*)?(?:<\|message\|>)?\s*")
_HARMONY_END = re.compile(r"\s*<\|call\|>")


def _harmony(text: str, names) -> list:
    out = []
    for m in _HARMONY.finditer(text):
        name = _name(m.group(1), names)
        if not name or not text.startswith("{", m.end()):
            continue
        args, end = _json_at(text, m.end(), with_end=True)
        if not isinstance(args, dict):
            continue
        close = _HARMONY_END.match(text, end)
        out.append((m.start(), close.end() if close else end, name, args, "json"))
    return out


PARSERS = (_qwen_xml, _hermes_json, _invoke_xml, _deepseek, _glm, _kimi, _mistral,
           _harmony)


# --- Свободные формы: голый JSON, ```tool_code```, ReAct ------------------------
# Их легко спутать с примером в объяснении, поэтому они берутся только в
# самом конце ответа (как trailing), и только с именем известного тула.

_ARG_KEYS = ("arguments", "parameters", "args", "input", "tool_input", "action_input",
             "params")


def _shape(data, names):
    """(имя, аргументы) из JSON-объекта вызова любой из ходовых форм, или None."""
    if not isinstance(data, dict):
        return None
    if isinstance(data.get("function"), dict):  # OpenAI: {"type":"function","function":{…}}
        return _shape(data["function"], names)
    for key in ("name", "tool", "tool_name", "action", "function", "command_name"):
        raw = data.get(key)
        if not isinstance(raw, str):
            continue
        name = _name(raw, names)
        if not name:
            return None
        rest = {k: v for k, v in data.items() if k != key}
        for arg_key in _ARG_KEYS:
            if arg_key in rest:
                args = _args_of(rest[arg_key])
                return (name, args) if args is not None else None
        # {"name": "read", "path": "a.py"} — аргументы прямо рядом с именем.
        rest.pop("type", None)
        rest.pop("id", None)
        return name, rest
    return None


_FENCE = re.compile(r"```[ \t]*(\w*)[ \t]*\n(.*?)(?:\n?```|\Z)", re.S)
_REACT = re.compile(r"Action:\s*([\w.\-:]+)\s*\n\s*Action Input:\s*")
_TAIL = 12000  # свободные формы ищем только в хвосте — дальше их всё равно не взять


def _bare_json(text: str, names) -> list:
    out = []
    base = max(0, len(text) - _TAIL)
    for m in _FENCE.finditer(text, base):
        if m.group(1).lower() not in ("", "json", "tool_call", "tool_calls", "function"):
            continue
        body = m.group(2).strip()
        data = _json_at(body, 0) if body[:1] in "{[" else None
        items = data if isinstance(data, list) else [data]
        if isinstance(data, dict) and isinstance(data.get("tool_calls"), list):
            items = data["tool_calls"]
        pos = m.start()
        for item in items:
            found = _shape(item, names)
            if found:
                out.append((pos, m.end(), found[0], found[1], "json"))
                pos = m.end()
    taken = [(s, e) for s, e, *_ in out]
    starts = [i for i in range(base, len(text)) if text[i] in "{["][-200:]
    for i in starts:
        if any(s <= i < e for s, e in taken):
            continue
        data, end = _json_at(text, i, with_end=True)
        if data is None:
            continue
        items = data if isinstance(data, list) else [data]
        if isinstance(data, dict) and isinstance(data.get("tool_calls"), list):
            items = data["tool_calls"]
        pos, hit = i, False
        for item in items:
            found = _shape(item, names)
            if found:
                out.append((pos, end, found[0], found[1], "json"))
                pos, hit = end, True
        if hit:
            taken.append((i, end))
    return out


def _call_args(node):
    """Аргументы из ast.Call: только литералы, иначе None."""
    import ast

    if node.args:
        return None
    args = {}
    for kw in node.keywords:
        if kw.arg is None:
            return None
        try:
            args[kw.arg] = ast.literal_eval(kw.value)
        except (ValueError, SyntaxError, TypeError):
            return None
    return args


def _tool_code(text: str, names) -> list:
    """Gemma и др.: ```tool_code\\nread(path="a.py")\\n``` — вызов как питон."""
    import ast

    out = []
    for m in _FENCE.finditer(text, max(0, len(text) - _TAIL)):
        if m.group(1).lower() not in ("tool_code", "tool_call", "python", "py", ""):
            continue
        try:
            tree = ast.parse(m.group(2).strip())
        except (SyntaxError, ValueError):
            continue
        calls = []
        for stmt in tree.body:
            call = getattr(stmt, "value", None)
            if not isinstance(stmt, ast.Expr) or not isinstance(call, ast.Call):
                calls = []
                break
            func = call.func
            raw = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            name = _name(raw, names)
            args = _call_args(call) if name else None
            if args is None:
                calls = []
                break
            calls.append((name, args))
        pos = m.start()
        for name, args in calls:
            out.append((pos, m.end(), name, args, "json"))
            pos = m.end()
    return out


def _react(text: str, names) -> list:
    out = []
    for m in _REACT.finditer(text, max(0, len(text) - _TAIL)):
        name = _name(m.group(1), names)
        args, end = _json_at(text, m.end(), with_end=True)
        if name and isinstance(args, dict):
            out.append((m.start(), end, name, args, "json"))
    return out


LOOSE = (_tool_code, _bare_json, _react)


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


def _filler(piece: str) -> bool:
    """Между вызовами и после них — только пробелы и обёртки разметки."""
    piece = _DS_WRAP.sub("", piece)
    for tag in _WRAPS:
        piece = piece.replace(tag, "")
    return not piece.strip()


# Обёртки разметки, которые не принадлежат одному вызову: между вызовами и
# в хвосте они не делают текст «словами модели».
_WRAPS = ("<tool_call>", "</tool_call>", "<function_calls>", "</function_calls>",
          "<|tool_calls_section_begin|>", "<|tool_calls_section_end|>", "<|call|>",
          "<|python_tag|>", "<|eom_id|>", "<|eot_id|>", "```")


def _collect(text: str, names, parsers) -> list:
    spans = []
    for parser in parsers:
        try:
            found_all = parser(text, names)
        except Exception:  # noqa: BLE001 — кривой ответ модели не должен ронять ход
            continue
        prev_end = None
        for found in found_all:
            if found[0] == found[1]:
                # Пустой отрезок — хвост списка вызовов: живёт, только если
                # жив вызов, к которому он приклеен.
                if prev_end == found[0]:
                    spans.append(found)
                continue
            # Один кусок текста — один вызов: первый узнавший парсер прав.
            if any(found[0] < e and s < found[1] for s, e, *_ in spans):
                prev_end = None
                continue
            spans.append(found)
            prev_end = found[1]
    return spans


def extract(text: str, tools, trailing: bool = False) -> tuple[list, str]:
    """([ToolCall], текст без разметки вызовов). Нечего найти — ([], text).

    trailing — только вызовы, которыми текст кончается: в мыслях модель
    прикидывает черновики («вызову rm… нет, лучше иначе»), и в силе лишь
    то, после чего она уже ничего не думала.
    """
    if not text:
        return [], text
    schemas = _schemas(tools)
    names = set(schemas)
    if not names:
        return [], text
    spans = _collect(text, names, PARSERS)
    if not spans:
        # Свободные формы — только хвостом ответа: пример в объяснении
        # посреди текста вызовом не станет.
        spans = _collect(text, names, LOOSE)
        trailing = True
    if not spans:
        return [], text
    spans.sort(key=lambda s: (s[0], s[1]))
    if trailing:
        keep, after = [], len(text)
        for span in reversed(spans):
            if not _filler(text[span[1]:after]):
                break
            keep.insert(0, span)
            after = span[0]
        spans = keep
        if not spans:
            return [], text
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
    for tag in _WRAPS[4:-1]:  # служебные токены; ``` и <tool_call> в словах бывают
        rest = rest.replace(tag, "")
    return calls, rest.strip()


def rescue(result, tools) -> None:
    """Штатных вызовов нет — ищем написанные текстом: в ответе, потом в мыслях."""
    if result.tool_calls or not tools:
        return
    for field in ("text", "thinking"):
        calls, rest = extract(getattr(result, field), tools,
                              trailing=(field == "thinking"))
        if calls:
            result.tool_calls.extend(calls)
            setattr(result, field, rest)
            where = "в мыслях" if field == "thinking" else "в тексте"
            result.notes.append("Модель написала вызов тула " + where +
                                " — Луна его распознала: " +
                                ", ".join(c.name for c in calls))
            return
