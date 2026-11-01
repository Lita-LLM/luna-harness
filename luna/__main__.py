"""Точка входа: python -m luna [doctor | try <что> | settings | set <ключ> <значение> | --version].

Импорты тяжёлого — ленивые, внутри команд: --version и doctor не тянут рендерер.
Время старта фиксируем при импорте модуля: заставка покажет, за сколько миллисекунд
процесс дошёл до первого кадра.
"""

from __future__ import annotations

import os
import sys
import time

_T0 = time.monotonic()

from . import __version__  # noqa: E402 — лёгкий импорт, только строка версии
from .core.stdio import ensure_utf8_stdio  # noqa: E402 — лёгкий: внутри только sys


def _note_start_marker() -> None:
    """Метка старта процесса для замеров: если задан LUNA_START_FILE, пишем
    туда монотонное время старта и PID самой Луны. Родитель (luna try
    measure на Windows) считает старт от этой метки внутри процесса,
    а не от Popen — иначе в замер падает создание новой консоли —
    и меряет память/ЦП по этому PID, а не по обёртке (ЗАДАЧА-03)."""
    path = os.environ.get("LUNA_START_FILE")
    if not path:
        return
    try:
        with open(path, "w", encoding="ascii") as f:
            f.write(repr(_T0) + "\n" + str(os.getpid()) + "\n")
    except OSError:
        pass


_note_start_marker()

USAGE = """Лунапарк — сессионный ИИ-агент.

  luna                      чат с агентом (заставка, затем полноэкранный чат)
  luna --continue           продолжить последнюю сессию этого каталога
  luna --resume [id]        выбрать сессию из списка до чата; с id или его началом — сразу в неё
  luna doctor               самопроверка: Python, терминал, каталоги, настройки
  luna settings             список настроек со значениями и источниками
  luna set <ключ> <значение>  изменить настройку (например: luna set ui.splash off)
  luna try render           витрина рендерера: палитра, градиенты, фазы луны
  luna try splash           заставка ещё раз
  luna try chat             чат-экран с фальшивым агентом (сценарии событий)
  luna try snapshot <экран> снимок экрана в HTML: splash | render | main | chat | sessions
  luna try measure [--json] замеры: старт, память и ЦП в простое
  luna try brain            мозг на фальшивой модели: ход с тулом, без сети
  luna try undo             откат на фальшивой модели: пишет → /undo → прежние → /redo
  luna try compact          прилив на фальшивой модели: длинная беседа сжимается в сводку
  luna update [--check]     обновление git-копии программы (--check — только проверить)
  luna setup                настройка провайдера и моделей (ключ — скрытым вводом)
  luna models               что из моделей настроено и живо ли
  luna manners              манеры речи: в терминале — выбиралка; list — список текстом,
                            show <имя> — текст, use <имя> — включить, off — свой голос,
                            new <имя> — создать свою из шаблона, edit <имя> — открыть в редакторе
  luna try prompt           показать собранный системный промпт для текущего каталога
  luna mcp list             MCP-серверы из mcp.json: статус и число тулов
  luna mcp add <имя> -- <команда …> | --url <адрес>   добавить MCP-сервер
  luna mcp remove <имя>     удалить MCP-сервер
  luna mcp test <имя>       подключиться к MCP-серверу и перечислить его тулы
  luna -p "задача"          headless: один ход; --json — события JSONL, --mode, --model
  luna --version            версия
"""


def _print_error(err) -> int:
    print(f"Хьюстон, у нас проблема: {err.title}")
    if err.hint:
        print(f"  {err.hint}")
    if err.detail:
        print("  Подробности:")
        for ln in err.detail.strip().splitlines()[-15:]:
            print(f"    {ln}")
    return 2


def _cmd_settings() -> int:
    from .core.settings import SOURCE_NAMES, Settings

    settings = Settings()
    rows = settings.describe()
    width = max(len(r["key"]) for r in rows)
    for r in rows:
        value = Settings.value_text(r["type"], r["value"])
        key = r["key"]
        source_name = SOURCE_NAMES[r["source"]]
        title = r["title"]
        print(f"{key:<{width}}  {value:<10}  [{source_name}]  {title}")
    unknown = settings.unknown_keys()
    if unknown:
        print()
        print("В файлах есть ключи вне схемы (сохраняются, но пока не действуют):")
        for key in unknown:
            print(f"  {key} = {settings.get(key)!r}")
    print()
    print(f"Глобальный файл: {settings.global_path}")
    print(f"Проектный файл:  {settings.project_path} (сильнее глобального)")
    return 0


def _cmd_set(argv: list[str]) -> int:
    from .core.errors import HumanError
    from .core.settings import Settings

    if len(argv) != 2:
        raise HumanError("usage", "Нужно: luna set <ключ> <значение>",
                         "Например: luna set ui.splash off. Список: luna settings.")
    key, text = argv
    settings = Settings()
    value = settings.parse_value(key, text)
    settings.set(key, value)
    print(f"{key} = {Settings.value_text(settings_value_type(key), value)} (записано в {settings.global_path})")
    return 0


