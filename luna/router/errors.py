# Перенесено из BENDER: bender/router/errors.py (коммит 6552ed660bfabf5202569f9905212b77dbf6d31a), сокращено.
"""Классификация ошибок провайдеров: что чинится повтором, что — сменой модели."""

from __future__ import annotations

from ..net.http_client import HttpError

RETRYABLE = {408, 409, 425, 429, 500, 502, 503, 504, 529}


def is_retryable(exc: BaseException) -> bool:
    """Временная ли ошибка (имеет смысл запасная модель или повтор)."""
    if isinstance(exc, HttpError):
        return exc.http_status in RETRYABLE
    return False


def is_context_overflow(exc: BaseException) -> bool:
    """Переполнение контекста по тексту ошибки провайдера."""
    if isinstance(exc, HttpError):
        body = exc.body.lower()
        return "context length" in body or "maximum context" in body or "too many tokens" in body
    return False
