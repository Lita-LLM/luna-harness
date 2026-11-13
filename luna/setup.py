"""luna setup и luna models: настройка провайдера без боли.

Провайдер → ключ (getpass, ввод скрыт) → список моделей прямо из API
провайдера → выбор ролей → проверочный запрос. Ключ уходит только в сейф
секретов (~/.luna/secrets.json, права 600) и никогда не печатается.
"""

from __future__ import annotations

import getpass as getpass_module
import re

from .core.errors import HumanError
from .router.core import Router, secret_name

_POOL_NAME_RULE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


def pool_secret_name(name: str) -> str:
    """Имя секрета ключа пула: pool:<имя>. Имя — латиница/цифры/._-,
    без двоеточий и пробелов, чтобы ссылка из профиля была однозначной."""
    name = (name or "").strip()
    if not _POOL_NAME_RULE.fullmatch(name):
        raise HumanError("pool_key_name",
                         "Плохое имя пула: " + (name or "(пусто)"),
                         "Имя — латиницей, до 64 знаков: буквы, цифры, точки, "
                         "дефисы, подчёркивания (например: luna pool key node-1).")
    return "pool:" + name


def run_pool_key(name: str, *, secrets, getpass_fn=None, out=None) -> int:
    """luna pool key <имя>: ключ адреса пула — скрытым вводом в сейф.

    Ключи кладёт только человек; значение не печатается и не возвращается.
    """
    ensure = out or print
    secret_input = getpass_fn or getpass_module.getpass
    full = pool_secret_name(name)
    value = secret_input("Ключ пула «" + name.strip() +
                         "» (ввод скрыт, не покажется и не залогируется): ").strip()
    if not value:
        raise HumanError("pool_key_empty", "Ключ пустой — ничего не записал",
                         "Запусти luna pool key " + name.strip() + " ещё раз.")
    secrets.set(full, value)
    ensure("Ключ пула сохранён в сейф именем " + full +
           " (значение не показываю). В профиле ячейки адрес пишется: "
           "secret: " + full + ".")
    return 0


SEARCH_KEY_PROVIDERS = (("exa", "Exa"), ("brave", "Brave Search"),
                        ("tavily", "Tavily"), ("perplexity", "Perplexity"))


def run_models(*, settings, secrets, router: Router | None = None, check: bool = True,
               out=None) -> int:
    """luna models: что настроено и живо ли (список из API провайдера)."""
    ensure = out or print
    provider = settings.get("model.provider", "") or "(не задан)"
    kind = settings.get("model.kind", "openai")
    base = settings.get("model.base_url", "") or "(адрес по типу)"
    ensure("Провайдер: " + str(provider) + " (тип " + str(kind) + ")")
    ensure("Адрес API: " + str(base))
    key_state = "нет"
    if secrets is not None and provider != "(не задан)":
        masked = secrets.masked(secret_name(str(provider)))
        key_state = ("есть, " + masked) if masked else "нет"
    ensure("Ключ: " + key_state)
    for role in ("main", "fast", "guard", "vision"):
        ref = settings.get("model." + role, "")
        shown = ref if ref else "(как главная)"
        ensure("Роль " + role + ": " + str(shown))
    fallbacks = settings.get("model.fallbacks", "")
    if fallbacks:
        ensure("Запасные: " + str(fallbacks))
    if router is None:
        router = Router(settings, secrets)
    if check and provider != "(не задан)":
        try:
            models = router.fetch_models(str(provider))
            ensure("Живость: провайдер отвечает, моделей в каталоге: " + str(len(models)))
        except HumanError as exc:
            ensure("Живость: НЕ отвечает — " + exc.title)
            return 2
    return 0
