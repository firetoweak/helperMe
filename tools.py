"""Step 5: 用 Pydantic 定义工具入参，自动注册 TOOLS"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
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


class AddInput(BaseModel):
    a: int = Field(description="第一个加数，整数")
    b: int = Field(description="第二个加数，整数")


def register_tool(description: str, input_model: type[BaseModel] = EmptyInput):
    """装饰器：注册工具，自动生成 TOOLS schema 和 handler 映射。"""

    def decorator(fn: Callable[[BaseModel], str]) -> Callable[[BaseModel], str]:
        spec = ToolSpec(
            name=fn.__name__,
            description=description,
            input_model=input_model,
            handler=fn,
        )
        _TOOL_SPECS[spec.name] = spec
        return fn

    return decorator


@register_tool(
    """获取当前系统日期（本地时区）。

    适用场景：用户询问「今天几号」「今天日期」等，且不应自行编造日期。
    输入：无参数，传 {} 即可。
    输出：ISO 8601 日期字符串，格式 YYYY-MM-DD，例如 2026-06-11。
    """,
)
def get_today_date(_: EmptyInput) -> str:
    return date.today().isoformat()


@register_tool(
    """计算两个整数的加法。

    适用场景：用户需要做整数加法时调用；不要心算，应使用本工具。
    输入：a 和 b，均为整数。
    输出：JSON 字符串，字段含义如下：
      - sum: 两数之和（整数）
      - expression: 算式字符串，例如 "17 + 25"
    示例：{"sum": 42, "expression": "17 + 25"}
    """,
    input_model=AddInput,
)
def add(data: AddInput) -> dict[str, int | str]:
    return {"sum": data.a + data.b, "expression": f"{data.a} + {data.b}"}


# 供 llm_interface 使用
TOOLS = [spec.to_openai_tool() for spec in _TOOL_SPECS.values()]
TOOL_SPECS = _TOOL_SPECS
TOOL_HANDLERS = {name: spec.handler for name, spec in _TOOL_SPECS.items()}
