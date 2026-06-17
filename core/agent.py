# agent 的调用核心 loop循环 
from core.messages import Conversation
from core.llm_client import LLMClient
# 注册
import tools
from core.tool_registry import get_tools
from core.tools_executor import execute_tool


class Agent:
    def __init__(self, model: str = "default"):
        self.llm_client = LLMClient()
        self.conversation = Conversation()
        self.model = model

    def run(self, user_message: str, max_rounds: int = 10):

        self.conversation.add_user(user_message)
        for _ in range(max_rounds):
            response = self.llm_client.chat(
                self.conversation.messages, 
                self.model, 
                get_tools())

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
    # agent.conversation.set_system_prompt(
    #     "你是一个助手。"
    #     "需要日期时调用 get_today_date；需要计算时调用 add。"
    #     "不要自己编造，要用工具。"
    # )
    # print("=== 测试 get_today_date ===")
    # print(agent.run("今天是几号？"))
    # agent.conversation.reset()
    agent.conversation.set_system_prompt(
        "你是一个助手。"
        "需要什么工具就调用啥工具。"
        "不要自己编造，要用工具。"
    )
    print("\n=== 测试 工具集合 ===")
    print(agent.run("你知道当前的工作区core目录下有啥吗？"))
    print("\n=== messages 完整链路 ===")
    round_no = 0
    for i, msg in enumerate(agent.conversation.messages):
        if msg["role"] == "assistant":
            round_no += 1
            print(f"\n--- 第 {round_no} 轮 assistant ---")
        print(f"  [{i}] {msg['role']}: ", end="")
        if msg.get("tool_calls"):
            print(msg["tool_calls"])
        else:
            print(msg.get("content", ""))
