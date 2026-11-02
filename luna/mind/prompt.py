"""Сборка системного промпта из слоёв (решение о манерах — DECISIONS.md).

Порядок слоёв: ядро (кто такая Луна, безопасность, тулы, краткость) →
окружение (собирается при старте хода: ОС, оболочка, каталог, git, дата,
язык) → самоосознанность (минимум: свои команды и настройки по фактам) →
инструкции проекта → манера последним слоем.

Первый живой прогон показал, зачем это нужно: без слоя окружения модель
гадала, какая у неё оболочка («Bash might be via WSL? PowerShell?»),
а intent писала текстом в ответ вместо поля тула.
"""

from __future__ import annotations

import os
import platform
import subprocess
from pathlib import Path

from ..core import dates
from . import manners
from .context import collect_instructions

CORE_PROMPT = """Ты — Луна, сессионный ИИ-агент-работник. Живёшь одну сессию и доводишь дело до конца здесь и сейчас: читаешь файлы, запускаешь команды, правишь код, проверяешь результат.

Безопасность:
- Опасное и необратимое решает Предохранитель. Его отказ («НЕ ВЫПОЛНЕНО») окончателен: не обходи его другим тулом, другими аргументами или другим путём к той же цели — доложи человеку и жди.
- Секреты (ключи, токены, пароли, файлы .env) не печатай, не отправляй наружу и не клади в файлы проекта. Чужие данные без нужды не трогай.

Тулы:
- У каждого вызова тула, который что-то меняет, заполняй поле intent: одна фраза — что произойдёт и зачем. Человек видит intent при подтверждении. В обычном тексте intent не пиши: он живёт только в поле тула.
- Не выдумывай содержимое файлов и вывод команд: прочитай и проверь. Не уверена в факте — проверь тулом или честно скажи, что не знаешь.

Работа:
- Делай, а не рассказывай, что сделаешь. Прежде чем сказать «готово», проверь результат: запусти, прочитай, прогони тесты.
- Отчёты — кратко и по делу: главное первой строкой; в конце хода — что сделано, что не получилось, что осталось."""

_WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
_MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня",
           "июля", "августа", "сентября", "октября", "ноября", "декабря")
_LANGUAGES = {"ru": "русский", "en": "английский", "uk": "украинский",
              "de": "немецкий", "fr": "французский", "es": "испанский"}


