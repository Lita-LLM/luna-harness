"""ЗАДАЧА-17: раскладка, панели, уборка сессий.

Всё без терминала: ChatApp на фальшивом агенте, сессии — настоящими
журналами во временных каталогах, клавиши — событиями Key/Mouse.
"""

import os
import tempfile
import time
import unittest
from pathlib import Path

from luna.core.bus import EventBus
from luna.core.paths import DataDir
from luna.core.sessions import (SessionJournal, drop_empty_sessions,
                                list_sessions, purge_trash, session_is_empty,
                                trash_session, undo_trash, load_records)
from luna.core.settings import Settings
from luna.ui import layout
from luna.ui.anim import Scheduler
from luna.ui.chat import ChatApp, resolve_command
from luna.ui.keys import Key, KeyParser, Mouse
from luna.ui.panels import LayoutTree, create_panel
from luna.ui.screen import Buffer

PANEL_BG = (0x10, 0x13, 0x24)


def make_app(case: unittest.TestCase, **overrides) -> ChatApp:
    tmp = tempfile.TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    root = Path(tmp.name)
    (root / "work").mkdir(parents=True)
    settings = Settings(home=root / "home", cwd=root / "work")
    for key, value in overrides.items():
        settings.set(key, value)
    return ChatApp(EventBus(), settings, Scheduler(), "emoji",
                   cwd=str(root / "work"), data=DataDir(root / "home"))


def key(name: str, char: str = "", mods=()) -> Key:
    return Key(name, char=char, mods=frozenset(mods))


def make_session(data: DataDir, cwd: str, *, text: str | None = None,
                 title: str = "") -> Path:
    """Журнал сессии на диске; text=None — пустая (без реплик человека)."""
    journal = SessionJournal.create(data, cwd)
    if title:
        journal.set_title(title)
    if text is not None:
        journal.append_command({"type": "user.message", "text": text})
    journal.close()
    return journal.path


class LayoutTest(unittest.TestCase):
    def test_translate(self):
        self.assertEqual(layout.to_latin("зфтуд"), "panel")
        self.assertEqual(layout.to_latin("дшьшеы"), "limits")
        self.assertEqual(layout.to_cyrillic("kbvbns"), "лимиты")
        self.assertEqual(layout.to_latin("Лимиты"), "Kbvbns")
        self.assertIn("panel", layout.variants("зфтуд"))

    def test_match_name(self):
        names = {"limits": ("limits", "Лимиты"), "moon": ("moon", "Луна")}
        self.assertEqual(layout.match_name("kbvbns", names), "limits")
        self.assertEqual(layout.match_name("дшьшеы", names), "limits")
        self.assertEqual(layout.match_name("Луна", names), "moon")
        self.assertIsNone(layout.match_name("рой", names))

    def test_resolve_command(self):
        self.assertEqual(resolve_command("панель"), "panel")
        self.assertEqual(resolve_command("зфтуд"), "panel")
        self.assertEqual(resolve_command("сессии"), "sessions")
        self.assertEqual(resolve_command("gfytkm"), "panel")
        self.assertIsNone(resolve_command("абракадабра"))

    def test_alt_cyrillic_is_physical(self):
        # POSIX-парсер: Esc + «д» — это Alt+L, а не Alt+д.
        events = KeyParser().feed("\x1bд".encode("utf-8"))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].label, "alt+l")
        # Тот же смысл — на уровне приложения (клавиша мимо парсера).
        fixed = layout.physical_key(key("д", "д", ("alt",)))
        self.assertEqual(fixed.label, "alt+l")
        plain = layout.physical_key(key("д", "д"))
        self.assertEqual(plain.name, "д")  # обычный текст не трогаем


