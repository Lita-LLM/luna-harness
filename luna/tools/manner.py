# Перенесено из BENDER: bender/tools/manner.py (коммит 6552ed660bfabf5202569f9905212b77dbf6d31a), адаптировано: синхронный реестр Луны, манеры — файлы, подтверждение — общий Предохранитель цикла.
"""Тул manner: агент видит, переключает и правит свою манеру речи.

Чтение (list, show) Предохранитель пропускает свободно; изменение
(use, off, create, edit) помечено mutating_for и в обычном режиме проходит
подтверждение человека — как любое меняющее действие.
"""

from __future__ import annotations

from .registry import Tool, ToolResult

_CHANGING = ("use", "off", "create", "edit")


def _mutating_for(args) -> bool:
    return (args or {}).get("action", "list") in _CHANGING


def _fail(err) -> ToolResult:
    text = err.title
    if err.hint:
        text += " " + err.hint
    return ToolResult(False, text)


def _manner(ctx, args) -> ToolResult:
    from ..core.errors import HumanError
    from ..mind import manners

    settings = ctx.settings
    if settings is None:
        return ToolResult(False, "Манеры недоступны: у этого клиента нет настроек.")
    action = args.get("action", "list")
    if action not in ("list", "show", "use", "off", "create", "edit"):
        return ToolResult(False, "Неизвестное действие " + str(action) +
                          ": бывает list, show, use, off, create, edit.")
    try:
        if action == "list":
            # Текущую смотрим по id из настройки, без resolve: список обязан
            # работать и тогда, когда настроенная манера как раз сломалась.
            current_id = str(settings.get("agent.manner", "") or "")
            current = manners.get(settings, current_id, ctx.cwd) if current_id else None
            rows = []
            for item in manners.library(settings, ctx.cwd):
                mark = "→ " if current and item["id"] == current["id"] else "• "
                row = mark + item["name"] + " (" + item["id"] + ", " + manners.SOURCE_NAMES[item["source"]] + ")"
                if item["description"]:
                    row += " — " + item["description"]
                rows.append(row)
            if current:
                head = "Сейчас: «" + current["name"] + "»"
            elif current_id:
                head = "Сейчас: в настройке «" + current_id + "», но такой манеры нет (ошибка)"
            else:
                head = "Сейчас: свой голос (манера не выбрана)"
            return ToolResult(True, head + "\n" + ("\n".join(rows) or "Библиотека пуста."))
        if action == "show":
            item = manners.resolve(settings, args.get("name", ""), ctx.cwd)
            return ToolResult(True, "«" + item["name"] + "» (" + item["id"] + ")\n" +
                              item["description"] + "\n\n" + item["text"])
        if action == "use":
            item = manners.choose(settings, args.get("name", ""), ctx.cwd)
            return ToolResult(True, "Манера «" + item["name"] + "» включена: в этой сессии "
                                    "применится со следующего хода, дальше — везде.")
        if action == "off":
            manners.choose(settings, "", ctx.cwd)
            return ToolResult(True, "Манера выключена: дальше говоришь своим голосом из ядра промпта.")
        if action == "create":
            item = manners.create(settings, args.get("name", ""), args.get("title", ""),
                                  args.get("description", ""), args.get("text", ""))
            return ToolResult(True, "Манера «" + item["name"] + "» (" + item["id"] + ") создана в " +
                              item["path"] + ". Включить — action=use.")
        # edit: не переданные поля остаются как были
        item = manners.edit(settings, args.get("name", ""), ctx.cwd,
                            name=args.get("title") or None,
                            description=args.get("description") or None,
                            text=args.get("text") or None)
        return ToolResult(True, "Манера «" + item["name"] + "» (" + item["id"] + ") обновлена: " +
                          item["path"] + ".")
    except HumanError as err:
        return _fail(err)


def manner_tool() -> Tool:
    return Tool(
        "manner",
        "Манера речи — стиль того, как ты говоришь (ты остаёшься собой, меняются слова). "
        "action=list — какие есть и текущая; show — полный текст манеры name; use — включить манеру name; "
        "off — говорить своим голосом; create — новая манера (name — id латиницей, title — название, "
        "description, text); edit — поправить манеру name (title/description/text — только то, что меняется). "
        "use/off/create/edit подтверждает человек.",
        {"type": "object", "properties": {
            "action": {"type": "string"},
            "name": {"type": "string"},
            "title": {"type": "string"},
            "description": {"type": "string"},
            "text": {"type": "string"}},
         "required": ["action"]},
        _manner, mutating_for=_mutating_for)
