"""Настройки со схемой в коде (идея — BENDER core/settings.py, здесь написана
компактная версия под Луну: без базы данных, слои — два JSON-файла).

У каждой настройки: ключ, тип, значение по умолчанию, описание по-русски и
допустимые значения. Слои, от слабого к сильному: умолчания → глобальный файл
<LUNA_HOME>/settings.json → проектный ./.luna/settings.json. Незнакомые поля
в файлах при записи сохраняются; ключи не переименовываются никогда. Запись
атомарная. Изменение публикует событие settings.changed на шине — горячая
перезагрузка позже просто подпишется на него.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import HumanError
from .fsutil import atomic_write_text
from .paths import luna_home

SOURCE_NAMES = {"default": "умолчание", "global": "глобально", "project": "проект"}


@dataclass(frozen=True)
class Field:
    key: str            # полный ключ: "ui.color"
    type: str           # bool | int | float | str | choice
    default: Any
    title: str          # описание по-русски
    choices: tuple = ()  # для choice: допустимые значения


SCHEMA: dict[str, Field] = {}


def _f(key: str, type_: str, default: Any, title: str, choices: tuple = ()) -> None:
    SCHEMA[key] = Field(key, type_, default, title, tuple(choices))


_f("ui.color", "choice", "auto", "Цвет терминала: auto — по возможностям, иначе вручную",
   ("auto", "truecolor", "256", "16", "mono"))
_f("ui.unicode", "choice", "auto", "Юникод в интерфейсе: auto — по терминалу, on, off",
   ("auto", "on", "off"))
_f("ui.emoji", "choice", "auto",
   "Эмодзи в интерфейсе: auto — по терминалу (на Windows без Windows Terminal выключены), on, off",
   ("auto", "on", "off"))
_f("ui.animations", "bool", True, "Анимации: заставка, спиннеры, мерцание звёзд")
_f("ui.splash", "bool", True, "Заставка с восходом луны при старте")
_f("ui.theme", "choice", "luna", "Тема оформления (пока одна)", ("luna",))


def _dig(data: dict, key: str):
    """Значение по точечному ключу из вложенных словарей; None, если нет."""
    node: Any = data
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None, False
        node = node[part]
    return node, True


def _plant(data: dict, key: str, value: Any) -> None:
    parts = key.split(".")
    node = data
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[parts[-1]] = value


def _flatten(data: dict, prefix: str = ""):
    for k, v in data.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            yield from _flatten(v, key + ".")
        else:
            yield key, v


class Settings:
    def __init__(self, *, home: Path | str | None = None, cwd: Path | str | None = None, bus=None):
        self.home_dir = Path(home).expanduser() if home is not None else luna_home()
        self.global_path = self.home_dir / "settings.json"
        base = Path(cwd) if cwd is not None else Path.cwd()
        self.project_path = base / ".luna" / "settings.json"
        self.bus = bus
        self._global = self._load(self.global_path)
        self._project = self._load(self.project_path)

    @staticmethod
    def _load(path: Path) -> dict:
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise HumanError(
                "settings_parse",
                f"Не читается файл настроек {path}",
                "Исправь JSON вручную или удали файл — Луна начнёт с умолчаний.",
                detail=str(exc),
            ) from None
        if not isinstance(data, dict):
            raise HumanError("settings_parse", f"В файле настроек {path} не объект JSON",
                             "Файл должен быть словарём вида {\"ui\": {\"color\": \"auto\"}}.")
        return data

    def reload(self) -> None:
        self._global = self._load(self.global_path)
        self._project = self._load(self.project_path)

    # чтение

    def get(self, key: str, default: Any = None) -> Any:
        value, found = _dig(self._project, key)
        if found:
            return value
        value, found = _dig(self._global, key)
        if found:
            return value
        field = SCHEMA.get(key)
        if field is not None:
            return field.default
        return default

    def source(self, key: str) -> str:
        if _dig(self._project, key)[1]:
            return "project"
        if _dig(self._global, key)[1]:
            return "global"
        return "default"

    def describe(self) -> list[dict]:
        """Все настройки схемы: ключ, значение, источник, описание."""
        out = []
        for key, field in SCHEMA.items():
            out.append({
                "key": key, "value": self.get(key), "source": self.source(key),
                "title": field.title, "type": field.type, "choices": field.choices,
                "default": field.default,
            })
        return out

    def unknown_keys(self) -> list[str]:
        """Ключи из файлов, которых нет в схеме: они сохраняются, но не действуют."""
        known = set(SCHEMA)
        out = set()
        for data in (self._global, self._project):
            for key, _ in _flatten(data):
                if key not in known:
                    out.add(key)
        return sorted(out)

    # запись

    def validate(self, key: str, value: Any) -> Any:
        field = SCHEMA.get(key)
        if field is None:
            return value  # незнакомый ключ не запрещаем: будущие версии спасибо скажут
        if field.type == "bool":
            if not isinstance(value, bool):
                raise HumanError("settings_value", f"Настройка {key} — переключатель",
                                 "Значение: on или off.")
        elif field.type == "choice":
            if value not in field.choices:
                raise HumanError("settings_value", f"Недопустимое значение {value!r} для {key}",
                                 "Допустимые: " + ", ".join(field.choices) + ".")
        elif field.type == "int":
            if not isinstance(value, int) or isinstance(value, bool):
                raise HumanError("settings_value", f"Настройка {key} — целое число", "")
        elif field.type == "float":
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise HumanError("settings_value", f"Настройка {key} — число", "")
        elif field.type == "str":
            if not isinstance(value, str):
                raise HumanError("settings_value", f"Настройка {key} — строка", "")
        return value

    def parse_value(self, key: str, text: str) -> Any:
        """Текст из командной строки → значение по типу поля схемы."""
        field = SCHEMA.get(key)
        if field is None:
            raise HumanError("settings_key", f"Нет настройки {key}",
                             "Список настроек: luna settings.")
        if field.type == "bool":
            low = text.strip().lower()
            if low in ("on", "true", "1", "yes", "да"):
                return True
            if low in ("off", "false", "0", "no", "нет"):
                return False
            raise HumanError("settings_value", f"Непонятное значение {text!r} для {key}",
                             "Переключатель понимает: on / off.")
        if field.type == "int":
            try:
                return int(text)
            except ValueError:
                raise HumanError("settings_value", f"Не целое число: {text!r}", "") from None
        if field.type == "float":
            try:
                return float(text)
            except ValueError:
                raise HumanError("settings_value", f"Не число: {text!r}", "") from None
        return self.validate(key, text)

    def set(self, key: str, value: Any, layer: str = "global") -> None:
        value = self.validate(key, value)
        data = self._project if layer == "project" else self._global
        path = self.project_path if layer == "project" else self.global_path
        old, _ = _dig(data, key)
        _plant(data, key, value)
        text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        atomic_write_text(path, text)
        if self.bus is not None:
            self.bus.publish({"type": "settings.changed", "key": key,
                              "value": value, "old": old, "layer": layer})

    @staticmethod
    def value_text(field_type: str, value: Any) -> str:
        if field_type == "bool":
            return "on" if value else "off"
        return str(value)
