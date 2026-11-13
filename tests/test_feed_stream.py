"""Лента под стримом: скорость, скролл на месте, клик по мыслям."""

import unittest

from luna.ui.chat import Entry, _incremental
from luna.ui.keys import Mouse
from luna.ui.markdown import render_markdown, wrap_text

from tests.test_zadacha17 import make_app

PARA = "\n\n"


class IncrementalTest(unittest.TestCase):
    def test_kak_tselikom(self):
        text = PARA.join("Абзац номер " + str(i) + " " + "слово " * 40 for i in range(40))
        text += PARA + "```\nкод\n\nвнутри\n```" + PARA + "хвост"
        entry = Entry("agent", text=text, streaming=True)

        def md(s):
            return [[(sp.text, sp.style) for sp in ln] for ln in render_markdown(s, 70)]

        self.assertEqual(_incremental(entry, "md", text, 70, md, None), md(text))
        entry2 = Entry("thinking", text=text, streaming=True)
        self.assertEqual(_incremental(entry2, "wt", text, 70, lambda s: wrap_text(s, 70), ""),
                         wrap_text(text, 70))


class ScrollHoldTest(unittest.TestCase):
    def test_lenta_ne_edet(self):
        app = make_app(self)
        for i in range(60):
            app.on_agent_event({"type": "notice", "text": "строка " + str(i)})
        app.draw(80, 24, 0.0)
        app.scroll = 10
        app.draw(80, 24, 0.0)
        top = app._feed_first
        for i in range(5):
            app.on_agent_event({"type": "notice", "text": "новое " + str(i)})
        app.draw(80, 24, 0.0)
        self.assertEqual(app._feed_first, top)
        self.assertGreater(app.scroll, 10)


class ThinkingClickTest(unittest.TestCase):
    def test_klik_po_telu_myslej(self):
        app = make_app(self, **{"ui.thinking": "full", "ui.drag_scroll": False})
        app.on_agent_event({"type": "agent.thinking", "text": "мысль " * 30})
        app.on_agent_event({"type": "turn.done"})
        app.draw(80, 24, 0.0)
        zone = next(z for z in app.zones if z[4][0] == "soft_block")
        entry = zone[4][1]
        before = app._exp(entry)
        x, y = zone[0] + 3, zone[1] + 1
        app.handle_mouse(Mouse("press", x, y, 0))
        app.handle_mouse(Mouse("release", x, y, 0))
        self.assertNotEqual(app._exp(entry), before)



class TideTest(unittest.TestCase):
    def test_volna_i_razdelitel(self):
        app = make_app(self)
        app.on_agent_event({"type": "context.compacting", "before_pct": 62.0})
        self.assertTrue(app.entries[-1].streaming)
        self.assertTrue(app.needs_frame(app._clock()))
        app.on_agent_event({"type": "context.compacted", "before_pct": 62.0,
                            "after_pct": 18.5, "summarized_turns": [1, 2]})
        app.on_agent_event({"type": "notice", "text": "Прилив: контекст сжат — было ~62%"})
        tide = [e for e in app.entries if e.kind == "tide"]
        self.assertEqual(len(tide), 1)
        self.assertEqual(tide[0].text, "62% → 18.5%")
        self.assertFalse(any(e.kind == "notice" for e in app.entries))

    def test_neudacha_ubiraet_volnu(self):
        app = make_app(self)
        app.on_agent_event({"type": "context.compacting"})
        app.on_agent_event({"type": "context.compact_end"})
        self.assertFalse(any(e.kind == "tide" for e in app.entries))


if __name__ == "__main__":
    unittest.main()
