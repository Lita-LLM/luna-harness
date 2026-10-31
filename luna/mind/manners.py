# Перенесено из BENDER: bender/mind/manners.py (коммит 6552ed660bfabf5202569f9905212b77dbf6d31a), адаптировано: у Луны манеры — Markdown-файлы, а не записи в настройках Бендера.
"""Манеры речи: переключаемый стиль того, как Луна говорит.

Агент остаётся тем же — имя, правила, дела. Манера меняет только слова:
характер, тон, длину. Активна одна манера (настройка agent.manner)
или ни одной (пустая строка — свой голос из ядра промпта).

Манера — Markdown-файл: заголовок-метаданные между --- (name, description),
дальше текст. Источники по возрастанию силы: встроенные в пакете
(luna/manners/), свои в LUNA_HOME/manners/, свои в .luna/manners/ проекта.
Своя манера перекрывает встроенную с тем же именем (имя — имя файла без .md).

Текст манеры — указание человека, поэтому он стоит в системном промпте.
Предохранитель живёт в коде, и манера его не ослабляет.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..core.errors import HumanError
from ..core.fsutil import atomic_write_text
from ..core.paths import luna_home

BUILTIN_DIR = Path(__file__).resolve().parents[1] / "manners"

MAX_TEXT = 16000
MAX_NAME = 80
MAX_DESCRIPTION = 400
ID_RULE = re.compile(r"[a-z0-9_-]{1,40}")

SOURCE_NAMES = {"builtin": "встроенная", "home": "своя (LUNA_HOME)", "project": "своя (проект)"}

FRAME = """# Манера речи: «{name}»

Манеру выбрал человек — настройкой или командой. Это указание о том, как говорить, а не о том, кто ты: имя, правила и дела остаются твоими. Манера касается только твоих слов, а не тулов, кода и файлов. Ядро выше — безопасность, Предохранитель, честность о сделанном — манера не отменяет; где стиль манеры расходится с «кратко, по делу» из ядра, в словах следуй манере. Манеру переключают тул manner и команда luna manners.

