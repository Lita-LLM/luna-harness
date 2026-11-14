"""Сжатие контекста — «прилив» (веха 3, ЗАДАЧА-13).

Когда разговор перестаёт влезать в окно модели, старые ходы заменяются
сводкой: её пишет модель роли fast по транскрипту уходящих ходов, а
последние keep_turns ходов и текущий план (todo) остаются как есть.
Подход — как у Бендера (bender/mind/compact.py): граница сжатия всегда
перед сообщением человека, пары «вызов тула — результат» не разрываются,
сводка встраивается в историю парой сообщений (user со сводкой +
короткое подтверждение assistant), чтобы чередование ролей не ломалось.

История здесь — список сообщений цикла агента (system, user, assistant
с tool_calls, tool). Ходы известны циклу по отметкам (turn, индекс
сообщения user в истории): сегмент хода — от его user до следующего
user хода. Сообщения до первой отметки (системный промпт) не сжимаются.
"""

from __future__ import annotations

import json

# Промпт сводки — данные: переопределяется настройкой compact.prompt.
# Транскрипт ходов дописывается после промпта одной строкой-разделителем.
DEFAULT_SUMMARY_PROMPT = """Ты сжимаешь начало разговора агента Луны с человеком: эти ходы уберут из контекста, и дальше агент будет помнить их только по твоей сводке.

Сохрани обязательно:
- цель и задачу человека, договорённости и решения;
- что уже сделано: изменённые файлы (пути!), команды и их итоги, найденные факты и цифры;
- открытые вопросы и незаконченные дела.

Выкинь: вежливость, повторы, рассуждения вслух, подробные выводы тулов.
Пиши по-русски, кратко, пунктами по темам, от лица агента («человек попросил…», «я сделал…»). Только сводка, без предисловий.

В самом конце — раздел «Перечитать:» и под ним списком «- путь» файлы (до 10), которые агенту надо открыть заново, чтобы продолжить незаконченное: их содержимое из контекста уйдёт. Нечего перечитывать — раздел не пиши."""

REREAD_HEAD = "Перечитать"
REREAD_NOTE = ("Перед продолжением открой заново файлы из раздела «Перечитать», "
               "если они нужны для текущей задачи: их текста в контексте больше нет.")


def parse_reread(summary: str) -> list:
    """Пути из раздела «Перечитать:» сводки (до 10, без повторов)."""
    paths: list = []
    inside = False
    for raw in summary.splitlines():
        line = raw.strip()
        bare = line.strip("#*_ ").rstrip(":").strip()
        if bare.lower().startswith(REREAD_HEAD.lower()):
            inside = True
            continue
        if not inside:
            continue
        if not line:
            if paths:
                break
            continue
        if line[0] not in "-*•" and not line[:1].isdigit():
            break  # пошёл другой раздел
        item = line.lstrip("-*•0123456789.) ").strip().strip("`").strip()
        item = item.split(" — ")[0].split(" - ")[0].strip().strip("`")
        if item and item not in paths:
            paths.append(item)
        if len(paths) >= 10:
            break
    return paths


TRANSCRIPT_CAP = 80_000   # общий потолок транскрипта для сводящей модели
MESSAGE_CAP = 2_000       # потолок одного сообщения в транскрипте
SUMMARY_HEAD = "Сводка прежнего разговора (контекст сжат — «прилив»):"
SUMMARY_ACK = "Понял: продолжаю по сводке, последние ходы помню целиком."


def message_chars(message: dict) -> int:
    """Грубая длина сообщения в символах — для оценки «было → стало»."""
    total = 0
    content = message.get("content")
    if isinstance(content, str):
        total += len(content)
    elif isinstance(content, list):
        total += len(json.dumps(content, ensure_ascii=False, default=str))
    for call in message.get("tool_calls") or []:
        total += len(json.dumps(call, ensure_ascii=False, default=str))
    return total


def history_chars(history: list) -> int:
    return sum(message_chars(m) for m in history)


