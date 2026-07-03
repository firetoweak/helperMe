from __future__ import annotations

from typing import Any


CONTEXT_LIMIT_ERROR_MARKERS = (
    "context length",
    "maximum context",
    "max context",
    "context window",
    "token limit",
    "tokens exceed",
    "too many tokens",
    "input is too long",
)


class ContextCompressionNotImplementedError(RuntimeError):
    """Raised when context compression is required but not implemented yet."""


def is_context_limit_error(error: str) -> bool:
    text = error.lower()
    return any(marker in text for marker in CONTEXT_LIMIT_ERROR_MARKERS)


class ContextCompactor:
    """
    Placeholder for a later context-compression phase.

    Phase 1 keeps tool-call chains reliable and does not trim messages. When
    context is too large, the runner should fail clearly instead of attempting
    partial truncation.
    """

    def compact(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        raise ContextCompressionNotImplementedError(
            "Context compression is not implemented in Phase 1."
        )
