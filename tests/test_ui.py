"""Рендерер: ширина, цвет, фазы, клавиши, дифф буфера, возможности терминала."""

from __future__ import annotations

import unicodedata
import unittest
from unittest import mock

from luna.ui import color, moon, term, width
from luna.ui.keys import Key, KeyParser, Mouse, Paste
from luna.ui.screen import Buffer, Screen
from luna.ui.term import detect_caps


class WidthTest(unittest.TestCase):
    def test_widths(self):
        self.assertEqual(width.char_width("a"), 1)
        self.assertEqual(width.char_width("你"), 2)
        self.assertEqual(width.char_width("🌕"), 2)
        self.assertEqual(width.char_width("́"), 0)  # комбинирующийся знак
        self.assertEqual(width.str_width("a你🌕b"), 6)
        self.assertEqual(width.str_width("кот"), 3)

    def test_known_glyph_widths(self):
        # Свои глифы зафиксированы таблицей, а не общими диапазонами:
        # фазы луны — 2, брайлевский спиннер — 1 (урок РЕВЬЮ-01, п. 1).
        for glyph in moon.EMOJI_PHASES:
            self.assertEqual(width.char_width(glyph), 2, glyph)
        self.assertEqual(width.char_width("🌙"), 2)
        for glyph in "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏":
            self.assertEqual(width.char_width(glyph), 1, glyph)
        self.assertEqual(width.str_width("⠸ Медитирует"), 12)

    def test_text_symbols_are_narrow(self):
        # ЗАДАЧА-06, п. 1: дингбаты и символы без эмодзи-представления
        # терминал рисует в одну ячейку. Раньше диапазоны 2600–26FF и
        # 2700–27BF целиком считались широкими — в Windows Terminal после
        # ⚙, ✓ и ✦ оставались хвосты прошлого кадра («√b Код выхода: 0»).
        for glyph in "⚙✓✗✦☑★☾✔✚⬅⬆":
            self.assertEqual(width.char_width(glyph), 1, glyph)
        # А у этих эмодзи-представление есть — честные две ячейки.
        for glyph in "⚠✨☀❤➕⬛⬜⭐⭕✈✌❌❗":
            self.assertEqual(width.char_width(glyph), 2, glyph)

    def test_interface_glyph_table(self):
        # Весь набор глифов интерфейса — с осознанной шириной из таблицы.
        for glyph, expected in width.INTERFACE_GLYPHS.items():
            self.assertEqual(width.char_width(glyph), expected, glyph)

    def test_all_frame_glyphs_covered(self):
        # Полнота: любой не-ASCII символ, который интерфейс реально рисует
        # (кадры всех экранов, справка, фразы), обязан быть в таблице ширин.
        from luna.devtools import snapshot
        from luna.ui import phrases
        from luna.ui.chat import HELP_TEXT

        texts = [HELP_TEXT] + phrases.spinner_phrases() + phrases.splash_phrases()
        for name in ("splash", "render", "main", "chat"):
            for _label, buf in snapshot.render_frames(name, 100, 28, [0.0, 0.6, 1.2]):
                texts.extend("".join(cell[0] for cell in row) for row in buf.cells)
        seen = set()
        for text in texts:
            for ch in text:
                code = ord(ch)
                if code < 128 or 0x400 <= code <= 0x4FF:
                    continue  # ASCII и кириллица — шириной 1 по построению
                if 0x3400 <= code <= 0x9FFF or unicodedata.combining(ch):
                    continue  # CJK — east_asian_width W, ноль у комбинирующихся
                seen.add(ch)
        for ch in sorted(seen):
            self.assertIn(ch, width.INTERFACE_GLYPHS,
                          "глиф интерфейса без осознанной ширины: " + repr(ch))


