# agent 的调用核心 loop循环 
from messages import Conversation
from llm_client import LLMClient
from tool_registry import TOOLS
from tools_executor import execute_tool


class Agent:
    def __init__(self, model: str = "default"):
        self.llm_client = LLMClient()
        self.conversation = Conversation()
        self.model = model

    def run(self, user_message: str, max_rounds: int = 5):

        self.conversation.add_user(user_message)
        for _ in range(max_rounds):
            response = self.llm_client.chat(
                self.conversation.messages, 
                self.model, 
                TOOLS)

            self.conversation.add_assistant(response)
            if response.type == "text":
                return response.content
            else:
                # 添加工具列表
                tool_results = []
                for call in response.calls:
                    tool_result = execute_tool(call.name, call.arguments)
                    tool_results.append({
                        "tool_call_id": call.id,
                        "content": tool_result
                    })
                self.conversation.add_tools_result(tool_results)
        return "工具调用次数过多，已停止。"

if __name__ == "__main__":
    agent = Agent(model="qwen27b")
    agent.conversation.set_system_prompt(
        "你是一个助手。"
        "需要日期时调用 get_today_date；需要计算时调用 add。"
        "不要自己编造，要用工具。"
    )
    print("=== 测试 get_today_date ===")
    print(agent.run("今天是几号？"))
    agent.conversation.reset()
    agent.conversation.set_system_prompt(
        "你是一个助手。"
        "需要日期时调用 get_today_date；需要计算时调用 add。"
        "不要自己编造，要用工具。"
    )
    print("\n=== 测试 add ===")
    print(agent.run("请计算 117 + 254"))
    print("\n=== messages 完整链路 ===")
    for msg in agent.conversation.messages:
        role = msg["role"]
        if role == "tool":
            print(f"  [tool] id={msg['tool_call_id']} content={msg['content']}")
        elif role == "assistant" and "tool_calls" in msg:
            print(f"  [assistant] tool_calls={msg['tool_calls']}")
        else:
            text = msg.get("content") or ""
            print(f"  [{role}] {text[:60]}")
