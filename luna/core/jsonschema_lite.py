"""Маленький валидатор JSON Schema для выхода работника (output.schema).

Не полный стандарт, а честное подмножество: type, properties, required,
additionalProperties, patternProperties, items, minItems/maxItems, enum,
const, minLength/maxLength, minimum/maximum (и exclusive*), pattern,
anyOf/oneOf/allOf, not. Неизвестные ключевые слова молча пропускаются —
схема от внешнего агента не должна ронять запуск.

validate() возвращает список ошибок по-русски с путём ($.a[0].b); пустой
список — всё сходится. Ошибки уходят модели на повторную попытку, поэтому
пишем их так, чтобы она поняла, что поправить.
"""

from __future__ import annotations

import json
import re

_TYPE_NAMES = {"null": "null", "boolean": "boolean", "integer": "integer",
               "number": "number", "string": "string", "array": "array",
               "object": "object"}

# Глубже не идём: схема или значение с бесконечной вложенностью — не вывод модели.
_MAX_DEPTH = 64


def _type_of(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (list, tuple)):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _is_type(value, name: str) -> bool:
    actual = _type_of(value)
    if name == "number":
        return actual in ("integer", "number")
    if name == "integer":
        # 1.0 в JSON — тоже целое; bool целым не считается никогда.
        return actual == "integer" or (actual == "number" and float(value).is_integer())
    return actual == name


def _json_eq(a, b) -> bool:
    """Равенство по JSON: True не равно 1, 1 равно 1.0."""
    ta, tb = _type_of(a), _type_of(b)
    if {ta, tb} <= {"integer", "number"}:
        return a == b
    if ta != tb:
        return False
    if ta == "array":
        return len(a) == len(b) and all(_json_eq(x, y) for x, y in zip(a, b))
    if ta == "object":
        return a.keys() == b.keys() and all(_json_eq(a[k], b[k]) for k in a)
    return a == b


def _short(value) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        text = repr(value)
    return text if len(text) <= 60 else text[:57] + "..."


def _key_path(path: str, key: str) -> str:
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        return path + "." + key
    return path + "[" + json.dumps(key, ensure_ascii=False) + "]"


def _check(value, schema, path: str, errors: list, depth: int) -> None:
    if depth > _MAX_DEPTH:
        errors.append(path + ": слишком глубокая вложенность")
        return
    if schema is True or schema is None:
        return
    if schema is False:
        errors.append(path + ": здесь значения быть не должно")
        return
    if not isinstance(schema, dict):
        return  # кривая схема — не повод ругать модель

    types = schema.get("type")
    if types is not None:
        names = [types] if isinstance(types, str) else list(types or [])
        names = [n for n in names if n in _TYPE_NAMES]
        if names and not any(_is_type(value, n) for n in names):
            errors.append(path + ": ожидался тип " + " или ".join(names)
                          + ", а пришёл " + _type_of(value))
            return  # дальше по чужому типу проверять бессмысленно

    if "const" in schema and not _json_eq(value, schema["const"]):
        errors.append(path + ": должно быть ровно " + _short(schema["const"]))
    enum = schema.get("enum")
    if isinstance(enum, list) and not any(_json_eq(value, e) for e in enum):
        errors.append(path + ": допустимо только одно из " + _short(enum))

    if isinstance(value, str):
        _check_string(value, schema, path, errors)
    elif _type_of(value) in ("integer", "number"):
        _check_number(value, schema, path, errors)
    elif isinstance(value, (list, tuple)):
        _check_array(value, schema, path, errors, depth)
    elif isinstance(value, dict):
        _check_object(value, schema, path, errors, depth)

    _check_combinators(value, schema, path, errors, depth)


def _num(schema, key):
    v = schema.get(key)
    return v if _type_of(v) in ("integer", "number") else None


def _check_string(value: str, schema, path, errors) -> None:
    lo, hi = _num(schema, "minLength"), _num(schema, "maxLength")
    if lo is not None and len(value) < lo:
        errors.append(path + ": строка короче " + str(lo) + " символов")
    if hi is not None and len(value) > hi:
        errors.append(path + ": строка длиннее " + str(hi) + " символов")
    pattern = schema.get("pattern")
    if isinstance(pattern, str):
        try:
            if not re.search(pattern, value):
                errors.append(path + ": строка не подходит под шаблон " + _short(pattern))
        except re.error:
            errors.append(path + ": в схеме кривой pattern " + _short(pattern))


def _check_number(value, schema, path, errors) -> None:
    lo, hi = _num(schema, "minimum"), _num(schema, "maximum")
    if lo is not None and value < lo:
        errors.append(path + ": число меньше " + str(lo))
    if hi is not None and value > hi:
        errors.append(path + ": число больше " + str(hi))
    xlo, xhi = _num(schema, "exclusiveMinimum"), _num(schema, "exclusiveMaximum")
    if xlo is not None and value <= xlo:
        errors.append(path + ": число должно быть больше " + str(xlo))
    if xhi is not None and value >= xhi:
        errors.append(path + ": число должно быть меньше " + str(xhi))


