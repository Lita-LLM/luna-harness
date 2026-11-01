"""Хозяин агентов-детей (ЗАДАЧА-11, docs/РОЙ.md §2, §3, §9).

У каждого полноценного агента (главного и спутников) — свой AgentHost:
он порождает детей, держит их права, бюджет, почту и состав. Ребёнок —
обычный Agent (luna/mind/loop.py) со своим промптом, тулами, моделью и
именем, работает в своём потоке; его события хозяин пересылает на общую
шину с полями agent_id, parent_id, kind, name (контракт — docs/СОБЫТИЯ.md).

Жёсткие правила фундамента (на них встанет рой, ЗАДАЧА-15):
- права ребёнка ⊆ прав родителя: режим не свободнее родительского, тулы —
  только из набора родителя; опасное идёт через Предохранитель хозяина, а
  вопрос человеку помечен именем ребёнка («Кодер1 хочет…»);
- детей звать могут только полноценные агенты (главный и спутники);
  глубина поколений спутников — agents.max_depth (по умолчанию 1);
- все дети тратят из общего кошелька сессии agents.session_budget_usd;
  упёрлись — ребёнок останавливается и сдаёт то, что успел;
- хозяину уходит сводка ребёнка, не весь лог: полный лог — события на шине
  (журнал сессии и «Обратная сторона»).
"""

from __future__ import annotations

import queue
import subprocess
import threading
import time
from pathlib import Path

from ..core.ids import new_id
from ..core.paths import luna_home
from .budget import Wallet
from .mail import MAIN_ID, MAIN_NAME, MailError, check_mail, endpoint, main_endpoint
from .prompts import child_suffix

ROLES = ("main", "fast", "guard", "vision")
# Режимы Предохранителя по возрастанию свободы: свободнее родителя нельзя.
MODE_ORDER = ["plan", "review", "normal", "auto", "yolo"]
SPAWN_TOOLS = ("assistant", "crew")
WRITE_TOOLS = ("write", "edit")
KIND_RU = {"assistant": "ассистент", "satellite": "спутник", "cell": "ячейка"}
FULL_KINDS = ("main", "satellite")  # кто вправе звать детей

_STOP = object()


def cell_guard_decision(tool_name: str, allowed_tools) -> str | None:
    """Точка решения для ячеек роя (ЗАДАЧА-15, РОЙ.md §2): «вне профиля —
    отказ, а не вопрос». Ячейка не полноценный агент и человека не дёргает:
    тул из профиля — None (решай дальше по режиму записи), чужой тул —
    "deny" без всякого guard.ask. Движок роя (luna/starfall/engine.py)
    спрашивает это перед каждым вызовом ячейки."""
    if tool_name not in allowed_tools:
        return "deny"
    return None


class SpawnError(Exception):
    """Ребёнка создать нельзя: права, глубина, бюджет, имя или настройки."""


class ChildBudget:
    """Личные лимиты ребёнка поверх общего кошелька (шаги — через
    agent.max_steps у самого Agent; здесь токены, деньги и время)."""

    def __init__(self, max_tokens: int = 0, max_usd: float = 0.0,
                 max_seconds: float = 0.0) -> None:
        self.max_tokens = int(max_tokens or 0)
        self.max_usd = float(max_usd or 0.0)
        self.max_seconds = float(max_seconds or 0.0)

    def check(self, totals: dict, elapsed: float) -> str | None:
        if self.max_tokens and totals["tokens"] >= self.max_tokens:
            return "исчерпан личный лимит токенов"
        if self.max_usd and totals["cost"] >= self.max_usd:
            return "исчерпан личный лимит денег"
        if self.max_seconds and elapsed >= self.max_seconds:
            return "исчерпан личный лимит времени"
        return None


class Child:
    """Живой ребёнок: запись хозяина + его Agent + поток (у спутников)."""

    def __init__(self, host: "AgentHost", agent_id: str, kind: str, name: str) -> None:
        self.host = host
        self.id = agent_id
        self.kind = kind
        self.name = name
        self.role = ""
        self.prompt = ""
        self.spec_model = ""
        self.model_label = ""
        self.mode = ""
        self.parent_id = MAIN_ID
        self.gen = 0
        self.can_spawn = False
        self.leader = False
        self.worktree = False
        self.budget = ChildBudget()
        self.agent = None
        self.state = "спит"
        self.totals = {"tokens": 0, "cost": 0.0, "steps": 0}
        self.last_cost = 0.0
        self.started_at = time.monotonic()
        self.turn_active = False
        self.last_text = ""
        self.summary = ""
        self.error = False
        self.budget_out = False
        self.budget_reason = ""
        self.dismissed = False
        self.spawn_notes: list[str] = []
        self.reply_to: dict | None = None
        self.inbox: queue.Queue = queue.Queue()
        self.pending = 0  # заданий/писем в очереди или в работе (для wait_idle)
        self.thread: threading.Thread | None = None
        self.done_event = threading.Event()
        self.pending_notes: list[str] = []

    @property
    def kind_ru(self) -> str:
        return KIND_RU.get(self.kind, self.kind)

    def endpoint(self) -> dict:
        return endpoint(self.id, self.kind, self.parent_id, self.name)


