"""luna setup и luna models: настройка провайдера без боли.

Провайдер → ключ (getpass, ввод скрыт) → список моделей прямо из API
провайдера → выбор ролей → проверочный запрос. Ключ уходит только в сейф
секретов (~/.luna/secrets.json, права 600) и никогда не печатается.
"""

from __future__ import annotations

import getpass as getpass_module

from .core.errors import HumanError
from .router.catalog import PROVIDER_PRESETS, preset
from .router.core import LOCAL_PROVIDERS, Router, secret_name


def _pick(input_fn, out, prompt: str, options: list[str]) -> str:
    for index, option in enumerate(options, start=1):
        out("  " + str(index) + ". " + option)
    raw = input_fn(prompt).strip()
    if raw.isdigit():
        num = int(raw)
        if 1 <= num <= len(options):
            return options[num - 1]
    return raw


# Ключи провайдеров поиска (ЗАДАЧА-19): необязательные, уходят в сейф
# именами «search:<провайдер>»; агенту не показываются никогда. Exa и
# Parallel работают и без ключей — ключи только поднимают лимиты.
SEARCH_KEY_PROVIDERS = (("exa", "Exa"), ("brave", "Brave Search"),
                        ("tavily", "Tavily"), ("perplexity", "Perplexity"))


def run_setup(*, settings, secrets, input_fn=None, getpass_fn=None,
              router: Router | None = None, out=None,
              ask_search_keys: bool = False) -> int:
    """Интерактивная настройка. Ввод и вывод подменяются в тестах."""
    ensure = out or print
    ask = input_fn or input
    secret_input = getpass_fn or getpass_module.getpass

    ensure("Настройка Луны: провайдер, ключ, модели.")
    names = [item[0] for item in PROVIDER_PRESETS]
    labels = [item[1] for item in PROVIDER_PRESETS]
    chosen_label = _pick(ask, ensure, "Провайдер (номер или имя): ", labels)
    name = chosen_label
    for item in PROVIDER_PRESETS:
        if item[1] == chosen_label or item[0] == chosen_label:
            name = item[0]
    found = preset(name)
    if found is None:
        raise HumanError("setup_provider", "Не знаю такого провайдера: " + name,
                         "Выбери из списка или укажи custom для своего API.")
    _pname, _label, kind, default_base = found
    base = ask("Адрес API [" + (default_base or "обязателен") + "]: ").strip() or default_base
    if not base:
        base = ask("Адрес API (например http://192.168.1.1:20128/v1): ").strip()
    if not base:
        raise HumanError("setup_base", "Нужен адрес API провайдера", "Для своего провайдера укажи его адрес.")
    key = ""
    if name not in LOCAL_PROVIDERS:
        key = secret_input("Ключ API (ввод скрыт, не покажется и не залогируется): ").strip()
    else:
        key = secret_input("Ключ API (локальному провайдеру обычно не нужен, Enter — пропустить): ").strip()

    settings.set("model.provider", name)
    settings.set("model.kind", kind)
    settings.set("model.base_url", base)
    if key:
        secrets.set(secret_name(name), key)
    if router is None:
        router = Router(settings, secrets)

    models: list[str] = []
    try:
        models = router.fetch_models(name)
        ensure("Провайдер отдал моделей: " + str(len(models)))
    except HumanError as exc:
        ensure("Список моделей из API не получился: " + exc.title)
        ensure("Укажи модель вручную по имени.")
    if models:
        shown = models[:50]
        main_model = _pick(ask, ensure, "Главная модель (номер или имя): ", shown)
    else:
        main_model = ask("Главная модель (имя): ").strip()
    if not main_model:
        raise HumanError("setup_model", "Модель не выбрана", "Запусти luna setup ещё раз.")
    main_ref = name + ":" + main_model
    settings.set("model.main", main_ref)
    for role, title in (("fast", "Быстрая"), ("guard", "Охранник"), ("vision", "Зрение")):
        if models:
            answer = ask(title + " модель (Enter — как главная, или имя): ").strip()
        else:
            answer = ask(title + " модель (Enter — как главная, или имя): ").strip()
        settings.set("model." + role, (name + ":" + answer) if answer else "")

    ensure("Проверочный запрос к модели…")
    result = router.chat([{"role": "user", "content": "Ответь одним словом: пинг"}],
                         None, ref=main_ref, on_event=lambda kind, text: None)
    answer_text = (result.text or "").strip()
    shown_answer = answer_text[:80] if answer_text else "(пустой ответ)"
    ensure("Модель ответила: " + shown_answer)
    ensure("Готово. Главная модель: " + main_ref + ". Ключ — только в сейфе секретов.")
    if ask_search_keys:
        # Необязательный хвост: ключи поиска. Отдельным флагом, чтобы
        # скриптовые вызовы run_setup (тесты, автоматика) не спотыкались
        # о новые вопросы; живой `luna setup` флаг включает.
        ensure("Ключи поиска (необязательно): Exa и Parallel работают без "
               "ключей, ключи поднимают лимиты; Brave, Tavily и Perplexity "
               "без ключей не работают. Enter — пропустить.")
        for name, label in SEARCH_KEY_PROVIDERS:
            value = secret_input("Ключ " + label + " (ввод скрыт): ").strip()
            if value:
                secrets.set("search:" + name, value)
                ensure("  " + label + ": ключ сохранён в сейф.")
    return 0


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
