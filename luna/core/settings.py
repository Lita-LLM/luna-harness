"""Настройки со схемой в коде (идея — BENDER core/settings.py, здесь написана
компактная версия под Луну: без базы данных, слои — два JSON-файла).

У каждой настройки: ключ, тип, значение по умолчанию, описание по-русски и
допустимые значения. Слои, от слабого к сильному: умолчания → глобальный файл
<LUNA_HOME>/settings.json → проектный ./.luna/settings.json. Проект вне
security.trusted_projects защиту только сужает (см. _narrow_project). Незнакомые поля
в файлах при записи сохраняются; ключи не переименовываются никогда. Запись
атомарная. Изменение публикует событие settings.changed на шине — горячая
перезагрузка позже просто подпишется на него.
"""

from __future__ import annotations

import json
import math
import os
import re
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
    type: str           # bool | int | float | str | choice | list | dict
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
_f("ui.click_hints", "bool", True,
   "Подсказки клавиш («Esc — закрыть», «Enter — да») нажимаются мышью и пальцем: клик подаёт ту же клавишу")
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
_f("scan.workers", "int", 1024, "Скан сети в пульте: сколько портов стучать параллельно")
_f("scan.connect_timeout", "float", 0.35, "Скан сети: сколько ждать ответа порта, секунд (своя сеть отвечает быстро)")
_f("scan.subnets", "bool", True, "Скан сети: обходить соседей по своим локальным подсетям /24, а не только свой компьютер и имена из hosts")
_f("scan.extra_hosts", "str", "", "Скан сети: свои узлы через запятую в добавок к hosts (имена или IP)")
_f("ui.command_aliases", "str", "",
   "Свои алиасы слэш-команд: «новое=команда» через запятую, например «resume=sessions, м=model». Встроенные уже есть: /resume и /history = /sessions, /quit = /exit, /models = /model, /reset = /new")
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
# Доверие к проектам (клетка): проектный .luna/settings.json чужого
# репозитория не должен ослаблять защиту. Список и карта «без вопросов»
# живут только в глобальном слое — проект их не задаёт ни при каком доверии.
_f("security.trusted_projects", "list", [],
   "Доверенные проекты: абсолютные пути папок (и всё внутри); их .luna/settings.json действует целиком. Остальные проекты только сужают защиту (luna trust)",
   human_only=True)
_f("guard.always_rank_projects", "dict", {},
   "Служебное: уровни риска «дальше без вопросов» по проектам {путь: уровень} (ставится ответом p в вопросе Предохранителя, не руками)",
   human_only=True)


# --- что недоверенный проект может менять ------------------------------------
# Принцип: проектный файл приходит вместе с чужим репозиторием, поэтому всё,
# что касается защиты, режимов и того, КУДА уходят запросы (и ключи API),
# из проекта только сужается или игнорируется.

# Проект не задаёт никогда (даже доверенный): служебные глобальные ключи.
_GLOBAL_ONLY_PREFIXES = ("security.", "guard.always_rank_projects")

# Недоверенный проект не задаёт: адреса провайдеров и поисковиков (туда
# уходят ключи и запросы), разрешения headless, модель-охранник review,
# папка вывода роя (запись вне проекта).
_UNTRUSTED_IGNORED = frozenset({
    "headless.allow", "guard.always_rank_project",
    "model.provider", "model.kind", "model.base_url", "model.guard",
    "web.searxng_url", "web.exa_url", "web.parallel_url",
    "starfall.output_dir",
    # Учёт денег: проект с ценой 0 или −1000 обнулял бы все потолки бюджета.
    "model.price_in", "model.price_out", "model.context_tokens",
})
# То же по форме имени: незнакомые ключи будущих версий с адресами и
# учётными данными тоже не принимаем из проекта.
_UNTRUSTED_IGNORED_RE = re.compile(
    r"(^|[._])(url|urls|endpoint|endpoints|host|hosts|token|auth|secret|secrets|"
    r"password|api_key|key|proxy)$")

# Режимы от строгого к свободному: проект может только ужесточить.
MODE_STRICTNESS = ("plan", "normal", "review", "auto", "yolo")

# Возможности: проект может выключить, но не включить.
_CAPABILITY_FLAGS = frozenset({"agents.enabled", "starfall.enabled", "web.enabled"})

# Потолки: проект может только опустить. True — 0 значит «без лимита».
_UPPER_BOUNDS = {
    "agent.max_steps": False, "agents.max_depth": False, "agents.max_children": False,
    "agents.max_steps": False, "agents.session_budget_usd": True,
    "starfall.max_cells": False, "starfall.max_parallel": False,
    "starfall.budget_usd": True, "starfall.ttl_s": False, "starfall.max_attempts": False,
    "starfall.sandbox_memory_mb": False, "starfall.sandbox_processes": False,
}