class _ChildSettings:
    """Настройки ребёнка: всё от хозяина, точечные перекрытия сверху."""

    def __init__(self, base, overrides: dict) -> None:
        object.__setattr__(self, "_base", base)
        object.__setattr__(self, "_ov", dict(overrides or {}))

    def get(self, key, default=None):
        if key in self._ov:
            return self._ov[key]
        if self._base is None:
            return default
        return self._base.get(key, default)

    def __getattr__(self, name):
        return getattr(self._base, name)


def identity_of(agent) -> dict:
    """Кто этот агент в почтовых терминах: главный или ребёнок."""
    child_id = getattr(agent, "_agents_child_id", "")
    if not child_id:
        return main_endpoint()
    return endpoint(child_id, getattr(agent, "_agents_kind", "satellite"),
                    getattr(agent, "_agents_parent_id", MAIN_ID),
                    getattr(agent, "_agents_name", child_id))


def host_for(agent) -> "AgentHost":
    """Хозяин детей этого агента (создаётся лениво, один на агента)."""
    host = getattr(agent, "_agents_host", None)
    if host is None:
        host = AgentHost(agent)
        agent._agents_host = host
    return host


def _eff_mode(agent) -> str:
    mode = getattr(agent, "mode", "") or ""
    if not mode and getattr(agent, "settings", None) is not None:
        mode = agent.settings.get("agent.mode", "normal") or "normal"
    return mode if mode in MODE_ORDER else "normal"


