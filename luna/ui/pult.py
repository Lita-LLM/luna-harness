"""Пульт ИИ (/pult, /providers): все источники моделей в одном месте.

Экраны одной выбиралки чата:
  main      — провайдеры реестра и старой настройки, скан, добавление, роли;
  provider  — карточка: модели → роль, проверка, ключ, адрес, тип, по
              умолчанию, вкл/выкл, удалить;
  models    — модели провайдера с поиском; Enter — для какой роли;
  role      — какой роли отдать выбранную модель;
  scan      — скан сети: прогресс вживую, Enter по находке — в реестр;
  add       — заготовки облаков и свой адрес.

Ключ вводится скрытым полем и уходит сразу в сейф; на экран и в ленту
не попадает. Сеть (скан, проверки, списки моделей) — в фоне: экран не
замирает, а результат подменяет пункты, если пульт ещё открыт.
"""

from __future__ import annotations

import threading
import time

from ..router.catalog import PROVIDER_PRESETS
from ..router.core import KNOWN_PROVIDERS, LOCAL_PROVIDERS, Router, secret_name
from ..router.providers import ProviderEntry, ProviderRegistry, slug
from .picker import PickerItem

ROLES = (("main", "главная"), ("fast", "быстрая"), ("guard", "охранник"),
         ("vision", "картинки"))
KIND = "pult"


