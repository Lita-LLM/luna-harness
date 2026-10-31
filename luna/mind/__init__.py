"""Мозг: цикл агента и сборка контекста. Основа — mind Бендера."""

from .context import build_system_prompt
from .loop import Agent

__all__ = ["Agent", "build_system_prompt"]
