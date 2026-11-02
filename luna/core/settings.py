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
_f("ui.theme", "str", "luna", "Тема оформления чата: палитра данными (ui/themes.py) — встроенные luna, amoled, contrast, mono, light и свои из LUNA_HOME/themes")
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
_f("nibbler.context", "choice", "full", "Ниблер: что видит — full (стенограмма сессии целиком, до nibbler.context_chars) или digest (короткая сводка, дешевле)", ("full", "digest"))
_f("nibbler.context_chars", "int", 60000, "Ниблер: потолок стенограммы в символах — старое срезается первым")
_f("guard.enabled", "bool", True, "Предохранитель включён (выключение ослабляет безопасность)", human_only=True)
_f("guard.rules", "str", "", "Правила allow/ask/deny по строкам: «шаблон=действие», шаблон — тул или тул:подстрока аргументов", human_only=True)
_f("tools.bash_timeout", "int", 120, "Таймаут команды bash, секунд")
_f("tools.output_limit", "int", 8000, "Обрезка вывода тулов, символов (полный вывод — в файл)")
_f("web.enabled", "bool", True, "Веб-тулы (web_fetch, web_search) включены")
_f("web.searxng_url", "str", "", "Свой поисковик SearXNG для web_search (адрес, с включённым форматом json); пусто — DuckDuckGo")

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
_f("agents.enabled", "bool", True, "Агенты-дети: тулы assistant и crew (ассистенты и спутники)")
_f("agents.max_depth", "int", 1, "Глубина поколений спутников: спутник спутника — только если значение больше 1")
_f("agents.session_budget_usd", "float", 5.0, "Общий кошелёк сессии на всех агентов-детей, USD (0 — без лимита, траты всё равно считаются)")
_f("agents.max_children", "int", 8, "Сколько детей одновременно может держать один хозяин")
_f("agents.assistant_role", "str", "fast", "Роль модели ассистентов по умолчанию (main/fast/guard/vision)")
_f("agents.max_steps", "int", 0, "Шагов на ход ребёнка по умолчанию (0 — как у хозяина, agent.max_steps)")

# Команды и тайлы (веха 2, ЗАДАЧА-16): статус-строка, детали ленты,
# vim-режим ввода. Опись решений — docs/research/КОМАНДЫ.md.
_f("ui.statusline", "choice", "full",
   "Статус-строка чата: full — фаза, контекст, режим, модель, стоимость и очередь; compact — только фаза, контекст и модель; off — пустая",
   ("full", "compact", "off"))
_f("ui.details", "bool", True,
   "Детали ленты: вывод тулов виден свёрнутой строкой и рассуждения — счётчиком (Ctrl+O раскрывает запись); выкл — вывод и рассуждения скрыты, пока запись не раскрыта")
_f("ui.show_thinking", "bool", True, "Показывать записи рассуждений модели в ленте (/thinking переключает)")
_f("ui.vim_mode", "bool", False,
   "Vim-режим поля ввода: Esc — нормальный режим (h l 0 $ w b — движение, x — стереть символ, dd — строку, i/a/A — обратно в ввод)")

# Раскладка и уборка сессий (ЗАДАЧА-17): команды и клавиши не зависят от
# раскладки; удаление сессий — в корзину с самоочисткой, пустые — сами.
_f("ui.layout_fix", "bool", True,
   "Управление не зависит от раскладки: команды угадываются в ЙЦУКЕН (.зфтуд = /panel), у команд есть русские имена (/панель), аргументы и Ctrl/Alt-клавиши понимаются в любой раскладке")
_f("chat.sessions_trash_days", "int", 7,
   "Корзина сессий: сколько дней хранить удалённые из /sessions сессии в sessions/.trash (u в выбиралке отменяет последнее удаление)")
_f("chat.sessions_drop_empty", "bool", True,
   "Удалять пустые сессии (ни одной реплики человека) автоматически: при старте чата и при выходе из него; текущую сессию не трогаем")
