# Перенесено из BENDER: bender/router/catalog.py (коммит 6552ed660bfabf5202569f9905212b77dbf6d31a), сокращено.
"""Каталог — только то, что отдаёт сам провайдер своим API (принцип Луны)."""

from __future__ import annotations

from .core import KNOWN_PROVIDERS

PROVIDER_PRESETS = [
    ("openai", "OpenAI", "openai", "https://api.openai.com/v1"),
    ("openrouter", "OpenRouter", "openai", "https://openrouter.ai/api/v1"),
    ("deepseek", "DeepSeek", "openai", "https://api.deepseek.com/v1"),
    ("anthropic", "Anthropic", "anthropic", "https://api.anthropic.com"),
    ("gemini", "Google Gemini", "gemini", "https://generativelanguage.googleapis.com"),
    ("ollama", "Ollama (локально)", "openai", "http://localhost:11434/v1"),
    ("lmstudio", "LM Studio (локально)", "openai", "http://localhost:1234/v1"),
    ("omniroute", "OmniRoute (свой)", "openai", ""),
    ("custom", "Свой OpenAI-совместимый", "openai", ""),
]


def preset(name: str):
    for item in PROVIDER_PRESETS:
        if item[0] == name:
            return item
    return None


def known_kind(name: str) -> str:
    found = KNOWN_PROVIDERS.get(name)
    return found[0] if found else "openai"
