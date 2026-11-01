"""ЗАДАЧА-19: мышление и effort, бесплатный поиск, удаление текущей сессии.

Всё на подменах: роутер — на stub-HTTP, пересказчик — на фальшивом
роутере и ручных часах, поиск — на ctx.web_fetcher / ctx.mcp_caller
(живая сеть в тестах не дёргается), чат — на фальшивом агенте.
"""

import json
import tempfile
import unittest
from pathlib import Path

from luna.core.bus import EventBus
from luna.core.errors import HumanError
from luna.core.paths import DataDir
from luna.core.secrets import Secrets
from luna.core.sessions import SessionJournal
from luna.core.settings import Settings
from luna.mind.thinking import ThinkingSummarizer
from luna.router import effort
from luna.router.core import Router
from luna.router.types import ChatResult, Usage
from luna.tools.registry import ToolContext
from luna.tools.web import web_search_tool
from luna.ui.anim import Scheduler
from luna.ui.chat import ChatApp
from luna.ui.keys import Key
from luna.ui.panels.builtin import PanelData


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


def feed_text(app, now=None) -> str:
    lines = app._feed_lines(100, now if now is not None else app._clock())
    return "\n".join("".join(t for t, _f, _a in spans) for spans, _bg in lines)


# --- усилие: перевод шкалы ----------------------------------------------------

class EffortTranslateTest(unittest.TestCase):
    def fields(self, kind, provider, model, level):
        return effort.body_fields(kind, provider, model, level)

    def test_openai_families(self):
        fields, note = self.fields("openai", "openai", "gpt-5.4", "xhigh")
        self.assertEqual(fields, {"reasoning_effort": "xhigh"})
        self.assertIsNone(note)
        # GPT-5 первого поколения: xhigh нет — ближайшее high, с нотой.
        fields, note = self.fields("openai", "openai", "gpt-5", "xhigh")
        self.assertEqual(fields, {"reasoning_effort": "high"})
        self.assertIsNotNone(note)
        # o3: только low/medium/high; max → high.
        fields, note = self.fields("openai", "openai", "o3", "max")
        self.assertEqual(fields, {"reasoning_effort": "high"})
        # GPT-5.6: minimal убрано из шкалы провайдера → low.
        fields, note = self.fields("openai", "openai", "gpt-5.6-sol", "minimal")
        self.assertEqual(fields, {"reasoning_effort": "low"})

    def test_openai_non_reasoning_model(self):
        fields, note = self.fields("openai", "ollama", "llama3.1", "high")
        self.assertEqual(fields, {})  # локальному нерассуждающему не шлём
        self.assertIsNotNone(note)

    def test_openrouter_object(self):
        fields, _ = self.fields("openai", "openrouter", "any/model", "medium")
        self.assertEqual(fields, {"reasoning": {"effort": "medium"}})
        fields, _ = self.fields("openai", "openrouter", "any/model", "none")
        self.assertEqual(fields, {"reasoning": {"enabled": False}})

    def test_deepseek_mapping(self):
        fields, _ = self.fields("openai", "deepseek", "deepseek-v4", "medium")
        self.assertEqual(fields, {"reasoning_effort": "high"})
        fields, _ = self.fields("openai", "deepseek", "deepseek-v4", "max")
        self.assertEqual(fields, {"reasoning_effort": "max"})
        fields, _ = self.fields("openai", "deepseek", "deepseek-v4", "none")
        self.assertEqual(fields, {"thinking": {"type": "disabled"}})

    def test_anthropic_adaptive_vs_budget(self):
        fields, _ = self.fields("anthropic", "anthropic",
                                "claude-opus-4-8", "xhigh")
        self.assertEqual(fields["thinking"], {"type": "adaptive"})
        self.assertEqual(fields["output_config"], {"effort": "xhigh"})
        # Старый бюджет новым моделям не шлём ни в каком виде.
        self.assertNotIn("budget_tokens", json.dumps(fields))
        fields, _ = self.fields("anthropic", "anthropic",
                                "claude-haiku-4-5", "high")
        self.assertEqual(fields["thinking"],
                         {"type": "enabled", "budget_tokens": 8192})
        fields, _ = self.fields("anthropic", "anthropic",
                                "claude-sonnet-4-6", "low")
        self.assertEqual(fields["thinking"], {"type": "adaptive"})
        # Fable: мышление не выключается — минимум с нотой.
        fields, note = self.fields("anthropic", "anthropic",
                                   "claude-fable-5", "none")
        self.assertEqual(fields["output_config"], {"effort": "low"})
        self.assertIsNotNone(note)

    def test_anthropic_budget_raises_max_tokens(self):
        from luna.router.adapters import AnthropicAdapter

        fields, _ = self.fields("anthropic", "anthropic",
                                "claude-haiku-4-5", "max")
        body = AnthropicAdapter("https://api.anthropic.com").body(
            "claude-haiku-4-5", [{"role": "user", "content": "привет"}],
            None, False, extra=fields)
        self.assertGreater(body["max_tokens"], 32000)

    def test_gemini_levels_and_budgets(self):
        fields, _ = self.fields("gemini", "gemini", "gemini-3-flash", "high")
        self.assertEqual(fields["generationConfig"]["thinkingConfig"],
                         {"thinkingLevel": "HIGH"})
        # Pro: лестница только low/high; medium при равенстве расстояний —
        # ступень выше (усилие — «не меньше этого»): high.
        fields, note = self.fields("gemini", "gemini", "gemini-3-pro", "medium")
        self.assertEqual(fields["generationConfig"]["thinkingConfig"],
                         {"thinkingLevel": "HIGH"})
        self.assertIsNotNone(note)
        # 2.5: бюджеты числами; Flash выключается нулём, Pro — нет.
        fields, _ = self.fields("gemini", "gemini", "gemini-2.5-flash", "none")
        self.assertEqual(fields["generationConfig"]["thinkingConfig"],
                         {"thinkingBudget": 0})
        fields, note = self.fields("gemini", "gemini", "gemini-2.5-pro", "none")
        self.assertEqual(fields["generationConfig"]["thinkingConfig"],
                         {"thinkingBudget": 128})
        self.assertIsNotNone(note)
        fields, _ = self.fields("gemini", "gemini", "gemini-2.5-flash", "xhigh")
        self.assertEqual(fields["generationConfig"]["thinkingConfig"],
                         {"thinkingBudget": 24576})

    def test_auto_sends_nothing(self):
        fields, note = self.fields("openai", "openai", "gpt-5.4", "")
        self.assertEqual(fields, {})
        self.assertIsNone(note)


