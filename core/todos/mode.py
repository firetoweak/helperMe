from __future__ import annotations

from core.model_call.types import InvalidLLMResponse, LLMResponse
from core.tools_runtime.tools_state import ToolStep
from core.todos.exit_barrier import check_todo_exit
from core.todos.prompts import (
    TODO_INITIALIZATION_PROMPT,
    format_todo_instructions,
)
from core.todos.rewrite_todos import (
    REWRITE_TODOS_NAME,
    execute_rewrite_todos,
    rewrite_todos_tool_schema,
)
from core.todos.todo_list import TodoList


class TodoMode:
    def create_state(self) -> TodoList:
        return TodoList()

    def start(self, state: TodoList) -> str | None:
        return TODO_INITIALIZATION_PROMPT

    def accept_start_response(
        self,
        state: TodoList,
        response: LLMResponse,
    ) -> dict | None:
        if (
            response.type != "tool_calls"
            or len(response.calls) != 1
            or response.calls[0].name != REWRITE_TODOS_NAME
        ):
            raw_preview = repr(response)[:2000]
            raise InvalidLLMResponse(
                "invalid_todo_initialization",
                "todo initialization must call rewrite_todos exactly once; "
                f"raw_response={raw_preview}",
            )

        call = response.calls[0]
        result = execute_rewrite_todos(state, call.arguments)
        if result["ok"] is not True:
            raw_preview = repr(response)[:2000]
            raise InvalidLLMResponse(
                "invalid_todo_initialization",
                f"{result['code']}: {result['error']}; "
                f"raw_response={raw_preview}",
            )
        return {"todo_list": state.to_dict()}

    def runtime_instructions(self, state: TodoList) -> list[str]:
        return [format_todo_instructions(state)]

    def check_final_candidate(self, state: TodoList) -> str | None:
        decision = check_todo_exit(state)
        return None if decision.allowed else decision.feedback

    def on_run_completed(self, state: TodoList) -> None:
        state.complete()

    def after_tool_batch(
        self,
        state: TodoList,
        batch_steps: list[ToolStep],
    ) -> str | None:
        if any(step.name != REWRITE_TODOS_NAME for step in batch_steps):
            state.mark_dirty()
        return None

    def runtime_tools(self, state: TodoList) -> list[dict]:
        return [rewrite_todos_tool_schema()]

    def handles_tool(self, name: str) -> bool:
        return name == REWRITE_TODOS_NAME

    def execute_tool(
        self,
        state: TodoList,
        name: str,
        arguments: str,
    ) -> dict:
        if name != REWRITE_TODOS_NAME:
            raise ValueError(f"unknown todo runtime tool: {name}")
        return execute_rewrite_todos(state, arguments)

    def checkpoint_data(self, state: TodoList) -> dict | None:
        return {"todo_list": state.to_dict()}
