"""/profile — профили моделей выбиралкой (luna/router/profiles.py).

Enter — применить; «＋ сохранить» — текущие роли под именем; d — удалить
(повтор d — подтверждение). Применение — сразу и на лету, со следующего
хода; в Новолунии каждая новая модель всё равно ждёт подтверждения
(шлагбаум роутера), а сохранение профилей там запрещено — следов нет.
"""

from __future__ import annotations

from ..router import profiles as P
from ..router.providers import ProviderRegistry, valid_name
from .picker import PickerItem

KIND = "profiles"


class ProfilesUI:
    def __init__(self, app):
        self.app = app
        self._delete_armed = ""

    def store(self) -> P.ProfileStore:
        return P.ProfileStore(self.app._data_dir().root / "model_profiles.json")

    def registry(self) -> ProviderRegistry:
        return ProviderRegistry(self.app._data_dir().root / "providers.json")

    def _current_roles(self) -> dict:
        return {r: str(self.app.settings.get("model." + r) or "") for r in P.ROLES}

    def items(self) -> list:
        now = P.capture(self.app.settings, self.registry())
        out = [PickerItem(key="__save__", title="＋ сохранить текущие роли как профиль…",
                          detail=P.describe(now))]
        current = self._current_roles()
        for name, prof in sorted(self.store().load().items()):
            same = (prof.get("roles") or {}) == current
            out.append(PickerItem(key=name, title=name, detail=P.describe(prof),
                                  mark="→" if same else ""))
        return out

    def open(self, arg: str = "") -> None:
        arg = arg.strip()
        if arg:
            self.apply(arg)
            return
        self._delete_armed = ""
        self.app._open_picker(KIND, "Профили моделей", self.items(),
                              hint="Enter — применить · d — удалить · Esc — закрыть",
                              extra={"d": "delete"}, preview_lines=0)

    def on_action(self, action) -> None:
        app = self.app
        kind = action[0]
        if kind == "close":
            app.picker = None
        elif kind == "pick":
            item = action[1]
            if item.key == "__save__":
                self._ask_name()
            else:
                app.picker = None
                self.apply(item.key)
        elif kind == "action" and action[1] == "delete":
            item = action[2]
            if item is None or item.key == "__save__":
                return
            if self._delete_armed != item.key:
                self._delete_armed = item.key
                app.picker.hint = "d ещё раз — удалить «" + item.key + "» насовсем"
                return
            self.store().remove(item.key)
            self._delete_armed = ""
            app.picker.set_items(self.items())
            app.picker.hint = "Профиль «" + item.key + "» удалён."
        elif kind == "input":
            self._save(action[1].strip())

    def _ask_name(self) -> None:
        from ..core import newmoon

        if newmoon.active():
            self.app.picker.hint = "В Новолунии профили не сохраняются — следов нет."
            return
        self.app.picker.ask_input("Имя профиля (буквы, цифры, точка, дефис): ", "save")

    def _save(self, name: str) -> None:
        app = self.app
        if not valid_name(name):
            app.picker.hint = "Имя — буквы, цифры, точка, дефис, до 40 знаков."
            return
        prof = P.capture(app.settings, self.registry())
        if not prof["roles"].get("main"):
            app.picker.hint = "Главная модель не задана — сохранять нечего (/model)."
            return
        self.store().put(name, prof)
        app.picker.set_items(self.items())
        app.picker.hint = "Сохранено: «" + name + "». Enter — применить · d — удалить"

    def apply(self, name: str) -> None:
        from .chat import Entry

        prof = self.store().get(name)
        if prof is None:
            names = ", ".join(self.store().names()) or "пока ни одного"
            self.app._add(Entry("notice", text="Профиля «" + name + "» нет. Есть: " + names))
            return
        added = P.apply(prof, self.app.settings, self.registry())
        text = "Профиль «" + name + "»: " + P.describe(prof) + " — со следующего хода."
        if added:
            text += " Вернул в реестр провайдеров: " + ", ".join(added) + \
                " (ключи — в /pult, если нужны)."
        self.app._add(Entry("notice", text=text))