class ChatLayoutTest(unittest.TestCase):
    def test_dot_command_in_ytsuken(self):
        app = make_app(self)
        app.submit_text(".зфтуд Лимиты")
        self.assertIn("limits", app.layout.leaves())

    def test_dot_message_untouched(self):
        app = make_app(self)
        app.submit_text(".Привет. Как дела?")
        sent = [c for c in app.sent_commands if c.get("type") == "user.message"]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["text"], ".Привет. Как дела?")
        self.assertEqual(app.layout.leaves(), [])

    def test_russian_command_names(self):
        app = make_app(self)
        app.submit_text("/панель Лимиты")
        self.assertIn("limits", app.layout.leaves())
        app.submit_text("/сессии")
        self.assertEqual(app._picker_kind, "sessions")

    def test_panel_arg_in_wrong_layout(self):
        app = make_app(self)
        app.submit_text("/panel kbvbns")
        self.assertIn("limits", app.layout.leaves())

    def test_layout_fix_off(self):
        app = make_app(self, **{"ui.layout_fix": False})
        app.submit_text(".зфтуд Лимиты")
        sent = [c for c in app.sent_commands if c.get("type") == "user.message"]
        self.assertEqual(len(sent), 1)  # ушло обычным сообщением
        self.assertEqual(app.layout.leaves(), [])
        app.submit_text("/панель Лимиты")
        self.assertEqual(app.layout.leaves(), [])
        notes = [e.text for e in app.entries if e.kind == "notice"]
        self.assertTrue(any("Не знаю команду" in t for t in notes))

    def test_alt_cyrillic_toggles_panel(self):
        app = make_app(self)
        app.handle_key(key("д", "д", ("alt",)))
        self.assertIn("limits", app.layout.leaves())

    def test_mode_and_model_args_in_wrong_layout(self):
        app = make_app(self)
        app.submit_text("/режим здфт")
        modes = [c for c in app.sent_commands if c.get("type") == "mode.set"]
        self.assertEqual(modes[-1]["mode"], "plan")
        app.submit_text("/model афые fake:echo")
        self.assertEqual(app.settings.get("model.fast"), "fake:echo")

    def test_tab_completion_knows_russian(self):
        app = make_app(self)
        app.editor.set_text("/па")
        app._update_completions()
        names = [name for name, _desc in app.cmd_completions]
        self.assertIn("панель", names)
        app.completion_index = names.index("панель")
        app.handle_key(key("tab"))
        self.assertEqual(app.editor.text, "/панель ")