class EffortRouterTest(unittest.TestCase):
    """Роутер кладёт перевод в тело запроса и ноту — в результат."""

    class StubHttp:
        def __init__(self):
            self.bodies = []

        def stream_lines(self, url, body, headers):
            self.bodies.append(body)
            return iter([])

        def post_json(self, url, body, headers):
            self.bodies.append(body)
            return {"choices": [{"message": {"content": "ок"}}]}

    def make_router(self, tmp, **settings_values):
        settings = Settings(home=Path(tmp) / "home", cwd=Path(tmp))
        settings.set("model.provider", "openai")
        settings.set("model.main", "openai:gpt-5.4")
        for name, value in settings_values.items():
            settings.set(name, value)
        http = self.StubHttp()
        return Router(settings, Secrets(Path(tmp) / "secrets.json"), http=http), http

    def test_effort_in_body_and_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            router, http = self.make_router(tmp, **{"model.effort": "max"})
            result = router.chat([{"role": "user", "content": "привет"}], None)
            # gpt-5.4 не умеет max — ближайшее xhigh, и нота в результате.
            self.assertEqual(http.bodies[0]["reasoning_effort"], "xhigh")
            self.assertEqual(len(result.notes), 1)
            self.assertIn("xhigh", result.notes[0])

    def test_role_override_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            router, http = self.make_router(
                tmp, **{"model.effort": "low", "model.effort_fast": "high"})
            router.chat([{"role": "user", "content": "привет"}], None,
                        role="fast")
            self.assertEqual(http.bodies[0]["reasoning_effort"], "high")

    def test_auto_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            router, http = self.make_router(tmp)
            result = router.chat([{"role": "user", "content": "привет"}], None)
            self.assertNotIn("reasoning_effort", http.bodies[0])
            self.assertEqual(result.notes, [])