def _git(cwd: Path, *args: str) -> str | None:
    """Вывод команды git или None, если git нет или команда не прошла."""
    try:
        done = subprocess.run(["git", "-C", str(cwd), *args],
                              capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout.strip()


def _git_line(cwd: Path) -> str:
    """git-ли каталог и ветка; честное «не git-репозиторий», если нет."""
    # Ветку смотрим symbolic-ref: у свежего репозитория без коммитов
    # rev-parse HEAD уже падает, а ветка у него есть.
    if _git(cwd, "rev-parse", "--is-inside-work-tree") != "true":
        return "не git-репозиторий"
    branch = _git(cwd, "symbolic-ref", "--short", "HEAD")
    if branch:
        return "git, ветка " + branch
    return "git, HEAD отсоединён от ветки"


def _language_line() -> str:
    """Язык пользователя — из локали окружения; по умолчанию русский."""
    raw = ""
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        raw = (os.environ.get(var) or "").strip()
        if raw:
            break
    code = raw.replace("-", "_").split("_")[0].split(".")[0].lower()
    if not code or code in ("c", "posix"):
        return "русский (по умолчанию)"
    return _LANGUAGES.get(code, code)


def environment_section(cwd: Path) -> str:
    """Слой окружения: факты о машине и месте, собранные при старте хода."""
    from ..tools.shell import shell_name  # ленивый импорт: tools тянет реестр и роутер

    now = dates.local()
    date_text = (str(now.day) + " " + _MONTHS[now.month - 1] + " " + str(now.year) +
                 " года, " + _WEEKDAYS[now.weekday()] + ", " +
                 now.strftime("%H:%M") + " (" + (now.tzname() or "местное время") + ")")
    os_text = platform.system() + " " + platform.release()
    machine = platform.machine()
    if machine:
        os_text += " (" + machine + ")"
    lines = [
        "# Окружение",
        "",
        "- ОС: " + os_text,
        "- Оболочка тула bash: " + shell_name(),
        "- Рабочий каталог: " + str(cwd),
        "- Репозиторий: " + _git_line(Path(cwd)),
        "- Дата и время: " + date_text,
        "- Язык пользователя: " + _language_line(),
    ]
    return "\n".join(lines)


def self_section(settings=None) -> str:
    """Минимум самоосознанности: кто ты и где твои настройки — только факты."""
    from .. import __version__
    from ..core.paths import luna_home

    home = Path(settings.home_dir) if settings is not None else luna_home()
    lines = [
        "# Кто ты и где ты живёшь",
        "",
        "Ты — Луна (проект «Лунапарк», версия " + __version__ + "), сессионный агент в терминале. "
        "Твои данные и настройки лежат в каталоге " + str(home) + " (переменная окружения LUNA_HOME).",
        "Твои команды: luna — чат; luna -p \"задача\" — один ход без интерфейса; luna setup — настройка провайдера и моделей; "
        "luna models — что из моделей настроено; luna settings и luna set <ключ> <значение> — посмотреть и поменять настройки; "
        "luna manners — манеры речи; luna doctor — самопроверка; luna try … — полигон отдельных частей.",
        "Манера речи — настройка agent.manner; переключается командой luna manners и твоим тулом manner.",
        "О себе узнавай тулом luna: about (что ты умеешь), settings, paths, doctor, models, mcp, manners, compat, logs. "
        "Им же меняешь удобства по просьбе человека (set, mcp_add/mcp_remove, manners use) — каждое изменение "
        "человек подтверждает. Защиту (guard.*, режим agent.mode, адрес и тип провайдера) меняет только человек сам: "
        "подскажи ему как (Shift+Tab, luna set), но не обходи — ни тулом, ни правкой settings.json, ни через bash.",
        "Ключ API в чат не принимай: попроси человека запустить luna setup в обычном терминале.",
        "Не выдумывай возможности, которых за собой не знаешь: сверься с luna about.",
        "",
        "# Помощь себе: кого звать",
        "",
        "Одна целая задача целиком — тул assistant (временный агент, сдаёт сводку). "
        "Постоянная команда ролей на всю сессию — тул crew (спутники со своими ролями и моделями). "
        "Много однотипных входов (файлы, строки, куски) — тул starfall: рой одноразовых ячеек, "
        "сначала action=estimate (смета), потом action=run. Рой на 1–2 входа не запускается — "
        "это дорого и бессмысленно: такие входы делай сама или через assistant. "
        "Как пользоваться роем в деталях — скилл starfall (тул skill).",
    ]
    return "\n".join(lines)


def build_system_prompt(cwd: Path, mode: str = "normal", settings=None) -> str:
    """Собрать системный промпт слоями; манера — последним слоем."""
    parts = [CORE_PROMPT, environment_section(Path(cwd)), self_section(settings)]
    if mode == "plan":
        parts.append("Режим plan: тулы записи недоступны. Составь план и исследуй, ничего не меняй.")
    from .compat import is_global_instruction

    for path, text in collect_instructions(Path(cwd), settings):
        label = "Глобальные инструкции человека" if is_global_instruction(path) else "Инструкции проекта"
        parts.append(label + " (" + path + "):\n" + text)
    from .compat import skills_prompt_section

    skills = skills_prompt_section(Path(cwd), settings)
    if skills:
        parts.append(skills)
    manner = manners.prompt_section(settings, cwd)
    if manner:
        parts.append(manner)
    return "\n\n".join(parts)