# Звездопад (веха 5, ЗАДАЧА-15): потолки одного роя ячеек. Мать решает в
# их пределах; результаты потоков лежат в starfall.output_dir (пусто —
# LUNA_HOME/local/starfall), незаконченные докручивает starfall resume.
_f("starfall.enabled", "bool", True, "Звездопад: тулы cells и starfall (рой ячеек)")
_f("starfall.max_cells", "int", 100, "Потолок ячеек в одном потоке роя")
_f("starfall.max_parallel", "int", 8, "Потолок параллельных исполнителей роя")
_f("starfall.budget_usd", "float", 1.0, "Потолок бюджета одного потока роя, USD")
_f("starfall.ttl_s", "int", 300, "Потолок TTL одной ячейки роя, секунд")
_f("starfall.max_attempts", "int", 3, "Сколько раз перезапускать упавшую ячейку")
_f("starfall.output_dir", "str", "", "Папка результатов потоков роя (пусто — local/starfall в каталоге данных)")
# Изоляция bash ячеек (ЗАДАЧА-21): бэкенды данными в luna/starfall/sandbox.py;
# auto — первый доступный и ПРОВЕРЕННЫЙ пробным запуском, иначе Предохранитель
# спрашивает про рой с bash, как раньше. Лимиты процесса ячейки — ниже.
_f("starfall.sandbox", "choice", "auto",
   "Изоляция bash ячеек роя: auto — первый доступный бэкенд (bwrap, podman, docker); none — без изоляции (рой с bash всегда с подтверждением)",
   ("auto", "bwrap", "podman", "docker", "none"))
_f("starfall.sandbox_memory_mb", "int", 1024, "Лимит памяти одного процесса bash ячейки, МБ (rlimit или --memory контейнера)")
_f("starfall.sandbox_processes", "int", 256, "Лимит числа процессов bash ячейки (rlimit или --pids-limit контейнера)")
# Headless (luna -p, ЗАДАЧА-21): явные разрешения без человека. Шаблоны —
# как в guard.rules, но без действия: «тул» или «тул:подстрока аргументов»;
# hard_block Предохранителя не разрешают никогда.
_f("headless.allow", "str", "",
   "Шаблоны тулов, разрешённые в luna -p без подтверждения: по одному на строку, «тул» или «тул:подстрока аргументов» (fnmatch по имени тула)",
   human_only=True)
# Отправка во время хода (ЗАДАЧА-18): что делает Enter, пока агент занят.
_f("chat.send_mode", "choice", "inject",
   "Отправка сообщения во время хода: inject — подмешать в текущий ход перед следующим вызовом модели, queue — отдельным ходом после, interrupt — прервать ход и сразу отправить; на лету — Alt+S или /send, разово прервать и отправить — Alt+Enter; режим всегда виден в строке состояния",
   ("inject", "queue", "interrupt"))
# Мышление, усилие и поиск (ЗАДАЧА-19). ui.thinking — режим показа
# рассуждений; ui.show_thinking старше и не переименовывается: выключенный
# ui.show_thinking всегда означает hidden, что бы ни говорил ui.thinking.
_f("ui.thinking", "choice", "summary",
   "Показ рассуждений модели: summary — шапка «думает» и пересказ одной строкой от быстрой роли (полный текст — Ctrl+O), full — полный текст сразу, hidden — не показывать; на лету — /thinking",
   ("hidden", "summary", "full"))
_f("ui.thinking_summary_s", "int", 15,
   "Раз в сколько секунд рассуждений делать пересказ быстрой ролью (и ещё раз — по концу размышления)")
_f("model.effort", "choice", "auto",
   "Усилие рассуждений по умолчанию для всех ролей: auto — провайдер решает сам; шкала none < minimal < low < medium < high < xhigh < max; роутер переводит в формат провайдера, неподдерживаемое заменяется ближайшим; на лету — /effort и Alt+E",
   ("auto", "none", "minimal", "low", "medium", "high", "xhigh", "max"))
_f("model.effort_main", "str", "",
   "Усилие рассуждений главной роли (пусто — как model.effort); значения шкалы: none, minimal, low, medium, high, xhigh, max")
_f("model.effort_fast", "str", "",
   "Усилие рассуждений быстрой роли (пусто — как model.effort); значения шкалы: none, minimal, low, medium, high, xhigh, max")
_f("model.effort_guard", "str", "",
   "Усилие рассуждений роли охранника (пусто — как model.effort); значения шкалы: none, minimal, low, medium, high, xhigh, max")
_f("model.effort_vision", "str", "",
   "Усилие рассуждений роли зрения (пусто — как model.effort); значения шкалы: none, minimal, low, medium, high, xhigh, max")
_f("web.search_providers", "str", "searxng,exa,parallel,duckduckgo,brave,tavily,perplexity",
   "Цепочка провайдеров web_search через запятую, в порядке попыток: searxng (только если задан web.searxng_url), exa и parallel — бесплатные hosted MCP без ключа, duckduckgo — резерв, brave/tavily/perplexity — по ключам из luna setup")
_f("web.exa_url", "str", "https://mcp.exa.ai/mcp",
   "Адрес hosted MCP поиска Exa (без ключа — свободный лимит; ключ — через luna setup)")
_f("web.parallel_url", "str", "https://search.parallel.ai/mcp",
   "Адрес hosted MCP Parallel Search (бесплатно без ключа; ключ — через luna setup)")