# --- пересказ рассуждений -------------------------------------------------------

class FakeSummaryRouter:
    def __init__(self, text="Обдумывает план проверки сборки", fail=None):
        self.text = text
        self.fail = fail
        self.calls = []

    def chat(self, messages, tools=None, *, role="", ref="", on_event=None):
        self.calls.append((messages, role))
        if self.fail is not None:
            raise self.fail
        return ChatResult(text=self.text, usage=Usage(10, 5), model="fake:fast")


class ThinkingSummaryTest(unittest.TestCase):
    def make_summarizer(self, tmp, router):
        settings = Settings(home=Path(tmp) / "home", cwd=Path(tmp))
        settings.set("ui.thinking", "summary")
        settings.set("ui.thinking_summary_s", 10)
        self.now = [1000.0]
        events = []
        summarizer = ThinkingSummarizer(
            settings=settings, router=router, publish=events.append,
            clock=lambda: self.now[0])
        return summarizer, events

    def test_summary_by_timer_and_flush(self):
        with tempfile.TemporaryDirectory() as tmp:
            router = FakeSummaryRouter()
            summarizer, events = self.make_summarizer(tmp, router)
            summarizer.feed("думаю про сборку ")
            self.assertEqual(events, [])  # таймер ещё не вышел
            self.now[0] += 11.0
            summarizer.feed("и про тесты")
            summarizer.wait_idle()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["type"], "thinking.summary")
            self.assertEqual(events[0]["text"],
                             "Обдумывает план проверки сборки")
            self.assertEqual(router.calls[0][1], "fast")
            # Конец размышления: остаток уходит финальным пересказом.
            summarizer.feed("последняя мысль")
            summarizer.flush()
            summarizer.wait_idle()
            self.assertEqual(len(events), 2)

    def test_fast_not_configured_disables_quietly(self):
        with tempfile.TemporaryDirectory() as tmp:
            router = FakeSummaryRouter(
                fail=HumanError("model_not_set", "Модель не настроена", ""))
            summarizer, events = self.make_summarizer(tmp, router)
            summarizer.feed("мысль")
            self.now[0] += 30.0
            summarizer.feed("ещё мысль")
            summarizer.wait_idle()
            self.assertEqual(events, [])
            self.assertFalse(summarizer.enabled())  # дальше не пробует

    def test_mode_off_no_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            router = FakeSummaryRouter()
            summarizer, events = self.make_summarizer(tmp, router)
            summarizer.settings.set("ui.thinking", "full")
            summarizer.feed("мысль")
            self.now[0] += 60.0
            summarizer.feed("мысль")
            summarizer.flush()
            summarizer.wait_idle()
            self.assertEqual(router.calls, [])
            self.assertEqual(events, [])


# --- поиск: цепочка на подменах -------------------------------------------------

EXA_TEXT = ("Title: Документация Python\nURL: https://docs.python.org/3/\n"
            "Published: 2026-01-01\nHighlights:\nОфициальная документация\n\n"
            "Title: Real Python\nURL: https://realpython.com/\nУроки и статьи")


def mcp_result(text):
    return {"content": [{"type": "text", "text": text}], "isError": False}