class Pult:
    def __init__(self, app):
        self.app = app
        self.screen = "main"
        self.current = ""         # имя провайдера карточки
        self.model = ""           # модель, ждущая выбора роли
        self.found: list = []     # находки последнего скана (живут, пока жив чат)
        self.scan_state = ""      # текст прогресса скана
        self._scan_cancel = None
        self._models: dict = {}   # имя → список моделей из API (кэш на сессию)

    # --- окружение ---------------------------------------------------------

    @property
    def settings(self):
        return self.app.settings

    def registry(self) -> ProviderRegistry:
        return ProviderRegistry(self.app._data_dir().root / "providers.json")

    def secrets(self):
        from ..core.secrets import Secrets

        return Secrets(self.app._data_dir().secrets)

    def router(self) -> Router:
        return Router(self.settings, self.secrets(), registry=self.registry())

    def _note(self, text: str) -> None:
        from .chat import Entry

        self.app._add(Entry("notice", text=text))

    def _refresh(self) -> None:
        """Перерисовать текущий экран, если пульт ещё открыт."""
        if self.app.picker is not None and self.app._picker_kind == KIND:
            title, items, hint = self._screen_data()
            self.app.picker.title = title
            self.app.picker.set_items(items)
            self.app.picker.hint = hint
            self.app._last_change = self.app._clock()

    def _show(self, screen: str) -> None:
        self.screen = screen
        title, items, hint = self._screen_data()
        self.app._open_picker(KIND, title, items, hint=hint, preview_lines=0,
                              extra=self._extra())
        self.app._last_change = self.app._clock()

    def _extra(self) -> dict:
        if self.screen == "main":
            return {"s": "scan", "+": "add", "r": "roles"}
        if self.screen == "scan":
            return {"a": "add_all", "s": "rescan"}
        if self.screen == "pools":
            return {"d": "delpool"}
        return {}

    def open(self) -> None:
        self._show("main")

    # --- данные провайдеров -------------------------------------------------

    def _all(self) -> list:
        """Записи реестра плюс провайдер из старых настроек, если его нет в реестре."""
        reg = self.registry()
        entries = reg.entries()
        legacy = str(self.settings.get("model.provider") or "")
        if legacy and reg.get(legacy) is None and legacy != "fake":
            kind = str(self.settings.get("model.kind") or "openai")
            base = str(self.settings.get("model.base_url") or "") or \
                KNOWN_PROVIDERS.get(legacy, ("", ""))[1]
            entries.append(ProviderEntry(name=legacy, kind=kind, base_url=base,
                                         origin="settings"))
        return entries

    def _entry(self, name: str):
        for entry in self._all():
            if entry.name == name:
                return entry
        return None

    def _has_key(self, name: str) -> bool:
        try:
            return bool(self.secrets().get(secret_name(name)))
        except Exception:  # noqa: BLE001 — сейф недоступен: считаем «нет»
            return False

    @staticmethod
    def _where(entry) -> str:
        url = entry.base_url.lower()
        if entry.name in LOCAL_PROVIDERS or "localhost" in url or "127.0.0.1" in url:
            return "этот компьютер"
        if "://" in url and not any(d in url for d in (".com", ".ai", ".io", ".org", ".net")):
            return "своя сеть"
        return "облако"

    def _detail(self, entry) -> str:
        bits = [entry.base_url or "адрес не задан", entry.kind, self._where(entry)]
        bits.append("ключ есть" if self._has_key(entry.name) else "без ключа")
        models = self._models.get(entry.name) or entry.models
        if entry.alive is False:
            bits.append("не отвечает")
        elif models:
            bits.append("моделей " + str(len(models)))
        if not entry.enabled:
            bits.append("выключен")
        if entry.origin == "settings":
            bits.append("из старых настроек")
        return " · ".join(bits)

    # --- экраны ---------------------------------------------------------------

    def _screen_data(self):
        return getattr(self, "_scr_" + self.screen)()

    def _scr_main(self):
        default = str(self.settings.get("model.provider") or "")
        items = []
        for entry in self._all():
            mark = "★" if entry.name == default else ("·" if entry.enabled else "×")
            items.append(PickerItem(key="p:" + entry.name, title=entry.name,
                                    detail=self._detail(entry), mark=mark))
        known = {e.base_url.rstrip("/").lower() for e in self._all()}
        fresh = [f for f in self.found if f.base_url.rstrip("/").lower() not in known]
        for f in fresh:
            items.append(PickerItem(key="f:" + f.base_url, title="＋ найден: " + f.base_url,
                                    detail=f.label + " — Enter: добавить", payload=f))
        scan_title = "⟳ Скан сети" + (" — " + self.scan_state if self.scan_state else "")
        items += [
            PickerItem(key="scan", title=scan_title,
                       detail="всё, что вокруг отвечает как API моделей (быстро: типичные порты)"),
            PickerItem(key="scan:deep", title="⟳ Глубокий скан",
                       detail="все 65535 портов на своём компьютере и узлах из hosts"),
            PickerItem(key="add", title="＋ Добавить провайдера",
                       detail="облако из заготовок или свой адрес"),
            PickerItem(key="search", title="⌕ Поиск в вебе",
                       detail="движки web_search, их порядок и ключи"),
            PickerItem(key="pools", title="✦ Пулы роя",
                       detail="ключи адресов пула ячеек (pool:<имя>)"),
            PickerItem(key="roles", title="◐ Роли моделей",
                       detail=" · ".join(r + ": " + (str(self.settings.get("model." + r) or "")
                                                     or "—") for r, _t in ROLES)),
        ]
        if not self._all():
            items.insert(0, PickerItem(key="", title="Провайдеров пока нет",
                                       detail="сканируй сеть (s) или добавь вручную (+)"))
        return ("Пульт ИИ — провайдеры и модели", items,
                "Enter — открыть · s — скан · + — добавить · r — роли · печатай — поиск · Esc — закрыть")

    def _scr_provider(self):
        entry = self._entry(self.current)
        if entry is None:
            self.screen = "main"
            return self._scr_main()
        default = str(self.settings.get("model.provider") or "")
        has_key = self._has_key(entry.name)
        models = self._models.get(entry.name) or entry.models
        items = [
            PickerItem(key="models", title="Модели → роль",
                       detail=("моделей " + str(len(models))) if models else "загрузить список из API"),
            PickerItem(key="check", title="Проверить связь",
                       detail="запросить список моделей сейчас"),
            PickerItem(key="key", title="Ключ: " + ("есть — заменить" if has_key else "задать"),
                       detail="вводится скрытно, хранится в сейфе"),
        ]
        if has_key:
            items.append(PickerItem(key="unkey", title="Удалить ключ"))
        items += [
            PickerItem(key="url", title="Адрес: " + (entry.base_url or "не задан"),
                       detail="изменить"),
            PickerItem(key="kind", title="Тип API: " + entry.kind,
                       detail="openai → anthropic → gemini по кругу"),
            PickerItem(key="default",
                       title=("★ По умолчанию" if entry.name == default else "Сделать по умолчанию"),
                       detail="модели без «провайдер:» идут сюда"),
            PickerItem(key="toggle", title="Выключить" if entry.enabled else "Включить"),
            PickerItem(key="delete", title="Удалить из пульта",
                       detail="ключ в сейфе остаётся, пока не удалишь его отдельно"),
            PickerItem(key="back", title="← Назад"),
        ]
        return ("Провайдер " + entry.name + " — " + self._where(entry), items,
                "Enter — действие · Esc — назад к списку")

    def _scr_models(self):
        models = self._models.get(self.current)
        items = [PickerItem(key="back", title="← Назад")]
        if models is None:
            items.append(PickerItem(key="", title="Загружаю список моделей…"))
        elif not models:
            items.append(PickerItem(key="", title="Провайдер не отдал моделей",
                                    detail="проверь ключ и адрес; можно вписать вручную"))
        items.append(PickerItem(key="manual", title="✎ Вписать модель вручную"))
        for model in models or []:
            items.append(PickerItem(key="m:" + model, title=model))
        return ("Модели " + self.current, items,
                "печатай — поиск · Enter — выбрать роль · Esc — назад")

    def _scr_role(self):
        ref = self.current + ":" + self.model
        items = []
        for role, title in ROLES:
            now = str(self.settings.get("model." + role) or "")
            items.append(PickerItem(key="r:" + role, title=role + " — " + title,
                                    detail="сейчас: " + (now or "—"),
                                    mark="→" if now == ref else ""))
        items.append(PickerItem(key="back", title="← Назад"))
        return ("Кому отдать " + ref, items, "Enter — назначить · Esc — назад")

    def _scr_scan(self):
        items = []
        for f in self.found:
            added = self.registry().find_by_url(f.base_url) is not None
            detail = f.label + ((" · также: " + ", ".join(f.aliases[:3])) if f.aliases else "")
            items.append(PickerItem(key="f:" + f.base_url,
                                    title=("✓ " if added else "＋ ") + f.base_url,
                                    detail=detail, payload=f))
        if not items:
            items.append(PickerItem(key="", title=self.scan_state or "Скан не запускался"))
        items.append(PickerItem(key="back", title="← Назад"))
        return ("Скан сети — " + (self.scan_state or "готово"), items,
                "Enter — добавить находку · a — добавить все · s — ещё раз · Esc — назад")

    def _scr_search(self):
        from ..net.search import DEFAULT_ORDER, PROVIDERS
        from ..setup import SEARCH_KEY_PROVIDERS

        order = str(self.settings.get("web.search_providers") or "") or DEFAULT_ORDER
        keyed = dict(SEARCH_KEY_PROVIDERS)
        items = [PickerItem(key="order", title="Порядок: " + order,
                            detail="Enter — изменить (через запятую)")]
        for name, (label, _fn) in PROVIDERS.items():
            if name in keyed:
                has = self._secret_has("search:" + name)
                detail = "ключ есть — Enter: заменить" if has else "без ключа — Enter: задать"
            elif name == "searxng":
                url = str(self.settings.get("web.searxng_url") or "")
                detail = (url or "адрес не задан") + " — Enter: адрес своего SearXNG"
            else:
                detail = "без ключа"
            mark = "·" if name in order.split(",") else ""
            items.append(PickerItem(key="s:" + name, title=label, detail=detail, mark=mark))
        items.append(PickerItem(key="back", title="← Назад"))
        return ("Поиск в вебе", items, "Enter — действие · Esc — назад")

    def _scr_pools(self):
        names = []
        try:
            names = [n[5:] for n in self.secrets().names() if n.startswith("pool:")]
        except Exception:  # noqa: BLE001
            pass
        items = [PickerItem(key="newpool", title="＋ Ключ нового адреса пула",
                            detail="имя, затем ключ скрытно")]
        for name in sorted(names):
            items.append(PickerItem(key="pool:" + name, title=name,
                                    detail="ключ есть — Enter: заменить · d — удалить"))
        items.append(PickerItem(key="back", title="← Назад"))
        return ("Пулы роя", items, "Enter — действие · Esc — назад")

    def _secret_has(self, name: str) -> bool:
        try:
            return bool(self.secrets().get(name))
        except Exception:  # noqa: BLE001
            return False

    def _pick_search(self, key: str, item) -> bool:
        from ..net.search import DEFAULT_ORDER
        from ..setup import SEARCH_KEY_PROVIDERS

        picker = self.app.picker
        if key == "order":
            picker.ask_input("Порядок движков: ", ("search_order", None),
                             initial=str(self.settings.get("web.search_providers") or "")
                             or DEFAULT_ORDER)
        elif key == "s:searxng":
            picker.ask_input("Адрес SearXNG: ", ("searxng_url", None),
                             initial=str(self.settings.get("web.searxng_url") or ""))
        elif key[2:] in dict(SEARCH_KEY_PROVIDERS):
            picker.ask_input("Ключ " + item.title + " (не показывается): ",
                             ("secret", "search:" + key[2:]), secret=True)
        return True

    def _pick_pools(self, key: str, item) -> bool:
        if key == "newpool":
            self.app.picker.ask_input("Имя адреса пула (например node-1): ", ("pool_name", None))
        elif key.startswith("pool:"):
            self.app.picker.ask_input("Ключ пула " + key[5:] + " (не показывается): ",
                                      ("secret", key), secret=True)
        return True

    def _scr_add(self):
        items = []
        for name, title, kind, base in PROVIDER_PRESETS:
            if name in ("fake", "custom", "omniroute"):
                continue
            items.append(PickerItem(key="preset:" + name, title=title,
                                    detail=(base or "") + " · " + kind))
        items.insert(0, PickerItem(key="custom", title="✎ Свой адрес",
                                   detail="любой OpenAI-совместимый: http://узел:порт/v1"))
        items.append(PickerItem(key="back", title="← Назад"))
        return ("Добавить провайдера", items, "Enter — выбрать · Esc — назад")

    # --- действия --------------------------------------------------------------

    def on_action(self, action) -> bool:
        """Действие выбиралки пульта. True — обработано (выбиралка ещё наша)."""
        kind = action[0]
        if kind == "close":
            if self.screen in ("main",):
                self.app.picker = None
            elif self.screen == "role":
                self._show("models")
            elif self.screen == "models":
                self._show("provider")
            else:
                self._show("main")
            return True
        if kind == "action":
            return self._extra_action(action[1], action[2])
        if kind == "input":
            return self._input(action[1], action[2])
        if kind == "confirmed":
            return self._confirmed(action[1])
        if kind != "pick" or action[1] is None:
            return True
        key = action[1].key
        if not key:
            return True
        if key == "back":
            return self.on_action(("close",))
        return getattr(self, "_pick_" + self.screen)(key, action[1])

    def _extra_action(self, name: str, item) -> bool:
        if name in ("scan", "rescan"):
            self.start_scan("fast")
        elif name == "add":
            self._show("add")
        elif name == "roles":
            self.app.picker = None
            self.app.open_models_picker()
        elif name == "delpool" and item is not None and item.key.startswith("pool:"):
            self.app.picker.ask_confirm("Удалить ключ " + item.key + "? Enter — да",
                                        ("delsecret", item.key))
        elif name == "add_all":
            for f in self.found:
                self._add_found(f, quiet=True)
            self._note("В пульт добавлено находок: " + str(len(self.found)))
            self._refresh()
        return True

    def _pick_main(self, key: str, item) -> bool:
        if key.startswith("p:"):
            self.current = key[2:]
            self._show("provider")
        elif key.startswith("f:"):
            name = self._add_found(item.payload)
            self.current = name
            self._show("provider")
        elif key == "scan":
            self.start_scan("fast")
        elif key == "scan:deep":
            self.start_scan("deep")
        elif key == "add":
            self._show("add")
        elif key == "roles":
            self.app.picker = None
            self.app.open_models_picker()
        elif key in ("search", "pools"):
            self._show(key)
        return True

    def _pick_provider(self, key: str, item) -> bool:
        entry = self._entry(self.current)
        if entry is None:
            self._show("main")
            return True
        picker = self.app.picker
        if key == "models":
            self._show("models")
            if self.current not in self._models:
                self._fetch_models(self.current)
        elif key == "check":
            self._models.pop(self.current, None)
            self._fetch_models(self.current, announce=True)
        elif key == "key":
            picker.ask_input("Ключ для " + entry.name + " (не показывается): ",
                             ("key", entry.name), secret=True)
        elif key == "unkey":
            picker.ask_confirm("Удалить ключ " + entry.name + " из сейфа? Enter — да",
                               ("unkey", entry.name))
        elif key == "url":
            picker.ask_input("Адрес API: ", ("url", entry.name), initial=entry.base_url)
        elif key == "kind":
            order = ("openai", "anthropic", "gemini")
            entry = self._materialize(entry)
            entry.kind = order[(order.index(entry.kind) + 1) % 3] if entry.kind in order else "openai"
            self.registry().upsert(entry)
            self._refresh()
        elif key == "default":
            self._materialize(entry)
            self.settings.set("model.provider", entry.name)
            self._note("Провайдер по умолчанию: " + entry.name)
            self._refresh()
        elif key == "toggle":
            entry = self._materialize(entry)
            entry.enabled = not entry.enabled
            self.registry().upsert(entry)
            self._refresh()
        elif key == "delete":
            picker.ask_confirm("Убрать " + entry.name + " из пульта? Enter — да",
                               ("delete", entry.name))
        return True

    def _pick_models(self, key: str, item) -> bool:
        if key == "manual":
            self.app.picker.ask_input("Модель (как её зовёт провайдер): ",
                                      ("model", self.current))
        elif key.startswith("m:"):
            self.model = key[2:]
            self._show("role")
        return True

    def _pick_role(self, key: str, item) -> bool:
        if key.startswith("r:"):
            role = key[2:]
            ref = self.current + ":" + self.model
            self.settings.set("model." + role, ref)
            self._note("Модель роли " + role + ": " + ref + " — со следующего хода.")
            self._show("provider")
        return True

    def _pick_scan(self, key: str, item) -> bool:
        if key.startswith("f:"):
            self.current = self._add_found(item.payload)
            self._show("provider")
        return True

    def _pick_add(self, key: str, item) -> bool:
        if key == "custom":
            self.app.picker.ask_input("Адрес API (например http://узел:11434/v1): ",
                                      ("new_url", None))
        elif key.startswith("preset:"):
            name = key[7:]
            _n, _title, kind, base = next(p for p in PROVIDER_PRESETS if p[0] == name)
            reg = self.registry()
            if reg.get(name) is None:
                reg.upsert(ProviderEntry(name=name, kind=kind, base_url=base, origin="preset"))
            self.current = name
            self._show("provider")
            if self._where(reg.get(name)) == "облако" and not self._has_key(name):
                self.app.picker.ask_input("Ключ для " + name + " (не показывается): ",
                                          ("key", name), secret=True)
        return True

    def _input(self, text: str, ctx) -> bool:
        if ctx is None:
            return True
        what, name = ctx
        if what == "secret":
            if text:
                self.secrets().set(name, text)
                self._note("Ключ " + name + " сохранён в сейф.")
            self._refresh()
            return True
        if what == "search_order":
            self.settings.set("web.search_providers", text)
            self._refresh()
            return True
        if what == "searxng_url":
            self.settings.set("web.searxng_url", text)
            self._refresh()
            return True
        if what == "pool_name":
            if text:
                from ..setup import pool_secret_name

                try:
                    secret = pool_secret_name(text)
                except Exception as exc:  # noqa: BLE001
                    self._note("Имя пула не подходит: " + str(exc))
                    return True
                self.app.picker.ask_input("Ключ пула " + text + " (не показывается): ",
                                          ("secret", secret), secret=True)
            return True
        if what == "key":
            if text:
                self._materialize(self._entry(name))
                self.secrets().set(secret_name(name), text)
                self._note("Ключ " + name + " сохранён в сейф.")
                self._models.pop(name, None)
            self._refresh()
        elif what == "url":
            entry = self._materialize(self._entry(name))
            entry.base_url = text.rstrip("/")
            self.registry().upsert(entry)
            self._models.pop(name, None)
            self._refresh()
        elif what == "model" and text:
            self.model = text
            self._show("role")
        elif what == "new_url" and text:
            url = text if "://" in text else "http://" + text
            reg = self.registry()
            found = reg.find_by_url(url)
            if found is None:
                host = url.split("://", 1)[1].split("/", 1)[0]
                found = reg.upsert(ProviderEntry(name=reg.free_name(host), kind="openai",
                                                 base_url=url.rstrip("/"), origin="manual"))
            self.current = found.name
            self._show("provider")
            self._fetch_models(found.name, announce=True)
        return True

    def _confirmed(self, action) -> bool:
        if not action:
            return True
        what, name = action
        if what == "delsecret":
            self.secrets().delete(name)
            self._note("Ключ " + name + " удалён из сейфа.")
            self._refresh()
            return True
        if what == "unkey":
            self.secrets().delete(secret_name(name))
            self._note("Ключ " + name + " удалён из сейфа.")
            self._refresh()
        elif what == "delete":
            self.registry().remove(name)
            if str(self.settings.get("model.provider") or "") == name:
                self._note(name + " был провайдером по умолчанию — выбери другой.")
            self._show("main")
        return True

    # --- служебное ----------------------------------------------------------

    def _materialize(self, entry):
        """Провайдер из старых настроек при первой правке переезжает в реестр."""
        if entry is not None and entry.origin == "settings":
            entry.origin = "manual"
            self.registry().upsert(entry)
        return entry

    def _add_found(self, found, quiet: bool = False) -> str:
        reg = self.registry()
        old = reg.find_by_url(found.base_url)
        if old is not None:
            return old.name
        host = found.host
        base = found.hint if found.hint in ("ollama", "lmstudio") else host
        name = reg.free_name(base if host == "localhost" else host + "-" + str(found.port))
        reg.upsert(ProviderEntry(name=name, kind=found.kind, base_url=found.base_url,
                                 origin="scan", alive=True, models=list(found.models),
                                 needs_key=found.needs_key, checked_at=time.time(),
                                 note=found.hint))
        if found.models:
            self._models[name] = list(found.models)
        if not quiet:
            self._note("В пульт добавлен " + name + " — " + found.base_url)
        return name

    def _fetch_models(self, name: str, announce: bool = False) -> None:
        def work():
            try:
                models = self.router().fetch_models(name)
                alive = True
                err = ""
            except Exception as exc:  # noqa: BLE001 — сеть/ключ: показать, не падать
                models, alive, err = [], False, str(exc)[:160]
            self._models[name] = models
            reg = self.registry()
            entry = reg.get(name)
            if entry is not None:
                entry.alive, entry.models, entry.checked_at = alive, models[:500], time.time()
                reg.upsert(entry)
            if announce:
                self._note(name + ": " + ("отвечает, моделей " + str(len(models)) if alive
                                          else "не отвечает — " + err))
            self._refresh()

        threading.Thread(target=work, daemon=True).start()

    def start_scan(self, mode: str = "fast") -> None:
        from ..net import scan as _scan

        if self._scan_cancel is not None:
            return  # уже идёт
        cancel = threading.Event()
        self._scan_cancel = cancel
        self.found = []
        self.scan_state = "запускаю…"
        self._show("scan")
        workers = int(self.settings.get("scan.workers") or 1024)
        timeout = float(self.settings.get("scan.connect_timeout") or 0.35)
        extra = [h.strip() for h in str(self.settings.get("scan.extra_hosts") or "").split(",")
                 if h.strip()]
        started = time.time()

        def progress(done, total, found_n):
            pct = int(done * 100 / total) if total else 100
            self.scan_state = (("глубокий " if mode == "deep" else "") + "скан " + str(pct)
                               + "% · проверено " + str(done) + " из " + str(total)
                               + " · найдено " + str(found_n))
            self._refresh()

        def work():
            try:
                self.found = _scan.scan(mode, extra_hosts=extra,
                                        subnets=bool(self.settings.get("scan.subnets")),
                                        workers=workers, connect_timeout=timeout,
                                        progress=progress, cancel=cancel)
                self.scan_state = "найдено " + str(len(self.found)) + " за " + \
                    format(time.time() - started, ".0f") + " с"
            except Exception as exc:  # noqa: BLE001
                self.scan_state = "скан упал: " + str(exc)[:120]
            finally:
                self._scan_cancel = None
            self._refresh()

        threading.Thread(target=work, daemon=True).start()

    def stop(self) -> None:
        if self._scan_cancel is not None:
            self._scan_cancel.set()


def slug_name(text: str) -> str:
    return slug(text)