def _manners_list(settings) -> int:
    """Текстовый список манер с настоящими путями (пайп и list)."""
    from .mind import manners

    current_id = str(settings.get("agent.manner", "") or "")
    items = manners.library(settings)
    if not items:
        print("Манер нет: ни встроенных, ни своих.")
        return 0
    for item in items:
        mark = "→ " if item["id"] == current_id else "• "
        line = mark + item["name"] + " (" + item["id"] + ", " + manners.SOURCE_NAMES[item["source"]] + ")"
        if item["description"]:
            line += " — " + item["description"]
        if item["source"] != "builtin":
            line += " · " + str(item["path"])
        print(line)
    print()
    if current_id:
        print("Сейчас: " + current_id)
    else:
        print("Сейчас: свой голос (манера не выбрана)")
    print("Встроенные манеры живут в программе: " + str(manners.BUILTIN_DIR))
    print("Свои кладутся файлами .md в " + str(manners.home_dir(settings))
          + " или в " + str(manners.project_dir(settings)) + " (проект).")
    print("Новая своя: luna manners new <имя> — создаст файл из шаблона и покажет путь.")
    return 0


def _manners_picker(settings) -> int:
    """luna manners без аргументов в живом терминале — та же выбиралка, что в чате."""
    from .core.errors import HumanError
    from .mind import manners
    from .ui.picker import Picker, manners_items, run_picker

    def refresh():
        return manners_items(settings)

    picker = Picker("Манеры речи", refresh(),
                    hint=("Enter — включить · n — новая · N — копия выбранной · "
                          "e — открыть в редакторе · Esc — закрыть"),
                    extra={"n": "new", "N": "copy", "e": "edit"})

    def dispatch(action, pk, term=None):
        try:
            if action[0] == "pick" and action[1] is not None:
                entry = manners.choose(settings, action[1].key)
                print("Манера «" + entry["name"] + "» включена.")
                return "close"
            if action[0] == "action" and action[1] == "new":
                pk.ask_input("Новая манера, имя латиницей: ", ("new", None))
            elif action[0] == "action" and action[1] == "copy" and action[2] is not None:
                pk.ask_input("Копия манеры " + action[2].key + ", имя латиницей: ",
                             ("copy", action[2].key))
            elif action[0] == "input":
                text, ctx = action[1], action[2]
                if text and ctx is not None:
                    entry = manners.create_from_template(settings, text, ctx[1] or "")
                    print("Манера создана: " + str(entry["path"]))
            elif action[0] == "action" and action[1] == "edit" and action[2] is not None:
                from .ui.extedit import run_editor

                path = manners.editable_path(settings, action[2].key)
                if term is not None:
                    term.suspend()
                    try:
                        run_editor(path)
                    finally:
                        term.resume()
                else:
                    run_editor(path)
        except HumanError as err:
            print(err.title + (" " + err.hint if err.hint else ""))
        return None

    run_picker(picker, dispatch, refresh=refresh)
    return 0


def _cmd_manners(argv: list[str]) -> int:
    from .core.errors import HumanError
    from .core.settings import Settings
    from .mind import manners

    settings = Settings()
    if not argv:
        if sys.stdin.isatty() and sys.stdout.isatty():
            return _manners_picker(settings)
        return _manners_list(settings)
    if argv[0] == "list":
        return _manners_list(settings)
    if argv[0] == "show" and len(argv) == 2:
        item = manners.resolve(settings, argv[1])
        print("«" + item["name"] + "» (" + item["id"] + ", " + manners.SOURCE_NAMES[item["source"]] + ")")
        if item["description"]:
            print(item["description"])
        print()
        print(item["text"])
        return 0
    if argv[0] == "use" and len(argv) == 2:
        item = manners.choose(settings, argv[1])
        print("Манера «" + item["name"] + "» (" + item["id"] + ") включена: настройка agent.manner записана.")
        return 0
    if argv[0] == "off" and len(argv) == 1:
        manners.choose(settings, "")
        print("Манера выключена: Луна говорит своим голосом.")
        return 0
    if argv[0] == "new" and len(argv) == 2:
        item = manners.create_from_template(settings, argv[1])
        print("Манера «" + item["name"] + "» создана из шаблона: " + str(item["path"]))
        print("Отредактируй файл под себя, включить — luna manners use " + item["id"])
        return 0
    if argv[0] == "edit" and len(argv) == 2:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            raise HumanError("usage", "Редактору нужен живой терминал",
                             "Открой файл руками: luna manners show " + argv[1]
                             + " покажет текст и источник.")
        from .ui.extedit import run_editor

        path = manners.editable_path(settings, argv[1])
        run_editor(path)
        print("Манера обновлена: " + str(path))
        return 0
    raise HumanError("usage", "Нужно: luna manners [list | show <имя> | use <имя> | off | new <имя> | edit <имя>]",
                     "Без аргументов в терминале — выбиралка, в пайпе — список.")


