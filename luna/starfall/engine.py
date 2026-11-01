"""Движок Звездопада (ЗАДАЧА-15, docs/РОЙ.md §7): очередь + пул исполнителей.

Не поток на ячейку: задания лежат компактными параллельными списками,
исполнители (starfall.max_parallel потоков) разбирают очередь. Результат
каждой ячейки пишется на диск сразу по готовности (results/<i>.json),
поэтому рой переживает сбой Луны: resume сканирует папку потока и
докручивает недостающие. В памяти не живут ни результаты, ни логи —
только счётчики и статусы, оттого 10 000 ячеек не раздувают процесс.

Ячейка исполняется runner-ом: по умолчанию RouterCellRunner — короткий
цикл роутера матери с тулами профиля. Перед каждым вызовом тула —
luna.agents.host.cell_guard_decision: вне профиля — отказ, а не вопрос;
необратимое из Предохранителя матери ячейке тоже нельзя (спросить ей
некого). Режимы записи: none (только чтение), sandbox (своя папка
ячейки), worktree (git worktree на звено), project (проект, только с
явным подтверждением человека при запуске).
"""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from pathlib import Path

from ..core.fsutil import atomic_write_text
from ..core.ids import new_id
from ..tools.registry import WRITE_TOOLS, ToolContext
from .addressing import address as cell_address
from .addressing import host_address, link_address
from .modelpool import ModelPool, is_rate_limited
from .profiles import CellProfile

DENIED_PREFIX = "НЕ ВЫПОЛНЕНО"
# Служебные тулы матери ячейке не положены ни в каком профиле.
FORBIDDEN_TOOLS = ("assistant", "crew", "starfall", "cells", "luna", "skill", "manner")
ROLES = ("main", "fast", "guard", "vision")
_PROGRESS_EVERY = 25


class SwarmError(Exception):
    """Рой нельзя запустить или продолжить: потолки, профили, папка."""


def usage_cost(settings, tokens_in: int, tokens_out: int) -> float:
    """Цена по настройкам model.price_in/out (как у цикла агента)."""
    if settings is None:
        return 0.0
    price_in = float(settings.get("model.price_in", 0.0) or 0.0)
    price_out = float(settings.get("model.price_out", 0.0) or 0.0)
    return (tokens_in * price_in + tokens_out * price_out) / 1_000_000.0


def render_task(template: str, input_text: str) -> str:
    """Задание ячейке: шаблон матери с {input} (без шаблона — просто вход)."""
    if template and "{input}" in template:
        return template.replace("{input}", input_text)
    if template:
        return template + "\n\nВход:\n" + input_text
    return input_text


def resolve_inputs(spec, cwd: Path) -> list:
    """Входы роя из описания матери: список, файлы по маске, строки файла,
    куски текста. Возвращает список строк-входов."""
    cwd = Path(cwd)
    if spec is None:
        return []
    if isinstance(spec, str):
        return [spec]
    if isinstance(spec, (list, tuple)):
        return [str(item) for item in spec]
    if isinstance(spec, dict):
        if "items" in spec:
            return [str(item) for item in spec.get("items") or []]
        if "glob" in spec:
            out = []
            for path in sorted(cwd.glob(str(spec["glob"]))):
                if path.is_file():
                    out.append(path.read_text(encoding="utf-8", errors="replace"))
            return out
        if "file_lines" in spec:
            text = (cwd / str(spec["file_lines"])).read_text(encoding="utf-8")
            return text.splitlines()
        if "file" in spec:
            return [(cwd / str(spec["file"])).read_text(encoding="utf-8")]
        if "text" in spec:
            text = str(spec["text"])
            chunk = int(spec.get("chunk_chars") or 0)
            if chunk <= 0:
                return [text]
            return [text[i:i + chunk] for i in range(0, len(text), chunk)] or [""]
    raise SwarmError("не понимаю описание входов: нужен список, {glob}, "
                     "{file_lines}, {file} или {text, chunk_chars}")


# --- исполнитель одной ячейки на роутере матери ------------------------------


