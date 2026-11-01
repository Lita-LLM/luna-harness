"""Чекпоинты файлов (ЗАДАЧА-13): снимки, дедупликация, лимиты, откат,
конфликты ручных правок, git-пометка хода."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from luna.core.checkpoints import CheckpointStore
from luna.core.settings import Settings


class StoreBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="luna-cp-"))
        self.work = self.tmp / "work"
        self.work.mkdir()
        self.settings = Settings(home=self.tmp / "home", cwd=self.work)

    def make_store(self, session="s1", settings=None) -> CheckpointStore:
        return CheckpointStore(session, cwd=self.work,
                               settings=settings or self.settings,
                               root=self.tmp / "cp")

    def change(self, store, turn: int, path: Path, content: str) -> None:
        """Имитация файлового тула: снимок до, запись, фиксация после."""
        store.begin_turn(turn)
        store.note_file(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        store.note_after(path)


class SnapshotTest(StoreBase):
    def test_dedup_same_content_one_blob(self):
        store = self.make_store()
        a = self.work / "a.txt"
        b = self.work / "b.txt"
        a.write_text("одинаковый текст\n", encoding="utf-8")
        b.write_text("одинаковый текст\n", encoding="utf-8")
        self.change(store, 1, a, "стало а\n")   # снимки «до» у обоих одинаковы
        self.change(store, 1, b, "стало б\n")
        blobs = list(store.objects.iterdir())
        self.assertEqual(len(blobs), 1)  # дедупликация по хэшу

    def test_restore_returns_previous_and_deletes_created(self):
        store = self.make_store()
        a = self.work / "a.txt"
        a.write_text("v1\n", encoding="utf-8")
        self.change(store, 1, a, "v2\n")
        b = self.work / "b.txt"
        self.change(store, 2, b, "новый\n")  # до хода файла не было
        report = store.restore_to(1)
        self.assertEqual(a.read_text(encoding="utf-8"), "v2\n")  # ход 1 не тронут
        self.assertFalse(b.exists())  # созданный ходом 2 — удалён
        self.assertIn(str(b), report["restored"])

    def test_restore_two_turns_back(self):
        store = self.make_store()
        a = self.work / "a.txt"
        a.write_text("v1\n", encoding="utf-8")
        self.change(store, 1, a, "v2\n")
        self.change(store, 2, a, "v3\n")
        store.restore_to(0)
        self.assertEqual(a.read_text(encoding="utf-8"), "v1\n")

    def test_settings_defaults_exist(self):
        settings = Settings(home=self.tmp / "home", cwd=self.work)
        self.assertTrue(settings.get("checkpoints.enabled"))
        self.assertEqual(settings.get("compact.threshold_pct"), 80)
        self.assertEqual(settings.get("compact.keep_turns"), 2)


class ConflictTest(StoreBase):
    def test_manual_edit_is_conflict_and_survives(self):
        store = self.make_store()
        a = self.work / "a.txt"
        a.write_text("v1\n", encoding="utf-8")
        self.change(store, 1, a, "v2\n")
        a.write_text("правка руками\n", encoding="utf-8")  # человек после хода
        plan = store.plan_restore(0)
        self.assertEqual([c["path"] for c in plan["conflicts"]], [str(a)])
        report = store.restore_to(0)
        self.assertEqual(a.read_text(encoding="utf-8"), "правка руками\n")
        self.assertEqual(report["conflicts"], [str(a)])
        report2 = store.restore_to(0, overwrite_conflicts=True)
        self.assertEqual(a.read_text(encoding="utf-8"), "v1\n")
        self.assertIn(str(a), report2["restored"])

    def test_big_file_has_no_snapshot(self):
        self.settings.set("checkpoints.max_file_bytes", 10)
        store = self.make_store()
        a = self.work / "big.txt"
        a.write_text("1234567890\n", encoding="utf-8")  # 11 байт > лимита 10
        self.change(store, 1, a, "ещё больше текста, длинная строка\n")
        report = store.restore_to(0)
        self.assertEqual(report["no_snapshot"], [str(a)])
        self.assertEqual(a.read_text(encoding="utf-8"),
                         "ещё больше текста, длинная строка\n")


class CleanupTest(StoreBase):
    def test_keep_sessions_prunes_others_not_self(self):
        self.settings.set("checkpoints.keep_sessions", 1)
        old = self.make_store("old-session")
        f = self.work / "x.txt"
        self.change(old, 1, f, "данные\n")
        self.assertTrue(old.dir.exists())
        fresh = self.make_store("new-session")
        fresh.begin_turn(1)  # чистка срабатывает при первом ходе
        self.assertFalse(old.dir.exists())
        self.assertTrue(fresh.dir.exists())


@unittest.skipUnless(shutil.which("git"), "нет git")
class GitNoteTest(StoreBase):
    def _repo(self) -> Path:
        subprocess.run(["git", "init", "-q"], cwd=self.work, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=self.work, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=self.work, check=True)
        a = self.work / "a.txt"
        a.write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.work, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=self.work, check=True)
        return a

    @unittest.skipIf(subprocess.run(["git", "--version"], capture_output=True).returncode != 0, "git битый")
    def test_bash_change_rolls_back_via_git(self):
        a = self._repo()
        store = self.make_store()
        store.begin_turn(1)
        store.note_bash(self.work)  # дерево чистое: stash create пуст, ref — HEAD
        a.write_text("changed by bash\n", encoding="utf-8")  # как будто bash
        untracked = self.work / "new.txt"
        untracked.write_text("untracked\n", encoding="utf-8")
        report = store.restore_to(0)
        self.assertEqual(a.read_text(encoding="utf-8"), "base\n")
        self.assertTrue(untracked.exists())  # честно: untracked git не убирает
        self.assertTrue(any("дерево приведено" in n for n in report["notes"]))

    def test_outside_git_honest_note(self):
        store = self.make_store()
        store.begin_turn(1)
        store.note_bash(self.work)
        plan = store.plan_restore(0)
        self.assertTrue(any("не гарантирован" in n for n in plan["git_notes"]))
