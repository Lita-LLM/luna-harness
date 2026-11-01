"""Полигон Звездопада: `luna try starfall [N]` (ЗАДАЧА-15).

Фальшивая модель, без сети. Поднимает настоящего агента на fake:echo,
ставит цены (иначе деньги не считаются), поднимает потолки под N —
и гонит рой профилем cls через настоящий тул starfall: смета, прогон,
сводка. В конце — время и пик памяти процесса (resource, ru_maxrss).
По умолчанию N=1000; 10000 обязано пройти без роста памяти.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from ..core.stdio import ensure_utf8_stdio
from .rewind_try import _make_agent


def _rss_mb() -> float:
    """Пик RSS процесса, МБ; на Windows resource нет — честный 0 (н/д)."""
    try:
        import resource

        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    except ImportError:
        return 0.0


def run_starfall_try(argv=None) -> int:
    ensure_utf8_stdio()
    argv = list(argv or [])
    total = 1000
    if argv:
        try:
            total = max(1, int(argv[0]))
        except ValueError:
            print("N — целое число ячеек, например: luna try starfall 10000")
            return 2
    root = Path(tempfile.mkdtemp(prefix="luna-starfall-try-"))
    owner, _work, settings = _make_agent(root)
    settings.set("model.main", "fake:echo")
    settings.set("model.fast", "fake:echo")
    # Цены — чтобы деньги и смета были видны (фальшивая модель иначе бесплатна).
    settings.set("model.price_in", 100.0)
    settings.set("model.price_out", 200.0)
    # Потолки витрины: под N ячеек и их честную цену; кошелёк сессии — без лимита.
    settings.set("starfall.max_cells", max(100, total))
    settings.set("starfall.budget_usd", max(1.0, total * 0.005))
    settings.set("agents.session_budget_usd", 0.0)
    settings.set("starfall.output_dir", str(root / "starfall"))

    from ..starfall.tools import _starfall

    ctx = owner.ctx
    inputs = ["отзыв номер " + str(i) + ": всё сломалось, верните деньги"
              for i in range(total)]
    args = {"profile": "cls", "inputs": inputs,
            "template": "Разметь тональность отзыва: {input}",
            "parallel": 8, "link_size": 8, "mail": "tree"}
    print("== Звездопад на фальшивой модели: " + str(total) + " ячеек ==")
    rss_before = _rss_mb()
    started = time.time()
    est = _starfall(ctx, dict(args, action="estimate"))
    print("Смета: " + est.text)
    out = _starfall(ctx, dict(args, action="run",
                              estimate="смета из luna try starfall"))
    seconds = time.time() - started
    print(out.text)
    print()
    rss_now = _rss_mb()
    if rss_now:
        print("Время всего: " + format(seconds, ".1f") + " с; пик памяти процесса: " +
              format(rss_now, ".1f") + " МБ (до прогона " + format(rss_before, ".1f") +
              " МБ, рост " + format(rss_now - rss_before, ".1f") + " МБ)")
    else:
        print("Время всего: " + format(seconds, ".1f") +
              " с; пик памяти на этой платформе не меряется")
    print("Результаты потока: " + str(root / "starfall"))
    return 0 if out.ok else 1
