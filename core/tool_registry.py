"""Step 5: 用 Pydantic 定义工具入参，自动注册 TOOLS"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from pydantic import BaseModel

# ---------------------------------------------------------------------------
# 注册表：每个工具只写一次 —— Pydantic 模型 + Python 函数 + description
# ---------------------------------------------------------------------------

@dataclass
class ToolSpec:
    """单个工具的完整定义（类似微服务里的 DTO + handler）。"""

    name: str
    description: str
    input_model: type[BaseModel]
    handler: Callable[[BaseModel], dict[str, Any]]

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


class ToolRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._specs:
            raise ValueError(f"duplicate tool registration: {spec.name}")
        self._specs[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def get_tools(self) -> list[dict[str, Any]]:
        return [spec.to_openai_tool() for spec in self._specs.values()]

    def clone(self) -> "ToolRegistry":
        registry = ToolRegistry()
        registry._specs = self._specs.copy()
        return registry


BUILTIN_TOOL_REGISTRY = ToolRegistry()


def register_tool(description: str, input_model: type[BaseModel] = EmptyInput):
    """装饰器：注册工具，自动生成 TOOLS schema 和 handler 映射。"""

    def decorator(
        fn: Callable[[BaseModel], dict[str, Any]],
    ) -> Callable[[BaseModel], dict[str, Any]]:
        spec = ToolSpec(
            name=fn.__name__,
            description=description,
            input_model=input_model,
            handler=fn,
        )
        BUILTIN_TOOL_REGISTRY.register(spec)
        return fn

    return decorator
