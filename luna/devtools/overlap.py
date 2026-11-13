"""Аудит наложений: `luna try overlap [--json]`.

Рисует все экраны снимков (devtools.snapshot) на сетке размеров терминала
и собирает нарушения зон из screen.AUDIT: текст, вылезший за свою
клип-зону или за край экрана. Пусто — значит каждая часть кадра сидит
в своём прямоугольнике, а оверлеи закрывают своё целиком.
"""

from __future__ import annotations

import json
import sys

from ..ui import screen as _screen

# От нетбука и узкого телефонного терминала до широкого монитора.
SIZES = ((40, 14), (60, 20), (80, 24), (100, 30), (132, 40), (200, 50))
TIMES = [0.0, 0.6, 1.5, 3.0]
# Витрина рендерера — стенд разработчика с фиксированной вёрсткой:
# на малом экране нижние секции просто не рисуются, ни на что не наползая.
SKIP = ("render",)


def audit(sizes=SIZES, screens=None) -> list:
    """Список нарушений: (экран, кадр, размер, зона, x, y, кусок текста)."""
    from .snapshot import SCREENS, render_frames

    found = []
    for name in screens or [n for n in SCREENS if n not in SKIP]:
        for cols, rows in sizes:
            _screen.AUDIT = []
            try:
                frames = render_frames(name, cols, rows, TIMES)
            except Exception as exc:  # noqa: BLE001 — падение экрана тоже находка
                found.append((name, "-", f"{cols}x{rows}", "ПАДЕНИЕ", 0, 0,
                              type(exc).__name__ + ": " + str(exc)[:80]))
                continue
            finally:
                hits, _screen.AUDIT = _screen.AUDIT, None
            label = frames[0][0] if frames else "-"
            seen = set()
            for zone, x, y, text in hits or []:
                key = (zone, y, text)
                if key in seen:
                    continue  # один и тот же вылез на разных кадрах — одна находка
                seen.add(key)
                found.append((name, label, f"{cols}x{rows}", zone, x, y, text))
    return found


def run_overlap(argv: list) -> int:
    found = audit()
    if "--json" in argv:
        print(json.dumps(found, ensure_ascii=False, indent=1))
        return 1 if found else 0
    if not found:
        print("Наложений нет: все части кадра в своих зонах на " +
              str(len(SIZES)) + " размерах.")
        return 0
    out = sys.stdout
    out.write("Найдено вылезаний: " + str(len(found)) + "\n")
    for name, label, size, zone, x, y, text in found:
        out.write(f"  {name:13} {size:7} зона «{zone}» ({x},{y}): {text}\n")
    return 1
