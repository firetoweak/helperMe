from collections.abc import Awaitable, Callable

from pydantic import BaseModel, Field

from helperme.tools.spec import PydanticParameters, ToolSpec


RESTORE_WORKSPACE = "restore_workspace"
RESTORE_DESCRIPTION = (
    "恢复到指定 tool_call_id 所在整批工具调用之前的工作区文件状态。"
    "引用当前上下文中的调用 id；同批任意调用指向同一个回退点。"
    "回退前自动保存当前文件，回退本身也可撤销。只影响文件，不撤销外部副作用或聊天历史；"
    "共享工作区的其他会话也会受影响。必须单独调用。"
)


class RestoreWorkspaceInput(BaseModel):
    tool_call_id: str = Field(min_length=1, description="要撤销的工具调用 id；恢复到其所在 Step 之前")


def create_workspace_restore_spec(
    restore: Callable[[str], Awaitable[dict]],
) -> ToolSpec:
    async def handler(raw: RestoreWorkspaceInput):
        return await restore(raw.tool_call_id)

    return ToolSpec(
        name=RESTORE_WORKSPACE,
        description=RESTORE_DESCRIPTION,
        parameters=PydanticParameters(RestoreWorkspaceInput),
        handler=handler,
        exclusive_batch=True,
    )
