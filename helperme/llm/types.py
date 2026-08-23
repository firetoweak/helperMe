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

    def __post_init__(self) -> None:
        if type(self.id) is not str or not self.id:
            raise InvalidLLMResponse(
                "invalid_llm_response",
                "tool call id must be a non-empty str",
            )
        if type(self.name) is not str or not self.name:
            raise InvalidLLMResponse(
                "invalid_llm_response",
                "tool call name must be a non-empty str",
            )
        if type(self.arguments) is not str:
            raise InvalidLLMResponse(
                "invalid_llm_response",
                "tool call arguments must be str",
            )


@dataclass(frozen=True)
class LLMResponse:
    content: str = ""
    calls: tuple[ToolCall, ...] = ()

    def __post_init__(self) -> None:
        if type(self.content) is not str:
            raise InvalidLLMResponse(
                "invalid_llm_response",
                "response content must be str",
            )
        if type(self.calls) is not tuple:
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
            if type(call) is not ToolCall:
                raise InvalidLLMResponse(
                    "invalid_llm_response",
                    f"tool call[{index}] must be ToolCall",
                )


@dataclass(frozen=True)
class LLMUsage:
    input_tokens: int
    output_tokens: int

    def __post_init__(self) -> None:
        if (
            type(self.input_tokens) is not int
            or self.input_tokens < 0
            or type(self.output_tokens) is not int
            or self.output_tokens < 0
        ):
            raise InvalidLLMResponse(
                "invalid_llm_usage",
                "token usage must contain nonnegative ints",
            )

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class LLMCallResult:
    response: LLMResponse
    usage: LLMUsage

    def __post_init__(self) -> None:
        if type(self.response) is not LLMResponse:
            raise TypeError("response must be LLMResponse")
        if type(self.usage) is not LLMUsage:
            raise TypeError("usage must be LLMUsage")
