"""Журнал сессий чата на диске (ЗАДАЧА-06, п. 5 — минимум вехи 3).

Каждая сессия полноэкранного чата — JSONL-файл в LUNA_HOME/sessions/:
первая строка — meta (id сессии, каталог, время старта), дальше записи
{"kind": "event", "data": {...}} и {"kind": "command", "data": {...}} —
всё, что экран получил событиями контракта docs/СОБЫТИЯ.md и отправил
командами. Из журнала восстанавливаются и лента (`luna --continue`,
выбиралка /sessions), и память модели: build_history собирает из тех же
записей историю сообщений агента (ЗАДАЧА-10). Имя сессии — запись
{"kind": "title", ...} (/rename); последняя побеждает. Форк и чекпоинты —
веха 3, здесь их нет.

Журнал не имеет права ронять чат: ошибки записи глотаются.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .ids import new_id


class SessionJournal:
    """Пишет записи сессии в JSONL-файл построчно, с немедленным сбросом."""

    def __init__(self, path, session_id: str, cwd: str):
        self.path = Path(path)
        self.session_id = session_id
        self.cwd = cwd
        self._file = None

    # --- создание и продолжение -------------------------------------------------

    @classmethod
    def create(cls, data, cwd: str) -> "SessionJournal":
        sid = new_id()
        name = time.strftime("%Y%m%d-%H%M%S") + "-" + sid + ".jsonl"
        journal = cls(data.sessions / name, sid, cwd)
        journal._open()
        journal.append({"kind": "meta", "session": sid, "cwd": cwd,
                        "started": time.time()})
        return journal

    @classmethod
    def resume(cls, data, path) -> tuple:
        """Открыть существующий журнал на дозапись; вернуть (журнал, записи)."""
        records = load_records(path)
        meta = next((r for r in records if r.get("kind") == "meta"), {})
        journal = cls(Path(path), str(meta.get("session", "")),
                      str(meta.get("cwd", "")))
        journal._open()
        journal.append({"kind": "resume", "ts": time.time()})
        return journal, records

    # --- запись --------------------------------------------------------------------

    def _open(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._file = open(self.path, "a", encoding="utf-8", buffering=1)
        except OSError:
            self._file = None

    def append(self, record: dict) -> None:
        if self._file is None:
            return
        try:
            line = json.dumps(record, ensure_ascii=False, default=str)
            self._file.write(line + "\n")
        except (OSError, ValueError):
            pass

    def append_event(self, ev: dict) -> None:
        self.append({"kind": "event", "data": ev})

    def append_command(self, cmd: dict) -> None:
        self.append({"kind": "command", "data": cmd})

    def set_title(self, title: str) -> None:
        """Имя сессии от человека (/rename): последняя запись побеждает."""
        self.append({"kind": "title", "title": title, "ts": time.time()})

    def close(self) -> None:
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                pass
            self._file = None


def load_records(path) -> list:
    """Все записи журнала; битые строки пропускаются, а не роняют чтение."""
    records = []
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return records
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            records.append(rec)
    return records


def _summary(records: list) -> dict:
    """Сводка по записям журнала: имя, первая реплика, число ходов."""
    title = ""
    first = ""
    turns = 0
    for rec in records:
        kind = rec.get("kind")
        if kind == "title":
            title = str(rec.get("title") or title)
        elif kind == "command":
            data = rec.get("data") or {}
            if data.get("type") == "user.message":
                turns += 1
                if not first:
                    first = str(data.get("text") or "")
    return {"title": title, "first": first, "turns": turns}


def list_sessions(data) -> list:
    """Сессии на диске, свежие сверху: id, каталог, старт, имя, сводка."""
    out = []
    root = Path(data.sessions)
    if not root.exists():
        return out
    for path in root.glob("*.jsonl"):
        records = load_records(path)
        meta = next((r for r in records if r.get("kind") == "meta"), {})
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        item = {
            "id": str(meta.get("session", path.stem)),
            "cwd": str(meta.get("cwd", "")),
            "started": float(meta.get("started") or mtime),
            "records": len(records),
            "path": path,
        }
        item.update(_summary(records))
        out.append(item)
    out.sort(key=lambda s: s["started"], reverse=True)
    return out


def find_last(data, cwd: str):
    """Файл последней сессии именно этого каталога — или None."""
    for session in list_sessions(data):
        if session["cwd"] == cwd:
            return session["path"]
    return None


def find_by_prefix(data, prefix: str):
    """Сессия по id или его началу (luna --resume <префикс>) — или None."""
    prefix = (prefix or "").strip()
    if not prefix:
        return None
    for session in list_sessions(data):
        if session["id"] == prefix or session["id"].startswith(prefix):
            return session
    return None


# --- память модели из журнала ------------------------------------------------

INTERRUPTED_RESULT = ("Прервано: прошлая сессия закрылась до завершения "
                      "этого вызова, результат неизвестен.")


def build_history(records: list) -> list:
    """История сообщений агента из записей журнала (ЗАДАЧА-10, п. 1).

    Собирается ровно то, что цикл агента держал в своей истории при жизни:
    реплики пользователя, ответы ассистента (текст склеивается из чанков
    agent.text), вызовы тулов (группируются в сообщение ассистента, как их
    отдала модель) и результаты тулов. Вызов, не дождавшийся результата
    (сессия закрылась на подтверждении или вопросе), получает результат
    «прервано» — иначе провайдер не примет историю. Системного промпта
    здесь нет: агент соберёт его сам к первому ходу.
    """
    history: list = []
    text_parts: list = []
    pending: dict | None = None   # сообщение ассистента текущего шага модели
    pending_in_history = False
    open_calls: dict = {}         # id вызова → имя тула, ждём результат

    def seal_pending() -> None:
        """Шаг модели закончен: сообщение — в историю, дальше новый шаг."""
        nonlocal pending, pending_in_history
        if pending is None:
            return
        if not pending_in_history:
            if not pending.get("tool_calls"):
                pending.pop("tool_calls", None)
            history.append(pending)
        pending = None
        pending_in_history = False

    def close_dangling() -> None:
        """Висячие вызовы получают честный результат «прервано»."""
        seal_pending()
        for call_id, name in open_calls.items():
            history.append({"role": "tool", "tool_call_id": call_id,
                            "name": name, "content": INTERRUPTED_RESULT})
        open_calls.clear()

    for rec in records:
        kind = rec.get("kind")
        data = rec.get("data") or {}
        if kind == "command":
            if data.get("type") == "user.message":
                close_dangling()
                if text_parts:
                    history.append({"role": "assistant",
                                    "content": "".join(text_parts)})
                    text_parts.clear()
                history.append({"role": "user",
                                "content": str(data.get("text") or "")})
            continue
        if kind != "event":
            continue
        etype = data.get("type")
        if etype == "agent.text":
            if pending is not None and pending.get("tool_calls") \
                    and not open_calls:
                seal_pending()  # прошлый шаг закрыт — это текст нового
            text_parts.append(str(data.get("text") or ""))
        elif etype == "agent.text.done":
            # Текст шага собран; вызовы этого же шага придут следом и
            # лягут в то же сообщение. Без вызовов сообщение запечатает
            # конец хода или следующая реплика.
            if pending is None:
                pending = {"role": "assistant",
                           "content": "".join(text_parts), "tool_calls": []}
                text_parts.clear()
        elif etype == "tool.call":
            if pending is None:
                pending = {"role": "assistant",
                           "content": "".join(text_parts), "tool_calls": []}
                text_parts.clear()
            call = {"id": str(data.get("id") or ""),
                    "name": str(data.get("name") or ""),
                    "arguments": data.get("args") or {},
                    "intent": str(data.get("intent") or "")}
            pending["tool_calls"].append(call)
            open_calls[call["id"]] = call["name"]
        elif etype == "tool.result":
            if pending is not None and not pending_in_history:
                history.append(pending)  # вызовы — раньше их результатов
                pending_in_history = True
            call_id = str(data.get("id") or "")
            name = open_calls.pop(call_id, "")
            history.append({"role": "tool", "tool_call_id": call_id,
                            "name": name,
                            "content": str(data.get("output") or "")})
        elif etype == "turn.done":
            if text_parts:
                history.append({"role": "assistant",
                                "content": "".join(text_parts)})
                text_parts.clear()
            seal_pending()
    if text_parts:
        history.append({"role": "assistant", "content": "".join(text_parts)})
    close_dangling()
    return history