def _check_array(value, schema, path, errors, depth) -> None:
    lo, hi = _num(schema, "minItems"), _num(schema, "maxItems")
    if lo is not None and len(value) < lo:
        errors.append(path + ": элементов меньше " + str(lo))
    if hi is not None and len(value) > hi:
        errors.append(path + ": элементов больше " + str(hi))
    items = schema.get("items")
    if isinstance(items, (dict, bool)):
        for i, item in enumerate(value):
            _check(item, items, path + "[" + str(i) + "]", errors, depth + 1)
    elif isinstance(items, list):
        # Старая форма «кортежа»: схема на каждую позицию.
        for i, (item, sub) in enumerate(zip(value, items)):
            _check(item, sub, path + "[" + str(i) + "]", errors, depth + 1)


def _check_object(value: dict, schema, path, errors, depth) -> None:
    props = schema.get("properties")
    props = props if isinstance(props, dict) else {}
    required = schema.get("required")
    if isinstance(required, list):
        for key in required:
            if isinstance(key, str) and key not in value:
                errors.append(_key_path(path, key) + ": обязательное поле отсутствует")
    patterns = schema.get("patternProperties")
    patterns = patterns if isinstance(patterns, dict) else {}
    additional = schema.get("additionalProperties", True)
    for key, item in value.items():
        key = str(key)
        sub_path = _key_path(path, key)
        known = False
        if key in props:
            known = True
            _check(item, props[key], sub_path, errors, depth + 1)
        for pat, sub in patterns.items():
            try:
                hit = re.search(pat, key)
            except re.error:
                continue
            if hit:
                known = True
                _check(item, sub, sub_path, errors, depth + 1)
        if known:
            continue
        if additional is False:
            errors.append(sub_path + ": лишнее поле, схема его не разрешает")
        elif isinstance(additional, dict):
            _check(item, additional, sub_path, errors, depth + 1)


def _check_combinators(value, schema, path, errors, depth) -> None:
    all_of = schema.get("allOf")
    if isinstance(all_of, list):
        for sub in all_of:
            _check(value, sub, path, errors, depth + 1)
    any_of = schema.get("anyOf")
    if isinstance(any_of, list) and any_of:
        variants = [_sub_errors(value, sub, path, depth) for sub in any_of]
        if all(variants):
            best = min(variants, key=len)
            errors.append(path + ": не подошёл ни один вариант anyOf; ближайший: "
                          + "; ".join(best[:3]))
    one_of = schema.get("oneOf")
    if isinstance(one_of, list) and one_of:
        variants = [_sub_errors(value, sub, path, depth) for sub in one_of]
        good = sum(1 for v in variants if not v)
        if good == 0:
            best = min(variants, key=len)
            errors.append(path + ": не подошёл ни один вариант oneOf; ближайший: "
                          + "; ".join(best[:3]))
        elif good > 1:
            errors.append(path + ": подошло несколько вариантов oneOf ("
                          + str(good) + "), а нужен ровно один")
    if "not" in schema and not _sub_errors(value, schema["not"], path, depth):
        errors.append(path + ": значение подходит под схему not, а не должно")


def _sub_errors(value, schema, path, depth) -> list:
    errs: list = []
    _check(value, schema, path, errs, depth + 1)
    return errs


def validate(instance, schema) -> list:
    """Ошибки соответствия instance схеме; пустой список — всё верно."""
    errors: list = []
    _check(instance, schema, "$", errors, 0)
    return errors


_FENCE = re.compile(r"\A```[ \t]*(?:json|JSON)?[ \t]*\r?\n(.*?)\r?\n?```\Z", re.S)


def _no_constants(name):
    raise ValueError("в JSON нет " + name)


def extract_json(text):
    """(объект, "") или (None, ошибка): финальный текст модели как ОДИН
    JSON-объект. Прощаем только пробелы по краям и обёртку ```json —
    никаких «вот ваш ответ:» вокруг, иначе внешний агент получит мусор."""
    if not isinstance(text, str):
        return None, "ответ не текст"
    body = text.strip()
    if not body:
        return None, "ответ пустой, а нужен JSON-объект"
    fence = _FENCE.match(body)
    if fence:
        body = fence.group(1).strip()
    elif body.startswith("```"):
        return None, "обёртка ``` не закрыта или кривая"
    try:
        obj = json.loads(body, parse_constant=_no_constants)
    except ValueError as exc:
        return None, "ответ не JSON: " + str(exc)
    if not isinstance(obj, dict):
        return None, "нужен JSON-объект {...}, а пришёл " + _type_of(obj)
    return obj, ""
