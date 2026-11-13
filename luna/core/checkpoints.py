"""Чекпоинты файлов и откат ходов (веха 3, ЗАДАЧА-13).

Идея — как /rewind у эталонов и теневой git из ПЛАН.md («Ядро и сессии»):
перед каждым изменением файла тулами запоминаем прежнее содержимое, и ход
можно откатить целиком — и файлы, и разговор.

Хранение — в LUNA_HOME/checkpoints/<сессия>/:
- objects/<sha256> — содержимое файлов, по одному разу на одинаковый
  текст (дедупликация по хэшу);
- index.json — манифест: по ходам, для каждого тронутого файла пара
  состояний «до» (before) и «после» (after), плюс git-пометка хода.

Состояние файла — словарь: {"h": sha256} (содержимое лежит в objects),
{"absent": True} (файла не было), {"big": размер} (файл больше лимита
checkpoints.max_file_bytes — снимка нет, восстановление не гарантируется).

Изменения от bash точно не отследить: перед первым bash хода в
git-репозитории запоминаем `git stash create` (коммит-объект дерева, саму
рабочую копию не трогает); при откате дерево сначала приводим к нему
(`git restore`), а уже потом точечно — снимками файловых тулов. Вне git —
честная пометка «откат команд оболочки не гарантирован».

Чекпоинты не имеют права ронять тулы и агента: все сбои записи глотаются,
а восстановление докладывает, что именно не вышло.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

from .fsutil import atomic_write, atomic_write_text
from .paths import luna_home

DEFAULT_MAX_FILE = 1_000_000
DEFAULT_MAX_TOTAL = 50_000_000
DEFAULT_KEEP_SESSIONS = 20


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def same_state(a: dict | None, b: dict | None) -> bool:
    """Равны ли два состояния файла (по хэшу, отсутствию или «большой»)."""
    if not a or not b:
        return a is b or a == b
    if "h" in a and "h" in b:
        return a["h"] == b["h"]
    if a.get("absent") and b.get("absent"):
        return True
    if "big" in a and "big" in b:
        return a["big"] == b["big"]
    return False


class CheckpointStore:
    """Снимки одной сессии: манифест по ходам + дедуплицированные блобы."""

    def __init__(self, session_id: str, cwd, settings=None, root=None):
        self.session_id = str(session_id or "session")
        self.cwd = Path(cwd)
        self.settings = settings
        from . import newmoon

        base = Path(root) if root is not None else             newmoon.redirect(luna_home() / "checkpoints", "checkpoints")
        self.dir = base / self.session_id
        self.objects = self.dir / "objects"
        self.index_path = self.dir / "index.json"
        self._turn = 0
        self._manifest = self._load()
        self._cleaned = False

    # --- настройки ---------------------------------------------------------

    def _limit(self, key: str, default: int) -> int:
        if self.settings is not None:
            try:
                value = int(self.settings.get(key, default) or default)
                return value if value > 0 else default
            except (TypeError, ValueError):
                return default
        return default

    @property
    def max_file_bytes(self) -> int:
        return self._limit("checkpoints.max_file_bytes", DEFAULT_MAX_FILE)

    # --- манифест ------------------------------------------------------------

    def _load(self) -> dict:
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict) or not isinstance(data.get("turns"), dict):
            data = {"session": self.session_id, "turns": {}}
        return data

    def _save(self) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            atomic_write_text(self.index_path,
                              json.dumps(self._manifest, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _turn_entry(self, turn: int) -> dict:
        turns = self._manifest.setdefault("turns", {})
        entry = turns.setdefault(str(turn), {"files": {}, "git": None})
        return entry

    def state_of(self, path) -> dict:
        """Текущее состояние файла на диске (для сравнений снаружи)."""
        return self._state_of(path)

    def forget_after(self, to_turn: int) -> None:
        """Сбросить записи ходов после to_turn: линия времени раздвоилась.

        После отката номера ходов переиспользуются; без сброса новый ход
        дописал бы снимки в манифест мёртвой линии и её «before» оказался
        бы неверным. Сами блобы не трогаем — они общие и дедуплицированы.
        """
        turns = self._manifest.get("turns", {})
        dead = [key for key in turns if key.isdigit() and int(key) > to_turn]
        for key in dead:
            turns.pop(key, None)
        if dead:
            self._save()

    def turns(self) -> list[int]:
        """Номера ходов, по которым есть записи, по возрастанию."""
        out = []
        for key in self._manifest.get("turns", {}):
            try:
                out.append(int(key))
            except ValueError:
                continue
        return sorted(out)

    # --- состояния и блобы -----------------------------------------------------

    def _state_of(self, path) -> dict:
        """Текущее состояние файла на диске (без чтения больших целиком зря)."""
        path = Path(path)
        try:
            size = path.stat().st_size
        except OSError:
            return {"absent": True}
        if size > self.max_file_bytes:
            return {"big": size}
        try:
            data = path.read_bytes()
        except OSError:
            return {"absent": True}
        return {"h": _hash(data)}

    def _store_blob(self, data: bytes) -> str:
        digest = _hash(data)
        target = self.objects / digest
        if not target.exists():
            try:
                self.objects.mkdir(parents=True, exist_ok=True)
                atomic_write(target, data)
            except OSError:
                pass
        return digest

    def _snapshot(self, path) -> dict:
        """Состояние файла со снимком содержимого (если влезает в лимит)."""
        path = Path(path)
        try:
            size = path.stat().st_size
        except OSError:
            return {"absent": True}
        if size > self.max_file_bytes:
            return {"big": size}
        try:
            data = path.read_bytes()
        except OSError:
            return {"absent": True}
        return {"h": self._store_blob(data)}

    def _blob(self, state: dict) -> bytes | None:
        digest = state.get("h")
        if not digest:
            return None
        try:
            return (self.objects / digest).read_bytes()
        except OSError:
            return None

    # --- запись: начало хода, файлы, bash ---------------------------------------

    def begin_turn(self, turn: int) -> None:
        self._turn = int(turn)
        self._turn_entry(self._turn)
        self._save()
        if not self._cleaned:
            self._cleaned = True
            self.cleanup()

    def note_file(self, path) -> None:
        """Снимок прежнего содержимого — вызывать ПЕРЕД изменением файла.

        Внутри одного хода важен только первый снимок: состояние до хода.
        """
        try:
            key = str(Path(path))
            entry = self._turn_entry(self._turn)
            if key not in entry["files"]:
                entry["files"][key] = {"before": self._snapshot(path)}
                self._save()
        except Exception:  # noqa: BLE001 — чекпоинт не роняет тул
            pass

    def note_after(self, path) -> None:
        """Зафиксировать состояние файла сразу после изменения тулом.

        По «after» откат отличает правки человека после хода от результата
        самого хода: содержимое не сошлось с after — файл трогали руками.
        """
        try:
            key = str(Path(path))
            entry = self._turn_entry(self._turn)
            record = entry["files"].get(key)
            if record is None:
                # Тул не звал note_file (не должен, но страхуемся): «до» —
                # текущее содержимое, снимок делается только в этом случае.
                record = entry["files"][key] = {"before": self._snapshot(path)}
            record["after"] = self._state_of(path)
            self._save()
        except Exception:  # noqa: BLE001
            pass

    def note_bash(self, cwd=None) -> None:
        """Git-пометка хода перед первым bash: дерево без касания рабочей копии."""
        try:
            entry = self._turn_entry(self._turn)
            if entry.get("git") is not None:
                return
            entry["git"] = self._git_note(Path(cwd) if cwd is not None else self.cwd)
            self._save()
        except Exception:  # noqa: BLE001
            pass

    def _git(self, args: list, cwd: Path):
        try:
            return subprocess.run(["git", "-C", str(cwd)] + args,
                                  capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return None

    def _git_note(self, cwd: Path) -> dict:
        probe = self._git(["rev-parse", "--is-inside-work-tree"], cwd)
        if probe is None or probe.returncode != 0 or probe.stdout.strip() != "true":
            return {"repo": False,
                    "note": "не git-репозиторий: откат команд оболочки не гарантирован"}
        created = self._git(["stash", "create"], cwd)
        sha = created.stdout.strip() if created is not None and created.returncode == 0 else ""
        return {"repo": True, "stash": sha,
                "note": "" if sha else "дерево было чистым"}

    # --- восстановление ------------------------------------------------------------

    def plan_restore(self, to_turn: int) -> dict:
        """Что вернёт откат к состоянию после хода to_turn.

        target — состояние, которое должно стать; expected — последнее
        известное состояние после всех ходов. Текущее содержимое не сошлось
        с expected — файл меняли руками после агента (конфликт).
        """
        targets: dict[str, dict] = {}
        expected: dict[str, dict] = {}
        git_ref = None
        git_notes: list[str] = []
        turns = self._manifest.get("turns", {})
        for turn in self.turns():
            entry = turns.get(str(turn), {})
            for path, record in entry.get("files", {}).items():
                if turn > to_turn and path not in targets:
                    targets[path] = record.get("before") or {"absent": True}
                state = record.get("after") or record.get("before")
                if state is not None:
                    expected[path] = state
            note = entry.get("git")
            if turn > to_turn and note:
                if git_ref is None and note.get("repo"):
                    git_ref = note.get("stash") or "HEAD"
                if not note.get("repo"):
                    git_notes.append("ход " + str(turn) + ": " + str(note.get("note", "")))
        changes = []
        conflicts = []
        for path, target in targets.items():
            current = self._state_of(path)
            if same_state(current, target):
                continue  # уже в нужном состоянии
            item = {"path": path, "target": target, "current": current}
            want = expected.get(path)
            if want is not None and not same_state(current, want):
                conflicts.append(item)
            else:
                changes.append(item)
        return {"changes": changes, "conflicts": conflicts,
                "git_ref": git_ref, "git_notes": git_notes}

    def capture(self, paths) -> dict:
        """Снять текущие состояния файлов (для redo: чем откат затирает)."""
        out = {}
        for path in paths:
            out[str(path)] = self._snapshot(path)
        return out

    def apply_states(self, states: dict) -> tuple:
        """Привести файлы к заданным состояниям. Возврат: (сделано, пропущено)."""
        done, skipped = [], []
        for path, state in states.items():
            if self._apply_one(path, state):
                done.append(path)
            else:
                skipped.append(path)
        return done, skipped

    def _apply_one(self, path, state: dict) -> bool:
        path = Path(path)
        if state.get("absent"):
            try:
                path.unlink(missing_ok=True)
                return True
            except OSError:
                return False
        data = self._blob(state)
        if data is None:
            return False  # {"big": …} или потерянный блоб: восстановить нечем
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(path, data)
            return True
        except OSError:
            return False

    def restore_to(self, to_turn: int, *, overwrite_conflicts: bool = False,
                   skip_paths=()) -> dict:
        """Откатить файлы к состоянию после хода to_turn.

        Порядок: сначала git-дерево (черновой проход по трекнутым файлам),
        потом точные снимки файловых тулов — они перекрывают git-результат.
        Файлы-конфликты (правлены руками) не трогаем, если не сказано
        overwrite_conflicts. Возвращает отчёт и previous — состояния файлов
        до отката (материал для redo).
        """
        plan = self.plan_restore(to_turn)
        notes = list(plan["git_notes"])
        skip = {str(p) for p in skip_paths}
        todo = [c for c in plan["changes"] if c["path"] not in skip]
        conflicts = [c for c in plan["conflicts"] if c["path"] not in skip]
        if overwrite_conflicts:
            todo = todo + conflicts
            conflicts = []
        paths = [c["path"] for c in todo]
        previous = self.capture(paths)
        if plan["git_ref"]:
            ok, note = self._git_restore(plan["git_ref"])
            notes.append(note if ok else "git restore не вышел: " + note)
        states = {c["path"]: c["target"] for c in todo}
        done, failed = self.apply_states(states)
        big = [c["path"] for c in todo if "big" in c["target"]]
        return {"restored": done,
                "failed": [p for p in failed if p not in big],
                "no_snapshot": big,
                "conflicts": [c["path"] for c in conflicts],
                "previous": previous,
                "notes": notes}

    def _git_restore(self, ref: str) -> tuple:
        result = self._git(["restore", "--source", ref, "--worktree", "--", "."], self.cwd)
        if result is None:
            return False, "git недоступен"
        if result.returncode != 0:
            return False, (result.stderr or result.stdout or "ошибка git").strip()[:200]
        return True, "дерево приведено к состоянию перед bash-ходами (" + ref[:12] + ")"

    # --- чистка ----------------------------------------------------------------------

    def total_size(self) -> int:
        total = 0
        try:
            for path in self.dir.rglob("*"):
                if path.is_file():
                    total += path.stat().st_size
        except OSError:
            pass
        return total

    def cleanup(self) -> None:
        """Лимиты: число хранимых сессий и общий размер папки checkpoints.

        Чистим только чужие сессии, старейшие первыми; свою не трогаем.
        """
        try:
            keep = self._limit("checkpoints.keep_sessions", DEFAULT_KEEP_SESSIONS)
            max_total = self._limit("checkpoints.max_total_bytes", DEFAULT_MAX_TOTAL)
            base = self.dir.parent
            if not base.exists():
                return
            sessions = []
            for path in base.iterdir():
                if path.is_dir() and path != self.dir:
                    try:
                        sessions.append((path.stat().st_mtime, path))
                    except OSError:
                        continue
            sessions.sort()
            total = self.total_size() + sum(
                sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
                for _, p in sessions)
            while sessions and (len(sessions) + 1 > keep or total > max_total):
                _mtime, victim = sessions.pop(0)
                size = sum(f.stat().st_size for f in victim.rglob("*") if f.is_file())
                shutil.rmtree(victim, ignore_errors=True)
                total -= size
        except Exception:  # noqa: BLE001 — чистка не роняет агента
            pass
