"""Профили работника: файл LUNA_HOME/workers/<имя>.json → Cage.

Разбор строгий: незнакомый ключ, дубль ключа, не тот тип — ошибка, а не
молчаливое умолчание. Опечатка в «limits» или «deny» иначе тихо сняла бы
потолок или запрет, а клетка ради того и существует, чтобы такого не было.

Профили ищутся только у хозяина (LUNA_HOME/workers). Папка проекта не
сканируется: проект пишет кто угодно, и работник не должен получать
права из файла, который подбросили в репозиторий. Явный путь
(--worker-file) — сознательный выбор того, кто запускает.

Флаги командной строки клетку только сужают (narrow), собрать клетку
из одних флагов — cage_from_flags.
"""

from __future__ import annotations

import fnmatch
import json
import math
import re
from dataclasses import replace
from pathlib import Path

from ..core.errors import HumanError
from .cage import (BASH_MODES, FORBIDDEN_TOOLS, FS_MODES, FS_TOOLS, FS_WRITE_TOOLS,
                   INSTRUCTION_MODES, Cage, McpRule)

NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
# Строже — левее: plan ничего не меняет, yolo не спрашивает почти ничего.
MODE_ORDER = ("plan", "normal", "review", "auto", "yolo")
_FS_ORDER = FS_MODES                 # none < ro < rw
_BASH_ORDER = BASH_MODES             # off < sandbox
_INSTR_ORDER = INSTRUCTION_MODES     # none < project < all

# Схема профиля: ключ → тип или вложенная схема. Всё, чего тут нет, — ошибка.
_SCHEMA = {
    "version": int, "description": str, "prompt_file": str, "prompt": str,
    "model": str, "mode": str, "tools": list, "deny": list, "allow": list,
    "mcp": {"config": str, "servers": dict},
    "secrets": list,
    "fs": {"mode": str, "roots": list},
    "bash": str,
    "net": {"fetch_hosts": (list, type(None)), "fetch_query": bool},
    "instructions": str,
    "limits": {"max_usd": (int, float), "max_steps": int, "timeout_s": (int, float),
               "max_tokens": int},
    "output": {"schema": dict, "retries": int},
    "ephemeral": bool, "fail_on_deny": bool,
}
_SERVER_SCHEMA = {"tools": list, "deny": list, "required": bool, "args": dict}
_TYPE_NAMES = {int: "целое число", float: "число", str: "строка", list: "список",
               dict: "объект", bool: "true/false", type(None): "null"}


def _mcp_args(raw, where: str) -> dict:
    """{тул: {поле: [шаблоны]}} — строгая проверка формы."""
    out: dict = {}
    for tool, fields in raw.items():
        if not isinstance(fields, dict) or not fields:
            raise _err(where + "." + str(tool) + " должен быть объектом {поле: [значения]}")
        out[str(tool)] = {}
        for name, patterns in fields.items():
            out[str(tool)][str(name)] = _str_list(patterns, where + "." + str(tool) + "." + str(name))
            if not out[str(tool)][str(name)]:
                raise _err(where + "." + str(tool) + "." + str(name) + ": пустой список запрещает всё")
    return out


def _err(title: str, hint: str = "") -> HumanError:
    return HumanError("worker_profile", title, hint)


def workers_dir(home=None) -> Path:
    from ..core.paths import luna_home

    return (Path(home).expanduser() if home is not None else luna_home()) / "workers"


# --- разбор файла -------------------------------------------------------------


