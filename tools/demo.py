from core.tool_registry import register_tool, EmptyInput
from datetime import date

@register_tool(
    """
用途：获取当前系统本地日期。
何时使用：用户询问今天日期，或记录、计划需要真实日期时使用；不要依赖模型记忆或自行推算代替本工具。
关键限制：只返回本地日期，不返回时间、时区详情或其他地区日期；无参数，必须传 {}。
失败/截断后：结果不会截断；调用失败时保留失败，不得编造日期。
""".strip(),
)
async def get_today_date(_: EmptyInput) -> dict:
    return {
        "ok": True,
        "code": "DATE_READ",
        "today": date.today().isoformat(),
    }
