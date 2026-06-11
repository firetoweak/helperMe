# agent 的调用核心
from llm_interface import (
    chat_with_tools,
    execute_tool,
    messages,
    reset,
    set_system_prompt,
    submit_tool_results,
)




def agent(user_message: str, model: str = "default", max_rounds: int = 5) -> str:
    """完整 agent 闭环：模型决策 -> 执行工具 -> 回传 -> 直到给出文本回复。"""
    result = chat_with_tools(user_message, model)

    for _ in range(max_rounds):
        if result["type"] == "text":
            return result["content"]

        tool_results = [
            {
                "tool_call_id": call["id"],
                "content": execute_tool(call["name"], call["arguments"]),
            }
            for call in result["calls"]
        ]
        result = submit_tool_results(tool_results, model)

    return "工具调用次数过多，已停止。"



if __name__ == "__main__":
    reset()
    set_system_prompt(
        "你是一个助手。"
        "需要日期时调用 get_today_date；需要计算时调用 add。"
        "不要自己编造，要用工具。"
    )

    print("=== 测试 get_today_date ===")
    print(agent("今天是几号？", model="qwen27b"))

    reset()
    set_system_prompt(
        "你是一个助手。"
        "需要日期时调用 get_today_date；需要计算时调用 add。"
        "不要自己编造，要用工具。"
    )
    print("\n=== 测试 add ===")
    answer = agent("请计算 17 + 25", model="qwen27b")
    print("最终回复:", answer)

    print("\n=== messages 完整链路 ===")
    for msg in messages:
        role = msg["role"]
        if role == "tool":
            print(f"  [tool] id={msg['tool_call_id']} content={msg['content']}")
        elif role == "assistant" and "tool_calls" in msg:
            print(f"  [assistant] tool_calls={msg['tool_calls']}")
        else:
            text = msg.get("content") or ""
            print(f"  [{role}] {text[:60]}")