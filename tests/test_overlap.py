"""Каждая часть кадра — в своей зоне: аудит наложений на всех экранах."""

from luna.devtools.overlap import audit
from luna.ui import screen as _screen
from luna.ui.screen import Buffer


def test_klip_otrezaet_i_zapisyvaet():
    buf = Buffer(20, 3)
    _screen.AUDIT = []
    try:
        with buf.clip(0, 0, 5, 1, "зона"):
            buf.text(0, 0, "длинный текст")
            buf.text(0, 1, "чужая строка")
    finally:
        hits, _screen.AUDIT = _screen.AUDIT, None
    assert "".join(buf.cell(x, 0)[0] for x in range(6)) == "длинн "
    assert buf.cell(0, 1)[0] == " "
    assert {h[0] for h in hits} == {"зона"}


def test_net_nalozheniy_na_ekranah():
    found = audit(sizes=((40, 14), (60, 20), (100, 30)))
    assert found == [], "\n".join(map(str, found[:20]))


def test_vse_frazy_zastavki_vlezayut():
    # Декоративные фразы случайны — проверяем каждую, а не одну наугад.
    from luna.ui import phrases
    from luna.ui.splash import render_frame

    bad = []
    for cols, rows in ((40, 14), (60, 20)):
        for phrase in phrases.splash_phrases():
            buf = Buffer(cols, rows)
            _screen.AUDIT = []
            try:
                render_frame(buf, 1.0, glyph_mode="emoji", phrase=phrase,
                             startup_ms=None, stars=[], now=0.0)
            finally:
                hits, _screen.AUDIT = _screen.AUDIT, None
            if hits:
                bad.append((cols, phrase, hits[:2]))
    assert bad == [], bad[:5]
