"""Step 5: 用 Pydantic 定义工具入参，自动注册 TOOLS"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# 注册表：每个工具只写一次 —— Pydantic 模型 + Python 函数 + description
# ---------------------------------------------------------------------------

_TOOL_SPECS: dict[str, ToolSpec] = {}


@dataclass
class ToolSpec:
    """单个工具的完整定义（类似微服务里的 DTO + handler）。"""

    name: str
    description: str
    input_model: type[BaseModel]
    handler: Callable[[BaseModel], str]

    def to_openai_tool(self) -> dict[str, Any]:
        """从 Pydantic 模型导出 OpenAI tools 所需的 parameters schema。"""
        schema = self.input_model.model_json_schema()
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": schema.get("properties", {}),
                    "required": schema.get("required", []),
                },
            },
        }


class EmptyInput(BaseModel):
    """无参工具的占位输入模型。"""


def register_tool(description: str, input_model: type[BaseModel] = EmptyInput):
    """装饰器：注册工具，自动生成 TOOLS schema 和 handler 映射。"""

    def decorator(fn: Callable[[BaseModel], str]) -> Callable[[BaseModel], str]:
        spec = ToolSpec(
            name=fn.__name__,
            description=description,
            input_model=input_model,
            handler=fn,
        )
        if spec.name in _TOOL_SPECS:
            raise ValueError(f"duplicate tool registration: {spec.name}")
        _TOOL_SPECS[spec.name] = spec
        return fn

    return decorator


# 供 llm_interface 使用
def get_tools()->list[dict[str, Any]]:
    return [spec.to_openai_tool() for spec in _TOOL_SPECS.values()]

TOOL_SPECS = _TOOL_SPECS
TOOL_HANDLERS = {name: spec.handler for name, spec in _TOOL_SPECS.items()}
