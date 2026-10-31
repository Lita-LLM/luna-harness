"""ЗАДАЧА-08: luna update на локальных git-репозиториях (без сети) и путь лаунчера в doctor."""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from luna.core.errors import HumanError
from luna.diagnostics import doctor
from luna.diagnostics.update import run_update

GIT = shutil.which("git")


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update({
        "GIT_AUTHOR_NAME": "Тест", "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Тест", "GIT_COMMITTER_EMAIL": "test@example.com",
        "GIT_TERMINAL_PROMPT": "0",
    })
    return subprocess.run([GIT, *args], cwd=cwd, env=env,
                          capture_output=True, text=True, check=True)


def _repo_with_commit(base: Path, name: str, text: str) -> Path:
    repo = base / name
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "file.txt").write_text(text, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "коммит")
    return repo


@unittest.skipUnless(GIT, "нужен git")
class UpdateGitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="luna-update-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.origin = _repo_with_commit(self.tmp, "origin", "раз\n")
        self.clone = self.tmp / "clone"
        _git(self.tmp, "clone", str(self.origin), str(self.clone))

    def _commit_to_origin(self, text: str) -> None:
        (self.origin / "file.txt").write_text(text, encoding="utf-8")
        _git(self.origin, "add", ".")
        _git(self.origin, "commit", "-m", "второй коммит")

    def _head(self, repo: Path) -> str:
        return _git(repo, "rev-parse", "HEAD").stdout.strip()

    def test_check_and_update(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(run_update(check_only=True, root=self.clone), 0)
        self.assertIn("актуален", out.getvalue())

        self._commit_to_origin("два\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(run_update(check_only=True, root=self.clone), 0)
        self.assertIn("Есть обновление", out.getvalue())
        self.assertNotEqual(self._head(self.clone), self._head(self.origin))

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(run_update(root=self.clone), 0)
        self.assertIn("Обновлено", out.getvalue())
        self.assertEqual(self._head(self.clone), self._head(self.origin))

    def test_local_edits_block_update(self):
        self._commit_to_origin("два\n")
        (self.clone / "file.txt").write_text("своя правка\n", encoding="utf-8")
        with self.assertRaises(HumanError) as ctx:
            run_update(root=self.clone)
        self.assertIn("локальные правки", ctx.exception.title)
        self.assertNotEqual(self._head(self.clone), self._head(self.origin))

    def test_detached_head(self):
        _git(self.clone, "checkout", "--detach", "HEAD")
        with self.assertRaises(HumanError) as ctx:
            run_update(root=self.clone)
        self.assertIn("отдельном коммите", ctx.exception.title)


class UpdateArchiveTest(unittest.TestCase):
    def test_archive_copy_gets_hint(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(HumanError) as ctx:
                run_update(root=Path(tmp))
            if GIT:
                self.assertIn("из архива", ctx.exception.title)
                self.assertIn("архив", ctx.exception.hint)


class LauncherPathTest(unittest.TestCase):
    def test_posix_default(self):
        with mock.patch.object(sys, "platform", "linux"), \
                mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TERMUX_VERSION", None)
            path = doctor.launcher_path()
        self.assertEqual(path.name, "luna")
        self.assertEqual(path.parent.name, "bin")

    def test_windows(self):
        env = {"LOCALAPPDATA": "C:\\Users\\test\\AppData\\Local"}
        with mock.patch.object(sys, "platform", "win32"), \
                mock.patch.dict(os.environ, env):
            path = doctor.launcher_path()
        self.assertEqual(path.name, "luna.cmd")
        self.assertIn("Luna", str(path))

    def test_dir_in_path(self):
        directory = Path("/some/where/bin")
        with mock.patch.dict(os.environ, {"PATH": "/a" + os.pathsep + "/some/where/bin"}):
            self.assertTrue(doctor._dir_in_path(directory))
        with mock.patch.dict(os.environ, {"PATH": "/a" + os.pathsep + "/some/where/bin/"}):
            self.assertTrue(doctor._dir_in_path(directory))
        with mock.patch.dict(os.environ, {"PATH": "/a" + os.pathsep + "/b"}):
            self.assertFalse(doctor._dir_in_path(directory))

    def test_dir_in_path_windows_semantics(self):
        # Регрессия CI (windows-latest): windows-режим форсируется явно, поэтому
        # кейсы обязаны ловить семантику Windows на любой платформе, а не только
        # на живой. Падавший случай — прямые слэши в PATH против обратных
        # в str(Path) на Windows.
        directory = Path("C:\\Users\\Test\\Luna\\bin")
        cases = [
            "c:\\users\\test\\luna\\bin",      # регистр букв не важен
            "C:\\Users\\Test\\Luna\\bin\\",    # завершающий разделитель
            "C:/Users/Test/Luna/bin",          # прямые слэши вместо обратных
            '"C:\\Users\\Test\\Luna\\bin"',    # кавычки вокруг записи
        ]
        for entry in cases:
            with self.subTest(entry=entry):
                path = "D:\\other" + ";" + entry
                self.assertTrue(
                    doctor._dir_in_path(directory, windows=True, path=path))
        self.assertFalse(doctor._dir_in_path(
            directory, windows=True, path="C:\\Users\\Test\\Other\\bin"))

    def test_dir_in_path_windows_env_var(self):
        with mock.patch.dict(os.environ, {"LUNA_TEST_ROOT": "C:\\Luna"}):
            self.assertTrue(doctor._dir_in_path(
                Path("C:\\Luna\\bin"), windows=True,
                path="%LUNA_TEST_ROOT%\\bin"))


if __name__ == "__main__":
    unittest.main()
