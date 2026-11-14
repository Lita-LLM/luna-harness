"""/keep — сохранить сессию Новолуния в файл под паролем и открыть обратно.

Правила, ради которых это сделано осторожно:
  * только руками человека — команда чата, у модели такого тула нет;
  * перед записью видно, что уйдёт на диск; Esc на пароле — отмена;
  * файл — сейф (core/vault.py), пароль не хранится нигде;
  * в /sessions файл не попадает; открывается только в Новолунии,
    и пока сессия открыта, на диск снова ничего не пишется — до
    следующего явного /keep (он перезаписывает тот же файл);
  * разрешения моделей Новолуния в файл не идут — после открытия
    их снова спросят.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from ..core import newmoon as _nm
from ..core import vault
from ..core.paths import luna_home


def default_dir() -> Path:
    return luna_home() / "kept"


def _stats(tape: list) -> tuple:
    turns = sum(1 for r in tape if r.get("kind") == "event"
                and (r.get("data") or {}).get("type") == "turn.start")
    size = len(json.dumps(tape, ensure_ascii=False, default=str).encode("utf-8"))
    return turns, size


def keep_command(app, arg: str) -> None:
    from .chat import Entry

    arg = (arg or "").strip()
    if arg.split(" ", 1)[0] in ("open", "открыть"):
        open_command(app, arg.split(" ", 1)[1] if " " in arg else "")
        return
    if not _nm.active():
        app._add(Entry("notice", text="/keep — для Новолуния: обычная сессия и так "
                                      "в журнале (/sessions)."))
        return
    if app.busy:
        app._add(Entry("notice", text="Идёт ход — дождись конца, потом /keep."))
        return
    tape = list(app.tape)
    turns, size = _stats(tape)
    if not turns:
        app._add(Entry("notice", text="Сохранять пока нечего: ходов не было."))
        return
    if arg:
        path = Path(arg).expanduser()
        if path.is_dir() or arg.endswith(("/", "\\")):
            path = path / time.strftime("%Y%m%d-%H%M%S")
    elif getattr(app, "_kept_path", None):
        path = Path(app._kept_path)
    else:
        path = default_dir() / time.strftime("%Y%m%d-%H%M%S")
    if path.suffix != vault.SUFFIX:
        path = path.with_name(path.name + vault.SUFFIX)
    app._add(Entry("notice", text=(
        "🔒 На диск уйдёт: ходов " + str(turns) + ", ~" + str(max(1, size // 1024))
        + " КБ (лента и память модели) → " + str(path) + ". Не уйдут: разрешения "
        "моделей, временные файлы, вывод фона. Пароль не хранится — забудешь, "
        "файл не открыть. Esc — отмена.")))

    def got_first(pw: str) -> None:
        if not pw:
            app._add(Entry("notice", text="Пустой пароль — не сохраняю."))
            return

        def got_second(pw2: str) -> None:
            if pw2 != pw:
                app._add(Entry("notice", text="Пароли не совпали — не сохраняю."))
                return
            payload = {"version": 1, "saved": time.time(), "tape": tape,
                       "title": getattr(app, "session_title", "") or "",
                       "cwd": str(getattr(app, "cwd", "") or "")}
            try:
                final = vault.write(path, payload, pw)
            except Exception as exc:  # noqa: BLE001 — человеку причина, сессия цела
                app._add(Entry("notice", text="Не сохранилось: " + str(exc)))
                return
            app._kept_path = str(final)
            app._add(Entry("notice", text="🌑 Сохранено под паролем: " + str(final)
                                          + ". Дальше снова ничего не пишу — "
                                            "до следующего /keep."))

        app.ask_secret("пароль ещё раз", got_second)

    app.ask_secret("пароль для файла", got_first)


def open_command(app, arg: str) -> None:
    from .chat import Entry

    if not _nm.active():
        app._add(Entry("notice", text="Открыть сохранённое можно только в Новолунии: "
                                      "сначала /newmoon, потом /keep open <файл>."))
        return
    if app.busy:
        app._add(Entry("notice", text="Идёт ход — дождись конца."))
        return
    arg = arg.strip()
    if not arg:
        found = sorted(default_dir().glob("*" + vault.SUFFIX)) if default_dir().is_dir() else []
        names = ", ".join(p.name for p in found[-8:]) or "пусто"
        app._add(Entry("notice", text="/keep open <файл>. В " + str(default_dir())
                                      + ": " + names))
        return
    path = Path(arg).expanduser()
    if not path.exists() and not path.is_absolute():
        alt = default_dir() / arg
        path = alt if alt.exists() else alt.with_name(alt.name + vault.SUFFIX)
    if not path.exists():
        app._add(Entry("notice", text="Нет такого файла: " + str(path)))
        return

    def got(pw: str) -> None:
        try:
            data = vault.read(path, pw)
        except vault.VaultError as exc:
            app._add(Entry("notice", text="Не открылось: " + str(exc) + "."))
            return
        records = [r for r in data.get("tape") or [] if isinstance(r, dict)]
        app.entries.clear()
        app.queue.clear()
        app.todos = []
        app.context_pct = 0
        app.tape = list(records)
        app.restore(records)
        app.attach_history(records)
        app.send_command({"type": "mode.set", "mode": app.mode})
        if data.get("title"):
            app.session_title = str(data["title"])
            app._session_titled = True
            app._update_title()
        app._kept_path = str(path)
        app._add(Entry("notice", text="🌑 Открыто из сейфа: " + path.name
                                      + ". Модель помнит разговор; модели "
                                        "снова подтвердишь при первом запросе. "
                                        "/keep — сохранить новое туда же."))

    app.ask_secret("пароль от " + path.name, got)
