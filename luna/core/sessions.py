"""Журнал сессий чата на диске (ЗАДАЧА-06, п. 5 — минимум вехи 3).

Каждая сессия полноэкранного чата — JSONL-файл в LUNA_HOME/sessions/:
первая строка — meta (id сессии, каталог, время старта), дальше записи
{"kind": "event", "data": {...}} и {"kind": "command", "data": {...}} —
всё, что экран получил событиями контракта docs/СОБЫТИЯ.md и отправил
командами. Этого хватает, чтобы восстановить ленту при `luna --continue`
и показать список командой /sessions. Форк, перемотка и чекпоинты —
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


def list_sessions(data) -> list:
    """Сессии на диске, свежие сверху: id, каталог, старт, число записей."""
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
        out.append({
            "id": str(meta.get("session", path.stem)),
            "cwd": str(meta.get("cwd", "")),
            "started": float(meta.get("started") or mtime),
            "records": len(records),
            "path": path,
        })
    out.sort(key=lambda s: s["started"], reverse=True)
    return out


def find_last(data, cwd: str):
    """Файл последней сессии именно этого каталога — или None."""
    for session in list_sessions(data):
        if session["cwd"] == cwd:
            return session["path"]
    return None