def split_segments(history: list, marks: list) -> tuple:
    """(голова, [(номер хода | None, сообщения)]) по отметкам цикла.

    Голова — всё до первого отмеченного хода (системный промпт и пролог).
    Если отметок нет, режем по сообщениям user с номером None.
    """
    points = sorted((idx, turn) for turn, idx in marks if 0 <= idx < len(history))
    if not points:
        points = [(i, None) for i, m in enumerate(history) if m.get("role") == "user"]
        if not points:
            return list(history), []
    head = list(history[:points[0][0]])
    segments = []
    for pos, (idx, turn) in enumerate(points):
        end = points[pos + 1][0] if pos + 1 < len(points) else len(history)
        segments.append((turn, list(history[idx:end])))
    return head, segments


def _message_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            elif isinstance(item, dict):
                parts.append("[вложение]")
        return " ".join(parts)
    return ""


def render_transcript(segments: list) -> str:
    """Транскрипт уходящих ходов для сводящей модели: коротко и по делу."""
    lines: list[str] = []
    for turn, messages in segments:
        lines.append("--- ход " + (str(turn) if turn is not None else "?") + " ---")
        for message in messages:
            role = message.get("role", "")
            text = _message_text(message)
            if role == "user":
                lines.append("Человек: " + text[:MESSAGE_CAP])
            elif role == "assistant":
                if text:
                    lines.append("Агент: " + text[:MESSAGE_CAP])
                for call in message.get("tool_calls") or []:
                    args = json.dumps(call.get("arguments") or {},
                                      ensure_ascii=False, default=str)
                    lines.append("Тул " + str(call.get("name", "")) + ": " + args[:400])
            elif role == "tool":
                lines.append("Результат " + str(message.get("name", "")) + ": " +
                             text[:600])
    transcript = "\n".join(lines)
    if len(transcript) > TRANSCRIPT_CAP:
        cut = TRANSCRIPT_CAP // 2
        transcript = (transcript[:cut] + "\n…[середина транскрипта обрезана]…\n" +
                      transcript[-cut:])
    return transcript


def compact_history(router, history: list, marks: list, *, keep_turns: int = 2,
                    hint: str = "", prompt_template: str = "",
                    todos: list | None = None) -> dict | None:
    """Сжать историю: старые ходы → сводка. None — сжимать нечего.

    Возвращает {"history", "marks", "summary", "summarized", "kept"}:
    история — голова + пара «сводка/подтверждение» + последние keep_turns
    сегментов как есть; отметки пересчитаны под новую историю. Текущий
    план (todos) дописывается в блок сводки дословно — «как есть».
    Ошибка модели пробрасывается наверх: вызывающий решает, что показать.
    """
    head, segments = split_segments(history, marks)
    if len(segments) <= keep_turns:
        return None
    old, kept = segments[:-keep_turns] if keep_turns else segments, \
        segments[-keep_turns:] if keep_turns else []
    prompt = prompt_template.strip() or DEFAULT_SUMMARY_PROMPT
    request = prompt + "\n\n" + render_transcript(old)
    if hint:
        request += "\n\nСохрани особенно: " + hint
    result = router.chat([{"role": "user", "content": request}], None,
                         role="fast", on_event=lambda kind, text: None)
    summary = (result.text or "").strip()
    if not summary:
        return None
    reread = parse_reread(summary)
    block = SUMMARY_HEAD + "\n" + summary
    if reread:
        block += "\n\n" + REREAD_NOTE
    if todos:
        lines = []
        for item in todos:
            if isinstance(item, dict):
                mark = {"done": "✓", "active": "…", "pending": "·"}.get(
                    str(item.get("status", "")), "·")
                lines.append(mark + " " + str(item.get("text", "")))
        if lines:
            block += "\n\nТекущий план (как есть):\n" + "\n".join(lines)
    new_history = list(head)
    new_history.append({"role": "user", "content": block})
    new_history.append({"role": "assistant", "content": SUMMARY_ACK})
    new_marks = []
    for turn, messages in kept:
        if turn is not None:
            new_marks.append((turn, len(new_history)))
        new_history.extend(messages)
    return {"history": new_history, "marks": new_marks, "summary": summary,
            "reread": reread,
            "summarized": [turn for turn, _ in old], "kept": [turn for turn, _ in kept]}
