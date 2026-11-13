"""Профили моделей: роли, усилие и провайдеры одним именем.

Профиль — снимок «кто на какой роли»: model.main/fast/guard/vision,
усилие рассуждений и описания провайдеров, на которых эти модели живут
(тип и адрес из реестра, без ключей — ключи в сейфе по имени провайдера).
Применить профиль = выставить роли и вернуть в реестр недостающих
провайдеров; существующих не трогаем — там могли поправить адрес руками.

Файл — LUNA_HOME/model_profiles.json.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path

from ..core.fsutil import atomic_write_text
from .core import parse_ref
from .providers import ProviderEntry, ProviderRegistry

ROLES = ("main", "fast", "guard", "vision")
EFFORT_KEYS = ("model.effort", "model.effort_main", "model.effort_fast",
               "model.effort_guard", "model.effort_vision")


def capture(settings, registry: ProviderRegistry) -> dict:
    """Текущие роли, усилие и провайдеры — как профиль."""
    roles = {role: str(settings.get("model." + role) or "") for role in ROLES}
    effort = {key: str(settings.get(key) or "") for key in EFFORT_KEYS}
    default = str(settings.get("model.provider") or "")
    providers = {}
    for ref in roles.values():
        if not ref:
            continue
        name, _model = parse_ref(ref, default)
        entry = registry.get(name) if name else None
        if entry is not None:
            data = asdict(entry)
            # Живое состояние проверки — не часть профиля.
            for key in ("alive", "models", "needs_key", "checked_at", "added_at"):
                data.pop(key, None)
            providers[name] = data
    return {"roles": roles, "effort": effort, "providers": providers,
            "saved_at": time.time()}


def describe(profile: dict) -> str:
    roles = profile.get("roles") or {}
    main = roles.get("main") or "—"
    others = [r + "=" + roles[r] for r in ROLES[1:] if roles.get(r)]
    return main + (" · " + ", ".join(others) if others else " · остальные как главная")


class ProfileStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)

    def load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        items = data.get("profiles") if isinstance(data, dict) else None
        return {str(k): v for k, v in (items or {}).items() if isinstance(v, dict)}

    def _save(self, items: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self.path, json.dumps({"profiles": items},
                                                ensure_ascii=False, indent=1))

    def names(self) -> list:
        return sorted(self.load())

    def get(self, name: str):
        return self.load().get(name)

    def put(self, name: str, profile: dict) -> None:
        items = self.load()
        items[name] = profile
        self._save(items)

    def remove(self, name: str) -> bool:
        items = self.load()
        if items.pop(name, None) is None:
            return False
        self._save(items)
        return True


def apply(profile: dict, settings, registry: ProviderRegistry) -> list:
    """Выставить роли и усилие; недостающих провайдеров — в реестр.
    Вернуть имена провайдеров, которых пришлось добавить."""
    added = []
    for name, data in (profile.get("providers") or {}).items():
        if registry.get(name) is None and isinstance(data, dict):
            registry.upsert(ProviderEntry.from_dict({**data, "name": name,
                                                     "added_at": time.time()}))
            added.append(name)
    roles = profile.get("roles") or {}
    for role in ROLES:
        settings.set("model." + role, str(roles.get(role) or ""))
    for key, value in (profile.get("effort") or {}).items():
        if key in EFFORT_KEYS:
            settings.set(key, str(value or ("auto" if key == "model.effort" else "")))
    return added
