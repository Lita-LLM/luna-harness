"""Агенты-дети Луны (ЗАДАЧА-11): фундамент ассистентов, спутников и роя.

Один движок (mind.loop.Agent), три вида детей — разница в сроке жизни,
почтовых правах и праве звать детей (docs/РОЙ.md §2). Хозяин детей —
host.AgentHost; тулы для модели — tools.assistant_tool / tools.crew_tool;
почта и её правила — mail; общий кошелёк сессии — budget.Wallet.
"""

from __future__ import annotations

from .host import AgentHost, Child, SpawnError, host_for, identity_of
from .mail import MailError

__all__ = ["AgentHost", "Child", "SpawnError", "MailError", "host_for", "identity_of"]
