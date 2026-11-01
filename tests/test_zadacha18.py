"""ЗАДАЧА-18: прерывание сразу (стрим, bash, ожидание), режимы отправки,
панели (фокус, граница), темы как данные, швы /btw и /mcp."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from luna.core.paths import DataDir
from luna.core.secrets import Secrets
from luna.core.settings import Settings
from luna.devtools.fake_llm import FakeModelClient
from luna.mind.loop import Agent
from luna.mind.source import AgentSource
from luna.net.http_client import HttpClient
from luna.router.core import ProviderConfig, Router
from luna.router.types import ChatResult, Usage
from luna.tools.registry import ToolContext
from luna.tools.shell import bash_tool
from luna.ui import color, themes
from luna.ui.chat import ChatApp, Entry
from luna.ui.keys import Key, KeyParser, Mouse
from luna.ui.markdown import render_markdown
from luna.ui.panels import LayoutTree
from luna.ui.width import str_width


class StubSource:
    def __init__(self):
        self.commands = []

    def start(self):
        pass

    def handle_command(self, cmd):
        self.commands.append(cmd)

    def poll(self, now=None):
        return []

    def has_pending(self):
        return False


def make_app(**kwargs):
    return ChatApp(source=StubSource(), **kwargs)


def key(name, mods=()):
    return Key(name, mods=frozenset(mods))


class TempHomeBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="luna-z18-"))
        self.work = self.tmp / "work"
        self.work.mkdir()
        self._old_home = os.environ.get("LUNA_HOME")
        os.environ["LUNA_HOME"] = str(self.tmp / "home")
        self.settings = Settings(home=self.tmp / "home", cwd=self.work)
        self.secrets = Secrets(self.tmp / "home" / "secrets.json")
        self.events: list = []

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("LUNA_HOME", None)
        else:
            os.environ["LUNA_HOME"] = self._old_home


class ScriptedRouter:
    def __init__(self, script):
        self.client = FakeModelClient(script=list(script))

    def chat(self, messages, tools=None, *, role="main", ref="", on_event=None):
        return self.client.chat("script", messages, tools,
                                on_event or (lambda k, t: None))


class BlockingRouter:
    """Первый вызов ждёт освобождения — ход «идёт», пока тест не отпустит."""

    def __init__(self, texts):
        self.texts = list(texts)
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls: list = []

    def chat(self, messages, tools=None, *, role="main", ref="", on_event=None):
        self.calls.append([dict(m) for m in messages])
        idx = len(self.calls) - 1
        if idx == 0:
            self.entered.set()
            self.release.wait(timeout=10)
        text = self.texts[idx] if idx < len(self.texts) else "готово"
        return ChatResult(model="fake", text=text, usage=Usage(1, 1))


# --- часть 1: прерывание -------------------------------------------------------


class _EternalHandler(BaseHTTPRequestHandler):
    """Стрим, который начался и никогда не кончается сам."""

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        chunk = {"choices": [{"delta": {"content": "частичный ответ "}}]}
        self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode("utf-8"))
        self.wfile.flush()
        time.sleep(30)  # вечность: оборвать должен клиент

    def log_message(self, *args):
        pass


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False


class EternalStreamInterruptTest(TempHomeBase):
    @classmethod
    def setUpClass(cls):
        cls.server = _Server(("127.0.0.1", 0), _EternalHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:" + str(cls.server.server_address[1]) + "/v1"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_interrupt_tears_stream_fast(self):
        router = Router(self.settings, self.secrets, http=HttpClient(timeout=30),
                        providers={"fake": ProviderConfig(
                            name="fake", kind="openai", base_url=self.base)})
        agent = Agent(settings=self.settings, secrets=self.secrets, router=router,
                      cwd=self.work, model_ref="fake:echo")
        agent.listen(self.events.append)
        outcome: list = []
        worker = threading.Thread(
            target=lambda: outcome.append(agent.run_turn("привет")), daemon=True)
        worker.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if any(e.get("type") == "agent.text" for e in self.events):
                break
            time.sleep(0.02)
        self.assertTrue(any(e.get("type") == "agent.text" for e in self.events),
                        "стрим должен был начаться")
        started = time.monotonic()
        agent.interrupt()
        worker.join(timeout=3)
        elapsed = time.monotonic() - started
        self.assertFalse(worker.is_alive(), "ход не оборвался")
        self.assertLess(elapsed, 1.0, "прерывание стрима дольше секунды")
        self.assertEqual(outcome, ["interrupted"])
        last = agent._history[-1]
        self.assertEqual(last["role"], "assistant")
        self.assertIn("частичный ответ", last["content"])
        self.assertIn("прерван", last["content"])


@unittest.skipUnless(os.name == "posix", "дерево процессов здесь — POSIX")
class BashInterruptTest(TempHomeBase):
    def test_cancel_event_kills_whole_tree(self):
        pidfile = self.tmp / "child.pid"
        cancel = threading.Event()
        ctx = ToolContext(cwd=self.work, settings=None, cancel_event=cancel)
        cmd = "sleep 60 & echo $! > " + str(pidfile) + "; wait"
        outcome: list = []
        worker = threading.Thread(
            target=lambda: outcome.append(bash_tool().run(ctx, {"command": cmd})),
            daemon=True)
        worker.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not pidfile.exists():
            time.sleep(0.02)
        self.assertTrue(pidfile.exists(), "дочерний процесс не стартовал")
        child_pid = int(pidfile.read_text().strip())
        started = time.monotonic()
        cancel.set()
        worker.join(timeout=3)
        elapsed = time.monotonic() - started
        self.assertFalse(worker.is_alive(), "bash не оборвался")
        self.assertLess(elapsed, 1.0, "прерывание bash дольше секунды")
        self.assertFalse(outcome[0].ok)
        self.assertIn("прервана", outcome[0].text)
        # Дитя (sleep) убито вместе с деревом, а не пережило команду.
        deadline = time.monotonic() + 2
        alive = True
        while time.monotonic() < deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                alive = False
                break
            time.sleep(0.05)
        self.assertFalse(alive, "дочерний sleep пережил прерывание")

    def test_timeout_still_works(self):
        ctx = ToolContext(cwd=self.work, settings=None)
        result = bash_tool().run(ctx, {"command": "sleep 5", "timeout": 1})
        self.assertFalse(result.ok)
        self.assertIn("не уложилась", result.text)


class GuardWaitInterruptTest(TempHomeBase):
    def test_interrupt_releases_guard_wait(self):
        router = ScriptedRouter([
            {"tool_calls": [{"name": "bash", "arguments": {"command": "echo hi"},
                             "intent": "проверить"}]},
            {"text": "не должно дойти"},
        ])
        # Настоящая проводка: агент ждёт ответ через ожидания источника,
        # а interrupt источника их отпускает с решением stop.
        source = AgentSource(settings=self.settings, cwd=str(self.work))
        agent = Agent(settings=self.settings, secrets=self.secrets, router=router,
                      cwd=self.work, ask_guard_fn=source._ask_guard,
                      ask_user_fn=source._ask_user)
        source._agent = agent
        source.start()
        source.handle_command({"type": "user.message", "text": "сделай"})
        seen: list = []
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            seen.extend(source.poll())
            if any(e.get("type") == "guard.ask" for e in seen):
                break
            time.sleep(0.02)
        self.assertTrue(any(e.get("type") == "guard.ask" for e in seen))
        started = time.monotonic()
        source.handle_command({"type": "interrupt"})
        while time.monotonic() - started < 3:
            seen.extend(source.poll())
            if any(e.get("type") == "turn.done" for e in seen):
                break
            time.sleep(0.02)
        done = [e for e in seen if e.get("type") == "turn.done"]
        self.assertTrue(done, "ход не завершился после прерывания ожидания")
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(done[-1].get("reason"), "interrupted")


class ChatInterruptKeysTest(unittest.TestCase):
    def test_esc_single_press_interrupts(self):
        app = make_app()
        app.on_agent_event({"type": "turn.start", "turn": 1})
        app.handle_key(key("esc"))
        kinds = [c["type"] for c in app.sent_commands]
        self.assertIn("interrupt", kinds)
        self.assertFalse(app.done)

    def test_ctrl_c_during_turn_interrupts_not_exits(self):
        app = make_app()
        app.on_agent_event({"type": "turn.start", "turn": 1})
        app.handle_key(key("c", ("ctrl",)))
        self.assertFalse(app.done)
        kinds = [c["type"] for c in app.sent_commands]
        self.assertIn("interrupt", kinds)

    def test_ctrl_c_clears_input_then_double_exits(self):
        app = make_app()
        now = [100.0]
        app._clock = lambda: now[0]
        app.editor.set_text("черновик")
        app.handle_key(key("c", ("ctrl",)))
        self.assertEqual(app.editor.text, "")
        self.assertFalse(app.done)
        app.handle_key(key("c", ("ctrl",)))   # первый на пустом — подсказка
        self.assertFalse(app.done)
        self.assertIsNotNone(app._hint)
        now[0] += 0.5
        app.handle_key(key("c", ("ctrl",)))   # второй подряд — выход
        self.assertTrue(app.done)

    def test_ctrl_c_second_after_pause_is_not_exit(self):
        app = make_app()
        now = [100.0]
        app._clock = lambda: now[0]
        app.handle_key(key("c", ("ctrl",)))
        now[0] += 2.0                          # пауза дольше секунды
        app.handle_key(key("c", ("ctrl",)))
        self.assertFalse(app.done)

    def test_eof_exits_immediately(self):
        app = make_app()
        app.handle_key(key("eof"))
        self.assertTrue(app.done)

    def test_term_ctrl_c_is_a_key_event(self):
        # POSIX: байт 0x03 при выключенном ISIG — клавиша ctrl+c.
        events = KeyParser().feed(b"\x03")
        labels = [e.label for e in events if isinstance(e, Key)]
        self.assertIn("ctrl+c", labels)
        # Windows: маска сырого режима гасит PROCESSED (0x0001) — Ctrl+C
        # не станет сигналом, а придёт событием клавиши.
        from luna.ui import term

        self.assertEqual((0xFFFF & term._DISABLE_LINE_ECHO_PROCESSED) & 0x0001, 0)


# --- часть 2: режимы отправки ---------------------------------------------------


class SendModesChatTest(unittest.TestCase):
    def busy_app(self):
        app = make_app()
        # Файл настроек у прогона общий: режим ставим явно, не надеясь
        # на чужую чистоту, и возвращаем inject после себя.
        app.settings.set("chat.send_mode", "inject")
        self.addCleanup(app.settings.set, "chat.send_mode", "inject")
        app.submit_text("первое")
        app.on_agent_event({"type": "turn.start", "turn": 1})
        return app

    def test_inject_is_default_and_sends_immediately(self):
        app = self.busy_app()
        app.submit_text("вставка")
        sent = [c for c in app.sent_commands if c["type"] == "user.message"]
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[-1].get("delivery"), "inject")
        self.assertEqual(app.queue, [])

    def test_interrupt_mode_interrupts_then_sends(self):
        app = self.busy_app()
        app.settings.set("chat.send_mode", "interrupt")
        app.submit_text("стоп, лучше так")
        kinds = [c["type"] for c in app.sent_commands]
        self.assertIn("interrupt", kinds)
        app.on_agent_event({"type": "turn.done", "turn": 1, "reason": "interrupted"})
        sent = [c for c in app.sent_commands if c["type"] == "user.message"]
        self.assertEqual(sent[-1]["text"], "стоп, лучше так")
        self.assertEqual(sent[-1].get("delivery"), "interrupt")

    def test_alt_enter_interrupts_and_sends_once(self):
        app = self.busy_app()
        app.editor.set_text("разово")
        app.handle_key(key("enter", ("alt",)))
        kinds = [c["type"] for c in app.sent_commands]
        self.assertIn("interrupt", kinds)
        app.on_agent_event({"type": "turn.done", "turn": 1, "reason": "interrupted"})
        sent = [c for c in app.sent_commands if c["type"] == "user.message"]
        self.assertEqual(sent[-1]["text"], "разово")

    def test_ctrl_s_cycles_modes(self):
        app = make_app()
        app.settings.set("chat.send_mode", "inject")
        self.addCleanup(app.settings.set, "chat.send_mode", "inject")
        self.assertEqual(app._send_mode(), "inject")
        app.handle_key(key("s", ("ctrl",)))
        self.assertEqual(app._send_mode(), "queue")
        app.handle_key(key("s", ("ctrl",)))
        self.assertEqual(app._send_mode(), "interrupt")
        app.handle_key(key("s", ("ctrl",)))
        self.assertEqual(app._send_mode(), "inject")

    def test_queue_visible_and_recallable(self):
        app = self.busy_app()
        app.settings.set("chat.send_mode", "queue")
        app.submit_text("подождёт")
        self.assertEqual(app.queue, ["подождёт"])
        meta = app._meta_lines(time.monotonic())
        self.assertTrue(any("подождёт" in line for line in meta))
        self.assertTrue(any("Ctrl+S" in line for line in meta))
        app.handle_key(key("up"))          # пустое поле + очередь — отозвать
        self.assertEqual(app.queue, [])
        self.assertEqual(app.editor.text, "подождёт")


class SendModesAgentTest(TempHomeBase):
    def test_inject_goes_into_current_turn(self):
        router = BlockingRouter(["шаг один", "шаг два"])
        agent = Agent(settings=self.settings, secrets=self.secrets,
                      router=router, cwd=self.work)
        outcome: list = []
        worker = threading.Thread(
            target=lambda: outcome.append(agent.run_turn("старт")), daemon=True)
        worker.start()
        self.assertTrue(router.entered.wait(5))
        agent.handle_command({"type": "user.message", "text": "вставка",
                              "delivery": "inject"})
        router.release.set()
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(outcome, ["end"])
        self.assertEqual(agent._turn, 1)               # ход — один
        self.assertEqual(len(router.calls), 2)         # вызова модели — два
        second = router.calls[1]
        self.assertTrue(any(m.get("role") == "user" and m.get("content") == "вставка"
                            for m in second))

    def test_queue_delivery_runs_as_next_turn(self):
        router = BlockingRouter(["первый", "второй"])
        agent = Agent(settings=self.settings, secrets=self.secrets,
                      router=router, cwd=self.work)
        source = AgentSource(settings=self.settings, cwd=str(self.work), agent=agent)
        source.start()
        source.handle_command({"type": "user.message", "text": "раз"})
        self.assertTrue(router.entered.wait(5))
        source.handle_command({"type": "user.message", "text": "два",
                               "delivery": "queue"})
        router.release.set()
        seen: list = []
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            seen.extend(source.poll())
            dones = [e for e in seen if e.get("type") == "turn.done"]
            if len(dones) >= 2:
                break
            time.sleep(0.02)
        dones = [e for e in seen if e.get("type") == "turn.done"]
        self.assertEqual(len(dones), 2)                # два отдельных хода
        self.assertEqual(agent._turn, 2)
        self.assertEqual(len(router.calls), 2)


# --- часть 3: панели и лента ------------------------------------------------------


class PanelsFocusTest(unittest.TestCase):
    def test_f6_toggles_focus_off(self):
        app = make_app()
        app.toggle_panel("limits")
        app.handle_key(key("f6"))
        self.assertTrue(app.panel_focus)
        app.handle_key(key("f6"))
        self.assertFalse(app.panel_focus)

    def test_mouse_press_on_feed_drops_focus(self):
        app = make_app()
        app.toggle_panel("limits")
        app.draw(90, 24, 1000.0)
        app.panel_focus = True
        app.handle_mouse(Mouse("press", 3, 3, button=0))
        self.assertFalse(app.panel_focus)

    def test_shift_arrows_resize_column(self):
        app = make_app()
        app.toggle_panel("limits")
        app.draw(90, 24, 1000.0)
        app.panel_focus = True
        app.handle_key(key("right", ("shift",)))
        self.assertEqual(app.layout.column_w, 28)      # 90//3 - 2
        app.handle_key(key("left", ("shift",)))
        self.assertEqual(app.layout.column_w, 30)

    def test_column_width_saved_in_layout(self):
        tree = LayoutTree()
        tree.toggle("limits")
        tree.column_w = 42
        tree.save_layout("wide")
        tree.column_w = None
        self.assertTrue(tree.load_layout("wide"))
        self.assertEqual(tree.column_w, 42)

    def test_column_drag_by_mouse(self):
        app = make_app()
        app.toggle_panel("limits")
        app.draw(90, 24, 1000.0)
        chat_w = app._feed_geom[0]
        self.assertEqual(chat_w, 60)
        app.handle_mouse(Mouse("press", chat_w + 1, 3, button=0))
        self.assertTrue(app._col_drag)
        app.handle_mouse(Mouse("move", chat_w - 9, 3, button=0))
        self.assertEqual(app.layout.column_w, 40)
        app.handle_mouse(Mouse("release", chat_w - 9, 3, button=0))
        self.assertFalse(app._col_drag)
        # Ширина применена: следующий кадр — колонка уже, лента шире.
        app.draw(90, 24, 1000.5)
        self.assertEqual(app._feed_geom[0], 50)


class FeedWidthTest(unittest.TestCase):
    def lines_width(self, app, entry, width):
        lines = app._entry_lines(entry, width, 1000.0)
        return [sum(str_width(t) for t, _f, _a in spans) for spans, _bg in lines]

    def test_tool_head_and_help_stay_in_width(self):
        app = make_app()
        tool = Entry("tool", name="bash", entry_id="t1", status="done", ok=True,
                     args={"command": "echo " + "очень длинная команда " * 10},
                     summary="готово")
        self.assertTrue(all(w <= 30 for w in self.lines_width(app, tool, 30)))
        help_entry = Entry("help", text="справка " + "слово " * 30)
        self.assertTrue(all(w <= 30 for w in self.lines_width(app, help_entry, 30)))

    def test_code_and_tables_clipped_not_wrapped(self):
        long_code = "```python\nx = " + "a + " * 30 + "b\n```"
        lines = render_markdown(long_code, 24)
        self.assertEqual(len(lines), 1)                # код не переносится
        width = sum(str_width(s.text) for s in lines[0])
        self.assertLessEqual(width, 24)
        self.assertTrue(lines[0][-1].text.endswith("…"))
        table = "| колонка | значение |\n| --- | --- |\n| " + "x" * 40 + " | 1 |"
        tlines = render_markdown(table, 24)
        self.assertEqual(len(tlines), 3)               # строки таблицы — по одной
        for line in tlines:
            self.assertLessEqual(sum(str_width(s.text) for s in line), 24)


# --- часть 4: темы ------------------------------------------------------------------


class ThemesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="luna-themes-"))
        self.addCleanup(lambda: themes.apply_theme("luna"))

    def test_amoled_is_pure_black(self):
        palette, applied = themes.load_palette("amoled")
        self.assertEqual(applied, "amoled")
        self.assertEqual(palette["space_bg"], (0, 0, 0))
        self.assertEqual(palette["cyan"], themes.load_palette("luna")[0]["cyan"])

    def test_unknown_theme_falls_back_to_luna(self):
        _palette, applied = themes.load_palette("нет-такой")
        self.assertEqual(applied, "luna")

    def test_apply_theme_recolors_modules(self):
        themes.apply_theme("amoled")
        self.assertEqual(color.SPACE_BG, (0, 0, 0))
        self.assertEqual(color.PALETTE["muted"], (0x6E, 0x6E, 0x6E))
        from luna.ui import chat as chat_mod

        self.assertEqual(chat_mod.PANEL_BG, color.PALETTE["panel_bg"])
        self.assertEqual(chat_mod.DIM_FG, color.PALETTE["muted"])

    def test_user_theme_file_roundtrip(self):
        path = themes.save_theme(self.tmp, "mrak", {"space_bg": "#010203"},
                                 title="Мрак")
        self.assertTrue(path.exists())
        names = {name for name, _t, own in themes.list_themes(self.tmp) if own}
        self.assertIn("mrak", names)
        palette, applied = themes.load_palette("mrak", self.tmp)
        self.assertEqual(applied, "mrak")
        self.assertEqual(palette["space_bg"], (1, 2, 3))
        # Недостающий ключ — наследован от luna.
        self.assertEqual(palette["cyan"], themes.load_palette("luna")[0]["cyan"])

    def test_save_theme_rejects_builtin_and_bad_colors(self):
        with self.assertRaises(ValueError):
            themes.save_theme(self.tmp, "luna", {"space_bg": "#000000"})
        with self.assertRaises(ValueError):
            themes.save_theme(self.tmp, "ok-name", {"space_bg": "не-цвет"})

    def test_chat_draws_in_amoled(self):
        settings = Settings(home=self.tmp / "home", cwd=self.tmp)
        settings.set("ui.theme", "amoled")
        app = make_app(settings=settings)
        buf, _cursor = app.draw(60, 16, 1000.0)
        self.assertEqual(buf.cells[0][0][2], (0, 0, 0))

    def test_luna_tool_theme_create_and_use(self):
        from luna.tools.luna_tool import is_changing, luna_tool

        settings = Settings(home=self.tmp / "home", cwd=self.tmp)
        ctx = ToolContext(cwd=self.tmp, settings=settings)
        tool = luna_tool()
        self.assertTrue(is_changing({"action": "theme_create"}))
        result = tool.run(ctx, {"action": "theme_create", "name": "mrak",
                                "colors": {"space_bg": "#000000"}, "use": True})
        self.assertTrue(result.ok, result.text)
        self.assertEqual(settings.get("ui.theme"), "mrak")
        self.assertTrue((self.tmp / "home" / "themes" / "mrak.json").exists())
        listing = tool.run(ctx, {"action": "themes"})
        self.assertIn("amoled", listing.text)


# --- швы: /btw и /mcp -----------------------------------------------------------------


class BtwTest(TempHomeBase):
    def test_btw_answers_without_touching_history(self):
        router = ScriptedRouter([{"text": "основной ответ"},
                                 {"text": "ответ в сторону"}])
        agent = Agent(settings=self.settings, secrets=self.secrets,
                      router=router, cwd=self.work)
        agent.listen(self.events.append)
        agent.run_turn("привет")
        history_before = [dict(m) for m in agent._history]
        agent.btw("а почему?")
        answers = [e for e in self.events if e.get("type") == "btw.answer"]
        self.assertEqual(len(answers), 1)
        self.assertEqual(answers[0]["text"], "ответ в сторону")
        self.assertEqual(agent._history, history_before)
        self.assertEqual(agent._turn, 1)

    def test_chat_btw_sends_command(self):
        app = make_app()
        app._btw_command("что там?")
        self.assertIn({"type": "btw", "text": "что там?"}, app.sent_commands)
        self.assertFalse(any(e.kind == "notice" and "не умеет" in e.text
                             for e in app.entries))


class McpToggleReloadTest(unittest.TestCase):
    def test_toggle_reloads_registry(self):
        from luna.tools import registry

        tmp = Path(tempfile.mkdtemp(prefix="luna-mcp-"))
        data = DataDir(tmp / "home")
        data.ensure()
        (data.root / "mcp.json").write_text(json.dumps(
            {"mcpServers": {"srv": {"command": "echo", "enabled": True}}}),
            encoding="utf-8")
        app = make_app(data=data)
        item = next(i for i in app._mcp_items() if i.key == "srv")
        before = registry._MCP_GENERATION
        app._mcp_toggle(item)
        self.assertGreater(registry._MCP_GENERATION, before)
        saved = json.loads((data.root / "mcp.json").read_text(encoding="utf-8"))
        self.assertFalse(saved["mcpServers"]["srv"]["enabled"])
        self.assertTrue(any(e.kind == "notice" and "перезапустится" in e.text
                            for e in app.entries))


if __name__ == "__main__":
    unittest.main()