class SearchChainTest(unittest.TestCase):
    def make_ctx(self, tmp, *, fetcher=None, caller=None, secrets=None,
                 **settings_values):
        settings = Settings(home=Path(tmp) / "home", cwd=Path(tmp))
        for name, value in settings_values.items():
            settings.set(name, value)
        ctx = ToolContext(cwd=Path(tmp), settings=settings, secrets=secrets,
                          web_fetcher=fetcher)
        ctx.mcp_caller = caller
        return ctx

    def run_search(self, ctx, query="python"):
        return web_search_tool().run(ctx, {"query": query})

    def test_exa_keyless_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            seen = []

            def caller(url, headers, tool, args):
                seen.append((url, tool))
                return mcp_result(EXA_TEXT)

            res = self.run_search(self.make_ctx(tmp, caller=caller))
            self.assertTrue(res.ok)
            self.assertEqual(seen[0][0], "https://mcp.exa.ai/mcp")
            self.assertEqual(seen[0][1], "web_search_exa")
            self.assertIn("источник: Exa (MCP)", res.text)
            self.assertIn("https://docs.python.org/3/", res.text)
            self.assertIn("Документация Python", res.text)

    def test_exa_down_parallel_next(self):
        with tempfile.TemporaryDirectory() as tmp:
            def caller(url, headers, tool, args):
                if "exa" in url:
                    raise RuntimeError("сеть недоступна")
                return {"structuredContent": {"results": [
                    {"title": "Параллельный результат",
                     "url": "https://p.example",
                     "excerpts": ["выдержка из параллельного поиска"]}],
                }, "content": [], "isError": False}

            res = self.run_search(self.make_ctx(tmp, caller=caller))
            self.assertTrue(res.ok)
            self.assertIn("источник: Parallel Search (MCP)", res.text)
            self.assertIn("https://p.example", res.text)
            self.assertIn("отвалились", res.text)  # честно про упавшую Exa

    def test_all_down_honest_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            def caller(url, headers, tool, args):
                raise RuntimeError("таймаут")

            ctx = self.make_ctx(tmp, caller=caller,
                                fetcher=lambda url: "<html>пусто</html>")
            res = self.run_search(ctx)
            self.assertFalse(res.ok)
            self.assertIn("Exa (MCP)", res.text)
            self.assertIn("SearXNG", res.text)

    def test_web_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self.make_ctx(tmp, **{"web.enabled": False})
            res = self.run_search(ctx)
            self.assertFalse(res.ok)
            self.assertIn("web.enabled", res.text)

    def test_brave_with_key_via_fetcher(self):
        with tempfile.TemporaryDirectory() as tmp:
            secrets = Secrets(Path(tmp) / "secrets.json")
            secrets.set("search:brave", "ключ-из-сейфа")
            payload = json.dumps({"web": {"results": [
                {"title": "Brave итог", "url": "https://b.example",
                 "description": "описание"}]}})
            ctx = self.make_ctx(
                tmp, secrets=secrets, fetcher=lambda url: payload,
                **{"web.search_providers": "brave"})
            res = self.run_search(ctx)
            self.assertTrue(res.ok)
            self.assertIn("источник: Brave Search", res.text)
            self.assertIn("https://b.example", res.text)
            self.assertNotIn("ключ-из-сейфа", res.text)  # ключ не в выводе

    def test_keyless_brave_skipped_not_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self.make_ctx(tmp, **{"web.search_providers": "brave"})
            res = self.run_search(ctx)
            self.assertFalse(res.ok)
            self.assertIn("нет ключа", res.text)

    def test_native_appended_only_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self.make_ctx(tmp, fetcher=lambda url: "<html></html>",
                                **{"web.search_providers": "duckduckgo",
                                   "web.native_search": True})
            res = self.run_search(ctx)
            self.assertFalse(res.ok)
            self.assertIn("native", res.text)  # в пропусках: нет агента


# --- чат: показ мышления, /effort, удаление сессии ------------------------------

