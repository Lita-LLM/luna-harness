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
    # Только человек: агент (тул luna) это поле не меняет ни с подтверждением,
    # ни без. Защита, режим и адрес провайдера (туда уходит ключ API).
    human_only: bool = False


SCHEMA: dict[str, Field] = {}


def _f(key: str, type_: str, default: Any, title: str, choices: tuple = (),
       human_only: bool = False) -> None:
    SCHEMA[key] = Field(key, type_, default, title, tuple(choices), human_only)


def is_human_only(key: str) -> bool:
    """Поле только для человека; незнакомые ключи защиты — тоже."""
    field = SCHEMA.get(key)
    if field is not None:
        return field.human_only
    return key.startswith("guard.")


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
_f("ui.hemisphere", "choice", "north",
   "Полушарие для настоящей фазы луны на заставке: north — северное, south — южное (серп зеркальный)",
   ("north", "south"))
_f("ui.panels", "bool", True, "Панели-тайлинг в чате (по умолчанию скрыты, открываются по Ctrl+T)")
_f("chat.session_log", "bool", True,
   "Журнал сессий чата: события и команды JSONL в папке sessions каталога данных (нужен для luna --continue и /sessions)")
_f("chat.sessions_scope", "choice", "all",
   "Выбиралка сессий (/sessions): all — все сессии, сессии этого каталога отмечены; cwd — только этот каталог",
   ("all", "cwd"))

# Модели и провайдеры (веха 1). Роль — строка вида «провайдер:модель»;
# пустая роль означает «как main». Провайдер по умолчанию задаётся тремя
# настройками ниже; setup пишет их и ключ в сейф секретов.
_f("model.provider", "str", "", "Провайдер по умолчанию: имя (openai, openrouter, anthropic, gemini, ollama, …)", human_only=True)
_f("model.kind", "choice", "openai", "Тип API провайдера по умолчанию",
   ("openai", "anthropic", "gemini", "fake"), human_only=True)
_f("model.base_url", "str", "", "Адрес API провайдера по умолчанию (пусто — адрес по типу)", human_only=True)
_f("model.main", "str", "", "Главная модель: «провайдер:модель» или просто модель")
_f("model.fast", "str", "", "Быстрая модель (разведка, сжатие); пусто — как главная")
_f("model.guard", "str", "", "Модель-охранник для режима review; пусто — как главная")
_f("model.vision", "str", "", "Модель для картинок; пусто — как главная")
_f("model.fallbacks", "str", "", "Запасные модели через запятую, если главная упала с ошибкой")
_f("model.context_tokens", "int", 128000, "Размер контекста главной модели, токенов (для процента заполнения)")
_f("model.price_in", "float", 0.0, "Цена главной модели за миллион входных токенов (0 — не считаем)")
_f("model.price_out", "float", 0.0, "Цена главной модели за миллион выходных токенов (0 — не считаем)")

# Агент и Предохранитель (веха 1)
_f("agent.max_steps", "int", 30, "Максимум шагов модели в одном ходе")
_f("agent.mode", "choice", "normal", "Режим Предохранителя по умолчанию",
   ("normal", "plan", "review", "auto", "yolo"), human_only=True)
_f("agent.manner", "str", "luna", "Манера речи: имя из luna manners (встроенные, свои в папке manners каталога данных, .luna/manners проекта); пусто — без манеры")
_f("nibbler.enabled", "bool", True, "Ниблер — компаньон внизу экрана: смотрит события агента и изредка говорит одну строку")
_f("nibbler.interval_sec", "int", 60, "Ниблер: минимальная пауза между репликами, секунд")
_f("nibbler.every_turns", "int", 2, "Ниблер: не чаще одной реплики в столько ходов")
_f("nibbler.to_agent", "choice", "serious", "Ниблер: какие замечания уходят главному агенту пометкой в следующий ход",
   ("serious", "all", "never"))
_f("nibbler.manner", "str", "nibbler", "Ниблер: его манера речи — файл манеры с «for: nibbler» в шапке; пусто — без манеры")
_f("nibbler.long_turn_sec", "int", 90, "Ниблер: ход дольше стольких секунд считается долгим и зовёт его высказаться")
_f("guard.enabled", "bool", True, "Предохранитель включён (выключение ослабляет безопасность)", human_only=True)
_f("guard.rules", "str", "", "Правила allow/ask/deny по строкам: «шаблон=действие», шаблон — тул или тул:подстрока аргументов", human_only=True)
_f("tools.bash_timeout", "int", 120, "Таймаут команды bash, секунд")
_f("tools.output_limit", "int", 8000, "Обрезка вывода тулов, символов (полный вывод — в файл)")
_f("web.enabled", "bool", True, "Веб-тулы (web_fetch, web_search) включены")