def _no_dupes(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise _err("В профиле ключ «" + key + "» повторяется",
                       "JSON молча взял бы последний — убери лишний.")
        out[key] = value
    return out


def _type_ok(value, expected) -> bool:
    types = expected if isinstance(expected, tuple) else (expected,)
    if isinstance(value, bool) and bool not in types:
        return False  # True — тоже int в Python, но «max_steps: true» — ошибка
    return isinstance(value, types)


def _type_name(expected) -> str:
    types = expected if isinstance(expected, tuple) else (expected,)
    return " или ".join(_TYPE_NAMES.get(t, t.__name__) for t in types)


def _check_obj(data, schema: dict, where: str) -> None:
    if not isinstance(data, dict):
        raise _err(where + " — должен быть объектом JSON")
    for key, value in data.items():
        if key not in schema:
            raise _err("Незнакомый ключ профиля: " + (where + "." if where != "профиль" else "") + key,
                       "Опечатка не должна молча снимать запрет или лимит. Допустимые: " +
                       ", ".join(schema) + ".")
        expected = schema[key]
        path = key if where == "профиль" else where + "." + key
        if isinstance(expected, dict):
            _check_obj(value, expected, path)
        elif not _type_ok(value, expected):
            raise _err("В профиле " + path + " — " + _type_name(expected) +
                       ", а не " + repr(value)[:60])


def _str_list(value, where: str) -> list:
    if not all(isinstance(x, str) and x.strip() for x in value):
        raise _err("В профиле " + where + " — список непустых строк")
    return [x.strip() for x in value]


def _num(value, where: str):
    # json.loads пропускает NaN и Infinity: min(nan, x) — nan, и лимит
    # молча исчезает. Только конечные числа (атака 04.11.2026).
    if not math.isfinite(value):
        raise _err("В профиле " + where + " — конечное число, а не " + repr(value))
    if value < 0:
        raise _err("В профиле " + where + " не может быть отрицательным")
    return value


def _resolve_roots(raw: list, cwd: Path, where: str) -> list:
    roots = []
    for item in raw:
        path = Path(item).expanduser()
        if not path.is_absolute():
            path = cwd / path
        try:
            path = path.resolve()
        except (OSError, RuntimeError):
            raise _err(where + ": корень не разрешается: " + item) from None
        if not path.is_dir():
            raise _err(where + ": корня нет или это не папка: " + str(path),
                       "Корни считаются от рабочей папки запуска (--cwd); «.» — она сама.")
        for guarded in _guarded_homes():
            if path == guarded or path in guarded.parents:
                # Список закрытых папок всегда отстаёт от новых программ:
                # корень, накрывший домашнюю папку, — ошибка, а не надежда.
                raise _err(where + ": корень " + str(path) + " накрывает " + str(guarded),
                           "Дай работнику узкую папку под задачу, а не домашнюю целиком.")
        if path not in roots:
            roots.append(path)
    return roots


def _read_json(path: Path, what: str):
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise _err("Не читается " + what + ": " + str(path), str(exc)) from None
    try:
        return json.loads(text, object_pairs_hook=_no_dupes)
    except ValueError as exc:
        raise _err("Кривой JSON в " + what + ": " + str(path), str(exc)) from None


def _guarded_homes() -> list:
    from ..core.paths import luna_home

    out = []
    for raw in (Path.home(), luna_home()):
        try:
            out.append(Path(raw).expanduser().resolve())
        except (OSError, RuntimeError):
            continue
    return out


def find_profile(name_or_path, *, home=None, cwd=None, kind: str = "auto") -> Path:
    """kind: name — только имя из LUNA_HOME/workers (--worker); path — только
    файл (--worker-file); auto — как раньше, для вызовов из кода."""
    text = str(name_or_path or "").strip()
    if not text:
        raise _err("Не задан работник", "Нужно: --worker <имя> или --worker-file <путь>.")
    if kind == "name":
        if text.lower().endswith(".json"):
            text = text[:-5]
        if not NAME_RE.match(text):
            # Иначе «--worker news.json» тихо брал файл из папки проекта.
            raise _err("--worker — только имя профиля из LUNA_HOME/workers: «" + text + "»",
                       "Буквы, цифры, _ и -. Файл по пути — флагом --worker-file.")
    if kind != "path" and NAME_RE.match(text):
        folder = workers_dir(home)
        path = folder / (text + ".json")
        if not path.is_file():
            known = sorted(p.stem for p in folder.glob("*.json")) if folder.is_dir() else []
            raise _err("Нет профиля работника «" + text + "» в " + str(folder),
                       ("Есть: " + ", ".join(known) + "." if known else
                        "Профиль — файл " + text + ".json в этой папке; образец — docs/РАБОТНИК.md."))
        return path
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = Path(cwd or Path.cwd()) / path
    if not path.is_file():
        raise _err("Нет файла профиля: " + str(path))
    return path.resolve()


def load_profile(name_or_path, *, home=None, cwd=None, kind: str = "auto") -> Cage:
    """Имя ([A-Za-z0-9_-]+) → LUNA_HOME/workers/<имя>.json; иначе путь к файлу."""
    path = find_profile(name_or_path, home=home, cwd=cwd, kind=kind)
    base = Path(cwd) if cwd is not None else Path.cwd()
    data = _read_json(path, "профиле работника")
    _check_obj(data, _SCHEMA, "профиль")
    if data.get("version") != 1:
        raise _err("Профиль работника: version должен быть 1",
                   "Сейчас: " + repr(data.get("version")) + ". Другие версии этой Луне неизвестны.")
    pdir = path.parent

    cage = Cage(name=path.stem, source=str(path))
    cage.description = data.get("description", "")
    if "prompt" in data and "prompt_file" in data:
        raise _err("В профиле и prompt, и prompt_file", "Оставь что-то одно.")
    if "prompt_file" in data:
        pfile = Path(data["prompt_file"]).expanduser()
        pfile = pfile if pfile.is_absolute() else pdir / pfile
        try:
            cage.prompt = pfile.read_text(encoding="utf-8").strip()
        except OSError:
            raise _err("Нет файла инструкций работника: " + str(pfile),
                       "prompt_file считается от папки профиля.") from None
    else:
        cage.prompt = data.get("prompt", "").strip()
    cage.model = data.get("model", "").strip()
    cage.mode = data.get("mode", "normal")
    cage.tools = _str_list(data.get("tools", []), "tools")
    cage.deny = _str_list(data.get("deny", []), "deny")
    cage.allow = _str_list(data.get("allow", []), "allow")
    cage.secrets = _str_list(data.get("secrets", []), "secrets")

    mcp = data.get("mcp", {})
    if mcp.get("config"):
        cfg = Path(mcp["config"]).expanduser()
        cfg = cfg if cfg.is_absolute() else pdir / cfg
        if not cfg.is_file():
            raise _err("Нет файла MCP-серверов работника: " + str(cfg),
                       "mcp.config считается от папки профиля.")
        cage.mcp_config = str(cfg.resolve())
    for server, rule in (mcp.get("servers") or {}).items():
        where = "mcp.servers." + server
        if not NAME_RE.match(server):
            raise _err("Кривое имя MCP-сервера в профиле: " + repr(server))
        _check_obj(rule, _SERVER_SCHEMA, where)
        cage.mcp[server] = McpRule(
            tools=_str_list(rule.get("tools", ["*"]), where + ".tools"),
            deny=_str_list(rule.get("deny", []), where + ".deny"),
            required=rule.get("required", False),
            args=_mcp_args(rule.get("args") or {}, where + ".args"))

    fs = data.get("fs", {})
    cage.fs_mode = fs.get("mode", "none")
    raw_roots = _str_list(fs.get("roots", []), "fs.roots")
    cage.bash = data.get("bash", "off")
    net = data.get("net", {})
    hosts = net.get("fetch_hosts", [])
    cage.fetch_hosts = None if hosts is None else [h.lower() for h in _str_list(hosts, "net.fetch_hosts")]
    cage.fetch_query = net.get("fetch_query", False)
    cage.instructions = data.get("instructions", "none")
    limits = data.get("limits", {})
    cage.max_usd = float(_num(limits.get("max_usd", 0), "limits.max_usd"))
    cage.max_steps = int(_num(limits.get("max_steps", 0), "limits.max_steps"))
    cage.timeout_s = float(_num(limits.get("timeout_s", 0), "limits.timeout_s"))
    cage.max_tokens = int(_num(limits.get("max_tokens", 0), "limits.max_tokens"))
    output = data.get("output", {})
    cage.output_schema = output.get("schema")
    retries = output.get("retries", 1)
    if not 0 <= retries <= 5:
        raise _err("В профиле output.retries — от 0 до 5")
    cage.schema_retries = retries
    cage.ephemeral = data.get("ephemeral", True)
    cage.fail_on_deny = data.get("fail_on_deny", False)

    check_cage(cage, raw_roots=raw_roots, cwd=base)
    return cage


# --- общая проверка клетки ------------------------------------------------------


def check_cage(cage: Cage, *, raw_roots=None, cwd=None, where: str = "Клетка") -> Cage:
    """Согласованность полей: противоречие — ошибка, а не тихий выбор."""
    if cage.mode not in MODE_ORDER:
        raise _err(where + ": неизвестный mode «" + str(cage.mode) + "»",
                   "Допустимые: " + ", ".join(MODE_ORDER) + ".")
    if cage.fs_mode not in FS_MODES:
        raise _err(where + ": fs.mode — " + "|".join(FS_MODES) + ", а не " + repr(cage.fs_mode))
    if cage.bash not in BASH_MODES:
        raise _err(where + ": bash — " + "|".join(BASH_MODES) + ", а не " + repr(cage.bash))
    if cage.instructions not in INSTRUCTION_MODES:
        raise _err(where + ": instructions — " + "|".join(INSTRUCTION_MODES) +
                   ", а не " + repr(cage.instructions))
    for name in cage.tools:
        if name in FORBIDDEN_TOOLS:
            raise _err(where + ": тул «" + name + "» в клетке запрещён всегда",
                       "Нельзя: " + ", ".join(sorted(FORBIDDEN_TOOLS)) +
                       " — они строят свои реестры мимо клетки или читают данные хозяина.")
        if name.startswith("mcp__"):
            raise _err(where + ": MCP-тул «" + name + "» в tools",
                       "MCP разрешается в разделе mcp.servers (или флагом --mcp сервер:тулы).")
        if name in FS_TOOLS and cage.fs_mode == "none":
            raise _err(where + ": тул «" + name + "» в tools, но fs.mode: none",
                       "Выдай файлы (fs.mode ro/rw и roots) или убери тул.")
        if name in FS_WRITE_TOOLS and cage.fs_mode == "ro":
            raise _err(where + ": тул «" + name + "» пишет, а fs.mode: ro",
                       "Нужен fs.mode rw — или убери тул.")
        if name == "bash" and cage.bash == "off":
            raise _err(where + ": тул bash в tools, но bash: off",
                       "Включи bash: sandbox или убери тул.")
    for pattern in cage.allow:
        _tool, sep, needle = pattern.partition(":")
        if sep and not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", needle):
            # «send_message:@мой_канал» модель выполнит, упомянув канал в
            # тексте письма в любой чат. В клетке — только поле=шаблон.
            raise _err(where + ": allow «" + pattern + "» с подстрокой небезопасен",
                       "В клетке — только «тул» или «тул:поле=шаблон», например "
                       "mcp__tg__send_message:chat_id=@my_channel.")
    if raw_roots is not None:
        base = Path(cwd) if cwd is not None else Path.cwd()
        cage.fs_roots = _resolve_roots(raw_roots, base, where)
    if cage.fs_mode == "none" and cage.fs_roots:
        raise _err(where + ": корни fs.roots заданы, а fs.mode: none",
                   "Либо выдай файлы (ro/rw), либо убери корни.")
    if cage.fs_mode != "none" and not cage.fs_roots:
        raise _err(where + ": fs.mode " + cage.fs_mode + ", но корней нет",
                   "Укажи fs.roots, например [\".\"] — рабочая папка запуска.")
    for server in cage.mcp:
        if not NAME_RE.match(server):
            raise _err(where + ": кривое имя MCP-сервера " + repr(server))
    if cage.output_schema is not None and not isinstance(cage.output_schema, dict):
        raise _err(where + ": схема ответа — объект JSON Schema")
    return cage


# --- клетка из флагов --------------------------------------------------------------


def parse_mcp_flag(text: str):
    """«сервер» или «сервер:glob,glob» → (сервер, [globs] | None)."""
    server, sep, rest = str(text).partition(":")
    server = server.strip()
    if not NAME_RE.match(server):
        raise _err("--mcp: кривое имя сервера " + repr(server),
                   "Нужно: --mcp СЕРВЕР или --mcp СЕРВЕР:тул,шаблон*.")
    if not sep:
        return server, None
    globs = [g.strip() for g in rest.split(",") if g.strip()]
    if not globs:
        raise _err("--mcp " + server + ": после двоеточия нет тулов")
    return server, globs


def load_schema_file(path, *, cwd=None) -> dict:
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = Path(cwd or Path.cwd()) / p
    data = _read_json(p, "схеме ответа")
    if not isinstance(data, dict):
        raise _err("Схема ответа должна быть объектом JSON: " + str(p))
    return data


def cage_from_flags(*, tools=None, deny=(), allow=(), mcp=None, mcp_config="",
                    fs="none", roots=(), bash="off", fetch_hosts=(), fetch_any=False,
                    fetch_query=False, instructions="none", max_usd=0.0, max_steps=0,
                    max_tokens=0,
                    timeout_s=0.0, mode="", model="", schema=None, fail_on_deny=False,
                    cwd=None) -> Cage:
    """Разовая клетка из одних флагов: что не названо — закрыто.

    mcp: {сервер: [globs] | None}. Явно названный сервер обязателен
    (required): раз его попросили, работа без него бессмысленна.
    """
    base = Path(cwd) if cwd is not None else Path.cwd()
    cage = Cage(name="", source="флаги командной строки")
    cage.tools = [t.strip() for t in (tools or []) if t.strip()]
    cage.deny = [d for d in deny if d]
    cage.allow = [a for a in allow if a]
    cage.mcp = {s: McpRule(tools=list(g) if g else ["*"], required=True)
                for s, g in (mcp or {}).items()}
    if mcp_config:
        cfg = Path(mcp_config).expanduser()
        cfg = cfg if cfg.is_absolute() else base / cfg
        if not cfg.is_file():
            raise _err("Нет файла MCP-серверов: " + str(cfg))
        cage.mcp_config = str(cfg.resolve())
    cage.fs_mode = fs or "none"
    raw_roots = list(roots or ())
    if cage.fs_mode != "none" and not raw_roots:
        raw_roots = ["."]  # --fs ro без --root — только рабочая папка
    cage.bash = bash or "off"
    cage.fetch_hosts = None if fetch_any else [h.strip().lower() for h in fetch_hosts if h.strip()]
    cage.fetch_query = bool(fetch_query)
    cage.instructions = instructions or "none"
    cage.max_usd = float(max_usd or 0)
    cage.max_steps = int(max_steps or 0)
    cage.timeout_s = float(timeout_s or 0)
    cage.max_tokens = int(max_tokens or 0)
    cage.mode = mode or "normal"
    cage.model = model or ""
    cage.output_schema = schema
    cage.fail_on_deny = bool(fail_on_deny)
    return check_cage(cage, raw_roots=raw_roots, cwd=base, where="Флаги клетки")


# --- сужение ----------------------------------------------------------------------


def _has_glob(p: str) -> bool:
    return any(c in p for c in "*?[")


def _covers(wide: str, narrow_: str) -> bool:
    """Доказуемо ли, что всё, что ловит narrow_, ловит и wide.

    Общий случай для fnmatch неразрешим просто, поэтому признаём только
    очевидное; сомнение — значит «не покрывает», и клетка выходит уже.
    """
    if wide == narrow_ or wide == "*":
        return True
    if not _has_glob(narrow_):
        return fnmatch.fnmatchcase(narrow_, wide)
    if wide.endswith("*") and not _has_glob(wide[:-1]):
        return narrow_.startswith(wide[:-1])
    return False


def intersect_patterns(base: list, flags: list) -> list:
    """Пересечение двух списков шаблонов, никогда не шире base."""
    out: list = []
    for f in flags:
        for b in base:
            if _covers(b, f):
                pick = f
            elif _covers(f, b):
                pick = b
            else:
                continue
            if pick not in out:
                out.append(pick)
    return out


def _stricter(order, a: str, b: str) -> str:
    return a if order.index(a) <= order.index(b) else b


def _min_nonzero(a, b):
    a = a if a and math.isfinite(a) else 0
    b = b if b and math.isfinite(b) else 0
    if a and b:
        return min(a, b)
    return a or b


def _no_wider(order, current: str, wanted: str, flag: str) -> str:
    if not wanted:
        return current
    if wanted not in order:
        raise HumanError("worker_narrow", flag + ": неизвестное значение " + repr(wanted),
                         "Допустимые: " + ", ".join(order) + ".")
    if order.index(wanted) > order.index(current):
        raise HumanError("worker_narrow", flag + " " + wanted + " шире профиля (" + current + ")",
                         "Флаги только сужают клетку; расширить — правкой профиля.")
    return wanted


def narrow(cage: Cage, *, tools=None, deny=(), allow=(), max_usd=0, max_steps=0,
           timeout_s=0, mode="", schema=None, fs_roots=None, fs_mode="", bash="",
           max_tokens=0,
           fetch_hosts=None, fetch_any=False, fetch_query=None, instructions="",
           mcp=None, mcp_config="", fail_on_deny=False, model="", cwd=None) -> Cage:
    """Новая клетка, никогда не шире исходной.

    tools — пересечение, deny — объединение, лимиты — меньший из ненулевых,
    режим — строже из двух. Явная попытка расширить (fs rw при ro, bash при
    off, --fetch-any при списке хостов, чужой MCP-сервер, корень вне корней
    профиля) — ошибка: молча урезать значит гадать, чего хотел вызывающий.
    allow добавляются: они лишь говорят «да» на вопросы Предохранителя по
    тулам, которые клетка и так показывает, и hard_block не перебивают.
    """
    out = replace(cage, tools=list(cage.tools), deny=list(cage.deny), allow=list(cage.allow),
                  mcp={k: McpRule(list(v.tools), list(v.deny), v.required,
                                  {t: {f: list(p) for f, p in fs.items()} for t, fs in v.args.items()})
                      for k, v in cage.mcp.items()},
                  secrets=list(cage.secrets), fs_roots=list(cage.fs_roots),
                  fetch_hosts=None if cage.fetch_hosts is None else list(cage.fetch_hosts))
    if tools is not None:
        out.tools = intersect_patterns(cage.tools, [t.strip() for t in tools if t.strip()])
    for d in deny or ():
        if d and d not in out.deny:
            out.deny.append(d)
    for a in allow or ():
        if a and a not in out.allow:
            out.allow.append(a)
    out.max_usd = float(_min_nonzero(cage.max_usd, max_usd))
    out.max_steps = int(_min_nonzero(cage.max_steps, max_steps))
    out.timeout_s = float(_min_nonzero(cage.timeout_s, timeout_s))
    out.max_tokens = int(_min_nonzero(cage.max_tokens, max_tokens))
    if mode:
        if mode not in MODE_ORDER:
            raise HumanError("worker_narrow", "--mode: неизвестный режим " + repr(mode),
                             "Допустимые: " + ", ".join(MODE_ORDER) + ".")
        out.mode = _stricter(MODE_ORDER, cage.mode, mode)
    if schema is not None and cage.output_schema is None:
        out.output_schema = schema
    if model:
        out.model = model
    out.fail_on_deny = cage.fail_on_deny or bool(fail_on_deny)
    out.fs_mode = _no_wider(_FS_ORDER, cage.fs_mode, fs_mode, "--fs")
    out.bash = _no_wider(_BASH_ORDER, cage.bash, bash, "--bash")
    out.instructions = _no_wider(_INSTR_ORDER, cage.instructions, instructions, "--instructions")
    if out.fs_mode == "none":
        out.fs_roots = []
    elif fs_roots:
        base = Path(cwd) if cwd is not None else Path.cwd()
        wanted = _resolve_roots(list(fs_roots), base, "--root")
        for r in wanted:
            if not any(r == c or c in r.parents for c in cage.fs_roots):
                raise HumanError("worker_narrow", "--root " + str(r) + " вне корней профиля",
                                 "Корни профиля: " + ", ".join(str(c) for c in cage.fs_roots) + ".")
        out.fs_roots = wanted
    if fetch_any and cage.fetch_hosts is not None:
        raise HumanError("worker_narrow", "--fetch-any шире профиля",
                         "Профиль открывает только хосты: " + ", ".join(cage.fetch_hosts or ["(никакие)"]) + ".")
    if fetch_hosts:
        hosts = [h.strip().lower() for h in fetch_hosts if h.strip()]
        out.fetch_hosts = hosts if cage.fetch_hosts is None else intersect_patterns(cage.fetch_hosts, hosts)
    if fetch_query:
        if not cage.fetch_query:
            raise HumanError("worker_narrow", "--fetch-query шире профиля (fetch_query: false)",
                             "Флаги только сужают клетку.")
    elif fetch_query is False:
        out.fetch_query = False
    if mcp_config and mcp_config != cage.mcp_config:
        raise HumanError("worker_narrow", "--mcp-config нельзя подменить у профиля",
                         "Файл MCP-серверов задаёт профиль (mcp.config).")
    if mcp is not None:
        new = {}
        for server, globs in mcp.items():
            rule = out.mcp.get(server)
            if rule is None:
                raise HumanError("worker_narrow", "--mcp " + server + ": сервера нет в профиле",
                                 "Профиль разрешает: " + (", ".join(out.mcp) or "никакие") + ".")
            if globs:
                rule.tools = intersect_patterns(rule.tools, list(globs))
            new[server] = rule
        out.mcp = new
    # Сужение могло оставить fs-тулы без файлов — их просто нет, это не ошибка.
    if out.fs_mode == "none":
        out.tools = [t for t in out.tools if t not in FS_TOOLS]
    elif out.fs_mode == "ro":
        out.tools = [t for t in out.tools if t not in FS_WRITE_TOOLS]
    if out.bash == "off":
        out.tools = [t for t in out.tools if t != "bash"]
    return check_cage(out, where="Суженная клетка")
