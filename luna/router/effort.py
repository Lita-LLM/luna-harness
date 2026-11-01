"""Единая шкала усилия рассуждений → поля конкретных провайдеров.

ЗАДАЧА-19. Человек и настройки говорят одной шкалой Луны:
none · minimal · low · medium · high · xhigh · max (и auto — «не трогать,
провайдер решает сам»). Роутер переводит шкалу в формат провайдера по
таблицам из docs/research/EFFORT.md (сверено с официальными
документациями 2026-11-01). Главное правило: неподдерживаемое значение
НЕ уходит в запрос как есть — берётся ближайшее допустимое, а расхождение
возвращается пометкой (нотой), которую агент публикует событием
effort.note в «Обратную сторону». Ошибки 400 из-за effort быть не должно.
"""

from __future__ import annotations

import re

LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")

_LOCAL_PROVIDERS = ("ollama", "lmstudio", "llamacpp")


def normalize(value) -> str:
    """Значение шкалы или пустая строка (auto / мусор)."""
    text = str(value or "").strip().lower()
    return text if text in LEVELS else ""


def closest(value: str, allowed: tuple) -> str:
    """Ближайшее допустимое значение шкалы; при равных расстояниях —
    ступень выше: усилие читается как «думать не меньше этого»,
    округление вниз (особенно в none) ломало бы замысел человека."""
    if value in allowed:
        return value
    rank = LEVELS.index(value)
    best = None
    best_key = None
    for item in allowed:
        key = (abs(LEVELS.index(item) - rank), -LEVELS.index(item))
        if best_key is None or key < best_key:
            best, best_key = item, key
    return best


def _clamp(value: str, allowed: tuple, what: str):
    """(значение, нота): нота — если значение пришлось подвинуть."""
    got = closest(value, allowed)
    if got != value:
        return got, (what + ": уровень " + value + " не поддерживается, "
                     "взято ближайшее — " + got)
    return got, None


# --- OpenAI и OpenAI-совместимые -------------------------------------------

def _openai_allowed(model: str):
    """Лестница reasoning_effort по семейству модели; None — не рассуждающая."""
    name = model.lower()
    match = re.match(r"gpt-(\d+)(?:\.(\d+))?", name)
    if match:
        major = int(match.group(1))
        minor = int(match.group(2) or 0)
        if major == 5 and minor >= 6:
            return ("none", "low", "medium", "high", "xhigh", "max")
        if major == 5 and minor >= 2:
            return ("none", "low", "medium", "high", "xhigh")
        if major == 5:
            return ("minimal", "low", "medium", "high")
        return None  # gpt-4 и старше: reasoning_effort не принимают
    if name.startswith(("o1", "o3", "o4")):
        return ("low", "medium", "high")
    if "deepseek" in name:
        return ("low", "high", "max")
    if "gpt-oss" in name or "qwen3" in name:
        return ("low", "medium", "high")
    return None


def _openai_fields(provider: str, model: str, effort: str):
    if provider == "openrouter":
        # OpenRouter: объект reasoning; effort или выключение флагом.
        if effort == "none":
            return {"reasoning": {"enabled": False}}, None
        got, note = _clamp(effort, ("low", "medium", "high", "xhigh", "max"),
                           "OpenRouter")
        return {"reasoning": {"effort": got}}, note
    if provider == "deepseek":
        # DeepSeek API: reasoning_effort принимает low/high/max, а перевод
        # шкалы у него свой, из официального гайда по thinking mode
        # (minimal→low, medium→high, xhigh→high) — берём дословно, а не
        # «ближайшим»: у провайдера на этот счёт своё мнение.
        if effort == "none":
            return {"thinking": {"type": "disabled"}}, None
        table = {"minimal": "low", "low": "low", "medium": "high",
                 "high": "high", "xhigh": "high", "max": "max"}
        got = table[effort]
        note = None
        if got != effort:
            note = ("DeepSeek: уровень " + effort + " по гайду провайдера "
                    "соответствует " + got)
        return {"reasoning_effort": got}, note
    allowed = _openai_allowed(model)
    if allowed is None:
        if provider in _LOCAL_PROVIDERS:
            return {}, ("Локальный провайдер " + provider +
                        ": усилие рассуждений не поддержано, не послано")
        got, _note = _clamp(effort, ("low", "medium", "high", "xhigh"), "")
        return ({"reasoning_effort": got},
                "Модель " + model + " не опознана как рассуждающая: "
                "reasoning_effort=" + got + " передан как есть")
    got, note = _clamp(effort, allowed, "OpenAI (" + model + ")")
    return {"reasoning_effort": got}, note


