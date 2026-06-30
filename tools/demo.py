from core.tool_registry import register_tool, EmptyInput
from datetime import date

@register_tool(
    """获取当前系统日期（本地时区）。不要自行编造日期，须调用本工具。

    适用场景：
    - 用户询问「今天几号」「当前日期」
    - 撰写记录、规划日程时需要真实时间戳
    """,
)
def get_today_date(_: EmptyInput) -> str:
    return {"today": date.today().isoformat()}