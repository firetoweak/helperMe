from __future__ import annotations

from typing import Any, Protocol

from core.model_call.types import LLMResponse
from core.tools_runtime.tools_state import ToolStep

class RuntimeMode(Protocol):
    def create_state(self) -> Any:
        ...

    def start(self, state: Any) -> str | None:
        ...

    def accept_start_response(
        self,
        state: Any,
        response: LLMResponse,
    ) -> dict | None:
        ...

    def runtime_instructions(self, state: Any) -> list[str]:
        ...

    def check_final_candidate(self, state: Any) -> str | None:
        ...

    def on_run_completed(self, state: Any) -> None:
        ...

    def after_tool_batch(
        self,
        state: Any,
        batch_steps: list[ToolStep],
    ) -> str | None:
        ...

    def runtime_tools(self, state: Any) -> list[dict]:
        ...

    def handles_tool(self, name: str) -> bool:
        ...

    def execute_tool(
        self,
        state: Any,
        name: str,
        arguments: str,
    ) -> dict:
        ...

    def checkpoint_data(self, state: Any) -> dict | None:
        ...
