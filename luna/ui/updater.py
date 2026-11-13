"""Обновления в чате: проверка на GitHub, установка и горячая перезагрузка.

/reload — применить код с диска (свежий git pull, правки) без перезапуска;
/update — проверить сервер, поставить fast-forward и сразу /reload;
/update check — только проверить. Фоном — раз в update.interval_min
минут; найдено новое → метка «↻ обновление» в статусе (клик — /update),
а при update.check = install — ставится и применяется само, как только
Луна свободна. git идёт в потоке: экран не замирает на fetch.

Перезагрузка ждёт тишины: ни хода, ни работающих спутников — иначе
обрывались бы их потоки. Сессия переезжает снимком (snapshot): журнал на
диске перечитывается, а без журнала (Новолуние) — лента записей из памяти.
"""

from __future__ import annotations

import threading
import time

from ..core import newmoon as _nm


class Updater:
    def __init__(self, app):
        self.app = app
        self.available = 0          # новых коммитов на сервере (0 — нет или не знаем)
        self.pending_reload = False
        self._job: threading.Thread | None = None
        self._result = None         # (что, данные) из потока
        self._next_at = time.monotonic() + 30.0  # первая проверка — не в старт
        self._install_after = False
        # Фоновая проверка — только в живом чате (run_chat включает):
        # тесты и витрины не ходят в git по сети.
        self.auto = False

    # --- настройки -------------------------------------------------------------

    def _mode(self) -> str:
        if not self.auto:
            return "off"
        mode = str(self.app.settings.get("update.check") or "notify")
        if _nm.active() and not self.app.settings.get("update.in_newmoon"):
            return "off"  # без спроса наружу из тёмной стороны не ходим
        return mode

    def _interval(self) -> float:
        return max(5, int(self.app.settings.get("update.interval_min") or 60)) * 60.0

    # --- цикл чата ---------------------------------------------------------------

    def timeout(self):
        """Когда главному циклу проснуться ради нас (None — не нужно)."""
        if self._job is not None or self.pending_reload:
            return 0.5
        if self._mode() == "off":
            return None
        return max(0.5, self._next_at - time.monotonic())

    def tick(self) -> None:
        if self._job is not None and not self._job.is_alive():
            self._job = None
            result, self._result = self._result, None
            if result is not None:
                self._finish(*result)
        if self._job is None and self._mode() != "off" and time.monotonic() >= self._next_at:
            self._next_at = time.monotonic() + self._interval()
            self._run("check", quiet=True)
        if self.pending_reload and self._idle():
            self.pending_reload = False
            self._do_reload()

    # --- команды -----------------------------------------------------------------

    def reload_command(self) -> None:
        if self._idle():
            self._do_reload()
            return
        self.pending_reload = True
        self._say("↻ Перезагружусь, как только закончится ход и спутники освободятся.")

    def update_command(self, arg: str = "") -> None:
        if self._job is not None:
            self._say("Проверка обновлений уже идёт.")
            return
        if arg.strip() in ("check", "проверить"):
            self._say("Проверяю обновления на сервере…")
            self._run("check", quiet=False)
        else:
            self._say("Ищу и ставлю обновление…")
            self._run("install", quiet=False)

    # --- фон ---------------------------------------------------------------------

    def _run(self, what: str, quiet: bool) -> None:
        def work():
            from ..diagnostics import update as upd

            try:
                info = upd.check()
                if what == "install" and info.behind:
                    new = upd.install(info=info)
                    self._result = ("installed", (info, new, quiet))
                else:
                    self._result = ("checked", (info, quiet))
            except Exception as exc:  # noqa: BLE001 — HumanError и прочее — в ленту
                self._result = ("error", (exc, quiet))

        self._job = threading.Thread(target=work, name="luna-update", daemon=True)
        self._job.start()

    def _finish(self, kind: str, data) -> None:
        if kind == "error":
            exc, quiet = data
            if not quiet:
                title = getattr(exc, "title", "") or str(exc)
                hint = getattr(exc, "hint", "")
                self._say("Обновление: " + title + (". " + hint if hint else ""))
            return
        if kind == "checked":
            info, quiet = data
            self.available = info.behind
            if not info.behind:
                if not quiet:
                    self._say("Луна актуальна: " + info.branch + " @ " + info.head + ".")
                return
            if quiet and self._mode() == "install":  # /update check — только проверить
                self._run("install", quiet=True)
                return
            if not quiet or not getattr(self, "_told", False):
                self._told = True
                self._say("↻ Есть обновление: новых коммитов — " + str(info.behind) +
                          self._log_tail(info) + "\nПоставить и применить на лету — /update.")
            return
        if kind == "installed":
            info, new, _quiet = data
            self.available = 0
            self._say("↻ Обновление поставлено: " + info.head + " → " + new +
                      self._log_tail(info) + "\nПрименяю без перезапуска…")
            self.reload_command()

    @staticmethod
    def _log_tail(info) -> str:
        lines = info.log[:5]
        more = len(info.log) - len(lines)
        text = "".join("\n  " + ln for ln in lines)
        return text + ("\n  … и ещё " + str(more) if more > 0 else "")

    # --- перезагрузка ------------------------------------------------------------

    def _idle(self) -> bool:
        app = self.app
        if app.busy:
            return False
        source = getattr(app, "source", None)
        agent = getattr(source, "_agent", None)
        host = getattr(agent, "_agents_host", None)
        busy = getattr(host, "busy_children", None)
        if callable(busy):
            try:
                if busy():
                    return False
            except Exception:  # noqa: BLE001
                pass
        return True

    def _do_reload(self) -> None:
        shutdown = getattr(getattr(self.app, "source", None), "shutdown", None)
        if callable(shutdown):
            shutdown()
        self.app.handoff = self.snapshot()
        self.app.relaunch = "reload"
        self.app.done = True

    def snapshot(self) -> dict:
        """Всё, что переживает перезагрузку: только простые данные, без объектов кода."""
        app = self.app
        journal = app.journal
        settings = app.settings
        return {
            "journal_path": str(journal.path) if journal is not None else "",
            "tape": list(app.tape) if journal is None else [],
            "settings_session": dict(getattr(settings, "_session", {}) or {}),
            "confirmed": sorted(_nm.confirmed_keys()),
            "draft": app.editor.text,
            "title": app.session_title,
            "titled": bool(getattr(app, "_session_titled", False)),
            "cwd": app.cwd,
            "at": time.time(),
        }

    def _say(self, text: str) -> None:
        from .chat import Entry

        self.app._add(Entry("notice", text=text))