class ColorTest(unittest.TestCase):
    def test_gradient_endpoints(self):
        stops = ((0, 0, 0), (255, 255, 255))
        g = color.gradient(stops, 3)
        self.assertEqual(g[0], (0, 0, 0))
        self.assertEqual(g[-1], (255, 255, 255))
        self.assertEqual(g[1], (128, 128, 128))

    def test_gradient_multi_stop(self):
        g = color.gradient(color.NEON_STOPS, 5)
        self.assertEqual(g[0], color.CYAN)
        self.assertEqual(g[2], color.VIOLET)
        self.assertEqual(g[-1], color.MAGENTA)

    def test_quantize_and_degrade(self):
        self.assertEqual(color.quantize256((255, 0, 0)), 196)
        self.assertEqual(color.quantize256((0, 0, 0)), 16)
        self.assertEqual(color.quantize256((255, 255, 255)), 231)
        self.assertIn(color.degrade((10, 200, 90), "16"), color.PALETTE16)
        self.assertEqual(color.degrade((10, 200, 90), "mono"), color.SILVER)
        rgb = (12, 130, 250)
        self.assertEqual(color.degrade(rgb, "256"), color.rgb_of_256(color.quantize256(rgb)))
        self.assertEqual(color.degrade(rgb, "truecolor"), rgb)

    def test_sgr_levels(self):
        self.assertIn("38;2;1;2;3", color.sgr((1, 2, 3), None, 0, "truecolor"))
        self.assertIn("38;5;", color.sgr((1, 2, 3), None, 0, "256"))
        self.assertNotIn("38", color.sgr((1, 2, 3), None, 0, "mono"))
        self.assertIn("1", color.sgr(None, None, color.BOLD, "mono"))


class MoonTest(unittest.TestCase):
    def test_phases(self):
        self.assertEqual(moon.phase_emoji(0), "🌑")
        self.assertEqual(moon.phase_emoji(50), "🌕")
        self.assertEqual(moon.phase_emoji(100), "🌘")
        self.assertEqual(moon.phase_emoji(-5), "🌑")     # зажато в диапазон
        self.assertEqual(moon.phase_ascii(50), "[####]")
        self.assertEqual(moon.context_phase(0), "🌑")    # пустой контекст
        self.assertEqual(moon.context_phase(100), "🌕")  # полный контекст
        self.assertEqual(len(set(moon.EMOJI_PHASES)), 8)

    def test_narrow_phases_are_really_narrow(self):
        # Узкий набор — замена эмодзи там, где терминал их ширину считает
        # по-своему (ConPTY): каждый глиф обязан быть шириной ровно 1.
        self.assertEqual(moon.phase_narrow(0), "○")
        self.assertEqual(moon.phase_narrow(50), "●")
        self.assertEqual(moon.phase(50, "narrow"), "●")
        self.assertEqual(moon.context_phase(0, "narrow"), "○")
        self.assertEqual(moon.context_phase(100, "narrow"), "●")
        for glyph in moon.NARROW_PHASES:
            self.assertNotIn(unicodedata.east_asian_width(glyph), ("W", "F"), glyph)
            self.assertEqual(width.char_width(glyph), 1, glyph)
        joined = "".join(moon.NARROW_PHASES)
        self.assertEqual(width.str_width(joined), len(moon.NARROW_PHASES))
        # Старые bool-вызовы живут: True — эмодзи, False — ASCII.
        self.assertEqual(moon.phase(50, True), "🌕")
        self.assertEqual(moon.phase(50, False), "[####]")


class KeysTest(unittest.TestCase):
    def parse(self, data: bytes):
        return KeyParser().feed(data)

    def test_plain_and_utf8(self):
        ev = self.parse("ю".encode("utf-8"))
        self.assertEqual(ev, [Key("ю", char="ю")])

    def test_arrows_and_mods(self):
        self.assertEqual(self.parse(b"\x1b[A"), [Key("up")])
        self.assertEqual(self.parse(b"\x1b[1;5C"), [Key("right", mods=frozenset({"ctrl"}))])
        self.assertEqual(self.parse(b"\x1bOH"), [Key("home")])
        self.assertEqual(self.parse(b"\x1b[3~"), [Key("delete")])
        self.assertEqual(self.parse(b"\x1b[Z"), [Key("tab", mods=frozenset({"shift"}))])

    def test_ctrl_and_esc(self):
        self.assertEqual(self.parse(b"\x03"), [Key("c", mods=frozenset({"ctrl"}))])
        self.assertEqual(self.parse(b"\r"), [Key("enter")])
        self.assertEqual(self.parse(b"\x7f"), [Key("backspace")])
        p = KeyParser()
        self.assertEqual(p.feed(b"\x1b"), [])            # ждём: вдруг последовательность
        self.assertEqual(p.flush(), [Key("esc")])        # тайм-аут — это Esc

    def test_split_sequence(self):
        p = KeyParser()
        self.assertEqual(p.feed(b"\x1b["), [])
        self.assertEqual(p.feed(b"A"), [Key("up")])

    def test_alt(self):
        self.assertEqual(self.parse(b"\x1bx"), [Key("x", char="x", mods=frozenset({"alt"}))])

    def test_mouse_sgr(self):
        ev = self.parse(b"\x1b[<0;10;5M")
        self.assertEqual(ev, [Mouse("press", 10, 5, button=0)])
        ev = self.parse(b"\x1b[<0;10;5m")
        self.assertEqual(ev[0].kind, "release")
        ev = self.parse(b"\x1b[<64;3;2M")
        self.assertEqual(ev[0].kind, "wheel_up")
        ev = self.parse(b"\x1b[<65;3;2M")
        self.assertEqual(ev[0].kind, "wheel_down")

    def test_paste(self):
        ev = self.parse("\x1b[200~привет\x1b[201~".encode("utf-8"))
        self.assertEqual(ev, [Paste("привет")])
        p = KeyParser()
        self.assertEqual(p.feed("\x1b[200~по частям".encode("utf-8")), [])
        ev = p.feed(" тут\x1b[201~".encode("utf-8"))
        self.assertEqual(ev, [Paste("по частям тут")])