{text}"""


# --- разбор и запись файлов ---------------------------------------------

def parse_manner(raw: str, mid: str) -> dict:
    """Markdown с метаданными → {id, name, description, text}."""
    text = raw.replace("\r\n", "\n")
    name = ""
    description = ""
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
                key = key.strip().lower()
                value = value.strip().strip("«»\"'")
                if key == "name":
                    name = value
                elif key == "description":
                    description = value
    return {"id": mid, "name": name or mid, "description": description,
            "text": body.strip()}


def render_manner(name: str, description: str, text: str) -> str:
    """{name, description, text} → Markdown с метаданными."""
    lines = ["---", "name: " + name]
    if description:
        lines.append("description: " + description)
    lines.append("---")
    lines.append("")
    lines.append(text.strip())
    return "\n".join(lines) + "\n"


def validate(name: str, description: str, text: str) -> dict:
    """Проверка полей новой или правленой манеры (правила — как у Бендера)."""
    name = re.sub(r"\s+", " ", str(name or "")).strip()
    text = str(text or "").replace("\r\n", "\n").strip()
    description = re.sub(r"\s+", " ", str(description or "")).strip()
    if not name:
        raise HumanError("manner_name", "У манеры нет названия",
                         "Назови так, чтобы было понятно с первого взгляда: «Луна», «Краткая».")
    if len(name) > MAX_NAME:
        raise HumanError("manner_name", "Слишком длинное название манеры",
                         "До " + str(MAX_NAME) + " знаков.")
    if not text:
        raise HumanError("manner_text", "У манеры нет текста",
                         "Опиши, как говорить: тон, длина, что можно и чего не надо.")
    if len(text) > MAX_TEXT:
        raise HumanError("manner_text", "Слишком длинный текст манеры",
                         "До " + str(MAX_TEXT) + " знаков: это стиль речи, а не книга. "
                         "Он едет в каждом запросе.")
    if len(description) > MAX_DESCRIPTION:
        description = description[:MAX_DESCRIPTION]
    return {"name": name, "description": description, "text": text}


# --- источники -----------------------------------------------------------

def home_dir(settings=None) -> Path:
    """Каталог своих манер уровня пользователя: LUNA_HOME/manners."""
    base = Path(settings.home_dir) if settings is not None else luna_home()
    return base / "manners"


def project_dir(settings=None, cwd=None) -> Path:
    """Каталог своих манер проекта: .luna/manners в рабочем каталоге."""
    if cwd is not None:
        root = Path(cwd)
    elif settings is not None:
        root = Path(settings.project_path).parent.parent
    else:
        root = Path.cwd()
    return root / ".luna" / "manners"


def _scan(directory: Path, source: str) -> dict:
    found = {}
    if not directory.is_dir():
        return found
    for path in sorted(directory.glob("*.md")):
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        entry = parse_manner(raw, path.stem)
        if not entry["text"]:
            continue
        entry["source"] = source
        entry["path"] = str(path)
        found[entry["id"]] = entry
    return found


def library(settings=None, cwd=None) -> list[dict]:
    """Все манеры: встроенные, потом свои; своя перекрывает встроенную с тем же именем."""
    merged: dict[str, dict] = {}
    order: list[str] = []
    for source, directory in (("builtin", BUILTIN_DIR),
                              ("home", home_dir(settings)),
                              ("project", project_dir(settings, cwd))):
        for mid, entry in _scan(directory, source).items():
            if mid not in merged:
                order.append(mid)
            merged[mid] = entry
    return [merged[mid] for mid in order]


def get(settings, mid: str, cwd=None) -> dict | None:
    return next((m for m in library(settings, cwd) if m["id"] == mid), None)


def resolve(settings, query: str, cwd=None) -> dict:
    """Манера по id или названию (без учёта регистра, можно начало названия)."""
    q = (query or "").strip().strip("«»\"'").lower()
    if not q:
        raise HumanError("manner_name", "Не сказано, какую манеру выбрать",
                         "Назови id или название: luna manners покажет все.")
    items = library(settings, cwd)
    tests = (lambda m: m["id"].lower() == q,
             lambda m: m["name"].lower() == q,
             lambda m: m["name"].lower().startswith(q))
    for test in tests:
        found = [m for m in items if test(m)]
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            names = ", ".join("«" + m["name"] + "» (" + m["id"] + ")" for m in found[:6])
            raise HumanError("manner_ambiguous", "Под это название подходит несколько манер",
                             "Назови точнее: " + names)
    names = ", ".join("«" + m["name"] + "» (" + m["id"] + ")" for m in items)
    raise HumanError("manner_missing", "Нет манеры «" + str(query) + "»",
                     "Есть: " + (names or "пока ни одной") + ". Список: luna manners.")


# --- активная манера и слой промпта --------------------------------------

def active(settings, cwd=None) -> dict | None:
    """Включённая манера или None (свой голос). Настроенная, но не найденная — понятная ошибка."""
    if settings is None:
        return None
    mid = str(settings.get("agent.manner", "") or "").strip()
    if not mid:
        return None
    return resolve(settings, mid, cwd)


def section(entry: dict) -> str:
    return FRAME.format(name=entry["name"], text=entry["text"].strip())


def prompt_section(settings, cwd=None) -> str:
    """Слой манеры для системного промпта; пусто — манера не выбрана."""
    entry = active(settings, cwd)
    return section(entry) if entry else ""


# --- запись: выбор, создание, правка --------------------------------------

def choose(settings, query: str, cwd=None) -> dict | None:
    """Записать выбор в настройку agent.manner. Пустой запрос — свой голос."""
    if not (query or "").strip():
        settings.set("agent.manner", "")
        return None
    entry = resolve(settings, query, cwd)
    settings.set("agent.manner", entry["id"])
    return entry


def create(settings, mid: str, name: str, description: str, text: str) -> dict:
    """Новая своя манера — файлом в LUNA_HOME/manners."""
    mid = (mid or "").strip().lower()
    if not ID_RULE.fullmatch(mid):
        raise HumanError("manner_id", "Неверный id манеры «" + str(mid) + "»",
                         "Латинские буквы, цифры, _ и -, до 40 знаков: id — это имя файла.")
    clean = validate(name, description, text)
    if get(settings, mid) is not None:
        raise HumanError("manner_exists", "Манера с id " + mid + " уже есть",
                         "Правь её действием edit или выбери другой id.")
    directory = home_dir(settings)
    directory.mkdir(parents=True, exist_ok=True)
    atomic_write_text(directory / (mid + ".md"),
                      render_manner(clean["name"], clean["description"], clean["text"]))
    return get(settings, mid)


def edit(settings, query: str, cwd=None, *, name=None, description=None, text=None) -> dict:
    """Правка манеры: файл правится там, где лежит; встроенная получает свою копию поверх."""
    entry = resolve(settings, query, cwd)
    clean = validate(entry["name"] if name is None else name,
                     entry["description"] if description is None else description,
                     entry["text"] if text is None else text)
    if entry["source"] in ("home", "project"):
        target = Path(entry["path"])
    else:
        target = home_dir(settings) / (entry["id"] + ".md")
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(target, render_manner(clean["name"], clean["description"], clean["text"]))
    return get(settings, entry["id"], cwd)
