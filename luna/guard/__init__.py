"""Предохранитель: одна точка решения по каждому вызову тула."""

from .policy import DENIED_PREFIX, Decision, Guard

__all__ = ["DENIED_PREFIX", "Decision", "Guard"]
