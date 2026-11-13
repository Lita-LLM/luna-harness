"""Реестр провайдеров ИИ: сколько угодно источников моделей данными.

Файл LUNA_HOME/providers.json — имя → описание (тип API, адрес, откуда
взялся, включён ли, что о нём известно). Ключей тут нет: ключ живёт в
сейфе под именем «provider:<имя>» (router.core.secret_name).

Старая настройка одного провайдера (model.provider/kind/base_url) не
ломается: Router сначала смотрит сюда, потом — как раньше.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from ..core.fsutil import atomic_write_text
from ..core.paths import luna_home

KINDS = ("openai", "anthropic", "gemini")
ORIGINS = ("manual", "scan", "preset", "settings")
_NAME_RE = re.compile(r"^[\w.-]{1,40}$")


@dataclass
class ProviderEntry:
    name: str
    kind: str = "openai"
    base_url: str = ""
    origin: str = "manual"     # manual | scan | preset | settings
    enabled: bool = True
    note: str = ""
    # Что узнали проверкой: жив ли, сколько моделей, нужен ли ключ.
    alive: bool | None = None
    models: list = field(default_factory=list)
    needs_key: bool | None = None
    checked_at: float = 0.0
    added_at: float = 0.0

    @classmethod
    def from_dict(cls, data: dict) -> "ProviderEntry":
        known = {f.name for f in fields(cls)}
        clean = {k: v for k, v in (data or {}).items() if k in known}
        entry = cls(**clean)
        if entry.kind not in KINDS:
            entry.kind = "openai"
        if not isinstance(entry.models, list):
            entry.models = []
        return entry


def valid_name(name: str) -> bool:
    return bool(_NAME_RE.match(name or ""))


def slug(text: str) -> str:
    """Имя провайдера из адреса или подписи: «suspect118-5:20128» → «suspect118-5-20128»."""
    out = re.sub(r"[^\w.-]+", "-", (text or "").strip().lower()).strip("-.")
    return out[:40] or "provider"


class ProviderRegistry:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path is not None else luna_home() / "providers.json"
        self._entries: dict = {}
        self._mtime = None
        self.load()

    # --- файл ---

    def load(self) -> None:
        try:
            stat = self.path.stat()
        except OSError:
            self._entries, self._mtime = {}, None
            return
        if stat.st_mtime == self._mtime:
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8") or "{}")
        except (OSError, ValueError):
            raw = {}  # битый файл не роняет Луну: пустой реестр, файл не трогаем
        items = raw.get("providers", raw) if isinstance(raw, dict) else {}
        self._entries = {}
        for name, data in (items or {}).items():
            if valid_name(name) and isinstance(data, dict):
                self._entries[name] = ProviderEntry.from_dict({**data, "name": name})
        self._mtime = stat.st_mtime

    def save(self) -> None:
        data = {"providers": {name: {k: v for k, v in asdict(e).items() if k != "name"}
                              for name, e in sorted(self._entries.items())}}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self.path, json.dumps(data, ensure_ascii=False, indent=1))
        try:
            self._mtime = self.path.stat().st_mtime
        except OSError:
            self._mtime = None

    # --- записи ---

    def names(self) -> list:
        self.load()
        return sorted(self._entries)

    def entries(self) -> list:
        self.load()
        return [self._entries[n] for n in sorted(self._entries)]

    def get(self, name: str):
        self.load()
        return self._entries.get(name)

    def upsert(self, entry: ProviderEntry) -> ProviderEntry:
        if not valid_name(entry.name):
            raise ValueError("имя провайдера — буквы, цифры, точка, дефис, до 40 знаков")
        if entry.kind not in KINDS:
            raise ValueError("тип API: " + ", ".join(KINDS))
        self.load()
        old = self._entries.get(entry.name)
        if not entry.added_at:
            entry.added_at = old.added_at if old and old.added_at else time.time()
        self._entries[entry.name] = entry
        self.save()
        return entry

    def remove(self, name: str) -> bool:
        self.load()
        if self._entries.pop(name, None) is None:
            return False
        self.save()
        return True

    def find_by_url(self, base_url: str):
        want = (base_url or "").rstrip("/").lower()
        for entry in self.entries():
            if entry.base_url.rstrip("/").lower() == want:
                return entry
        return None

    def free_name(self, base: str) -> str:
        name = slug(base)
        taken = set(self.names())
        if name not in taken:
            return name
        for i in range(2, 1000):
            cand = (name[:36] + "-" + str(i))
            if cand not in taken:
                return cand
        return name + "-" + str(int(time.time()))
