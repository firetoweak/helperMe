from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator

from core.tool_registry import PydanticParameters, ToolRegistry, ToolSpec
from core.tools_runtime.tools_executor import ToolsExecutor
from core.todos.todo_list import TodoDraft, TodoList, TodoStatus


REWRITE_TODOS_NAME = "rewrite_todos"
REWRITE_TODOS_DESCRIPTION = """
用途：用完整快照创建或同步当前 Turn 的 TodoList，可修改状态和内容，也可新增、删除、拆分、合并或重排 Todo。
何时使用：执行路径、任务范围或完成状态发生实质变化时使用，准备结束 Turn 前必须最后同步；它维护执行状态，不代替实际任务工具。
关键限制：首次创建提交 objective、2 到 6 个 id=null 且 status=pending 的 Todo；后续已有项保留 id，新增项 id=null，省略旧项表示删除，数组顺序表示建议执行顺序。
失败/截断后：结果不截断；INVALID_TODO_REWRITE 后按 hint 修正并重新提交完整快照，不能只提交增量；内容未变化时也可提交相同快照确认同步。
""".strip()


class RewriteTodoInput(BaseModel):
    id: int | None = Field(description="已有 Todo 的 id；新增 Todo 传 null")
    content: str = Field(min_length=1, description="可判断是否完成的执行意图")
    status: TodoStatus
    note: str | None = Field(default=None, description="状态依据或取消原因")

    @field_validator("content")
    @classmethod
    def content_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content must not be blank")
        return value.strip()

    @model_validator(mode="after")
    def cancelled_must_have_note(self) -> "RewriteTodoInput":
        if self.status == "cancelled" and (
            self.note is None or not self.note.strip()
        ):
            raise ValueError("cancelled todo must include a note")
        return self


class RewriteTodosInput(BaseModel):
    objective: str = Field(min_length=1, description="当前执行目标摘要")
    reason: str = Field(min_length=1, description="本次同步 TodoList 的原因")
    todos: list[RewriteTodoInput] = Field(min_length=1)

    @field_validator("objective", "reason")
    @classmethod
    def text_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value.strip()


def rewrite_todos_tool_schema() -> dict:
    return _create_spec(TodoList()).to_openai_tool()


async def execute_rewrite_todos(todo_list: TodoList, arguments: str) -> dict:
    registry = ToolRegistry()
    registry.register(_create_spec(todo_list))
    return await ToolsExecutor(registry).execute(REWRITE_TODOS_NAME, arguments)


def _create_spec(todo_list: TodoList) -> ToolSpec:
    async def apply(data: RewriteTodosInput) -> dict:
        try:
            changed = todo_list.apply_snapshot(
                data.objective,
                [
                    TodoDraft(
                        id=item.id,
                        content=item.content,
                        status=item.status,
                        note=item.note,
                    )
                    for item in data.todos
                ],
            )
        except ValueError as exc:
            return {
                "ok": False,
                "code": "INVALID_TODO_REWRITE",
                "error": str(exc),
                "hint": (
                    "提交完整快照；首次创建时所有 id 为 null、status 为 pending；"
                    "后续旧项使用已有 id，新增项的 id 传 null。"
                ),
            }

        return {
            "ok": True,
            "code": "TODOS_REWRITTEN",
            "data": {
                "reason": data.reason,
                "changed": changed,
                "todo_list": todo_list.to_dict(),
            },
        }

    return ToolSpec(
        name=REWRITE_TODOS_NAME,
        description=REWRITE_TODOS_DESCRIPTION,
        parameters=PydanticParameters(RewriteTodosInput),
        handler=apply,
    )