_f("web.native_search", "bool", False,
   "Родной поиск провайдера модели как последний резерв цепочки web_search (сейчас умеет OpenRouter-модели с суффиксом :online); по умолчанию выключен")
# Мышь, блоки ленты, раскладка, стрим и привычки (ЗАДАЧА-20).
_f("ui.expand_all", "bool", False,
   "Все блоки ленты развёрнуты (мысли, тулы, вывод): Ctrl+O переключает всё сразу, клик по заголовку блока — только его")
_f("ui.mouse", "choice", "full",
   "Мышь в чате: full — клики, колесо и выделение; scroll — только колесо; off — мышь не трогаем вовсе (выделение — средствами терминала; Shift+протяжка — обход и при full)",
   ("full", "scroll", "off"))
_f("ui.click_link", "choice", "copy",
   "Клик по ссылке или пути файла в ленте: copy — скопировать в буфер, open — открыть (ссылку — браузером, файл — программой по умолчанию), off — ничего",
   ("copy", "open", "off"))
_f("ui.remember_layout", "bool", True,
   "Запоминать раскладку панелей: какие открыты, где стоят, веса и ширину колонки — в LUNA_HOME, отдельно для проекта и общая; /panels reset — сброс")
_f("ui.stream_fade", "choice", "off",
   "Проявление стрима: off — как обычно, token — свежий кусок текста проявляется целиком, char — посимвольно от тусклого к обычному; только 256/truecolor, ui.animations=off выключает и это",
   ("off", "token", "char"))
_f("ui.stream_fade_ms", "int", 220,
   "За сколько миллисекунд свежий текст стрима проявляется от тусклого к обычному (150–300 разумно)")
_f("ui.notify", "choice", "off",
   "Сигнал человеку: off — тихо, bell — звонок терминала по концу хода и когда нужен ответ, osc — системное уведомление (OSC 9), both — и то и другое",
   ("off", "bell", "osc", "both"))
_f("ui.title", "bool", True,
   "Заголовок окна терминала: «Луна · тема сессии · идёт ход/ждёт» (OSC 2); выкл — заголовок не трогаем")
_f("ui.paste_collapse_lines", "int", 10,
   "Вставка из буфера длиннее стольких строк сворачивается в поле ввода в [вставлено N строк] (клик по ней — развернуть обратно); 0 — не сворачивать")
_f("ui.diff_lines", "int", 12,
   "Сколько строк диффа правки (тулы edit/write) видно в ленте без раскрытия блока; остальное — по Ctrl+O или клику по заголовку")
_f("chat.bang_triggers", "bool", True,
   "Команда человека «!…»: после выполнения запустить ход модели по её выводу (команда и вывод и так уходят в контекст); off — только выполнить и показать")
_f("chat.auto_title", "bool", True,
   "Автоназвание сессии по первому сообщению человека, если имя не задано /rename")
_f("guard.always_rank_project", "int", -1,
   "Служебное: уровень риска «дальше без вопросов» для этого проекта (ставится ответом p в вопросе Предохранителя, не руками)", human_only=True)
_f("net.retry_attempts", "int", 3,
   "Сколько раз повторять запрос модели при обрыве сети или временной ошибке провайдера, прежде чем ход честно упадёт")
_f("net.retry_delay_s", "int", 3,
   "Пауза перед первой повторной попыткой сети, секунд; дальше — с удвоением, отсчёт виден в ленте")
# Нетбук и понятный рой (ЗАДАЧА-22): прокрутка без колеса на сенсорном
# экране и тачпаде VTE, слабый режим для медленных машин, порог роя.
_f("ui.scrollbar", "choice", "auto",
   "Полоса прокрутки ленты у её правого края: auto — видна, когда лента длиннее экрана; on — всегда; off — никогда. Клик по дорожке листает страницу, ползунок тянется мышью или пальцем",
   ("auto", "on", "off"))
_f("ui.drag_scroll", "bool", True,
   "Перетаскивание ленты пальцем или мышью: нажал на ленту и тянешь — лента едет за пальцем, как на телефоне. Выделение текста при этом — Shift+протяжка (средствами терминала) или долгое нажатие (~0,5 с, дальше тяни — выделится)")
_f("ui.perf", "choice", "auto",
   "Производительность интерфейса: auto — обычно полная, но при стабильно долгом кадре Луна сама перейдёт в слабый режим и предупредит строкой; low — слабый режим всегда (реже кадры, без проявления стрима, звёзд и плавных анимаций); full — никогда не снижать",
   ("auto", "low", "full"))
_f("starfall.min_inputs", "int", 3,
   "Минимум входов, с которого рой запускается: меньше — starfall отвечает подсказкой взять assistant для одной задачи (человек может явно настоять — вызов с force=true)")


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
