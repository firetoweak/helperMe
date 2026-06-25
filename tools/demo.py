from core.tool_registry import register_tool, EmptyInput
from datetime import date

@register_tool(
    """获取当前系统日期（本地时区）。

    适用场景：
    - 用户询问「今天几号」「今天日期」等时间相关问题
    - 需要引用当前日期生成报告、日志或文档
    - 验证时间敏感信息（如有效期、截止日期）
    AI 助手不应自行编造日期，必须调用此工具获取真实系统时间。

    输入：无参数，传 {} 即可。
    输出：ISO 8601 日期字符串，格式 YYYY-MM-DD，例如 2026-06-11。
    """,
)
def get_today_date(_: EmptyInput) -> str:
    return {"today": date.today().isoformat()}