def settings_value_type(key: str) -> str:
    from .core.settings import SCHEMA

    field = SCHEMA.get(key)
    return field.type if field else "str"


def main(argv: list[str] | None = None) -> int:
    ensure_utf8_stdio()
    argv = list(sys.argv[1:] if argv is None else argv)
    from .core.errors import HumanError

    continue_last = "--continue" in argv
    argv = [a for a in argv if a != "--continue"]
    resume: str | None = None
    if "--resume" in argv:
        idx = argv.index("--resume")
        resume = ""
        rest_argv = argv[:idx]
        tail = argv[idx + 1:]
        if tail and not tail[0].startswith("-"):
            resume = tail[0]
            tail = tail[1:]
        argv = rest_argv + tail
    try:
        if not argv:
            from .ui.app import run_fullscreen

            return run_fullscreen(started_at=_T0, continue_last=continue_last,
                                  resume=resume)
        cmd, rest = argv[0], argv[1:]
        if cmd in ("--version", "-V", "version"):
            print(f"Лунапарк {__version__}")
            return 0
        if cmd in ("--help", "-h", "help"):
            from .core.paths import luna_home

            print(USAGE)
            print("Каталог данных: " + str(luna_home())
                  + " (переменная окружения LUNA_HOME; там настройки, секреты, сессии и свои манеры)")
            return 0
        if cmd in ("-p", "--prompt"):
            from .headless import run_headless

            prompt_parts = []
            json_mode = False
            mode = ""
            model_ref = ""
            idx = 0
            while idx < len(rest):
                item = rest[idx]
                if item == "--json":
                    json_mode = True
                elif item == "--mode" and idx + 1 < len(rest):
                    idx += 1
                    mode = rest[idx]
                elif item == "--model" and idx + 1 < len(rest):
                    idx += 1
                    model_ref = rest[idx]
                else:
                    prompt_parts.append(item)
                idx += 1
            if not prompt_parts:
                raise HumanError("usage", "Нужно: luna -p \"задача\" [--json] [--mode РЕЖИМ] [--model МОДЕЛЬ]", "")
            return run_headless(" ".join(prompt_parts), json_mode=json_mode,
                                mode=mode, model_ref=model_ref)
        if cmd == "setup":
            from .core.paths import DataDir
            from .core.secrets import Secrets
            from .core.settings import Settings
            from .setup import run_setup

            data = DataDir()
            data.ensure()
            return run_setup(settings=Settings(), secrets=Secrets(data.secrets))
        if cmd == "mcp":
            from .mcp.cli import run_mcp

            return run_mcp(rest)
        if cmd == "models":
            from .core.paths import DataDir
            from .core.secrets import Secrets
            from .core.settings import Settings
            from .setup import run_models

            data = DataDir()
            return run_models(settings=Settings(), secrets=Secrets(data.secrets))
        if cmd == "manners":
            return _cmd_manners(rest)
        if cmd == "doctor":
            from .diagnostics.doctor import run_doctor

            return run_doctor()
        if cmd == "update":
            from .diagnostics.update import run_update

            return run_update(check_only="--check" in rest)
        if cmd == "settings":
            return _cmd_settings()
        if cmd == "set":
            return _cmd_set(rest)
        if cmd == "try":
            if not rest:
                raise HumanError("usage", "Нужно: luna try render | splash | chat | measure | snapshot", "")
            what = rest[0]
            if what == "render":
                from .ui.render_try import run_render_demo

                return run_render_demo(started_at=_T0)
            if what == "splash":
                from .ui.splash import run_splash_demo

                return run_splash_demo(started_at=_T0)
            if what == "chat":
                from .ui.chat import run_chat

                return run_chat(started_at=_T0)
            if what == "measure":
                from .devtools.measure import run_measure

                return run_measure(as_json="--json" in rest)
            if what == "snapshot":
                from .devtools.snapshot import run_snapshot

                return run_snapshot(rest[1:])
            if what == "brain":
                from .devtools.brain import run_brain_try

                return run_brain_try()
            if what == "prompt":
                from .devtools.prompt_try import run_prompt_try

                return run_prompt_try()
            if what == "undo":
                from .devtools.rewind_try import run_undo_try

                return run_undo_try()
            if what == "compact":
                from .devtools.rewind_try import run_compact_try

                return run_compact_try()
            raise HumanError("usage", f"Не знаю такого try: {what}",
                             "Есть: render, splash, chat, measure, snapshot, brain, prompt, undo, compact.")
        raise HumanError("usage", f"Неизвестная команда: {cmd}", "Справка: luna --help.")
    except HumanError as err:
        return _print_error(err)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