_NO_VALUE = object()


def _rule_action(line: str) -> str:
    """Действие строки guard.rules (allow/ask/deny) или пусто."""
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        return ""
    action = line.rsplit("=", 1)[1].strip().lower()
    return action if action in ("allow", "ask", "deny") else ""


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
        field = SCHEMA.get(key)
        # Словарь-значение (карта путей) — лист: в путях бывают точки.
        if isinstance(v, dict) and not (field is not None and field.type == "dict"):
            yield from _flatten(v, key + ".")
        else:
            yield key, v


class Settings:
    def __init__(self, *, home: Path | str | None = None, cwd: Path | str | None = None, bus=None,
                 project_layer: bool = True):
        self.home_dir = Path(home).expanduser() if home is not None else luna_home()
        self.global_path = self.home_dir / "settings.json"
        base = Path(cwd) if cwd is not None else Path.cwd()
        self.project_path = base / ".luna" / "settings.json"
        try:
            self.project_dir = base.expanduser().resolve()
        except (OSError, RuntimeError):
            self.project_dir = base.absolute()
        # False — проектный файл не читается вовсе (клетка работника).
        self.project_layer = bool(project_layer)
        self.bus = bus
        self._global = self._load(self.global_path)
        self._project = self._load_project()
        self._trusted = self._compute_trust()
        self._stamps = self._file_stamps()

    def _load_project(self) -> dict:
        return self._load(self.project_path) if self.project_layer else {}

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
        self._project = self._load_project()
        self._trusted = self._compute_trust()
        self._stamps = self._file_stamps()

    def _file_stamps(self) -> tuple:
        out = []
        paths = [self.global_path] + ([self.project_path] if self.project_layer else [])
        for path in paths:
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
        new_project = self._load_project()
        old_values = {k: self.get(k) for k in set(before) | set(before_g) | set(SCHEMA)}
        self._global, self._project = new_global, new_project
        self._trusted = self._compute_trust()  # список доверия мог смениться
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

    # доверие

    def _compute_trust(self) -> bool:
        raw, found = _dig(self._global, "security.trusted_projects")
        if not found or not isinstance(raw, list):
            return False
        for item in raw:
            if not isinstance(item, str) or not item.strip():
                continue
            path = Path(item.strip()).expanduser()
            # Относительный путь разрешился бы от текущей папки и доверил бы
            # что угодно — такие записи пропускаем.
            if not path.is_absolute():
                continue
            try:
                root = path.resolve()
            except (OSError, RuntimeError):
                continue
            if self.project_dir == root or root in self.project_dir.parents:
                return True
        return False

    def project_trusted(self) -> bool:
        """Проект (папка с .luna) внутри одного из security.trusted_projects."""
        return self._trusted

    def project_key(self) -> str:
        """Ключ проекта для глобальных карт: разрешённый путь, на Windows без регистра."""
        return os.path.normcase(str(self.project_dir))

    def _global_value(self, key: str) -> Any:
        value, found = _dig(self._global, key)
        if found:
            return value
        field = SCHEMA.get(key)
        return field.default if field is not None else None

    def _project_value(self, key: str):
        """(значение, найдено) проектного слоя с учётом доверия.

        Недоверенный проект для ключей защиты только сужает: всё, что
        ослабило бы глобальное значение, считается незаданным.
        """
        if key.startswith(_GLOBAL_ONLY_PREFIXES):
            return None, False
        value, found = _dig(self._project, key)
        if not found:
            return None, False
        if self._trusted:
            return value, True
        narrowed = self._narrow_project(key, value)
        if narrowed is _NO_VALUE:
            return None, False
        return narrowed, True

    def _narrow_project(self, key: str, value: Any) -> Any:
        if key in _UNTRUSTED_IGNORED or _UNTRUSTED_IGNORED_RE.search(key):
            return _NO_VALUE
        if key == "guard.enabled":
            return value if value is True else _NO_VALUE
        if key == "guard.rules":
            return self._merged_rules(value)
        if key.startswith("guard."):
            return _NO_VALUE  # незнакомые ключи защиты — только человек глобально
        if key == "agent.mode":
            glob = self._global_value(key)
            if value not in MODE_STRICTNESS:
                return _NO_VALUE
            if glob in MODE_STRICTNESS and MODE_STRICTNESS.index(value) > MODE_STRICTNESS.index(glob):
                return _NO_VALUE
            return value
        if key == "starfall.sandbox":
            return _NO_VALUE if value == "none" else value
        if key in _CAPABILITY_FLAGS:
            return value if value is False or value == self._global_value(key) else _NO_VALUE
        if key in _UPPER_BOUNDS:
            return self._narrow_bound(key, value, _UPPER_BOUNDS[key])
        return value

    def _narrow_bound(self, key: str, value: Any, zero_unlimited: bool) -> Any:
        glob = self._global_value(key)
        num_types = (int, float)
        if isinstance(value, bool) or not isinstance(value, num_types):
            return _NO_VALUE
        if value < 0 or not math.isfinite(value):
            return _NO_VALUE  # отрицательный потолок у потребителей значит «без лимита»
        if isinstance(glob, bool) or not isinstance(glob, num_types):
            return value
        if zero_unlimited:
            if glob == 0:
                return value
            if value == 0:
                return _NO_VALUE
        return value if value <= glob else _NO_VALUE

    def _merged_rules(self, value: Any) -> Any:
        """Глобальные правила + проектные без allow.

        Порядок важен (первое совпадение решает): проектные deny — первыми,
        чтобы сужать и глобальный allow; проектные ask — после глобальных,
        иначе ask перекрыл бы глобальный deny.
        """
        if not isinstance(value, str):
            return _NO_VALUE
        glob = self._global_value("guard.rules")
        glob = glob if isinstance(glob, str) else ""
        deny, ask = [], []
        for line in value.splitlines():
            action = _rule_action(line)
            if action == "deny":
                deny.append(line.strip())
            elif action == "ask":
                ask.append(line.strip())
        parts = deny + ([glob.strip("\n")] if glob.strip() else []) + ask
        return "\n".join(parts)

    def ignored_project_keys(self) -> list[str]:
        """Проектные ключи, которые не действуют (целиком или частью) — для doctor и уведомлений."""
        out = []
        for key, value in _flatten(self._project):
            if key.startswith(_GLOBAL_ONLY_PREFIXES):
                out.append(key)
                continue
            if self._trusted:
                continue
            narrowed = self._narrow_project(key, value)
            if narrowed is _NO_VALUE:
                out.append(key)
            elif key == "guard.rules" and isinstance(value, str) and any(
                    _rule_action(line) == "allow" for line in value.splitlines()):
                out.append(key)
        return sorted(out)

    # чтение

    def get(self, key: str, default: Any = None) -> Any:
        value, found = self._project_value(key)
        if found:
            return value
        value, found = _dig(self._global, key)
        if found:
            return value
        field = SCHEMA.get(key)
        if field is not None:
            if field.type in ("list", "dict"):
                return type(field.default)(field.default)  # умолчание не должно меняться снаружи
            return field.default
        return default

    def source(self, key: str) -> str:
        if self._project_value(key)[1]:
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
        elif field.type == "list":
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise HumanError("settings_value", f"Настройка {key} — список строк", "")
        elif field.type == "dict":
            if not isinstance(value, dict):
                raise HumanError("settings_value", f"Настройка {key} — словарь", "")
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
        if field.type in ("list", "dict"):
            stripped = text.strip()
            if field.type == "list" and not stripped.startswith("["):
                # Одно значение без JSON — список из одного элемента (путь с запятой не ломаем).
                return [stripped] if stripped else []
            try:
                value = json.loads(stripped)
            except ValueError:
                raise HumanError("settings_value", f"Не JSON для {key}: {text!r}",
                                 "Например: [\"/путь/один\", \"/путь/два\"].") from None
            return self.validate(key, value)
        return self.validate(key, text)

    def set(self, key: str, value: Any, layer: str = "global") -> None:
        value = self.validate(key, value)
        if layer == "project" and not self.project_layer:
            raise HumanError("settings_layer", "Проектный слой настроек здесь выключен",
                             "Запиши настройку глобально (без --project).")
        data = self._project if layer == "project" else self._global
        path = self.project_path if layer == "project" else self.global_path
        old, _ = _dig(data, key)
        _plant(data, key, value)
        text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        atomic_write_text(path, text)
        if layer != "project":
            self._trusted = self._compute_trust()
        self._stamps = self._file_stamps()  # своя запись — не «внешнее изменение»
        if self.bus is not None:
            self.bus.publish({"type": "settings.changed", "key": key,
                              "value": value, "old": old, "layer": layer})

    @staticmethod
    def value_text(field_type: str, value: Any) -> str:
        if field_type == "bool":
            return "on" if value else "off"
        if field_type == "list" and isinstance(value, list):
            return ", ".join(str(v) for v in value) if value else "—"
        if field_type == "dict" and isinstance(value, dict):
            return json.dumps(value, ensure_ascii=False) if value else "—"
        return str(value)