class RouterCellRunner:
    """Одна ячейка: короткий цикл роутера с тулами профиля и жёсткими воротами."""

    def __init__(self, *, router, settings, secrets, mother_tools: dict,
                 guard=None, project_dir: Path | None = None,
                 project_confirmed: bool = False) -> None:
        self.router = router
        self.settings = settings
        self.secrets = secrets
        self.mother_tools = mother_tools or {}
        self.guard = guard
        self.project_dir = Path(project_dir) if project_dir else Path.cwd()
        self.project_confirmed = bool(project_confirmed)

    # --- ворота ------------------------------------------------------------

    def _gate(self, profile: CellProfile, call, meta: dict) -> str | None:
        """None — можно выполнять; строка — текст отказа для модели ячейки."""
        from ..agents.host import cell_guard_decision

        if call.name in FORBIDDEN_TOOLS:
            return DENIED_PREFIX + ": служебный тул матери ячейке запрещён."
        if cell_guard_decision(call.name, profile.tools) is not None:
            return (DENIED_PREFIX + ": тул " + call.name + " вне профиля " +
                    profile.caste + " — отказ, человека ячейки не спрашивают.")
        if self.guard is not None:
            hard = self.guard.hard_block(call.name, call.arguments or {})
            if hard is not None:
                return (DENIED_PREFIX + ": Предохранитель матери: " + hard.reason +
                        " Ячейка подтвердить не может — отказ.")
        if call.name in WRITE_TOOLS or call.name == "bash":
            return self._gate_write(profile, call, meta)
        return None

    def _gate_write(self, profile: CellProfile, call, meta: dict) -> str | None:
        mode = profile.write_mode
        if mode == "none":
            return (DENIED_PREFIX + ": профиль " + profile.caste +
                    " только читает (write: none), а " + call.name + " пишет.")
        if call.name == "bash":
            return None  # каталог исполнения выставит ctx; жёсткое — выше по списку
        root = self._write_root(profile, meta)
        if root is None:
            return DENIED_PREFIX + ": режим записи " + mode + " ячейке недоступен."
        raw = str((call.arguments or {}).get("path") or "")
        if not raw:
            return None
        target = Path(raw)
        if not target.is_absolute():
            target = root / target
        try:
            target.resolve().relative_to(root.resolve())
        except (ValueError, OSError):
            return (DENIED_PREFIX + ": путь " + raw + " выходит за пределы " +
                    ("своей папки" if mode == "sandbox" else "разрешённой папки") +
                    " (" + mode + ").")
        return None

    def _write_root(self, profile: CellProfile, meta: dict) -> Path | None:
        mode = profile.write_mode
        if mode == "sandbox":
            return Path(meta["cell_dir"])
        if mode == "worktree":
            return Path(meta["write_dir"])
        if mode == "project":
            return self.project_dir if self.project_confirmed else None
        return None

    def _cell_ctx(self, profile: CellProfile, meta: dict) -> ToolContext:
        mode = profile.write_mode
        if mode == "sandbox":
            cwd = Path(meta["cell_dir"])
            cwd.mkdir(parents=True, exist_ok=True)
        elif mode == "worktree":
            cwd = Path(meta["write_dir"])
            cwd.mkdir(parents=True, exist_ok=True)
        else:
            # none и project: читаем (и в project — пишем) в проекте матери;
            # папку ячейки ради чтения не плодим (важно на 10 000).
            cwd = self.project_dir
        return ToolContext(cwd=cwd, settings=self.settings, secrets=self.secrets,
                           output_dir=Path(meta["cell_dir"]), agent=None)

    # --- прогон ---------------------------------------------------------------

    def __call__(self, profile: CellProfile, task_text: str, meta: dict):
        tools = {}
        for name in profile.tools:
            tool = self.mother_tools.get(name)
            if tool is not None:
                tools[name] = tool
        specs = [tool.spec() for tool in tools.values()]
        system = profile.prompt or ("Ты — ячейка " + profile.caste + ".")
        if profile.input_hint or profile.output_hint:
            system += "\n\nВход: " + (profile.input_hint or "текст") + \
                "\nВыход: " + (profile.output_hint or "текст")
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": task_text}]
        ctx = self._cell_ctx(profile, meta)
        total_in = total_out = 0
        endpoint = meta.get("endpoint") or ""
        for _step in range(max(1, profile.steps)):
            result = self._chat(profile, messages, specs, endpoint)
            total_in += result.usage.input
            total_out += result.usage.output
            if not result.tool_calls:
                return result.text or "", total_in, total_out
            assistant = {"role": "assistant", "content": result.text,
                         "tool_calls": [{"id": c.id, "name": c.name,
                                         "arguments": c.arguments,
                                         "intent": c.intent}
                                        for c in result.tool_calls]}
            messages.append(assistant)
            for call in result.tool_calls:
                deny = self._gate(profile, call, meta)
                if deny is not None:
                    content = deny
                else:
                    tool = tools.get(call.name)
                    if tool is None:
                        content = DENIED_PREFIX + ": тула нет у матери."
                    else:
                        res = tool.run(ctx, call.arguments or {})
                        content = res.text
                messages.append({"role": "tool", "tool_call_id": call.id,
                                 "name": call.name, "content": content})
        return "(лимит шагов ячейки исчерпан)", total_in, total_out

    def _chat(self, profile: CellProfile, messages, specs, endpoint: str):
        ref = profile.model or "fast"
        if endpoint:
            # Пул: конкретный адрес одной модели — свой ProviderConfig.
            from ..router.core import ProviderConfig, parse_ref

            _prov, model = parse_ref(ref if ref not in ROLES else
                                     (self.router.role_ref(ref) if self.router else ref))
            cfg = ProviderConfig(name="pool", kind="openai", base_url=endpoint,
                                 api_key=None)
            return self.router._chat_once(cfg, model, messages, specs,
                                          lambda kind, text: None)
        if ref in ROLES:
            return self.router.chat(messages, specs, role=ref,
                                    on_event=lambda kind, text: None)
        return self.router.chat(messages, specs, ref=ref,
                                on_event=lambda kind, text: None)


