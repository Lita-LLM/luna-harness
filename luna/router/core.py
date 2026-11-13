# Перенесено из BENDER: bender/router/core.py (коммит 6552ed660bfabf5202569f9905212b77dbf6d31a), сокращено до синхронного ядра.
"""Роутер: роли моделей, провайдеры, запасные модели при ошибке.

Ссылка на модель — «провайдер:модель» или просто «модель» (провайдер по
умолчанию из настроек). Ключ провайдера — только из сейфа секретов
(имя «provider:<провайдер>»), в логи и события он не попадает.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from ..core.errors import HumanError
from ..net.http_client import HttpClient
from . import effort as effort_mod
from .adapters import adapter_for
from .errors import is_retryable
from .types import ChatResult

DEFAULT_BASE = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com",
    "gemini": "https://generativelanguage.googleapis.com",
}

KNOWN_PROVIDERS = {
    "openai": ("openai", "https://api.openai.com/v1"),
    "openrouter": ("openai", "https://openrouter.ai/api/v1"),
    "deepseek": ("openai", "https://api.deepseek.com/v1"),
    "anthropic": ("anthropic", "https://api.anthropic.com"),
    "gemini": ("gemini", "https://generativelanguage.googleapis.com"),
    "google": ("gemini", "https://generativelanguage.googleapis.com"),
    "ollama": ("openai", "http://localhost:11434/v1"),
    "lmstudio": ("openai", "http://localhost:1234/v1"),
    "llamacpp": ("openai", "http://localhost:8080/v1"),
}

LOCAL_PROVIDERS = {"ollama", "lmstudio", "llamacpp"}


@dataclass
class ProviderConfig:
    name: str
    kind: str
    base_url: str
    api_key: str | None = None


def secret_name(provider: str) -> str:
    return "provider:" + provider


def parse_ref(ref: str, default_provider: str = "") -> tuple[str, str]:
    """«провайдер:модель» → (провайдер, модель); без двоеточия — провайдер по умолчанию."""
    ref = (ref or "").strip()
    if ":" in ref:
        provider, model = ref.split(":", 1)
        return provider.strip(), model.strip()
    return default_provider, ref


class Router:
    def __init__(self, settings, secrets, http: HttpClient | None = None, providers: dict | None = None,
                 registry=None):
        self.settings = settings
        self.secrets = secrets
        self.http = http or HttpClient()
        self._providers = providers or {}
        self._registry = registry  # реестр провайдеров; None — взять из LUNA_HOME при первом вызове
        self._sleep = time.sleep  # пауза автоповтора сети; тесты подменяют

    def cancel_active(self) -> None:
        """Оборвать идущие стримы (прерывание хода, ЗАДАЧА-18).

        HTTP-клиент закрывает потоковые ответы: разбор в рабочем потоке
        получает конец потока и честно собирает частичный результат,
        а не ждёт сеть до таймаута.
        """
        cancel = getattr(self.http, "cancel_active", None)
        if callable(cancel):
            cancel()

    # --- конфигурация ---

    @property
    def registry(self):
        if self._registry is None:
            from .providers import ProviderRegistry

            # Реестр — рядом с настройками (их home), а не всегда LUNA_HOME:
            # чат и тесты с своим каталогом данных видят свой реестр.
            home = getattr(self.settings, "home_dir", None)
            self._registry = ProviderRegistry(home / "providers.json" if home else None)
        return self._registry

    def provider_config(self, name: str) -> ProviderConfig:
        if name in self._providers:
            return self._providers[name]
        entry = self.registry.get(name) if name else None
        if entry is not None:
            # Реестр (пульт) — главный источник; запись не кэшируем в
            # _providers, чтобы правка адреса в пульте применялась сразу.
            key = self.secrets.get(secret_name(name)) if self.secrets is not None else None
            return ProviderConfig(name=name, kind=entry.kind, base_url=entry.base_url,
                                  api_key=key)
        default_name = self.settings.get("model.provider", "") if self.settings else ""
        if name and name == default_name:
            kind = self.settings.get("model.kind", "openai")
            base = self.settings.get("model.base_url", "") or DEFAULT_BASE.get(kind, "")
        elif name in KNOWN_PROVIDERS:
            kind, base = KNOWN_PROVIDERS[name]
        else:
            kind = "openai"
            base = DEFAULT_BASE["openai"]
        key = None
        if self.secrets is not None and name:
            key = self.secrets.get(secret_name(name))
        cfg = ProviderConfig(name=name, kind=kind, base_url=base, api_key=key)
        self._providers[name] = cfg
        return cfg

    def role_ref(self, role: str) -> str:
        """Ссылка роли: пустая роль = главная; главная пуста — ошибка настройки."""
        if self.settings is None:
            return ""
        ref = self.settings.get("model." + role, "")
        if not ref and role != "main":
            ref = self.settings.get("model.main", "")
        return ref or ""

    def resolve(self, ref: str) -> tuple[ProviderConfig, str]:
        default_name = self.settings.get("model.provider", "") if self.settings else ""
        provider_name, model = parse_ref(ref, default_name)
        if not model:
            raise HumanError(
                "model_not_set",
                "Модель не настроена",
                "Запусти luna setup или задай модель: luna set model.main <модель>.",
            )
        if not provider_name:
            provider_name = default_name or "openai"
        return self.provider_config(provider_name), model

    # --- вызовы ---

    def _effort_value(self, role: str) -> str:
        """Уровень усилия для роли: свой model.effort_<роль> или общий
        model.effort; пустой — auto (провайдер решает сам, ничего не шлём)."""
        if self.settings is None:
            return ""
        specific = effort_mod.normalize(self.settings.get("model.effort_" + role, ""))
        if specific:
            return specific
        return effort_mod.normalize(self.settings.get("model.effort", ""))

    def _chat_once(self, cfg: ProviderConfig, model: str, messages, tools, emit,
                   role: str = "main") -> ChatResult:
        if cfg.kind == "fake":
            from ..devtools.fake_llm import FakeModelClient

            client = FakeModelClient()
            return client.chat(model, messages, tools, emit)
        # Новолуние: ни одного запроса туда, что человек не подтвердил.
        # Здесь — единая точка: главный агент, ассистенты, рой и пулы.
        from ..core import newmoon

        newmoon.gate(cfg.kind, cfg.base_url, model)
        adapter = adapter_for(cfg.kind, cfg.base_url)
        fields, note = effort_mod.body_fields(
            cfg.kind, cfg.name, model, self._effort_value(role))
        body = adapter.body(model, messages, tools, True, extra=fields)
        if cfg.kind == "gemini":
            url = adapter.url(model)
        else:
            url = adapter.url()
        gen_probe = getattr(self.http, "cancel_generation", None)
        gen0 = gen_probe() if callable(gen_probe) else None
        lines = self.http.stream_lines(url, body, adapter.headers(cfg.api_key))
        try:
            result = adapter.parse_stream(lines, model, emit)
        except Exception:
            # Стрим умер от ОТМЕНЫ (Esc/стоп роя), а не от ошибки: полный
            # запрос без стрима не отменяется ничем и висит до таймаута —
            # на Windows красный CI ЗАДАЧИ-21 поймал рой, висевший в таком
            # fallback дольше 5 с после остановки. Честно пробрасываем.
            if gen0 is not None and gen_probe() != gen0:
                raise
            # Стрим мог не поддерживаться провайдером: пробуем одним ответом.
            data = self.http.post_json(url if cfg.kind != "gemini" else adapter.full_url(model),
                                       adapter.body(model, messages, tools, False,
                                                    extra=fields),
                                       adapter.headers(cfg.api_key))
            result = adapter.parse_full(data, model)
        if note:
            result.notes.append(note)
        if self.settings.get("model.text_tool_calls", True):
            from . import textcalls

            textcalls.rescue(result, tools)
        return result

    def chat(self, messages, tools=None, *, role: str = "main", ref: str = "",
             on_event=None) -> ChatResult:
        """Один запрос модели роли (или явной ссылки) со стримом и запасными."""
        emit = on_event or (lambda kind, text: None)
        primary = ref or self.role_ref(role)
        chain = [primary]
        if self.settings is not None:
            fallbacks = self.settings.get("model.fallbacks", "") or ""
            for item in fallbacks.split(","):
                item = item.strip()
                if item and item not in chain:
                    chain.append(item)
        last_exc: BaseException | None = None
        for candidate in chain:
            cfg, model = self.resolve(candidate)
            attempts = 1
            delay = 0
            if self.settings is not None:
                attempts = max(1, int(self.settings.get("net.retry_attempts", 3) or 3))
                delay = max(0, int(self.settings.get("net.retry_delay_s", 3) or 0))
            for attempt in range(attempts):
                try:
                    result = self._chat_once(cfg, model, messages, tools, emit, role=role)
                    result.model = candidate
                    return result
                except Exception as exc:  # noqa: BLE001 — решает классификация ниже
                    last_exc = exc
                    if not is_retryable(exc):
                        raise
                    if attempt + 1 < attempts:
                        # Автоповтор с обратным отсчётом вместо падения
                        # хода (ЗАДАЧА-20): пауза видна в ленте событием
                        # retry, сон — через self._sleep (тесты подменяют).
                        wait = delay * (2 ** attempt)
                        emit("retry", "Сеть или провайдер недоступны ("
                             + str(exc)[:80] + "). Повтор через "
                             + str(wait) + " с — попытка "
                             + str(attempt + 2) + " из " + str(attempts) + ".")
                        self._sleep(wait)
                        continue
                    break  # попытки кончились — дальше запасная модель
        if last_exc is not None:
            raise last_exc
        raise HumanError("model_not_set", "Модель не настроена", "Запусти luna setup.")

    # --- каталог ---

    def fetch_models(self, provider_name: str = "") -> list[str]:
        """Список моделей прямо из API провайдера (без чужих каталогов)."""
        name = provider_name or (self.settings.get("model.provider", "") if self.settings else "")
        cfg = self.provider_config(name)
        if cfg.kind == "fake":
            from ..devtools.fake_llm import MODELS

            return list(MODELS)
        adapter = adapter_for(cfg.kind, cfg.base_url)
        data = self.http.get_json(adapter.models_url(), adapter.headers(cfg.api_key), timeout=30.0)
        items = data.get("data") or data.get("models") or []
        out = []
        for item in items:
            if isinstance(item, dict):
                mid = item.get("id") or item.get("name") or ""
                if mid.startswith("models/"):
                    mid = mid[len("models/"):]
                if mid:
                    out.append(mid)
            elif isinstance(item, str):
                out.append(item)
        return sorted(out)