class ScreenTest(unittest.TestCase):
    def make(self, cols=8, rows=2):
        frames = []
        return Screen(frames.append, "truecolor", cols, rows), frames

    def test_only_changes_written(self):
        screen, frames = self.make()
        buf = Buffer(8, 2)
        buf.text(0, 0, "привет")
        first = screen.present(buf)
        self.assertIn("привет", first)
        buf.set(0, 0, "П")
        second = screen.present(buf)
        self.assertIn("П", second)
        self.assertNotIn("ривет", second)  # неизменное не пересылается
        third = screen.present(buf)
        self.assertEqual(third, "")        # кадр без изменений — пустой

    def test_wide_char_span(self):
        screen, _ = self.make()
        buf = Buffer(8, 2)
        buf.text(0, 0, "a🌕b")
        frame = screen.present(buf)
        self.assertIn("🌕", frame)
        buf.set(2, 0, "x")  # затираем правую половину широкого символа
        frame = screen.present(buf)
        self.assertIn("x", frame)
        self.assertNotIn("🌕", frame)  # половинки луны не бывает: символ стёрт целиком
        buf.set(1, 0, "🌕")  # а целиком — перерисовывается одним куском
        frame = screen.present(buf)
        self.assertIn("🌕", frame)

    def test_anchor_after_wide_char(self):
        # После широкого символа следующий глиф предваряется абсолютной
        # координатой: чужая модель ширины (ConPTY) не накапливает сдвиг.
        screen, _ = self.make()
        buf = Buffer(8, 2)
        buf.text(0, 0, "a🌕b")  # луна на ячейках 1-2, b — на 3
        frame = screen.present(buf)
        self.assertIn("🌕\x1b[1;4Hb", frame)

    def test_update_at_col0_starts_with_absolute(self):
        # Ревью просило: после строки с эмодзи обновление в колонке 0 той же
        # и соседней строки начинается с абсолютной координаты.
        screen, _ = self.make()
        buf = Buffer(10, 2)
        buf.text(0, 0, "🌕 кот")
        buf.text(0, 1, "строка")
        screen.present(buf)
        buf.set(0, 0, "🌑")
        buf.set(0, 1, "С")
        frame = screen.present(buf)
        self.assertTrue(frame.startswith("\x1b[1;1H"), repr(frame))
        self.assertIn("\x1b[2;1H", frame)
        self.assertIn("🌑", frame)
        self.assertIn("С", frame)

    def test_single_write_per_frame(self):
        screen, frames = self.make()
        buf = Buffer(8, 2)
        buf.text(0, 0, "раз")
        screen.present(buf)
        self.assertEqual(len(frames), 1)

    def test_resize_forces_full_redraw(self):
        screen, _ = self.make()
        buf = Buffer(8, 2)
        buf.text(0, 0, "раз")
        screen.present(buf)
        screen.resize(8, 2)
        frame = screen.present(buf)
        self.assertIn("раз", frame)


class _FakeOut:
    def __init__(self, encoding="utf-8"):
        self.encoding = encoding

    def isatty(self):
        return True