# Совместимость с другими харнессами (ЗАДАЧА-12)
_f("compat.global_instructions", "bool", True,
   "Читать глобальные инструкции других агентов: ~/.claude/CLAUDE.md, ~/.codex/AGENTS.md, ~/.gemini/GEMINI.md")
_f("compat.skills", "bool", True, "Скиллы из .claude/skills, .opencode/skill и папки skills Луны (тул skill)")
_f("compat.commands", "bool", True, "Слэш-команды из .claude/commands, .opencode/command и папки commands Луны")

# Чекпоинты и откат (веха 3, ЗАДАЧА-13): снимки файлов перед изменениями
# тулами — в LUNA_HOME/checkpoints/<сессия>/, откат — /undo и /rewind.
_f("checkpoints.enabled", "bool", True, "Чекпоинты файлов: снимки перед изменениями и откат ходов (/undo, /rewind)")
_f("checkpoints.max_file_bytes", "int", 1000000, "Максимальный размер файла для снимка, байт (больше — без снимка, откат файла не гарантирован)")
_f("checkpoints.max_total_bytes", "int", 50000000, "Общий лимит папки checkpoints, байт (старые сессии чистятся первыми)")
_f("checkpoints.keep_sessions", "int", 20, "Сколько последних сессий чекпоинтов хранить")

# Сжатие контекста (веха 3, ЗАДАЧА-13): «прилив» — старые ходы заменяются
# сводкой быстрой модели, последние ходы и план остаются как есть.
_f("compact.enabled", "bool", True, "Сжатие контекста: автоматический «прилив» по порогу и команда /compact")
_f("compact.threshold_pct", "int", 80, "Порог сжатия: процент заполненного контекста, после которого старые ходы заменяются сводкой")
_f("compact.keep_turns", "int", 2, "Сколько последних ходов сжатие оставляет как есть")
_f("compact.prompt", "str", "", "Свой промпт сводки сжатия (пусто — встроенный); транскрипт добавляется после промпта")


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
        self._stamps = self._file_stamps()

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
        self._stamps = self._file_stamps()

    def _file_stamps(self) -> tuple:
        out = []
        for path in (self.global_path, self.project_path):
            try:
                st = path.stat()
                out.append((st.st_mtime_ns, st.st_size))
            except OSError:
                out.append(None)
        return tuple(out)

    def reload_if_changed(self) -> list[dict]:
        """Горячая перезагрузка: файлы поменяли руками или другой процесс.

        Дёшево (два stat), зовётся перед ходом и при вводе — без фонового
        опроса. Возвращает изменения [{key, old, value}] и публикует
        settings.changed на каждое. Битый JSON не роняет: прошлые значения
        остаются, HumanError летит наверх — вызывающий покажет уведомление,
        а повторно ругаться на тот же битый файл не будем.
        """
        stamps = self._file_stamps()
        if stamps == getattr(self, "_stamps", None):
            return []
        self._stamps = stamps  # и при ошибке: тот же битый файл — одно уведомление
        before = dict(_flatten(self._project))
        before_g = dict(_flatten(self._global))
        new_global = self._load(self.global_path)
        new_project = self._load(self.project_path)
        old_values = {k: self.get(k) for k in set(before) | set(before_g) | set(SCHEMA)}
        self._global, self._project = new_global, new_project
        keys = set(old_values) | {k for k, _ in _flatten(new_global)} | {k for k, _ in _flatten(new_project)}
        changes = []
        for key in sorted(keys):
            old, new = old_values.get(key), self.get(key)
            if old != new:
                changes.append({"key": key, "old": old, "value": new})
                if self.bus is not None:
                    self.bus.publish({"type": "settings.changed", "key": key, "value": new,
                                      "old": old, "layer": "file"})
        return changes

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
                "default": field.default, "human_only": field.human_only,
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
        self._stamps = self._file_stamps()  # своя запись — не «внешнее изменение»
        if self.bus is not None:
            self.bus.publish({"type": "settings.changed", "key": key,
                              "value": value, "old": old, "layer": layer})

    @staticmethod
    def value_text(field_type: str, value: Any) -> str:
        if field_type == "bool":
            return "on" if value else "off"
        return str(value)
