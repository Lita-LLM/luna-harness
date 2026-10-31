# Перенесено из BENDER: bender/core/secrets.py (коммит 6552ed660bfabf5202569f9905212b77dbf6d31a), сокращено под Луну.
"""Сейф секретов: ~/.luna/secrets.json с правами 600, без шифрования (решение автора).

Значения секретов никогда не попадают в модель, в события, в логи и в вывод:
модули ссылаются на них по имени, а наружу значения уходят только замаскированными.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .errors import HumanError
from .fsutil import atomic_write_text

MASK = "•••"


def mask(value: str) -> str:
    """Маска для показа: первые и последние символы, середина скрыта."""
    if not value:
        return ""
    if len(value) <= 8:
        return "•" * len(value)
    head = value[:4]
    tail = value[-4:]
    return head + "…" + tail


class Secrets:
    """Файл вида {имя: {"value": строка, ...}}; читается лениво, пишется атомарно."""

    def __init__(self, path: Path | str):
        self.path = Path(path)

    def _load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise HumanError(
                "secrets_corrupt",
                "Не удалось прочитать сейф секретов",
                "Проверь файл secrets.json в каталоге Луны; он не перезаписан.",
                detail=str(exc),
            ) from None
        if not isinstance(data, dict):
            raise HumanError(
                "secrets_corrupt",
                "В сейфе секретов не объект JSON",
                "Файл должен быть словарём вида {\"имя\": {\"value\": \"…\"}}.",
            )
        return data

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self.path, json.dumps(data, ensure_ascii=False, indent=1) + "\n")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def get(self, name: str) -> str | None:
        item = self._load().get(name)
        if isinstance(item, dict):
            value = item.get("value")
            return value if isinstance(value, str) else None
        if isinstance(item, str):
            return item
        return None

    def set(self, name: str, value: str) -> None:
        data = self._load()
        data[name] = {"value": value}
        self._save(data)

    def delete(self, name: str) -> bool:
        data = self._load()
        if name in data:
            del data[name]
            self._save(data)
            return True
        return False

    def names(self) -> list[str]:
        return sorted(self._load())

    def masked(self, name: str) -> str:
        return mask(self.get(name) or "")

    def values(self) -> list[str]:
        """Все значения длиной от 6 — чтобы вычищать их из событий и вывода тулов."""
        out = []
        for item in self._load().values():
            value = item.get("value") if isinstance(item, dict) else item
            if isinstance(value, str) and len(value) >= 6:
                out.append(value)
        return out


def redact(text, secrets: Secrets | None) -> str:
    """Заменить все значения секретов в тексте на маску. None-текст не трогаем."""
    if not isinstance(text, str) or secrets is None:
        return text
    out = text
    for value in secrets.values():
        out = out.replace(value, MASK)
    return out


def redact_obj(obj, secrets: Secrets | None):
    """Рекурсивно вычистить секреты из словарей/списков/строк (для событий)."""
    if secrets is None:
        return obj
    if isinstance(obj, str):
        return redact(obj, secrets)
    if isinstance(obj, dict):
        return {key: redact_obj(value, secrets) for key, value in obj.items()}
    if isinstance(obj, list):
        return [redact_obj(value, secrets) for value in obj]
    return obj