# --- рой ------------------------------------------------------------------------


class SwarmRun:
    """Один поток: очередь заданий, пул исполнителей, результаты на диске."""

    def __init__(self, *, stream: str, profiles: list, inputs: list, template: str = "",
                 output_dir: Path, settings=None, publish=None, runner=None,
                 parallel: int = 8, link_size: int = 8, budget_usd: float = 1.0,
                 mail: str = "tree", collapse: str = "auto", max_attempts: int = 3,
                 interrupt_check=None, wallet=None, swarm_id: str = "") -> None:
        if not profiles:
            raise SwarmError("рою нужен хотя бы один профиль ячейки")
        if not inputs:
            raise SwarmError("рою нечего делать: входы пустые")
        self.stream = stream
        self.profiles = list(profiles)
        self.inputs = [str(x) for x in inputs]
        self.template = template or ""
        self.settings = settings
        self.publish = publish or (lambda ev: None)
        self.runner = runner
        self.parallel = max(1, int(parallel))
        self.link_size = max(1, int(link_size))
        self.budget_usd = float(budget_usd or 0.0)
        self.mail = mail if mail in ("none", "tree", "board", "neighbors") else "tree"
        self.collapse = collapse
        self.max_attempts = max(1, int(max_attempts))
        self.interrupt_check = interrupt_check
        self.wallet = wallet
        self.swarm_id = swarm_id or (stream + "-" + new_id()[:6])
        self.dir = Path(output_dir) / self.swarm_id
        self.results_dir = self.dir / "results"
        self.total = len(self.inputs)
        self.links_total = (self.total + self.link_size - 1) // self.link_size
        # Параллельные списки состояния — компактно и для 10 000.
        self.status: list = ["wait"] * self.total      # wait|run|done|failed|skipped
        self.attempts: list = [0] * self.total
        self.tokens_in: list = [0] * self.total
        self.tokens_out: list = [0] * self.total
        self.costs: list = [0.0] * self.total
        self.done = 0
        self.failed = 0
        self.skipped = 0
        self.spent = 0.0
        self.board: list = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._budget_out = threading.Event()
        self._started_at = 0.0
        self._last_progress = 0.0
        self._pools: dict = {}
        for prof in self.profiles:
            if prof.pool:
                self._pools[prof.caste] = ModelPool(prof.pool, base_parallel=2)
        self.stopped_reason = ""

    # --- адреса и раскладка -----------------------------------------------------

    def profile_of(self, index: int) -> CellProfile:
        return self.profiles[index % len(self.profiles)]

    def link_of(self, index: int) -> int:
        return index // self.link_size

    def address_of(self, index: int) -> str:
        prof = self.profile_of(index)
        link = self.link_of(index)
        return cell_address(self.stream, prof.caste, link, index - link * self.link_size)

    # --- диск ---------------------------------------------------------------------

    def _write_meta(self, finished: bool) -> None:
        meta = {"format": "luna-swarm/1", "swarm": self.swarm_id, "stream": self.stream,
                "template": self.template, "mail": self.mail,
                "link_size": self.link_size, "parallel": self.parallel,
                "budget_usd": self.budget_usd, "collapse": self.collapse,
                "max_attempts": self.max_attempts, "total": self.total,
                "profiles": [p.to_dict() for p in self.profiles],
                "finished": bool(finished), "done": self.done,
                "failed": self.failed, "skipped": self.skipped,
                "spent_usd": round(self.spent, 6)}
        atomic_write_text(self.dir / "state.json",
                          json.dumps(meta, ensure_ascii=False, indent=1))

    def _prepare_disk(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.results_dir.mkdir(parents=True, exist_ok=True)
        inputs_path = self.dir / "inputs.jsonl"
        if not inputs_path.exists():
            with inputs_path.open("w", encoding="utf-8") as fh:
                for item in self.inputs:
                    fh.write(json.dumps(item, ensure_ascii=False) + "\n")
        self._write_meta(finished=False)

    def _write_result(self, index: int, ok: bool, text: str, error: str = "") -> None:
        data = {"index": index, "address": self.address_of(index),
                "link": self.link_of(index), "caste": self.profile_of(index).caste,
                "ok": bool(ok), "text": text, "error": error,
                "attempts": self.attempts[index],
                "tokens_in": self.tokens_in[index], "tokens_out": self.tokens_out[index],
                "cost": round(self.costs[index], 6)}
        atomic_write_text(self.results_dir / (str(index) + ".json"),
                          json.dumps(data, ensure_ascii=False))

    def read_result(self, index: int) -> dict | None:
        try:
            data = json.loads((self.results_dir / (str(index) + ".json"))
                              .read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    # --- события --------------------------------------------------------------------

    def _emit(self, ev: dict) -> None:
        ev = dict(ev)
        ev.setdefault("swarm", self.swarm_id)
        ev.setdefault("stream", self.stream)
        try:
            self.publish(ev)
        except Exception:  # noqa: BLE001 — события не роняют рой
            pass

    def _emit_progress(self, force: bool = False) -> None:
        now = time.monotonic()
        with self._lock:
            done, failed, skipped, spent = self.done, self.failed, self.skipped, self.spent
        if not force and self.total > 200 and done % _PROGRESS_EVERY and \
                now - self._last_progress < 0.5:
            return
        self._last_progress = now
        elapsed = max(0.001, now - self._started_at) if self._started_at else 0.001
        rate = done / elapsed if done else 0.0
        eta = (self.total - done - failed - skipped) / rate if rate else None
        self._emit({"type": "swarm.progress", "done": done, "failed": failed,
                    "skipped": skipped, "total": self.total, "spent_usd": round(spent, 6),
                    "eta_sec": None if eta is None else round(eta, 1),
                    "host": host_address(self.stream)})
        self._emit_cells()

    def _emit_cells(self) -> None:
        if self.total <= 2000:
            cells = [[self.address_of(i), self.status[i]] for i in range(self.total)]
        else:
            cells = []
            for link in range(self.links_total):
                lo = link * self.link_size
                hi = min(self.total, lo + self.link_size)
                states = self.status[lo:hi]
                state = "done" if all(s == "done" for s in states) else \
                    "failed" if any(s == "failed" for s in states) else \
                    "run" if any(s == "run" for s in states) else "wait"
                cells.append([link_address(self.stream, self.profile_of(lo).caste, link),
                              state, states.count("done"), len(states)])
        self._emit({"type": "swarm.cells", "cells": cells, "links": self.total > 2000})

    def _emit_mail(self, frm: str, to: str, text: str) -> None:
        if self.total > 500:
            return  # в большом рое письма — только между звеньями (см. _finish_link)
        self._emit({"type": "agent.message", "agent_id": to, "parent_id": frm,
                    "kind": "cell", "name": to, "from": frm, "to": to,
                    "from_id": frm, "to_id": to, "text": text[:200]})

    # --- одна ячейка -----------------------------------------------------------------

    def _cell_meta(self, index: int, profile: CellProfile) -> dict:
        link = self.link_of(index)
        cell_dir = self.dir / "cells" / (profile.caste + "." + str(link) + "." +
                                         str(index - link * self.link_size))
        write_dir = cell_dir
        if profile.write_mode == "worktree":
            write_dir = self._worktree_for(link)
        elif profile.write_mode == "project":
            write_dir = Path.cwd()
        return {"index": index, "address": self.address_of(index), "link": link,
                "cell_dir": cell_dir, "write_dir": write_dir, "endpoint": ""}

    def _worktree_for(self, link: int) -> Path:
        path = self.dir / "worktrees" / ("link-" + str(link))
        if path.exists():
            return path
        base = Path.cwd()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            proc = subprocess.run(
                ["git", "-C", str(base), "worktree", "add", "-b",
                 "luna-" + self.swarm_id + "-" + str(link), str(path)],
                capture_output=True, timeout=60)
            if proc.returncode == 0:
                return path
        except Exception:  # noqa: BLE001 — не git: честный откат в песочницу звена
            pass
        fallback = self.dir / "cells" / ("link-" + str(link))
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback

    def _run_one(self, index: int) -> None:
        profile = self.profile_of(index)
        meta = self._cell_meta(index, profile)
        task_text = render_task(self.template, self.inputs[index])
        pool = self._pools.get(profile.caste)
        last_error = ""
        while self.attempts[index] < self.max_attempts:
            if self._stop.is_set() or self._budget_out.is_set():
                return
            ep = None
            if pool is not None:
                ep = pool.acquire()
                if ep is None:
                    time.sleep(0.05)
                    continue
                meta["endpoint"] = ep.url
            self.attempts[index] += 1
            started = time.monotonic()
            try:
                text, tin, tout = self.runner(profile, task_text, meta)
            except Exception as exc:  # noqa: BLE001 — ячейка упала: повтор по лимиту
                if pool is not None and ep is not None:
                    pool.release(ep, rate_limited=is_rate_limited(exc), ok=False)
                last_error = str(exc)
                continue
            if pool is not None and ep is not None:
                pool.release(ep, ok=True)
            elapsed = time.monotonic() - started
            cost = usage_cost(self.settings, tin, tout)
            self.tokens_in[index] += tin
            self.tokens_out[index] += tout
            self.costs[index] += cost
            if elapsed > profile.ttl_s:
                last_error = "TTL ячейки исчерпан: " + format(elapsed, ".1f") + \
                    " с > " + format(profile.ttl_s, ".0f") + " с"
                continue
            if profile.budget_tokens and tin + tout > profile.budget_tokens:
                last_error = "личный лимит токенов профиля исчерпан"
                self._finish_cell(index, False, "", last_error, cost)
                return
            if profile.budget_usd and self.costs[index] > profile.budget_usd:
                last_error = "личный бюджет профиля исчерпан"
                self._finish_cell(index, False, "", last_error, cost)
                return
            self._finish_cell(index, True, text or "", "", cost)
            return
        self._finish_cell(index, False, "", last_error or "не вышло", 0.0)

    def _finish_cell(self, index: int, ok: bool, text: str, error: str, cost: float) -> None:
        with self._lock:
            if self.status[index] in ("done", "failed"):
                return
            self.status[index] = "done" if ok else "failed"
            if ok:
                self.done += 1
            else:
                self.failed += 1
            self.spent += cost
            spent = self.spent
        self._write_result(index, ok, text, error)
        if self.wallet is not None and cost:
            try:
                snap = self.wallet.charge(self.address_of(index), self.address_of(index),
                                           cost, self.tokens_in[index] +
                                           self.tokens_out[index])
                self._emit(dict({"type": "budget.update"}, **snap))
                if snap.get("exhausted"):
                    self._budget_out.set()
                    self.stopped_reason = "исчерпан общий кошелёк сессии"
            except Exception:  # noqa: BLE001 — кошелёк не роняет рой
                pass
        if self.budget_usd and spent >= self.budget_usd:
            self._budget_out.set()
            self.stopped_reason = "потолок бюджета роя исчерпан"
        if self.mail == "tree" and ok:
            self._emit_mail(self.address_of(index),
                            link_address(self.stream, self.profile_of(index).caste,
                                         self.link_of(index)),
                            text)
        elif self.mail == "neighbors" and ok:
            link = self.link_of(index)
            nxt = index + 1
            if nxt < min(self.total, (link + 1) * self.link_size):
                self._emit_mail(self.address_of(index), self.address_of(nxt), text)
        elif self.mail == "board":
            with self._lock:
                self.board.append({"address": self.address_of(index),
                                   "ok": ok, "text": text[:300]})
        self._emit_progress()

    # --- главный прогон ----------------------------------------------------------------

    def stop(self) -> None:
        """Esc хозяина: весь поток останавливается, недоделанное — skipped."""
        self._stop.set()

    def run(self) -> dict:
        if self.runner is None:
            raise SwarmError("рою нужен исполнитель ячеек (runner)")
        self._prepare_disk()
        # Докрутка: готовые результаты с диска не переделываем.
        for index in range(self.total):
            prev = self.read_result(index)
            if prev is not None:
                self.status[index] = "done" if prev.get("ok") else "failed"
                self.attempts[index] = int(prev.get("attempts") or 1)
                self.tokens_in[index] = int(prev.get("tokens_in") or 0)
                self.tokens_out[index] = int(prev.get("tokens_out") or 0)
                self.costs[index] = float(prev.get("cost") or 0.0)
                if prev.get("ok"):
                    self.done += 1
                else:
                    self.failed += 1
                self.spent += self.costs[index]
        pending = [i for i in range(self.total) if self.status[i] == "wait"]
        self._started_at = time.monotonic()
        self._emit({"type": "swarm.start", "total": self.total,
                    "links": self.links_total, "profiles": [p.caste for p in self.profiles],
                    "budget_usd": self.budget_usd, "mail": self.mail,
                    "dir": str(self.dir), "host": host_address(self.stream)})
        self._emit_cells()
        work: queue.Queue = queue.Queue()
        for index in pending:
            work.put(index)

        def worker() -> None:
            while not self._stop.is_set() and not self._budget_out.is_set():
                try:
                    index = work.get_nowait()
                except queue.Empty:
                    return
                if self.interrupt_check is not None and self.interrupt_check():
                    self._stop.set()
                    return
                with self._lock:
                    if self.status[index] != "wait":
                        continue
                    self.status[index] = "run"
                try:
                    self._run_one(index)
                except Exception:  # noqa: BLE001 — сбой движка на одном задании не роняет рой
                    with self._lock:
                        if self.status[index] == "run":
                            self.status[index] = "wait"
                with self._lock:
                    if self.status[index] == "run":
                        self.status[index] = "wait"

        threads = [threading.Thread(target=worker, name="luna-cell-" + str(n),
                                    daemon=True)
                   for n in range(min(self.parallel, max(1, len(pending))))]
        for thread in threads:
            thread.start()
        while any(t.is_alive() for t in threads):
            if self.interrupt_check is not None and self.interrupt_check():
                self._stop.set()
            time.sleep(0.02)
        for thread in threads:
            thread.join(timeout=1.0)
        with self._lock:
            for index in range(self.total):
                if self.status[index] in ("wait", "run"):
                    self.status[index] = "skipped"
                    self.skipped += 1
        if self._stop.is_set() and not self.stopped_reason:
            self.stopped_reason = "остановлен хозяином (Esc)"
        if self._budget_out.is_set() and not self.stopped_reason:
            self.stopped_reason = "бюджет исчерпан"
        summary = self._collect()
        self._write_meta(finished=not self._stop.is_set() and self.skipped == 0)
        self._emit({"type": "swarm.done", "done": summary["done"],
                    "failed": summary["failed"], "skipped": summary["skipped"],
                    "total": self.total, "spent_usd": summary["spent_usd"],
                    "tokens": summary["tokens"], "seconds": summary["seconds"],
                    "dir": str(self.dir), "stopped_reason": self.stopped_reason,
                    "host": host_address(self.stream)})
        self._emit_progress(force=True)
        return summary

    # --- свёртка звеньев и итог ----------------------------------------------------------

    def _collapse_link(self, link: int) -> str:
        lo = link * self.link_size
        hi = min(self.total, lo + self.link_size)
        rows = []
        for index in range(lo, hi):
            data = self.read_result(index)
            if data is None:
                rows.append({"address": self.address_of(index), "ok": False,
                             "text": "", "error": "нет результата"})
            else:
                rows.append(data)
        ok_rows = [r for r in rows if r.get("ok")]
        mode = self.collapse
        if mode == "auto":
            mode = "code"  # механическая свёртка — кодом; model — только по просьбе
        if mode == "model" and ok_rows and self.runner is not None:
            col = next((p for p in self.profiles if p.caste == "col"), None)
            if col is None:
                from .profiles import get_profile

                col = get_profile("col", self.settings)
            if col is not None:
                joined = "\n\n".join("### " + str(r.get("address")) + "\n" +
                                     str(r.get("text") or "") for r in ok_rows)
                meta = {"index": -1, "address": link_address(self.stream, col.caste, link),
                        "link": link, "cell_dir": self.dir / "cells",
                        "write_dir": self.dir / "cells", "endpoint": ""}
                try:
                    text, tin, tout = self.runner(col, joined, meta)
                    self.spent += usage_cost(self.settings, tin, tout)
                    return text or joined
                except Exception:  # noqa: BLE001 — сборщик упал: честная склейка кодом
                    pass
        # Свёртка кодом: JSON-результаты склеиваются списком, текст — с подписями.
        parsed = []
        all_json = bool(ok_rows)
        for row in ok_rows:
            try:
                parsed.append(json.loads(str(row.get("text") or "")))
            except ValueError:
                all_json = False
                break
        head = "Звено " + str(link) + " (" + str(len(ok_rows)) + "/" + str(hi - lo) + " сдали)"
        if all_json and ok_rows:
            return head + "\n" + json.dumps(parsed, ensure_ascii=False)
        parts = [head]
        for row in rows:
            if row.get("ok"):
                parts.append("### " + str(row.get("address")) + "\n" + str(row.get("text") or ""))
            else:
                parts.append("### " + str(row.get("address")) + "\n[упала: " +
                             str(row.get("error") or "ошибка") + "]")
        return "\n\n".join(parts)

    def _collect(self) -> dict:
        seconds = time.monotonic() - self._started_at if self._started_at else 0.0
        link_summaries = []
        for link in range(self.links_total):
            text = self._collapse_link(link)
            link_summaries.append(text)
            if self.mail != "tree":
                continue  # сводка звена хозяину — только схема tree
            self._emit({"type": "agent.message", "agent_id": host_address(self.stream),
                        "parent_id": self.swarm_id, "kind": "cell",
                        "name": host_address(self.stream),
                        "from": link_address(self.stream,
                                             self.profile_of(link * self.link_size).caste,
                                             link),
                        "to": host_address(self.stream),
                        "from_id": link_address(self.stream,
                                                self.profile_of(link * self.link_size).caste,
                                                link),
                        "to_id": host_address(self.stream),
                        "text": text[:200]})
        summary = {"swarm": self.swarm_id, "stream": self.stream,
                   "dir": str(self.dir), "total": self.total, "done": self.done,
                   "failed": self.failed, "skipped": self.skipped,
                   "links": self.links_total, "spent_usd": round(self.spent, 6),
                   "tokens": sum(self.tokens_in) + sum(self.tokens_out),
                   "seconds": round(seconds, 2),
                   "stopped_reason": self.stopped_reason,
                   "link_summaries": link_summaries}
        lines = ["# Сводка потока " + self.swarm_id + " (" + self.stream + ")",
                 "",
                 "Сдали: " + str(self.done) + " из " + str(self.total) +
                 "; упали: " + str(self.failed) + "; не начаты: " + str(self.skipped) +
                 "; звеньев: " + str(self.links_total) +
                 "; деньги: $" + format(self.spent, ".4f") +
                 "; токены: " + str(summary["tokens"]) +
                 "; время: " + format(seconds, ".1f") + " с",
                 ""]
        if self.stopped_reason:
            lines.append("Остановлен: " + self.stopped_reason)
            lines.append("")
        if self.mail == "board":
            lines.append("Доска: " + str(len(self.board)) + " заметок (board.json)")
            lines.append("")
            atomic_write_text(self.dir / "board.json",
                              json.dumps(self.board, ensure_ascii=False, indent=1))
        for link, text in enumerate(link_summaries):
            lines.append("## Звено " + str(link))
            lines.append("")
            lines.append(text)
            lines.append("")
        summary["summary_text"] = "\n".join(lines)
        atomic_write_text(self.dir / "summary.md", summary["summary_text"])
        atomic_write_text(self.dir / "summary.json",
                          json.dumps({k: v for k, v in summary.items()
                                      if k not in ("link_summaries", "summary_text")},
                                     ensure_ascii=False, indent=1))
        return summary


# --- смета, продолжение, поиск незаконченных -----------------------------------------


def estimate(runner, profile: CellProfile, inputs: list, template: str = "",
             settings=None, parallel: int = 8, tmp_dir: Path | None = None) -> dict:
    """Смета: пробный прогон на 3 входах, цена и время одного × N."""
    sample = [str(x) for x in list(inputs)[:3]]
    if not sample:
        raise SwarmError("смета невозможна: входы пустые")
    base = Path(tmp_dir) if tmp_dir else Path.cwd()
    total_cost = 0.0
    total_time = 0.0
    for i, item in enumerate(sample):
        meta = {"index": i, "address": "estimate/" + profile.caste + ".0." + str(i),
                "link": 0, "cell_dir": base / ("cell-" + str(i)),
                "write_dir": base / ("cell-" + str(i)), "endpoint": ""}
        started = time.monotonic()
        _text, tin, tout = runner(profile, render_task(template, item), meta)
        total_time += time.monotonic() - started
        total_cost += usage_cost(settings, tin, tout)
    n = len(sample)
    per_cost = total_cost / n
    per_time = total_time / n
    total = len(inputs)
    return {"samples": n, "per_cost_usd": round(per_cost, 6),
            "per_time_s": round(per_time, 3),
            "total_cost_usd": round(per_cost * total, 4),
            "total_time_s": round(per_time * total / max(1, parallel), 1),
            "model": profile.model, "profile": profile.caste}


def load_unfinished(swarm_dir: Path) -> dict | None:
    """Мета незаконченного потока из его папки; None — закончен или нет такого."""
    try:
        meta = json.loads((Path(swarm_dir) / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return None if meta.get("finished") else meta


def find_unfinished(output_dir: Path) -> list:
    """Папки незаконченных потоков в общем каталоге роёв."""
    out = []
    root = Path(output_dir)
    if not root.is_dir():
        return out
    for child in sorted(root.iterdir()):
        if child.is_dir() and load_unfinished(child) is not None:
            out.append(child)
    return out


def unfinished_from_records(records: list) -> dict | None:
    """Последний незаконченный поток по записям журнала сессии (для --continue)."""
    found = None
    for rec in records or []:
        if isinstance(rec, dict) and rec.get("kind") == "swarm":
            data = rec.get("data") or {}
            if data.get("finished"):
                found = None
            elif data.get("dir"):
                found = data
    return found


def resume(swarm_dir: Path, *, settings=None, publish=None, runner=None,
           parallel: int | None = None, interrupt_check=None, wallet=None) -> dict:
    """Докрутить поток из его папки: готовые результаты не переделываются."""
    swarm_dir = Path(swarm_dir)
    meta = json.loads((swarm_dir / "state.json").read_text(encoding="utf-8"))
    inputs = []
    with (swarm_dir / "inputs.jsonl").open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                inputs.append(json.loads(line))
    profiles = [CellProfile.from_dict(d) for d in meta.get("profiles") or []]
    run = SwarmRun(stream=str(meta.get("stream") or "leo"), profiles=profiles,
                   inputs=inputs, template=str(meta.get("template") or ""),
                   output_dir=swarm_dir.parent, settings=settings, publish=publish,
                   runner=runner, parallel=int(parallel or meta.get("parallel") or 8),
                   link_size=int(meta.get("link_size") or 8),
                   budget_usd=float(meta.get("budget_usd") or 1.0),
                   mail=str(meta.get("mail") or "tree"),
                   collapse=str(meta.get("collapse") or "auto"),
                   max_attempts=int(meta.get("max_attempts") or 3),
                   interrupt_check=interrupt_check, wallet=wallet,
                   swarm_id=str(meta.get("swarm") or swarm_dir.name))
    return run.run()
