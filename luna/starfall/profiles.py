"""Профили ячеек роя данными (ЗАДАЧА-15, docs/РОЙ.md §5).

Профиль — Markdown-файл с шапкой, как манеры (luna/mind/manners.py):
метаданные между --- (caste, name, description, model, pool, tools,
write, input, output, ttl, budget_usd, budget_tokens, can_spawn, mail,
steps), дальше — текст промпта. Встроенные лежат в пакете luna/cells/,
свои — в LUNA_HOME/cells/ (на весь харнесс, перекрывают встроенные с той
же кастой), третьим слоем — профили памяти сессии (их держит инструмент
cells на агенте матери, сюда они приходят словарём session).

Модель профиля: роль (fast/main/…), конкретная ссылка «провайдер:модель»
или пул — поле pool списком адресов одной модели через запятую, модель
при этом задаётся полем model.

Адрес пула может нести имя секрета и тип (ЗАДАЧА-21): запись вида
`http://узел/v1 | secret: pool:узел1 | kind: openai` (в словаре профиля —
{"url": …, "secret": "pool:узел1", "kind": "openai"}). Значение ключа
живёт только в сейфе под именем pool:<имя>; в профиль пишется лишь имя.
Поле net: true разрешает bash ячейки сеть (по умолчанию изоляции сеть
выключена).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ..core.fsutil import atomic_write_text
from ..core.paths import luna_home

BUILTIN_DIR = Path(__file__).resolve().parents[1] / "cells"

CASTE_RULE = re.compile(r"[a-z][a-z0-9_-]{0,15}")
WRITE_MODES = ("none", "sandbox", "worktree", "project")
MAIL_SCHEMES = ("none", "tree", "board", "neighbors")
MAX_PROMPT = 8000

SOURCE_NAMES = {"builtin": "встроенный", "home": "свой", "session": "сессии"}


def parse_pool_entry(raw) -> dict:
    """Один адрес пула → {"url", "secret", "kind", "provider"}.

    Принимает строку (адрес, дальше через «|» или пробелом: secret:
    pool:<имя>, kind: <тип>, provider: <имя>) и словарь с ключами
    url/address, secret, kind, provider. Имя секрета нормализуется к
    виду pool:<имя> — именно так ключ лежит в сейфе.
    """
    out = {"url": "", "secret": "", "kind": "", "provider": ""}
    if isinstance(raw, dict):
        out["url"] = str(raw.get("url") or raw.get("address") or
                         raw.get("base_url") or "").strip()
        out["secret"] = str(raw.get("secret") or raw.get("secret_name") or "").strip()
        out["kind"] = str(raw.get("kind") or raw.get("type") or "").strip()
        out["provider"] = str(raw.get("provider") or "").strip()
    else:
        text = str(raw or "").strip()
        head = text.split("|", 1)[0].strip()
        out["url"] = head.split()[0] if head else ""
        for key, field in (("secret", "secret"), ("kind", "kind"),
                           ("type", "kind"), ("provider", "provider")):
            match = re.search(key + r"\s*[:=]\s*([^\s|,]+)", text)
            if match and not out[field]:
                out[field] = match.group(1)
    secret = out["secret"]
    if secret.startswith("secret:"):
        secret = secret[len("secret:"):].strip()
    if secret and not secret.startswith("pool:"):
        secret = "pool:" + secret
    out["secret"] = secret
    return out


def render_pool_entry(spec: dict) -> str:
    """Адрес пула обратно в строку шапки профиля (имя секрета, не значение)."""
    text = str(spec.get("url") or "")
    if spec.get("secret"):
        text += " | secret: " + str(spec["secret"])
    if spec.get("kind"):
        text += " | kind: " + str(spec["kind"])
    if spec.get("provider"):
        text += " | provider: " + str(spec["provider"])
    return text


@dataclass
class CellProfile:
    caste: str
    name: str = ""
    description: str = ""
    model: str = "fast"            # роль или ссылка «провайдер:модель»
    pool: list = field(default_factory=list)  # адреса одной модели (пул, §7)
    pool_specs: list = field(default_factory=list)  # те же адреса словарями:
    # {"url", "secret", "kind", "provider"} — secret только ИМЯ (pool:<имя>)
    net: bool = False              # сеть для bash ячейки (изоляция, ЗАДАЧА-21)
    prompt: str = ""
    tools: list = field(default_factory=list)
    write_mode: str = "none"       # none | sandbox | worktree | project
    input_hint: str = ""
    output_hint: str = ""
    ttl_s: float = 300.0
    budget_usd: float = 0.0        # 0 — без личного лимита (только общий роя)
    budget_tokens: int = 0
    can_spawn: bool = False
    mail: str = "tree"
    steps: int = 4
    source: str = "builtin"
    path: str = ""

    def to_dict(self) -> dict:
        pool = []
        specs = self.pool_specs or [parse_pool_entry(u) for u in self.pool]
        for spec in specs:
            if spec.get("secret") or spec.get("kind") or spec.get("provider"):
                pool.append(dict(spec))
            else:
                pool.append(spec.get("url", ""))
        return {"caste": self.caste, "name": self.name,
                "description": self.description, "model": self.model,
                "pool": pool, "net": bool(self.net), "prompt": self.prompt,
                "tools": list(self.tools), "write_mode": self.write_mode,
                "input_hint": self.input_hint, "output_hint": self.output_hint,
                "ttl_s": self.ttl_s, "budget_usd": self.budget_usd,
                "budget_tokens": self.budget_tokens,
                "can_spawn": self.can_spawn, "mail": self.mail,
                "steps": self.steps, "source": self.source}

    @staticmethod
    def from_dict(data: dict) -> "CellProfile":
        prof = CellProfile(caste=str(data.get("caste", "")).strip().lower())
        prof.name = str(data.get("name") or prof.caste)
        prof.description = str(data.get("description") or "")
        prof.model = str(data.get("model") or "fast")
        pool = data.get("pool") or []
        if isinstance(pool, str):
            pool = [p.strip() for p in pool.split(",") if p.strip()]
        specs = [parse_pool_entry(p) for p in pool]
        specs = [s for s in specs if s.get("url")]
        prof.pool_specs = specs
        prof.pool = [s["url"] for s in specs]
        raw_net = data.get("net")
        if isinstance(raw_net, str):
            prof.net = raw_net.strip().lower() in ("true", "1", "yes", "да")
        else:
            prof.net = bool(raw_net)
        prof.prompt = str(data.get("prompt") or "")
        tools = data.get("tools") or []
        if isinstance(tools, str):
            tools = [t.strip() for t in tools.split(",") if t.strip()]
        prof.tools = [str(t) for t in tools]
        prof.write_mode = str(data.get("write_mode") or data.get("write") or "none")
        prof.input_hint = str(data.get("input_hint") or data.get("input") or "")
        prof.output_hint = str(data.get("output_hint") or data.get("output") or "")
        prof.ttl_s = float(data.get("ttl_s") or data.get("ttl") or 300)
        prof.budget_usd = float(data.get("budget_usd") or 0.0)
        prof.budget_tokens = int(data.get("budget_tokens") or 0)
        raw_spawn = data.get("can_spawn")
        if isinstance(raw_spawn, str):
            prof.can_spawn = raw_spawn.strip().lower() in ("true", "1", "yes", "да")
        else:
            prof.can_spawn = bool(raw_spawn)
        prof.mail = str(data.get("mail") or "tree")
        prof.steps = int(data.get("steps") or 4)
        prof.source = str(data.get("source") or "session")
        return prof


# --- разбор и запись файлов -----------------------------------------------


def parse_profile(raw: str, caste_hint: str = "") -> CellProfile:
    """Markdown с шапкой → CellProfile. Шапки нет — весь текст промпт."""
    text = raw.replace("\r\n", "\n")
    data: dict = {}
    body = text
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            head = text[4:end]
            body = text[end + 4:]
            for line in head.splitlines():
                key, sep, value = line.partition(":")
                if not sep:
                    continue
                data[key.strip().lower()] = value.strip().strip("«»\"'")
    if caste_hint and not data.get("caste"):
        data["caste"] = caste_hint
    prof = CellProfile.from_dict(data)
    prof.prompt = body.strip()
    return prof


def render_profile(prof: CellProfile) -> str:
    """CellProfile → Markdown с шапкой (формат файлов cells/)."""
    lines = ["---",
             "name: " + (prof.name or prof.caste),
             "caste: " + prof.caste]
    if prof.description:
        lines.append("description: " + prof.description)
    lines.append("model: " + prof.model)
    if prof.pool:
        specs = prof.pool_specs or [parse_pool_entry(u) for u in prof.pool]
        lines.append("pool: " + ", ".join(render_pool_entry(s) for s in specs))
    if prof.net:
        lines.append("net: true")
    lines.append("tools: " + ", ".join(prof.tools))
    lines.append("write: " + prof.write_mode)
    if prof.input_hint:
        lines.append("input: " + prof.input_hint)
    if prof.output_hint:
        lines.append("output: " + prof.output_hint)
    lines.append("ttl: " + str(int(prof.ttl_s)))
    if prof.budget_usd:
        lines.append("budget_usd: " + str(prof.budget_usd))
    if prof.budget_tokens:
        lines.append("budget_tokens: " + str(prof.budget_tokens))
    lines.append("can_spawn: " + ("true" if prof.can_spawn else "false"))
    lines.append("mail: " + prof.mail)
    lines.append("steps: " + str(prof.steps))
    lines.append("---")
    lines.append("")
    lines.append(prof.prompt.strip())
    return "\n".join(lines) + "\n"


def validate_profile(prof: CellProfile) -> CellProfile:
    """Проверка полей; проблемы — ValueError по-русски."""
    caste = (prof.caste or "").strip().lower()
    if not CASTE_RULE.fullmatch(caste):
        raise ValueError("плохая каста «" + str(prof.caste) + "»: латиница с буквы, "
                         "до 16 знаков (это часть адреса ячейки)")
    prof.caste = caste
    if prof.write_mode not in WRITE_MODES:
        raise ValueError("режим записи write: один из " + ", ".join(WRITE_MODES))
    if prof.mail not in MAIL_SCHEMES:
        raise ValueError("схема почты mail: одна из " + ", ".join(MAIL_SCHEMES))
    if len(prof.prompt or "") > MAX_PROMPT:
        raise ValueError("промпт профиля длиннее " + str(MAX_PROMPT) + " знаков")
    prof.ttl_s = max(1.0, float(prof.ttl_s or 300))
    prof.steps = max(1, int(prof.steps or 4))
    return prof


# --- источники ---------------------------------------------------------------


def home_dir(settings=None) -> Path:
    """Каталог своих профилей: LUNA_HOME/cells."""
    base = Path(settings.home_dir) if settings is not None else luna_home()
    return base / "cells"


def _scan(directory: Path, source: str) -> dict:
    found: dict = {}
    if not directory.is_dir():
        return found
    for path in sorted(directory.glob("*.md")):
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        prof = parse_profile(raw, path.stem.lower())
        if not prof.caste:
            continue
        prof.source = source
        prof.path = str(path)
        found[prof.caste] = prof
    return found


def library(settings=None, session: dict | None = None) -> list:
    """Все профили: встроенные → свои → сессии; поздний слой перекрывает."""
    merged: dict = {}
    order: list = []
    for source, profs in (("builtin", _scan(BUILTIN_DIR, "builtin")),
                          ("home", _scan(home_dir(settings), "home"))):
        for caste, prof in profs.items():
            if caste not in merged:
                order.append(caste)
            merged[caste] = prof
    for caste, prof in (session or {}).items():
        if caste not in merged:
            order.append(caste)
        merged[caste] = prof
    return [merged[c] for c in order]


def get_profile(caste: str, settings=None, session: dict | None = None) -> CellProfile | None:
    caste = (caste or "").strip().lower()
    for prof in library(settings, session):
        if prof.caste == caste:
            return prof
    return None


def save_home_profile(prof: CellProfile, settings=None) -> Path:
    """Сохранить профиль на весь харнесс: файл в LUNA_HOME/cells."""
    validate_profile(prof)
    folder = home_dir(settings)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (prof.caste + ".md")
    atomic_write_text(path, render_profile(prof))
    return path


def editable_path(caste: str, settings=None) -> Path:
    """Файл профиля для редактора человека: свой правится на месте,
    встроенный сначала получает свою копию поверх (как манеры)."""
    prof = get_profile(caste, settings)
    if prof is None:
        raise ValueError("нет профиля «" + str(caste) + "»")
    if prof.source == "home" and prof.path:
        return Path(prof.path)
    return save_home_profile(prof, settings)


# --- профили памяти сессии (слой на агенте матери) ----------------------------


def session_profiles(agent) -> dict:
    """Словарь {каста: CellProfile} профилей сессии этого агента."""
    store = getattr(agent, "_cells_session", None)
    if store is None:
        store = {}
        try:
            agent._cells_session = store
        except Exception:  # noqa: BLE001 — объект мог запретить запись
            pass
    return store