class PanelsViewTest(unittest.TestCase):
    def test_height_hint_gives_content_size(self):
        tree = LayoutTree()
        tree.open("moon")
        tree.open("limits")
        rects = tree.rects(0, 0, 40, 20, hints={"limits": (3, 3)})
        self.assertEqual(rects["limits"][3], 3)   # «нет данных» — не треть экрана
        self.assertEqual(rects["moon"][3], 17)

    def test_manual_resize_wins_over_hint(self):
        app = make_app(self)
        app.toggle_panel("limits")
        app.toggle_panel("moon")
        app.layout.focused = "limits"
        app.panel_focus = True
        app.handle_key(key("right"))  # ←→ — ручной размер
        self.assertIn("limits", app.layout.manual)
        self.assertNotIn("limits", app._panel_hints(app.layout, 40))

    def test_moon_panel_draws_disc(self):
        app = make_app(self)
        app.on_agent_event({"type": "usage", "model": "fake", "input": 10,
                            "output": 5, "cost": 0.001, "context_pct": 50})
        content = create_panel("moon")
        buf = Buffer(36, 14)
        buf.clear(bg=PANEL_BG)
        self.assertTrue(content.draw_into(buf, app, 0, 1, 36, 12))
        cells = [buf.cell(x, y) for y in range(14) for x in range(36)]
        self.assertTrue(any(ch == "▀" for ch, _f, _b, _a in cells))
        # Фаза — цветом подпикселей: внутри диска есть ячейки, где верх и низ разные.
        self.assertTrue(any(ch == "▀" and fg != bg for ch, fg, bg, _a in cells))

    def test_panels_have_headers_dividers_and_hint(self):
        app = make_app(self)
        app.toggle_panel("moon")
        app.toggle_panel("limits")
        app.panel_focus = True
        buf, _cursor = app.draw(100, 30, 1000.0)
        text = "\n".join("".join(buf.cell(x, y)[0] for x in range(100))
                         for y in range(30))
        self.assertIn("Луна", text)
        self.assertIn("Лимиты", text)
        self.assertIn("─", text)               # тонкий разделитель
        flat = " ".join(text.split())
        self.assertIn("x — закрыть", flat)      # подсказка режима панелей
        self.assertIn("назад к вводу", flat)
        self.assertIn("▸", text)                # фокус подсвечен

    def test_small_panel_gets_small_height_in_draw(self):
        app = make_app(self)
        app.toggle_panel("limits")     # «нет данных»: 3 строки
        app.toggle_panel("backside")   # журнал: занимает остаток
        app.draw(100, 30, 1000.0)
        self.assertLess(app._panel_rects["limits"][3],
                        app._panel_rects["backside"][3])

    def test_x_closes_focused_panel(self):
        app = make_app(self)
        app.toggle_panel("limits")
        app.panel_focus = True
        app.handle_key(key("x", "x"))
        self.assertEqual(app.layout.leaves(), [])
        self.assertFalse(app.panel_focus)

    def test_mouse_header_focus_and_wheel(self):
        app = make_app(self)
        app.toggle_panel("moon")
        app.toggle_panel("backside")
        app.draw(100, 30, 1000.0)
        px, py, pw, _ph = app._panel_rects["backside"]
        app.handle_mouse(Mouse("press", px + 2, py + 1, button=0))  # заголовок
        self.assertEqual(app.layout.focused, "backside")
        self.assertTrue(app.panel_focus)
        content = app._panel_content("backside")
        app.handle_mouse(Mouse("wheel_up", px + 2, py + 3, button=0))
        self.assertEqual(content.scroll, 1)


class SessionsCleanupTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data = DataDir(Path(self._tmp.name) / "home")

    def test_empty_detection_and_drop(self):
        empty = make_session(self.data, "/w")
        full = make_session(self.data, "/w", text="привет")
        self.assertTrue(session_is_empty(load_records(empty)))
        self.assertFalse(session_is_empty(load_records(full)))
        dropped = drop_empty_sessions(self.data)
        self.assertEqual(dropped, [empty])
        self.assertFalse(empty.exists())
        self.assertTrue(full.exists())
        self.assertTrue((self.data.sessions / ".trash" / empty.name).exists())

    def test_drop_keeps_current(self):
        empty = make_session(self.data, "/w")
        dropped = drop_empty_sessions(self.data, keep_path=empty)
        self.assertEqual(dropped, [])
        self.assertTrue(empty.exists())

    def test_trash_and_undo_with_checkpoints(self):
        path = make_session(self.data, "/w", text="привет")
        sid = list_sessions(self.data)[0]["id"]
        cp = Path(self.data.root) / "checkpoints" / sid
        (cp / "objects").mkdir(parents=True)
        (cp / "index.json").write_text("{}", encoding="utf-8")
        moved = trash_session(self.data, path)
        self.assertIsNotNone(moved)
        self.assertFalse(path.exists())
        self.assertFalse(cp.exists())  # чекпоинты ушли вместе с сессией
        restored = undo_trash(self.data)
        self.assertEqual(restored, path)
        self.assertTrue(path.exists())
        self.assertTrue((cp / "index.json").exists())

    def test_purge_trash_by_age(self):
        path = make_session(self.data, "/w", text="привет")
        moved = trash_session(self.data, path)
        old = time.time() - 10 * 86400
        os.utime(moved, (old, old))
        self.assertEqual(purge_trash(self.data, 7), 1)
        self.assertFalse(moved.exists())
        path2 = make_session(self.data, "/w", text="ещё")
        moved2 = trash_session(self.data, path2)
        self.assertEqual(purge_trash(self.data, 7), 0)  # свежая лежит
        self.assertTrue(moved2.exists())

    def test_settings_defaults(self):
        settings = Settings(home=Path(self._tmp.name) / "h",
                            cwd=Path(self._tmp.name))
        self.assertTrue(settings.get("ui.layout_fix"))
        self.assertTrue(settings.get("chat.sessions_drop_empty"))
        self.assertEqual(settings.get("chat.sessions_trash_days"), 7)