# --- Anthropic ---------------------------------------------------------------

_BUDGET_MAP = {"minimal": 1024, "low": 2048, "medium": 4096,
               "high": 8192, "xhigh": 16384, "max": 32000}
_ADAPTIVE_NEW = ("claude-opus-4-7", "claude-opus-4-8", "claude-sonnet-5",
                 "claude-opus-5", "claude-fable", "claude-mythos")
_ADAPTIVE_OK = ("claude-opus-4-6", "claude-sonnet-4-6")
_BUDGET_ONLY = ("claude-haiku-4-5", "claude-opus-4-5", "claude-sonnet-4-5",
                "claude-opus-4-1", "claude-sonnet-4", "claude-opus-4",
                "claude-3")
_ALWAYS_THINKING = ("claude-fable", "claude-mythos")


def _anthropic_fields(model: str, effort: str):
    name = model.lower()
    adaptive = (name.startswith(_ADAPTIVE_NEW) or name.startswith(_ADAPTIVE_OK)
                or not name.startswith(_BUDGET_ONLY))
    if adaptive:
        # Современный путь: adaptive thinking + output_config.effort.
        # Старый budget_tokens новым моделям отдаёт 400 — его не шлём.
        if effort == "none":
            if name.startswith(_ALWAYS_THINKING):
                return ({"thinking": {"type": "adaptive"},
                         "output_config": {"effort": "low"}},
                        "Anthropic (" + model + "): мышление не выключается, "
                        "взят минимальный effort=low")
            return {"thinking": {"type": "disabled"}}, None
        got, note = _clamp(effort, ("low", "medium", "high", "xhigh", "max"),
                           "Anthropic (" + model + ")")
        return ({"thinking": {"type": "adaptive"},
                 "output_config": {"effort": got}}, note)
    # Старые модели: thinking включён вручную бюджетом токенов.
    if effort == "none":
        return {"thinking": {"type": "disabled"}}, None
    return ({"thinking": {"type": "enabled",
                          "budget_tokens": _BUDGET_MAP[effort]}}, None)


# --- Gemini ------------------------------------------------------------------

def _gemini_fields(model: str, effort: str):
    name = model.lower()
    if "gemini-2.5" in name:
        pro = "pro" in name
        lite = "lite" in name
        cap = 32768 if pro else 24576
        if effort == "none":
            if pro:
                return ({"generationConfig": {"thinkingConfig":
                                              {"thinkingBudget": 128}}},
                        "Gemini (" + model + "): мышление не выключается, "
                        "взят минимальный бюджет 128")
            budget = 512 if lite else 0
            return ({"generationConfig": {"thinkingConfig":
                                           {"thinkingBudget": budget}}}, None)
        table = {"minimal": 1024, "low": 2048, "medium": 8192,
                 "high": 16384, "xhigh": cap, "max": cap}
        return ({"generationConfig": {"thinkingConfig":
                                       {"thinkingBudget": table[effort]}}},
                None)
    if "gemini-2.0" in name or "gemini-1" in name:
        return {}, ("Gemini (" + model + "): у поколения нет настройки "
                    "мышления, усилие не послано")
    # Поколение 3 и новее: уровни thinkingLevel. У Pro лестница уже.
    pro = "pro" in name
    allowed = ("low", "high") if pro else ("minimal", "low", "medium", "high")
    got, note = _clamp(effort, allowed, "Gemini (" + model + ")")
    if effort == "none":
        note = ("Gemini (" + model + "): мышление полностью не выключается, "
                "взят минимум — " + got)
    return ({"generationConfig": {"thinkingConfig":
                                  {"thinkingLevel": got.upper()}}}, note)


# --- вход роутера --------------------------------------------------------------

def body_fields(kind: str, provider: str, model: str, effort: str):
    """(поля тела запроса, нота|None) для уже нормализованного effort.

    Пустой effort (auto) — ({}, None): провайдер решает сам, ничего не шлём.
    """
    if not effort:
        return {}, None
    if kind == "anthropic":
        return _anthropic_fields(model, effort)
    if kind == "gemini":
        return _gemini_fields(model, effort)
    return _openai_fields(provider, model, effort)
