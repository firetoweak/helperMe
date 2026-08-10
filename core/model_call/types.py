from __future__ import annotations

from dataclasses import dataclass


class InvalidLLMResponse(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class LLMResponse:
    content: str = ""
    calls: tuple[ToolCall, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.content, str):
            raise InvalidLLMResponse(
                "invalid_llm_response",
                "response content must be str",
            )
        if not isinstance(self.calls, tuple):
            raise InvalidLLMResponse(
                "invalid_llm_response",
                "response calls must be tuple",
            )
        if not self.calls and not self.content.strip():
            raise InvalidLLMResponse(
                "empty_model_response",
                "model response contains neither tool calls nor non-empty text",
            )
        for index, call in enumerate(self.calls):
            if not isinstance(call, ToolCall):
                raise InvalidLLMResponse(
                    "invalid_llm_response",
                    f"tool call[{index}] must be ToolCall",
                )
            if not call.id or not call.name or not isinstance(call.arguments, str):
                raise InvalidLLMResponse(
                    "invalid_llm_response",
                    f"tool call[{index}] has invalid id/name/arguments",
                )


@dataclass(frozen=True)
class LLMUsage:
    input_tokens: int
    output_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class LLMCallResult:
    response: LLMResponse
    usage: LLMUsage