class ChatThinkingTest(unittest.TestCase):
    def test_thinking_cycle_and_modes(self):
        app = make_app(self)
        app.on_agent_event({"type": "agent.thinking", "text": "секретная мысль"})
        app.on_agent_event({"type": "agent.text.done"})
        # Умолчание summary: полный текст свёрнут, шапка видна.
        text = feed_text(app)
        self.assertIn("думала", text)
        self.assertNotIn("секретная мысль", text)
        # Пересказ крепится строкой под шапку.
        app.on_agent_event({"type": "thinking.summary",
                            "text": "Пересказ одной строкой", "seconds": 3})
        self.assertIn("Пересказ одной строкой", feed_text(app))
        # По кругу: summary → hidden → full.
        app.submit_text("/thinking")
        self.assertEqual(app.settings.get("ui.thinking"), "hidden")
        self.assertFalse(app.settings.get("ui.show_thinking"))
        self.assertNotIn("думала", feed_text(app))
        app.submit_text("/thinking")
        self.assertEqual(app.settings.get("ui.thinking"), "full")
        self.assertIn("секретная мысль", feed_text(app))

    def test_thinking_explicit_arg(self):
        app = make_app(self)
        app.submit_text("/thinking hidden")
        self.assertEqual(app._thinking_mode(), "hidden")
        app.submit_text("/thinking summary")
        self.assertEqual(app._thinking_mode(), "summary")
        # Alt+O — тот же круг на лету: summary → hidden.
        app.handle_key(key("o", "o", mods=frozenset({"alt"})))
        self.assertEqual(app._thinking_mode(), "hidden")

    def test_effort_command_and_key(self):
        app = make_app(self)
        app.submit_text("/effort high")
        self.assertEqual(app.settings.get("model.effort"), "high")
        app.submit_text("/effort fast low")
        self.assertEqual(app.settings.get("model.effort_fast"), "low")
        app.submit_text("/effort")  # выбиралка общего уровня
        self.assertIsNotNone(app.picker)
        self.assertEqual(app._picker_kind, "effort")
        app.handle_key(key("esc", ""))
        app.handle_key(key("e", "e", mods=frozenset({"alt"})))  # Alt+E по кругу
        self.assertEqual(app.settings.get("model.effort"), "xhigh")

    def test_effort_note_lands_in_backside(self):
        data = PanelData()
        data.observe({"type": "effort.note", "model": "openai:gpt-5",
                      "text": "уровень max не поддерживается, взято high"})
        self.assertTrue(any(item["kind"] == "effort"
                            for item in data.backside))


class DeleteCurrentSessionTest(unittest.TestCase):
    def setUp(self):
        self.app = make_app(self)
        data = self.app._data_dir()
        self.data = data
        journal = SessionJournal.create(data, self.app.cwd)
        journal.append_command({"type": "user.message", "text": "текущая"})
        self.current_path = journal.path
        self.app.journal = journal
        # Журнал на момент уборки может быть уже другим (новая сессия):
        # закрываем актуальный, иначе Windows не удалит открытый файл.
        self.addCleanup(lambda: self.app.journal
                        and self.app.journal.close())

    def test_delete_command_double_confirm(self):
        app = self.app
        app.submit_text("/delete")
        self.assertTrue(self.current_path.exists())  # сначала спрашивает
        app.submit_text("/delete")
        self.assertFalse(self.current_path.exists())
        trash = self.data.sessions / ".trash"
        self.assertTrue((trash / self.current_path.name).exists())
        self.assertIsNotNone(app.journal)
        self.assertNotEqual(str(app.journal.path), str(self.current_path))

    def test_undo_in_picker_returns_and_switches_back(self):
        app = self.app
        app.submit_text("/delete")
        app.submit_text("/delete")
        self.assertFalse(self.current_path.exists())
        app.open_sessions_picker()
        app.handle_key(key("u", "u"))
        self.assertTrue(self.current_path.exists())
        self.assertIsNotNone(app.journal)
        self.assertEqual(str(app.journal.path), str(self.current_path))

    def test_russian_alias(self):
        app = self.app
        app.submit_text("/удалить")
        app.submit_text("/удалить")
        self.assertFalse(self.current_path.exists())


class LunaToolThemesWordsTest(unittest.TestCase):
    """ЗАДАЧА-19 §4: «сделай темнее» словами — описание тула ведёт модель."""

    def test_description_hints_words(self):
        from luna.tools.luna_tool import luna_tool

        description = luna_tool().description
        self.assertIn("темнее", description)
        self.assertIn("theme_create", description)
        self.assertIn("model.effort", description)
        self.assertIn("settings", description)


if __name__ == "__main__":
    unittest.main()