class SessionsPickerTest(unittest.TestCase):
    def setUp(self):
        self.app = make_app(self)
        data = self.app._data_dir()
        self.current = make_session(data, self.app.cwd, text="текущая")
        self.app.journal = SessionJournal.resume(data, self.current)[0]
        # Закрывать журнал приложения на момент уборки, а не исходный
        # объект: ЗАДАЧА-19 умеет менять журнал (удаление текущей сессии
        # создаёт новый), а Windows не удаляет открытый файл (WinError 32).
        self.addCleanup(lambda: self.app.journal
                        and self.app.journal.close())
        self.other = make_session(data, self.app.cwd, text="другая",
                                  title="Другая сессия")
        self.third = make_session(data, self.app.cwd, text="третья")

    def _select(self, path):
        for item in self.app.picker.items:
            if str(item.payload["path"]) == str(path):
                self.app.picker._sel_key = item.key
                return item
        raise AssertionError("нет пункта " + str(path))

    def test_delete_one_with_confirm(self):
        app = self.app
        app.open_sessions_picker()
        self._select(self.other)
        app.handle_key(key("d", "d"))
        self.assertEqual(app.picker.mode, "confirm")
        app.handle_key(key("enter"))
        self.assertFalse(self.other.exists())
        trash = app._data_dir().sessions / ".trash"
        self.assertTrue((trash / self.other.name).exists())
        app.handle_key(key("u", "u"))  # отмена удаления
        self.assertTrue(self.other.exists())

    def test_delete_current_forbidden(self):
        # ЗАДАЧА-19 отменила запрет ЗАДАЧИ-17: текущую удалять можно —
        # подтверждение, корзина и сразу новая пустая сессия.
        app = self.app
        app.open_sessions_picker()
        self._select(self.current)
        app.handle_key(key("d", "d"))
        self.assertEqual(app.picker.mode, "confirm")
        app.handle_key(key("enter"))
        self.assertFalse(self.current.exists())
        trash = app._data_dir().sessions / ".trash"
        self.assertTrue((trash / self.current.name).exists())
        self.assertIsNotNone(app.journal)
        self.assertNotEqual(str(app.journal.path), str(self.current))

    def test_mark_and_delete_marked(self):
        app = self.app
        app.open_sessions_picker()
        self._select(self.other)
        app.handle_key(key(" ", " "))
        self._select(self.third)
        app.handle_key(key(" ", " "))
        self.assertEqual(len(app._sessions_marks), 2)
        app.handle_key(key("D", "D"))
        self.assertEqual(app.picker.mode, "confirm")
        self.assertIn("2", app.picker._confirm_prompt)
        app.handle_key(key("enter"))
        self.assertFalse(self.other.exists())
        self.assertFalse(self.third.exists())
        self.assertTrue(self.current.exists())

    def test_rename_in_place(self):
        app = self.app
        app.open_sessions_picker()
        self._select(self.other)
        app.handle_key(key("r", "r"))
        self.assertEqual(app.picker.mode, "input")
        for ch in "Z":
            app.handle_key(key(ch, ch))
        app.handle_key(key("enter"))
        titles = {s["id"]: s["title"] for s in list_sessions(app._data_dir())}
        target = [s for s in list_sessions(app._data_dir())
                  if str(s["path"]) == str(self.other)][0]
        self.assertTrue(titles[target["id"]].endswith("Z"))


if __name__ == "__main__":
    unittest.main()