class _Stub:
    """Заглушка настроек для detect_caps: только .get по словарю."""

    def __init__(self, values):
        self.values = values

    def get(self, key, default=None):
        return self.values.get(key, default)


class CapsTest(unittest.TestCase):
    def detect(self, env, settings=None, encoding="utf-8"):
        return detect_caps(env=env, out=_FakeOut(encoding), settings=settings)

    def test_levels(self):
        self.assertEqual(self.detect({"COLORTERM": "truecolor", "TERM": "xterm"}).color_level,
                         "truecolor")
        self.assertEqual(self.detect({"TERM": "xterm-256color"}).color_level, "256")
        self.assertEqual(self.detect({"TERM": "xterm", "NO_COLOR": "1"}).color_level, "mono")
        self.assertEqual(self.detect({"TERM": "dumb"}).color_level, "mono")
        self.assertEqual(self.detect({"TERM": "xterm", "WT_SESSION": "x"}).color_level,
                         "truecolor")

    def test_unicode(self):
        self.assertTrue(self.detect({"TERM": "xterm-256color"}).unicode)
        self.assertFalse(self.detect({"TERM": "linux"}).unicode)

    def test_linux_rule_beats_windows_codepage(self):
        # Сценарий РЕВЬЮ-01, п. 2, смоделированный на любой платформе:
        # Windows, кодовая страница 65001, но TERM=linux — юникода нет,
        # потому что правило про linux применяется последним.
        with mock.patch.object(term, "IS_WINDOWS", True), \
                mock.patch.object(term, "_windows_cp", lambda: 65001):
            self.assertFalse(self.detect({"TERM": "linux"}, encoding="cp1251").unicode)
            # А без TERM=linux та же кодовая страница юникод разрешает:
            self.assertTrue(self.detect({"TERM": "xterm"}, encoding="cp1251").unicode)

    def test_settings_override(self):
        caps = self.detect({"TERM": "xterm-256color"},
                           settings=_Stub({"ui.color": "16", "ui.unicode": "off"}))
        self.assertEqual(caps.color_level, "16")
        self.assertFalse(caps.unicode)

    def test_emoji_auto_windows_conpty_vs_terminal(self):
        # auto на Windows: без WT_SESSION — ConPTY-хост, эмодзи выключены
        # и интерфейс уходит в узкие фазы; в Windows Terminal — включены.
        with mock.patch.object(term, "IS_WINDOWS", True):
            caps = self.detect({"TERM": "xterm-256color"})
            self.assertTrue(caps.unicode)
            self.assertFalse(caps.emoji)
            self.assertEqual(caps.glyph_mode, "narrow")
            self.assertIn("ConPTY", caps.emoji_reason)
            caps = self.detect({"TERM": "xterm-256color", "WT_SESSION": "x"})
            self.assertTrue(caps.emoji)
            self.assertEqual(caps.glyph_mode, "emoji")

    def test_emoji_auto_posix(self):
        with mock.patch.object(term, "IS_WINDOWS", False):
            caps = self.detect({"TERM": "xterm-256color"})
            self.assertTrue(caps.emoji)
            self.assertEqual(caps.glyph_mode, "emoji")

    def test_emoji_setting_overrides_auto(self):
        # Настройка перекрывает автоопределение в обе стороны.
        with mock.patch.object(term, "IS_WINDOWS", True):
            caps = self.detect({"TERM": "xterm-256color"},
                               settings=_Stub({"ui.emoji": "on"}))
            self.assertTrue(caps.emoji)
            self.assertEqual(caps.emoji_reason, "настройка ui.emoji")
        with mock.patch.object(term, "IS_WINDOWS", False):
            caps = self.detect({"TERM": "xterm-256color"},
                               settings=_Stub({"ui.emoji": "off"}))
            self.assertFalse(caps.emoji)
            self.assertEqual(caps.glyph_mode, "narrow")
        # ui.unicode=off сильнее ui.emoji=on: режим — ASCII.
        caps = self.detect({"TERM": "xterm-256color"},
                           settings=_Stub({"ui.emoji": "on", "ui.unicode": "off"}))
        self.assertFalse(caps.emoji)
        self.assertEqual(caps.glyph_mode, "ascii")


if __name__ == "__main__":
    unittest.main()