class AgentHost:
    """Хозяин детей одного полноценного агента."""

    def __init__(self, owner) -> None:
        self.owner = owner
        parent_host = getattr(owner, "_agents_parent_host", None)
        self.root: AgentHost = parent_host.root if parent_host is not None else self
        if self.root is self:
            limit = 0.0
            if getattr(owner, "settings", None) is not None:
                limit = float(owner.settings.get("agents.session_budget_usd", 5.0) or 0.0)
            self._wallet = Wallet(limit)
            self._directory: dict[str, Child] = {}
            self._owner_notes: list[str] = []
        self.children: dict[str, Child] = {}
        self._lock = threading.RLock()
        self._claims: dict[str, str] = {}  # путь файла -> id спутника, кто правит
        self._restored = False
        self._restoring = False

    # --- общие вещи ----------------------------------------------------

    @property
    def wallet(self) -> Wallet:
        return self.root._wallet

    @property
    def settings(self):
        return getattr(self.owner, "settings", None)

    def _setting(self, key, default):
        if self.settings is None:
            return default
        return self.settings.get(key, default)

    def _publish(self, event: dict) -> None:
        try:
            self.owner.publish(event)
        except Exception:  # noqa: BLE001 — события не роняют работу
            pass

    def _annotated(self, child: Child, event: dict) -> dict:
        ev = dict(event)
        ev["agent_id"] = child.id
        ev["parent_id"] = child.parent_id
        ev["kind"] = child.kind
        ev["name"] = child.name
        return ev

    # --- порождение ------------------------------------------------------

    def spawn(self, *, kind: str, name: str = "", role: str = "", prompt: str = "",
              model: str = "", mode: str = "", tools=None, steps: int = 0,
              tokens: int = 0, usd: float = 0.0, seconds: float = 0.0,
              worktree: bool = False, leader: bool = False,
              can_spawn: bool | None = None, restore: dict | None = None) -> Child:
        """Создать ребёнка. Ошибки прав и правил — SpawnError с причиной."""
        if not self._restoring:
            self.ensure_restored()
        if not bool(self._setting("agents.enabled", True)):
            raise SpawnError("агенты-дети выключены настройкой agents.enabled")
        spawner = identity_of(self.owner)
        if spawner["kind"] not in FULL_KINDS:
            raise SpawnError("детей могут звать только полноценные агенты "
                             "(главный и спутники), а не " + spawner["kind"])
        if can_spawn is None:
            can_spawn = kind == "satellite"
        spawner_gen = int(getattr(self.owner, "_agents_gen", 0) or 0)
        gen = spawner_gen
        if kind == "satellite":
            gen = spawner_gen + 1
            max_depth = int(self._setting("agents.max_depth", 1) or 1)
            if gen > max_depth:
                raise SpawnError("глубина поколений спутников " + str(gen) +
                                 " больше agents.max_depth=" + str(max_depth))
        with self._lock:
            alive = [c for c in self.children.values() if not c.dismissed]
            max_children = int(self._setting("agents.max_children", 8) or 8)
            if len(alive) >= max_children:
                raise SpawnError("у хозяина уже " + str(len(alive)) +
                                 " детей (agents.max_children=" + str(max_children) + ")")
            name = (name or "").strip()
            if not name:
                base = "Ассистент" if kind == "assistant" else "Спутник"
                n = 1
                while any(c.name == base + str(n) for c in alive):
                    n += 1
                name = base + str(n)
            if any(c.name == name for c in alive):
                raise SpawnError("имя «" + name + "» уже занято в этой команде")
        # Режим: не свободнее родителя (права ребёнка ⊆ прав родителя).
        parent_mode = _eff_mode(self.owner)
        want_mode = mode or parent_mode
        if want_mode not in MODE_ORDER:
            raise SpawnError("неизвестный режим: " + str(want_mode))
        notes: list[str] = []
        if MODE_ORDER.index(want_mode) > MODE_ORDER.index(parent_mode):
            notes.append("режим урезан с " + want_mode + " до " + parent_mode +
                         " (не свободнее хозяина)")
            want_mode = parent_mode
        # Тулы: только из набора родителя.
        owner_tools = getattr(self.owner, "tools", None)
        parent_tools = ({tool.name: tool for tool in owner_tools.values()}
                        if owner_tools is not None else {})
        if tools is None:
            child_tools = dict(parent_tools)
        else:
            missing = [t for t in tools if t not in parent_tools]
            if missing:
                raise SpawnError("у хозяина нет тулов, которые просят ребёнку: " +
                                 ", ".join(missing) + " (права ребёнка ⊆ прав родителя)")
            child_tools = {t: parent_tools[t] for t in tools}
        if kind == "assistant":
            for tool_name in SPAWN_TOOLS:
                child_tools.pop(tool_name, None)  # ассистент детей не зовёт
        # Модель: роль (main/fast/guard/vision) или конкретная ссылка.
        spec_model = (model or "").strip()
        if not spec_model:
            if kind == "assistant":
                spec_model = str(self._setting("agents.assistant_role", "fast") or "fast")
            else:
                spec_model = getattr(self.owner, "model_ref", "") or "main"
        ref = spec_model
        router = getattr(self.owner, "router", None)
        if spec_model in ROLES and router is not None:
            ref = router.role_ref(spec_model) or getattr(self.owner, "model_ref", "") or ""
        # Рабочий каталог: свой worktree по флагу, иначе каталог хозяина.
        cwd = Path(getattr(self.owner, "cwd", Path.cwd()))
        worktree_path = None
        if worktree and kind == "satellite":
            worktree_path = _make_worktree(cwd, name)
            if worktree_path is not None:
                cwd = worktree_path
            else:
                notes.append("worktree не вышел (не git-репозиторий?) — работаю в общем каталоге")
        # Системный хвост: роль и правила команды.
        report_hint = ("Отчёты о готовой работе уходят хозяину." if kind == "assistant"
                       else "Отчёты о готовой работе уходят лидеру команды, а если "
                            "его нет — хозяину.")
        suffix = child_suffix(name=name, kind=kind, role=role, prompt=prompt,
                              owner_name=spawner["name"], report_hint=report_hint)
        overrides: dict = {}
        eff_steps = int(steps or 0) or int(self._setting("agents.max_steps", 0) or 0)
        if eff_steps:
            overrides["agent.max_steps"] = eff_steps
        settings_obj = self.settings
        if overrides and settings_obj is not None:
            settings_obj = _ChildSettings(settings_obj, overrides)
        child = Child(self, new_id(), kind, name)
        child.role = role
        child.prompt = prompt
        child.spec_model = spec_model
        child.model_label = spec_model
        child.mode = want_mode
        child.parent_id = spawner["id"]
        child.gen = gen
        child.can_spawn = bool(can_spawn)
        child.leader = bool(leader)
        child.worktree = worktree_path is not None
        child.spawn_notes = notes
        child.budget = ChildBudget(tokens, usd, seconds)
        from ..mind.loop import Agent  # ленивый импорт: loop тянет реестр тулов

        agent = Agent(settings=settings_obj, secrets=getattr(self.owner, "secrets", None),
                      router=router, cwd=cwd, bus=None, session_id=new_id(),
                      mode=want_mode, model_ref=ref,
                      guard=getattr(self.owner, "guard", None),
                      ask_guard_fn=self._wrap_guard(child),
                      ask_user_fn=self._wrap_ask_user(child),
                      tools=child_tools, system_suffix=suffix)
        agent._agents_child_id = child.id
        agent._agents_kind = kind
        agent._agents_name = name
        agent._agents_gen = gen
        agent._agents_parent_host = self
        agent._agents_parent_id = spawner["id"]
        agent._agents_can_spawn = bool(can_spawn)
        child.agent = agent
        agent.listen(lambda ev, c=child: self._forward(c, ev))
        with self._lock:
            self.children[child.id] = child
            self.root._directory[child.id] = child
            if leader:
                for other in self.children.values():
                    if other is not child and other.kind == "satellite":
                        other.leader = False
        self._publish(self._annotated(child, {
            "type": "agent.spawn", "role": role, "model": spec_model,
            "mode": want_mode, "leader": bool(leader), "notes": notes}))
        self._set_state(child, "ждёт" if kind == "assistant" else "спит")
        if restore is not None:
            try:
                agent.load_history(restore.get("history") or restore.get("messages") or [],
                                   turn=int(restore.get("turn", 0) or 0),
                                   marks=restore.get("marks") or {})
            except Exception:  # noqa: BLE001 — битая история не мешает возвращению
                pass
        if kind == "satellite":
            child.thread = threading.Thread(target=self._satellite_loop, args=(child,),
                                            name="luna-sat-" + name, daemon=True)
            child.thread.start()
        if kind == "satellite" and not self._restoring:
            self._persist_crew()
        return child

    # --- вопросы человеку от имени ребёнка ------------------------------

    def _wrap_guard(self, child: Child):
        owner_fn = getattr(self.owner, "ask_guard_fn", None)

        def ask(event: dict):
            ev = dict(event)
            intent = str(ev.get("intent") or "")
            ev["intent"] = (child.name + " хочет: " + intent) if intent else \
                (child.name + " запрашивает подтверждение")
            reason = str(ev.get("reason") or "")
            ev["reason"] = "Запрашивает " + child.name + " (" + child.kind_ru + "): " + reason
            ev["from_agent"] = child.name
            ev["agent_id"] = child.id
            if owner_fn is None:
                return None
            return owner_fn(ev)

        return ask

    def _wrap_ask_user(self, child: Child):
        owner_fn = getattr(self.owner, "ask_user_fn", None)

        def ask(qid: str, question: str, options: list):
            if owner_fn is None:
                return None
            return owner_fn(qid, "[" + child.name + "] " + str(question), options)

        return ask

    # --- пересылка и учёт событий ребёнка --------------------------------

    def _forward(self, child: Child, event: dict) -> None:
        """Событие ребёнка -> на общую шину с пометками, плюс учёт хозяина."""
        if event.get("agent_id") and event.get("agent_id") != child.id:
            # Событие внука: внук уже помечен своим хозяином — просто транзит.
            self._publish(dict(event))
            return
        ev = self._annotated(child, event)
        self._observe(child, ev)
        self._publish(ev)

    def _observe(self, child: Child, ev: dict) -> None:
        etype = ev.get("type")
        if etype == "turn.start":
            child.turn_active = True
            child.last_text = ""
            child.error = False
            self._set_state(child, "думает")
        elif etype == "tool.call":
            self._set_state(child, "действует")
            self._claim_file(child, ev)
        elif etype == "guard.ask":
            self._set_state(child, "ждёт")
        elif etype == "agent.text":
            child.last_text += str(ev.get("text") or "")
        elif etype == "usage":
            self._charge(child, ev)
        elif etype == "error":
            child.error = True
        elif etype == "turn.done":
            child.turn_active = False
            for path in [p for p, owner_id in self._claims.items() if owner_id == child.id]:
                del self._claims[path]

    def _charge(self, child: Child, ev: dict) -> None:
        cost = float(ev.get("cost") or 0.0)
        delta = max(0.0, cost - child.last_cost)
        child.last_cost = cost
        tokens = int(ev.get("input") or 0) + int(ev.get("output") or 0)
        child.totals["tokens"] += tokens
        child.totals["cost"] += delta
        child.totals["steps"] += 1
        snap = self.wallet.charge(child.id, child.name, delta, tokens)
        self._publish(self._annotated(child, dict({"type": "budget.update"}, **snap)))
        reason = child.budget.check(child.totals, time.monotonic() - child.started_at)
        if reason is None and snap["exhausted"]:
            reason = "исчерпан общий кошелёк сессии (agents.session_budget_usd)"
        if reason is not None and not child.budget_out:
            child.budget_out = True
            child.budget_reason = reason
            try:
                child.agent.interrupt()
            except Exception:  # noqa: BLE001
                pass

    def _claim_file(self, child: Child, ev: dict) -> None:
        """Два спутника правят один файл: второй получает предупреждение."""
        if child.kind != "satellite" or ev.get("tool") not in WRITE_TOOLS:
            return
        args = ev.get("args") or {}
        raw = args.get("path") or args.get("file_path") or ""
        if not raw:
            return
        path = Path(str(raw))
        if not path.is_absolute():
            path = Path(getattr(child.agent, "cwd", ".")) / path
        key = str(path)
        holder = self._claims.get(key)
        if holder and holder != child.id:
            other = self.root._directory.get(holder)
            other_name = other.name if other is not None else "другой спутник"
            note = ("Файл " + str(raw) + " сейчас правит " + other_name +
                    " — возможна пересекающаяся правка")
            child.pending_notes.append(note)
            self._publish({"type": "notice", "text": note + " (" + child.name + ")"})
        else:
            self._claims[key] = child.id

    def _set_state(self, child: Child, state: str) -> None:
        if child.state == state or child.dismissed:
            return
        child.state = state
        self._publish(self._annotated(child, {"type": "agent.state", "state": state}))

    # --- ходы детей -------------------------------------------------------

    def _pending_add(self, child: Child, delta: int) -> None:
        with self._lock:
            child.pending = max(0, child.pending + delta)

    def _run_turn(self, child: Child, text: str, from_ep: dict) -> str:
        """Один ход ребёнка синхронно. Возвращает статус: ok|budget|error."""
        child.turn_active = True
        if child.budget_out or self.wallet.exhausted:
            child.budget_out = True
            if not child.budget_reason:
                child.budget_reason = "бюджет исчерпан до старта хода"
            child.summary = "Не стал работать: " + child.budget_reason + "."
            self._finish_turn(child, from_ep, budget=True)
            return "budget"
        child.reply_to = from_ep
        if child.pending_notes:
            text = "\n".join("[Заметка: " + n + "]" for n in child.pending_notes) + \
                "\n\n" + text
            child.pending_notes = []
        status = ""
        try:
            status = child.agent.handle_command({"type": "user.message", "text": text}) or ""
        except Exception as exc:  # noqa: BLE001 — ребёнок упал, хозяин живёт дальше
            child.error = True
            child.summary = "Ход упал: " + str(exc)
            self._finish_turn(child, from_ep, budget=False)
            return "error"
        summary = child.last_text.strip()
        if child.budget_out:
            summary = (summary + "\n" if summary else "") + \
                "[Остановлен: " + child.budget_reason + "]"
        child.summary = summary or "(ответ без текста)"
        self._finish_turn(child, from_ep, budget=child.budget_out)
        return "error" if child.error else "ok"

    def _finish_turn(self, child: Child, from_ep: dict, budget: bool) -> None:
        child.turn_active = False
        if child.kind == "assistant":
            self._set_state(child, "упал" if child.error and not budget else "сдал")
            self._publish(self._annotated(child, {
                "type": "agent.done", "summary": child.summary,
                "tokens": child.totals["tokens"], "cost": round(child.totals["cost"], 6),
                "budget_out": bool(budget)}))
            child.done_event.set()
            return
        # Спутник: отчёт — отправителю задания, если это хозяин или лидер.
        self._set_state(child, "сдал" if budget else "спит")
        target = None
        if from_ep.get("kind") == "main":
            target = from_ep
        elif from_ep.get("id") == self._leader_id():
            target = from_ep
        if target is not None and child.summary:
            try:
                self.send_mail(child.endpoint(), target["id"], child.summary)
            except MailError:
                pass
        if budget:
            self._publish(self._annotated(child, {
                "type": "agent.done", "summary": child.summary,
                "tokens": child.totals["tokens"], "cost": round(child.totals["cost"], 6),
                "budget_out": True}))
        if not self._restoring:
            self._persist_crew()

    def _leader_id(self) -> str:
        for host in self._all_hosts():
            for child in host.children.values():
                if child.kind == "satellite" and child.leader and not child.dismissed:
                    return child.id
        return ""

    def _all_hosts(self) -> list["AgentHost"]:
        hosts = [self.root]
        seen = {id(self.root)}
        queue_hosts = [self.root]
        while queue_hosts:
            host = queue_hosts.pop()
            for child in list(host.children.values()):
                child_host = getattr(child.agent, "_agents_host", None) if child.agent else None
                if child_host is not None and id(child_host) not in seen:
                    seen.add(id(child_host))
                    hosts.append(child_host)
                    queue_hosts.append(child_host)
        return hosts

    # --- спутники: поток и задания ----------------------------------------

    def _satellite_loop(self, child: Child) -> None:
        """Поток спутника: спит на очереди входящих, письмо/задание будит."""
        while not child.dismissed:
            item = child.inbox.get()
            if item is _STOP or child.dismissed:
                break
            from_ep = endpoint(item.get("from_id", MAIN_ID),
                               item.get("from_kind", "main"),
                               item.get("from_parent", ""),
                               item.get("from_name", MAIN_NAME))
            if item.get("kind") == "letter":
                text = "[Письмо от " + from_ep["name"] + "] " + str(item.get("text", ""))
            else:
                text = str(item.get("text", ""))
            try:
                self._run_turn(child, text, from_ep)
            finally:
                self._pending_add(child, -1)

    def assign_task(self, child: Child, text: str, sender: dict) -> None:
        """Задание спутнику в очередь (будит его). Ассистентам — run_assistant."""
        if child.dismissed:
            raise MailError("агент «" + child.name + "» распущен")
        if child.kind == "satellite":
            self._pending_add(child, 1)
            child.inbox.put({"kind": "task", "text": text,
                             "from_id": sender["id"], "from_kind": sender["kind"],
                             "from_parent": sender["parent_id"],
                             "from_name": sender["name"]})
        else:
            self.run_assistant(child, text, sender)

    def run_assistant(self, child: Child, text: str, sender: dict,
                      wait: bool = True) -> str:
        """Ассистент: работает до сдачи. wait=False — в своём потоке."""
        if child.dismissed:
            raise MailError("ассистент «" + child.name + "» распущен")

        def work() -> None:
            self._run_turn(child, text, sender)

        if wait:
            work()
        else:
            child.thread = threading.Thread(target=work, name="luna-asst-" + child.name,
                                            daemon=True)
            child.thread.start()
        return child.summary

    # --- почта ---------------------------------------------------------------

    def resolve(self, to: str) -> dict | None:
        """Адресат по id или имени: хозяин или любой ребёнок сессии."""
        to = (to or "").strip()
        if to in (MAIN_ID, MAIN_NAME, "хозяин", "Луна"):
            return main_endpoint()
        child = self.root._directory.get(to)
        if child is not None and not child.dismissed:
            return child.endpoint()
        for candidate in self.root._directory.values():
            if candidate.name == to and not candidate.dismissed:
                return candidate.endpoint()
        return None

    def find_child(self, name_or_id: str) -> Child | None:
        child = self.children.get(name_or_id)
        if child is not None:
            return child
        for candidate in self.children.values():
            if candidate.name == name_or_id and not candidate.dismissed:
                return candidate
        root_child = self.root._directory.get(name_or_id or "")
        if root_child is not None:
            return root_child
        for candidate in self.root._directory.values():
            if candidate.name == name_or_id and not candidate.dismissed:
                return candidate
        return None

    def send_mail(self, sender: dict, to: str, text: str) -> str:
        """Доставить письмо по правилам родителя (mail.check_mail)."""
        recipient = self.resolve(to)
        if recipient is None:
            raise MailError("нет такого агента: " + str(to))
        problem = check_mail(sender, recipient)
        if problem:
            raise MailError(problem)
        self.root._publish_mail(sender, recipient, text)
        if recipient["kind"] == "main":
            self.root._owner_notes.append(
                "[Письмо от " + sender["name"] + "] " + str(text))
            return "письмо ушло хозяину (увидит со следующим сообщением)"
        child = self.root._directory.get(recipient["id"])
        if child is None or child.dismissed:
            raise MailError("агент «" + recipient["name"] + "» недоступен")
        if child.kind == "satellite":
            child.host._pending_add(child, 1)
            child.inbox.put({"kind": "letter", "text": text,
                             "from_id": sender["id"], "from_kind": sender["kind"],
                             "from_parent": sender["parent_id"],
                             "from_name": sender["name"]})
            return "письмо доставлено: " + child.name + " проснётся и прочитает"
        # Ассистенту письмо — сообщением в его ход (ставится в очередь агента).
        try:
            child.agent.handle_command(
                {"type": "user.message",
                 "text": "[Письмо от " + sender["name"] + "] " + str(text)})
        except Exception as exc:  # noqa: BLE001
            raise MailError("не доставлено: " + str(exc)) from exc
        return "письмо доставлено: " + child.name

    def _publish_mail(self, sender: dict, recipient: dict, text: str) -> None:
        self._publish({"type": "agent.message", "agent_id": recipient["id"],
                       "parent_id": sender["id"], "kind": recipient["kind"],
                       "name": recipient["name"], "from": sender["name"],
                       "to": recipient["name"], "from_id": sender["id"],
                       "to_id": recipient["id"], "text": str(text)})

    def take_owner_notes(self) -> str:
        """Письма и отчёты детей хозяину: в начало его следующего хода."""
        notes = self.root._owner_notes
        if not notes:
            return ""
        self.root._owner_notes = []
        return "\n".join("[Почта команды] " + note for note in notes)

    # --- управление спутниками ------------------------------------------------

    def wake(self, child: Child, sender: dict) -> None:
        """«Пнуть» спутника: короткое письмо-пинг, будит спящего."""
        self.send_mail(sender, child.name, "пинг")

    def sleep(self, child: Child) -> None:
        """Усыпить: прервать текущий ход и выкинуть висящие задания."""
        if child.agent is not None:
            try:
                child.agent.interrupt()
            except Exception:  # noqa: BLE001
                pass
        while True:
            try:
                child.inbox.get_nowait()
            except queue.Empty:
                break
        with self._lock:
            child.pending = 0
        self._set_state(child, "спит")

    def dismiss(self, child: Child) -> None:
        """Распустить: стоп потокам, worktree убрать, состав сохранить."""
        if child.dismissed:
            return
        child.dismissed = True
        if child.agent is not None:
            try:
                child.agent.interrupt()
            except Exception:  # noqa: BLE001
                pass
        child.inbox.put(_STOP)
        if child.thread is not None and child.thread is not threading.current_thread():
            child.thread.join(timeout=2.0)
        if child.worktree and child.agent is not None:
            base = Path(getattr(self.owner, "cwd", Path.cwd()))
            _remove_worktree(base, Path(child.agent.cwd))
        self.root._directory.pop(child.id, None)
        self._publish(self._annotated(child, {
            "type": "agent.done", "summary": "распущен хозяином",
            "tokens": child.totals["tokens"], "cost": round(child.totals["cost"], 6),
            "dismissed": True}))
        child.done_event.set()
        if child.kind == "satellite":
            self._persist_crew()

    def interrupt_all(self) -> None:
        """Esc хозяина гасит всех детей (и recursively — их детей)."""
        for child in list(self.children.values()):
            if child.dismissed or child.agent is None:
                continue
            try:
                child.agent.interrupt()
            except Exception:  # noqa: BLE001
                pass

    def wait_idle(self, timeout: float = 5.0) -> bool:
        """Дождаться, пока все дети хозяина закончат ходы (для try и тестов)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            busy = False
            for child in list(self.children.values()):
                if child.dismissed:
                    continue
                if child.kind == "assistant":
                    if child.turn_active or (child.thread is not None
                                             and child.thread.is_alive()):
                        busy = True
                elif child.turn_active or child.pending > 0:
                    busy = True
            if not busy:
                return True
            time.sleep(0.01)
        return False

    # --- состав и описание ------------------------------------------------------

    def roster(self) -> list[Child]:
        self.ensure_restored()
        return [c for c in self.children.values() if not c.dismissed]

    def describe(self) -> str:
        rows = []
        for child in self.roster():
            mark = " (лидер)" if child.leader else ""
            rows.append(child.name + mark + " — " + child.kind_ru +
                        (", " + child.role if child.role else "") +
                        "; модель " + (child.model_label or "—") +
                        "; " + child.state +
                        "; токенов " + str(child.totals["tokens"]) +
                        "; $" + format(child.totals["cost"], ".4f"))
        if not rows:
            return "Команда пуста."
        snap = self.wallet.snapshot()
        rows.append("Кошелёк сессии: потрачено $" + format(snap["spent_usd"], ".4f") +
                    (" из $" + format(snap["limit_usd"], ".2f") if snap["limit_usd"] else "") +
                    ", темп $" + format(snap["rate_per_min_usd"], ".4f") + "/мин")
        return "\n".join(rows)

    # --- команда данными: файлы crews/ -------------------------------------------

    def crew_data(self) -> dict:
        from .crewfile import crew_payload

        members = []
        leader_name = ""
        for child in self.roster():
            if child.kind != "satellite":
                continue
            members.append({"name": child.name, "role": child.role,
                            "prompt": child.prompt, "model": child.spec_model,
                            "mode": child.mode, "worktree": child.worktree,
                            "can_spawn": child.can_spawn})
            if child.leader:
                leader_name = child.name
        return crew_payload(members, leader_name)

    def save_crew(self, name: str, where: str = "home") -> Path:
        from .crewfile import save_crew_file

        return save_crew_file(name, self.crew_data(), where,
                              Path(getattr(self.owner, "cwd", Path.cwd())))

    def load_crew(self, name: str) -> list[str]:
        """Загрузить состав из файла: отсутствующих создать спящими."""
        from .crewfile import load_crew_file

        data = load_crew_file(name, Path(getattr(self.owner, "cwd", Path.cwd())))
        notes: list[str] = []
        leader_name = data.get("leader") or ""
        for member in data.get("members") or []:
            if self.find_child(str(member.get("name", ""))) is not None:
                notes.append("уже есть: " + str(member.get("name")))
                continue
            child = self.spawn(kind="satellite", name=str(member.get("name", "")),
                               role=str(member.get("role", "")),
                               prompt=str(member.get("prompt", "")),
                               model=str(member.get("model", "")),
                               mode=str(member.get("mode", "")),
                               worktree=bool(member.get("worktree")),
                               can_spawn=bool(member.get("can_spawn", True)),
                               leader=str(member.get("name", "")) == leader_name)
            notes.append("создан спящим: " + child.name)
        return notes

    # --- сессия: спутники переживают --continue -----------------------------------

    def _persist_crew(self) -> None:
        """Снимок спутников (состав + история) в журнал сессии."""
        if self._restoring:
            return
        journal = getattr(self.owner, "journal", None)
        if journal is None:
            return
        members = []
        for child in self.children.values():
            if child.kind != "satellite" or child.dismissed or child.agent is None:
                continue
            try:
                snap = child.agent._state_snapshot()
            except Exception:  # noqa: BLE001
                snap = {}
            members.append({"name": child.name, "role": child.role,
                            "prompt": child.prompt, "model": child.spec_model,
                            "mode": child.mode, "worktree": child.worktree,
                            "can_spawn": child.can_spawn, "leader": child.leader,
                            "history": snap})
        try:
            journal.append({"kind": "crew", "data": {"members": members}})
        except Exception:  # noqa: BLE001 — журнал не должен ронять команду
            pass

    def _journal_records(self) -> list:
        journal = getattr(self.owner, "journal", None)
        if journal is None:
            return []
        records = getattr(journal, "records", None)
        if isinstance(records, list):
            return records
        path = getattr(journal, "path", None)
        if path:
            from ..core.sessions import load_records

            try:
                return load_records(Path(path))
            except Exception:  # noqa: BLE001
                return []
        return []

    def ensure_restored(self) -> None:
        """Вернуть спутников из журнала сессии — спящими, с историей."""
        if self._restored or self._restoring:
            return
        self._restored = True
        if getattr(self.owner, "_agents_child_id", ""):
            return  # восстанавливает только хозяин сессии (главный агент)
        records = self._journal_records()
        data = None
        for record in records:
            if isinstance(record, dict) and record.get("kind") == "crew":
                data = record.get("data") or {}
        if not data:
            return
        self._restoring = True
        try:
            for member in data.get("members") or []:
                name = str(member.get("name", ""))
                if not name or self.find_child(name) is not None:
                    continue
                try:
                    self.spawn(kind="satellite", name=name,
                               role=str(member.get("role", "")),
                               prompt=str(member.get("prompt", "")),
                               model=str(member.get("model", "")),
                               mode=str(member.get("mode", "")),
                               worktree=bool(member.get("worktree")),
                               can_spawn=bool(member.get("can_spawn", True)),
                               leader=bool(member.get("leader")),
                               restore=member.get("history") or {})
                except SpawnError:
                    continue  # настройки поменялись — спутник не вернулся, сессия жива
        finally:
            self._restoring = False


# --- git worktree для спутников -------------------------------------------------


def _git(cwd: Path, *args: str) -> bool:
    try:
        proc = subprocess.run(["git", "-C", str(cwd), *args],
                              capture_output=True, timeout=30)
        return proc.returncode == 0
    except Exception:  # noqa: BLE001 — нет git или не репозиторий
        return False


def _make_worktree(base_cwd: Path, name: str) -> Path | None:
    """Отдельный git worktree спутника; None — не вышло (не git и т.п.)."""
    if not _git(base_cwd, "rev-parse", "--is-inside-work-tree"):
        return None
    slug = "sat-" + new_id()[:8].lower()
    path = luna_home() / "worktrees" / slug
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        return None
    if _git(base_cwd, "worktree", "add", "-b", "luna-" + slug, str(path)):
        return path
    return None


def _remove_worktree(base_cwd: Path, path: Path) -> bool:
    """Убрать worktree при роспуске; грязный git не уберёт — останется на диске."""
    return _git(base_cwd, "worktree", "remove", str(path))
