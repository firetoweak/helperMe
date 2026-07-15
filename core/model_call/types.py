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
    type: str
    content: str = ""
    calls: list[ToolCall] | None = None

    def __post_init__(self) -> None:
        if self.type == "text":
            if not isinstance(self.content, str) or not self.content.strip():
                raise InvalidLLMResponse(
                    "empty_model_response",
                    "text response content must be non-empty",
                )
            if self.calls is not None:
                raise InvalidLLMResponse(
                    "invalid_llm_response",
                    "text response cannot contain tool calls",
                )
            return

        if self.type == "tool_calls":
            if not isinstance(self.calls, list) or not self.calls:
                raise InvalidLLMResponse(
                    "invalid_llm_response",
                    "tool_calls response must contain at least one call",
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
            return

        raise InvalidLLMResponse(
            "invalid_llm_response",
            f"unknown LLM response type: {self.type}",
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
