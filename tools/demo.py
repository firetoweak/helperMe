from core.tool_registry import register_tool, EmptyInput
from datetime import date
from pydantic import BaseModel, Field

class AddInput(BaseModel):
    a: int = Field(description="第一个加数，整数")
    b: int = Field(description="第二个加数，整数")


@register_tool(
    """获取当前系统日期（本地时区）。

    适用场景：用户询问「今天几号」「今天日期」等，且不应自行编造日期。
    输入：无参数，传 {} 即可。
    输出：ISO 8601 日期字符串，格式 YYYY-MM-DD，例如 2026-06-11。
    """,
)
def get_today_date(_: EmptyInput) -> str:
    return {"today": date.today().isoformat()}


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
